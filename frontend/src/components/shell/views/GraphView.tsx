import './views.css';
import { useEffect, useMemo, useCallback, useRef, useState } from 'react';
import type React from 'react';
import { useUiStore, type TaskPlanMode } from '../../../state/uiStore';
import { useSessionWatch, sendStatusQuery } from '../../../rpc/hooks';
import { colorForAgent, USER_ACTOR_ID } from '../../../theme/agentColors';
import type { Span, Task, TaskPlan } from '../../../gantt/types';
import type { DriftRecord, SessionStore } from '../../../gantt/index';
import { bareAgentName } from '../../../gantt/index';
import { hasThinking } from '../../../lib/thinking';
import {
  SteeringDetailPanel,
  type SteeringSelection,
} from './SteeringDetailPanel';
import {
  DEFAULT_VIEWPORT,
  MIN_SCALE,
  MAX_SCALE,
  type Viewport,
  type Rect as ViewRect,
  type Size as ViewSize,
  zoomAt,
  fitRect,
  visibleContentRect,
  centerOn,
  minimapViewportRect,
  minimapPointToContent,
  zoomStep,
  wheelZoomFactor,
} from './graphViewport';

const MINIMAP_W = 200;
const MINIMAP_H = 140;
const MINIMAP_PAD = 6;
const PAN_THRESHOLD_PX = 3;

// ─── Layout constants ─────────────────────────────────────────────────────────
const TIME_LABEL_W = 56;
const COL_W = 200;
const HEADER_H = 84;
const ACT_W = 16;
const MIN_PX_PER_SEC = 60;
const MAX_PLOT_H = 2400;
const MIN_PLOT_H = 400;
// Width of the pre-t=0 "task plan" strip per agent column. Reserved to the
// left of each column in 'pre-strip' and 'hybrid' modes so planned tasks do
// not overlap the t=0 axis or the activation boxes.
const TASK_STRIP_W = 44;
const TASK_STRIP_HYBRID_W = 28;
const TASK_BOX_H = 18;
const TASK_BOX_H_HYBRID = 12;
const TASK_BOX_GAP = 4;

// ─── Types ────────────────────────────────────────────────────────────────────

type ArrowKind = 'transfer' | 'delegation' | 'return';

interface SeqArrow {
  id: string;
  kind: ArrowKind;
  fromCol: number;
  toCol: number;
  yMs: number;     // time in ms (for y-position mapping)
  label: string;
}

interface ActivationBox {
  agentIdx: number;
  startMs: number;
  endMs: number | null; // null = still running
  isRunning: boolean;
  spanId: string;      // for span lookup
  thinking: boolean;   // true if lib/thinking.hasThinking(span) — honors
                       // the has_reasoning bool flag stamped by the plugin
                       // plus any reasoning text carrier (see #107).
}

// Goldfive interventions overlaid on the sequence diagram. Distinct from
// the topology arrows so the renderer can style them differently and the
// viewer can tell a "delegation" (call edge) apart from a "steering"
// (orchestrator-authored plan change). See fix B of harmonograf#192.
type DriftSeverity = 'info' | 'warning' | 'critical' | '';

interface InterventionGlyph {
  id: string;
  // Column index the glyph sits on. For drift glyphs: goldfive col for
  // goldfive-authored drifts, user col for user-authored ones. For
  // cancel glyphs: the cancelled agent's own column (each cancel lands
  // on the lifeline whose invocation was cancelled).
  agentIdx: number;
  yMs: number;
  severity: DriftSeverity;
  kind: string;
  detail: string;
  // Composite identity for jumping to the underlying record.
  driftSeq: number;
  // Plan revision this drift triggered (if any) — surfaces the steering
  // panel's detail when clicked. 0 on cancel glyphs.
  revisionIndex: number;
  authoredBy: string; // 'user' | 'goldfive' | ''
  // Glyph variant — 'drift' reuses the existing chevron; 'cancel' is
  // the stop-glyph rendered for InvocationCancelled markers.
  variant: 'drift' | 'cancel';
}

interface SteeringArrow {
  id: string;
  fromCol: number;
  toCol: number;
  yMs: number;
  kind: 'steer' | 'user-steer';
  label: string;
  severity: DriftSeverity;
  revisionIndex: number;
  driftSeq: number | null;
}

interface SeqLayout {
  agentIds: string[];      // ordered columns
  arrows: SeqArrow[];
  activations: ActivationBox[];
  totalMs: number;         // duration of entire session in ms
  // Goldfive intervention overlays. Empty when no drifts/plan revisions
  // have landed yet — the graph still renders cleanly without them.
  glyphs: InterventionGlyph[];
  steerArrows: SteeringArrow[];
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtTime(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ─── Layout computation ───────────────────────────────────────────────────────

function computeSequence(store: SessionStore): SeqLayout {
  const agents = store.agents.list;
  const allSpans = store.spans.queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);

  const spanById = new Map<string, Span>();
  for (const s of allSpans) spanById.set(s.id, s);

  // ── Derive edges (same logic as old GraphView for topology) ───────────────
  type RawEdgeKind = 'transfer' | 'delegation';
  interface RawEdge {
    fromId: string;
    toId: string;
    kind: RawEdgeKind;
    yMs: number;
    label: string;
    spanId: string;
  }

  const transferArrows: RawEdge[] = [];
  const coveredPairs = new Set<string>(); // "fromId→toId@approxMs" to dedup

  // Method 1 — INVOCATION spans whose `links` carry an INVOKED reference back
  // to a TRANSFER span on the caller agent. This is the pattern the client
  // emits: the SOURCE agent starts a TRANSFER span, the DESTINATION agent
  // starts an INVOCATION whose link points to that TRANSFER. The arrow runs
  // from (source, target-invocation.startMs) to (dest, target-invocation.startMs)
  // so the head lands on the top edge of the destination's activation box.
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (!link.targetAgentId || link.targetAgentId === s.agentId) continue;
      const sourceSpan = link.targetSpanId ? spanById.get(link.targetSpanId) : undefined;
      const anchorMs = s.startMs;
      const key = `${link.targetAgentId}→${s.agentId}@${Math.round(anchorMs / 500)}`;
      coveredPairs.add(key);
      const rawLabel = sourceSpan?.name || s.name;
      transferArrows.push({
        fromId: link.targetAgentId,
        toId: s.agentId,
        kind: 'transfer',
        yMs: anchorMs,
        label: rawLabel.length > 22 ? rawLabel.slice(0, 21) + '…' : rawLabel,
        spanId: sourceSpan?.id ?? s.id,
      });
    }
  }

  // Method 1b — legacy/alternate pattern: a TRANSFER span itself carries the
  // INVOKED link to the destination invocation. Some clients emit this shape
  // instead; handle both so the view is robust.
  for (const s of allSpans) {
    if (s.kind !== 'TRANSFER') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (!link.targetAgentId || link.targetAgentId === s.agentId) continue;
      const targetSpan = link.targetSpanId ? spanById.get(link.targetSpanId) : undefined;
      const anchorMs = targetSpan ? targetSpan.startMs : s.startMs;
      const key = `${s.agentId}→${link.targetAgentId}@${Math.round(anchorMs / 500)}`;
      if (coveredPairs.has(key)) continue;
      coveredPairs.add(key);
      transferArrows.push({
        fromId: s.agentId,
        toId: link.targetAgentId,
        kind: 'transfer',
        yMs: anchorMs,
        label: s.name.length > 22 ? s.name.slice(0, 21) + '…' : s.name,
        spanId: s.id,
      });
    }
  }

  // Method 2 — cross-agent INVOCATION parents (fallback/delegation)
  const delegationArrows: RawEdge[] = [];
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (!s.parentSpanId) continue;
    const parent = spanById.get(s.parentSpanId);
    if (!parent || parent.agentId === s.agentId) continue;
    const key = `${parent.agentId}→${s.agentId}@${Math.round(s.startMs / 500)}`;
    if (coveredPairs.has(key)) continue; // already covered by method 1
    coveredPairs.add(key);
    delegationArrows.push({
      fromId: parent.agentId,
      toId: s.agentId,
      kind: 'delegation',
      yMs: s.startMs,
      label: s.name.length > 22 ? s.name.slice(0, 21) + '…' : s.name,
      spanId: s.id,
    });
  }

  // Method 3 — goldfive DelegationObserved events (#107). ADK coordinators
  // that hand a sub-task to an agent via AgentTool emit a TOOL_CALL span on
  // the coordinator row and a separate INVOCATION on the sub-agent row — the
  // span tree does NOT have a cross-agent parent pointer, so Method 2 would
  // miss it. goldfive's observer-side `delegation_observed` event is the
  // authoritative record of that coordinator→sub-agent edge; we surface it
  // as a delegation arrow here just like the Gantt renderer does.
  const delegations = store.delegations.list();
  for (const d of delegations) {
    if (!d.fromAgentId || !d.toAgentId) continue;
    if (d.fromAgentId === d.toAgentId) continue;
    const key = `${d.fromAgentId}→${d.toAgentId}@${Math.round(d.observedAtMs / 500)}`;
    if (coveredPairs.has(key)) continue; // already covered by Method 1 or 2
    coveredPairs.add(key);
    delegationArrows.push({
      fromId: d.fromAgentId,
      toId: d.toAgentId,
      kind: 'delegation',
      yMs: d.observedAtMs,
      label: 'delegate',
      spanId: `deleg-${d.seq}`,
    });
  }

  const forwardArrows: RawEdge[] = [...transferArrows, ...delegationArrows];

  // ── Topological level assignment ──────────────────────────────────────────
  const inDegree = new Map<string, number>();
  const outAdj = new Map<string, Set<string>>();
  for (const a of agents) { inDegree.set(a.id, 0); outAdj.set(a.id, new Set()); }
  for (const e of forwardArrows) {
    if (!outAdj.has(e.fromId) || !inDegree.has(e.toId)) continue;
    if (!outAdj.get(e.fromId)!.has(e.toId)) {
      outAdj.get(e.fromId)!.add(e.toId);
      inDegree.set(e.toId, (inDegree.get(e.toId) ?? 0) + 1);
    }
  }

  const level = new Map<string, number>();
  const bfsQueue: string[] = [];
  for (const a of agents) {
    if ((inDegree.get(a.id) ?? 0) === 0) { level.set(a.id, 0); bfsQueue.push(a.id); }
  }
  while (bfsQueue.length) {
    const curr = bfsQueue.shift()!;
    const currLvl = level.get(curr) ?? 0;
    for (const next of (outAdj.get(curr) ?? [])) {
      const nl = currLvl + 1;
      if ((level.get(next) ?? -1) < nl) { level.set(next, nl); bfsQueue.push(next); }
    }
  }
  for (const a of agents) { if (!level.has(a.id)) level.set(a.id, 0); }

  // First-activity time per agent (for stable ordering within the same level)
  const firstActivity = new Map<string, number>();
  for (const s of allSpans) {
    const prev = firstActivity.get(s.agentId);
    if (prev === undefined || s.startMs < prev) firstActivity.set(s.agentId, s.startMs);
  }

  const agentIds = agents
    .map((a) => a.id)
    .sort((a, b) => {
      const la = level.get(a) ?? 0;
      const lb = level.get(b) ?? 0;
      if (la !== lb) return la - lb;
      return (firstActivity.get(a) ?? 0) - (firstActivity.get(b) ?? 0);
    });

  const colIdx = new Map<string, number>();
  agentIds.forEach((id, i) => colIdx.set(id, i));

  // ── Return arrows ─────────────────────────────────────────────────────────
  // For each INVOCATION span with endMs, find who called it and emit a return.
  // We also track return arrows from delegation (cross-agent parent) invocations.
  const returnArrows: SeqArrow[] = [];

  // Build a map: calledAgentId → list of forward arrows that went TO it (sorted by yMs)
  const forwardByDest = new Map<string, RawEdge[]>();
  for (const e of forwardArrows) {
    const arr = forwardByDest.get(e.toId) ?? [];
    arr.push(e);
    forwardByDest.set(e.toId, arr);
  }

  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (s.endMs === null) continue;

    // Find the most recent forward arrow that went TO this agent before this invocation started
    const incoming = forwardByDest.get(s.agentId) ?? [];
    const callerArrow = incoming
      .filter((e) => e.yMs <= s.startMs)
      .sort((a, b) => b.yMs - a.yMs)[0];

    if (!callerArrow) continue;
    const fromCol = colIdx.get(s.agentId);
    const toCol = colIdx.get(callerArrow.fromId);
    if (fromCol === undefined || toCol === undefined) continue;
    if (fromCol === toCol) continue;

    returnArrows.push({
      id: `return-${s.id}`,
      kind: 'return',
      fromCol,
      toCol,
      yMs: s.endMs,
      label: '↩ return',
    });
  }

  // ── Combine all arrows ────────────────────────────────────────────────────
  const arrows: SeqArrow[] = [
    ...forwardArrows
      .map((e, i): SeqArrow | null => {
        const fc = colIdx.get(e.fromId);
        const tc = colIdx.get(e.toId);
        if (fc === undefined || tc === undefined || fc === tc) return null;
        return { id: `fwd-${i}-${e.spanId}`, kind: e.kind, fromCol: fc, toCol: tc, yMs: e.yMs, label: e.label };
      })
      .filter((a): a is SeqArrow => a !== null),
    ...returnArrows,
  ];

  // ── Activation boxes ──────────────────────────────────────────────────────
  const activations: ActivationBox[] = [];
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    const idx = colIdx.get(s.agentId);
    if (idx === undefined) continue;
    // Route through lib/thinking.hasThinking() so we honor both the
    // has_reasoning bool flag and any reasoning text carrier (#107).
    const thinking = hasThinking(s);
    activations.push({
      agentIdx: idx,
      startMs: s.startMs,
      endMs: s.endMs,
      isRunning: s.endMs === null,
      spanId: s.id,
      thinking,
    });
  }

  // ── Total time ────────────────────────────────────────────────────────────
  let totalMs = 1000;
  for (const s of allSpans) {
    const end = s.endMs ?? store.nowMs;
    if (end > totalMs) totalMs = end;
  }

  // ── Goldfive interventions overlay ────────────────────────────────────────
  // Drift glyphs (markers on goldfive's / user's column) + steering arrows
  // (goldfive→target-agent on plan_revised, user→goldfive on USER_STEER).
  // The goldfive row is the survivor of the __goldfive__ / <client>:goldfive
  // alias collapse; SessionStore exposes the canonical id via
  // resolveGoldfiveActorId() so we never duplicate the intervention overlay
  // when both ids happen to be live in the registry mid-burst.
  const goldfiveId = store.resolveGoldfiveActorId();
  const goldfiveCol = colIdx.get(goldfiveId);
  const userCol = colIdx.get(USER_ACTOR_ID);

  const glyphs: InterventionGlyph[] = [];
  const steerArrows: SteeringArrow[] = [];

  const driftsList = store.drifts.list();
  // Index drifts by their driftId so we can resolve the triggering drift
  // for each plan revision (used to find the target agent + severity of
  // the resulting steer arrow).
  const driftById = new Map<string, DriftRecord>();
  for (const d of driftsList) {
    if (d.driftId) driftById.set(d.driftId, d);
  }

  // Plan revisions the user can click through — we need emittedAtMs per
  // revision, which lives on the planHistory registry (TaskRegistry carries
  // only createdAtMs). Join via (planId, revisionIndex).
  const planHistory = store.planHistory;

  for (const drift of driftsList) {
    const sev: DriftSeverity =
      (drift.severity === 'info' ||
      drift.severity === 'warning' ||
      drift.severity === 'critical'
        ? drift.severity
        : '') as DriftSeverity;
    const isUser =
      drift.authoredBy === 'user' ||
      (!!drift.kind && drift.kind.startsWith('user_'));
    const col = isUser ? userCol : goldfiveCol;
    if (col === undefined) continue;
    // Find the plan rev triggered by this drift (for click-through).
    let revIndex = 0;
    for (const plan of store.tasks.listPlans()) {
      if (plan.triggerEventId && plan.triggerEventId === drift.driftId) {
        revIndex = plan.revisionIndex ?? 0;
        break;
      }
    }
    glyphs.push({
      id: `gly-${drift.seq}`,
      agentIdx: col,
      yMs: drift.recordedAtMs,
      severity: sev,
      kind: drift.kind || 'drift',
      detail: drift.detail || '',
      driftSeq: drift.seq,
      revisionIndex: revIndex,
      authoredBy: drift.authoredBy || (isUser ? 'user' : 'goldfive'),
      variant: 'drift',
    });
  }

  // InvocationCancelled glyphs — each cancel lands on the cancelled
  // agent's own lifeline (its own column index), not on the goldfive
  // lane. This matches the design constraint: a cancel is an event
  // that happened *to* that agent's invocation.
  for (const cancel of store.invocationCancels.list()) {
    const cancelCol = colIdx.get(cancel.agentId);
    if (cancelCol === undefined) continue;
    const sev: DriftSeverity =
      cancel.severity === 'info' ||
      cancel.severity === 'warning' ||
      cancel.severity === 'critical'
        ? (cancel.severity as DriftSeverity)
        : '';
    glyphs.push({
      id: `cancel-gly-${cancel.seq}`,
      agentIdx: cancelCol,
      yMs: cancel.recordedAtMs,
      severity: sev,
      kind: cancel.reason || 'cancelled',
      detail:
        cancel.detail ||
        (cancel.reason && cancel.driftKind
          ? `cancelled (${cancel.reason} → ${cancel.driftKind})`
          : cancel.reason
            ? `cancelled (${cancel.reason})`
            : 'cancelled'),
      driftSeq: -1 - cancel.seq, // negative so it doesn't collide with drift seq
      revisionIndex: 0,
      authoredBy: 'goldfive',
      variant: 'cancel',
    });
  }

  // Steering arrows: one per plan_revised with revisionIndex > 0 AND a
  // resolvable target agent. Target comes from the refine span's stamped
  // `refine.target_agent_id` (post-judge-observability bump) OR from the
  // triggering drift's `current_agent_id` when that attribute is absent.
  if (goldfiveCol !== undefined) {
    // Pre-index goldfive-lane refine spans by revisionIndex so we can
    // read back target_agent_id without a nested scan.
    const goldfiveSpans: Span[] = [];
    store.spans.queryAgent(goldfiveId, 0, Number.POSITIVE_INFINITY, goldfiveSpans);
    const refineByRev = new Map<number, Span>();
    for (const s of goldfiveSpans) {
      if (!s.name.startsWith('refine:')) continue;
      const attr = s.attributes['refine.index'];
      if (attr?.kind === 'string') {
        const n = Number(attr.value);
        if (Number.isFinite(n)) refineByRev.set(n, s);
      }
    }

    for (const plan of store.tasks.listPlans()) {
      const revIdx = plan.revisionIndex ?? 0;
      if (revIdx <= 0) continue;
      const refineSpan = refineByRev.get(revIdx);
      const refineTargetAttr = refineSpan?.attributes['refine.target_agent_id'];
      const refineTarget =
        refineTargetAttr?.kind === 'string' ? refineTargetAttr.value : '';
      const triggeringDrift = plan.triggerEventId
        ? driftById.get(plan.triggerEventId)
        : undefined;
      const targetAgent = refineTarget || triggeringDrift?.agentId || '';
      if (!targetAgent) continue;
      const targetCol = colIdx.get(targetAgent);
      if (targetCol === undefined || targetCol === goldfiveCol) continue;
      // Timestamp: prefer the refine span (matches the emittedAt of
      // the plan_revised event), else fall back to the plan's createdAt
      // (close enough for visual ordering when the refine synth didn't run).
      const yMs = refineSpan?.startMs ?? plan.createdAtMs ?? 0;
      const sev: DriftSeverity =
        triggeringDrift?.severity === 'info' ||
        triggeringDrift?.severity === 'warning' ||
        triggeringDrift?.severity === 'critical'
          ? (triggeringDrift.severity as DriftSeverity)
          : '';
      const labelKind =
        plan.revisionKind || triggeringDrift?.kind || plan.revisionReason || 'refine';
      steerArrows.push({
        id: `steer-${plan.id}-${revIdx}`,
        fromCol: goldfiveCol,
        toCol: targetCol,
        yMs,
        kind: 'steer',
        label: `refine: ${labelKind}`,
        severity: sev,
        revisionIndex: revIdx,
        driftSeq: triggeringDrift?.seq ?? null,
      });
    }

    // User steer arrows: `__user__` → goldfive, one per USER_STEER drift.
    if (userCol !== undefined) {
      for (const drift of driftsList) {
        if (!drift.kind || !drift.kind.startsWith('user_')) continue;
        const sev: DriftSeverity =
          drift.severity === 'info' ||
          drift.severity === 'warning' ||
          drift.severity === 'critical'
            ? (drift.severity as DriftSeverity)
            : '';
        steerArrows.push({
          id: `user-steer-${drift.seq}`,
          fromCol: userCol,
          toCol: goldfiveCol,
          yMs: drift.recordedAtMs,
          kind: 'user-steer',
          label: 'user steer',
          severity: sev,
          revisionIndex: 0,
          driftSeq: drift.seq,
        });
      }
    }
  }
  // Touch planHistory so the hot-path re-evaluation captures subscriber
  // changes; the actual join is already done via triggerEventId above.
  void planHistory;

  return { agentIds, arrows, activations, totalMs, glyphs, steerArrows };
}

// ─── Status colors ────────────────────────────────────────────────────────────

const STATUS_COLOR: Record<string, string> = {
  CONNECTED: '#4caf7d',
  DISCONNECTED: '#777',
  CRASHED: '#e06070',
};

// ─── Main component ───────────────────────────────────────────────────────────

export function GraphView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const selectTask = useUiStore((s) => s.selectTask);
  const selectedSpanId = useUiStore((s) => s.selectedSpanId);
  const selectedTaskId = useUiStore((s) => s.selectedTaskId);
  const taskPlanMode = useUiStore((s) => s.taskPlanMode);
  const taskPlanVisible = useUiStore((s) => s.taskPlanVisible);
  const setTaskPlanMode = useUiStore((s) => s.setTaskPlanMode);
  const toggleTaskPlanVisible = useUiStore((s) => s.toggleTaskPlanVisible);
  const persistedViewport = useUiStore((s) => s.graphViewport);
  const setGraphViewport = useUiStore((s) => s.setGraphViewport);
  const setGraphActions = useUiStore((s) => s.setGraphActions);
  const watch = useSessionWatch(sessionId);
  const [tick, setTick] = useState(0);
  const [askingAgents, setAskingAgents] = useState<Set<string>>(new Set());
  const [hoveredTask, setHoveredTask] = useState<{
    task: Task;
    plan: TaskPlan;
    x: number;
    y: number;
  } | null>(null);
  // Goldfive intervention detail panel — opened by clicking a glyph or
  // steering arrow on the overlay. Hover preview uses a lightweight title
  // tooltip; click opens the full three-section panel.
  const [steeringSelection, setSteeringSelection] =
    useState<SteeringSelection | null>(null);

  // ─── Viewport state (zoom + pan) ────────────────────────────────────────
  // `viewport` is the live, reactive state; `viewportRef` mirrors it so the
  // pointer/wheel/keyboard handlers can read the latest without re-binding
  // on every change. Persisted to uiStore on every commit.
  const [viewport, setViewportState] = useState<Viewport>(
    () => persistedViewport ?? DEFAULT_VIEWPORT,
  );
  const viewportRef = useRef<Viewport>(viewport);
  const commitViewport = useCallback(
    (vp: Viewport) => {
      viewportRef.current = vp;
      setViewportState(vp);
      setGraphViewport(vp);
    },
    [setGraphViewport],
  );

  // Container size for fit/center math. Tracked via ResizeObserver.
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [containerSize, setContainerSize] = useState<ViewSize>({ w: 0, h: 0 });
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      const rect = el.getBoundingClientRect();
      setContainerSize({ w: rect.width, h: rect.height });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Spacebar held → panning mode (show grab cursor + drag-to-pan without
  // requiring middle-click). Installed on window so focus isn't required.
  const spaceDownRef = useRef(false);
  const [spaceHeld, setSpaceHeld] = useState(false);
  useEffect(() => {
    const onDown = (e: KeyboardEvent) => {
      if (e.code === 'Space' && !spaceDownRef.current) {
        const t = e.target as HTMLElement | null;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {
          return;
        }
        spaceDownRef.current = true;
        setSpaceHeld(true);
        e.preventDefault();
      }
    };
    const onUp = (e: KeyboardEvent) => {
      if (e.code === 'Space') {
        spaceDownRef.current = false;
        setSpaceHeld(false);
      }
    };
    window.addEventListener('keydown', onDown);
    window.addEventListener('keyup', onUp);
    return () => {
      window.removeEventListener('keydown', onDown);
      window.removeEventListener('keyup', onUp);
    };
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    const u1 = watch.store.spans.subscribe(() => setTick((n) => n + 1));
    const u2 = watch.store.agents.subscribe(() => setTick((n) => n + 1));
    const u3 = watch.store.tasks.subscribe(() => setTick((n) => n + 1));
    // goldfive DelegationObserved events arrive on the delegations registry
    // (#107). Without this subscription the sequence diagram never repaints
    // when a new cross-agent delegation fires — so coordinator→sub-agent
    // arrows lag or never appear until another store (spans/tasks/agents)
    // happens to tick.
    const u4 = watch.store.delegations.subscribe(() => setTick((n) => n + 1));
    // Goldfive drift events drive intervention glyphs + steering arrows
    // on the graph overlay (fix B of harmonograf#goldfive-unify).
    const u5 = watch.store.drifts.subscribe(() => setTick((n) => n + 1));
    // Plan-history revisions feed the steering-arrow + intervention
    // detail panel so newly-landed refines show up on the overlay without
    // waiting for the next span tick.
    const u6 = watch.store.planHistory.subscribe(() => setTick((n) => n + 1));
    // InvocationCancelled markers (goldfive#251 Stream C). Same role as
    // the drift subscription: repaint the sequence-diagram overlay when
    // a new cancel marker lands.
    const u7 = watch.store.invocationCancels.subscribe(() =>
      setTick((n) => n + 1),
    );
    return () => { u1(); u2(); u3(); u4(); u5(); u6(); u7(); };
  }, [sessionId, watch.store]);

  // ── Viewport handlers (depend on refs, not reactive state) ──────────────
  // `contentSizeRef` is written each render once the SVG dimensions are
  // computed; `selectionBoxRef` holds the bbox of the currently selected
  // span/task (or null). Handlers read these refs so they never close over
  // stale values.
  const contentSizeRef = useRef<ViewSize>({ w: 0, h: 0 });
  const selectionBoxRef = useRef<ViewRect | null>(null);
  const didInitFitRef = useRef(false);
  const minimapRef = useRef<HTMLDivElement | null>(null);
  const minimapDragRef = useRef(false);

  const handleWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      e.preventDefault();
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      commitViewport(zoomAt(viewportRef.current, wheelZoomFactor(e.deltaY), cx, cy));
    },
    [commitViewport],
  );

  // Pan state. `panning` becomes true once the pointer has moved past the
  // threshold (for primary button) or immediately (for middle-click and
  // space-held primary). When `panning` was true we block the trailing click
  // so that a pan-drag doesn't accidentally select whatever was underneath
  // where the press started.
  const panStateRef = useRef({
    active: false,
    panning: false,
    startClientX: 0,
    startClientY: 0,
    startVp: DEFAULT_VIEWPORT,
    pointerId: -1,
  });
  const clickBlockedRef = useRef(false);

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const middle = e.button === 1;
      const primary = e.button === 0;
      if (!primary && !middle) return;
      const forced = middle || spaceDownRef.current;
      panStateRef.current = {
        active: true,
        panning: forced,
        startClientX: e.clientX,
        startClientY: e.clientY,
        startVp: viewportRef.current,
        pointerId: e.pointerId,
      };
      if (forced) {
        e.preventDefault();
        try {
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
        } catch {
          /* ignore */
        }
      }
    },
    [],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const st = panStateRef.current;
      if (!st.active) return;
      const dx = e.clientX - st.startClientX;
      const dy = e.clientY - st.startClientY;
      if (!st.panning) {
        if (dx * dx + dy * dy < PAN_THRESHOLD_PX * PAN_THRESHOLD_PX) return;
        st.panning = true;
        try {
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
        } catch {
          /* ignore */
        }
      }
      commitViewport({
        scale: st.startVp.scale,
        tx: st.startVp.tx + dx,
        ty: st.startVp.ty + dy,
      });
    },
    [commitViewport],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const st = panStateRef.current;
      if (st.panning) {
        clickBlockedRef.current = true;
      }
      st.active = false;
      st.panning = false;
      try {
        (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    },
    [],
  );

  const handleClickCapture = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (clickBlockedRef.current) {
      clickBlockedRef.current = false;
      e.stopPropagation();
      e.preventDefault();
      return;
    }
    setHoveredTask(null);
  }, []);

  const fitContent = useCallback(() => {
    if (containerSize.w <= 0 || containerSize.h <= 0) return;
    const cs = contentSizeRef.current;
    if (cs.w <= 0 || cs.h <= 0) return;
    // Initial-fit: clamp scale floor at 1.0 so a large DAG doesn't open at
    // 0.37× (which used to leave most of the canvas blank — Item 2 of the
    // UX cleanup batch). User-driven fits via the toolbar still clamp to
    // 1.0 too — the operator can pan from there or hit zoom-out
    // explicitly to see the whole DAG. See graphViewport.fitRect.
    commitViewport(
      fitRect({ x: 0, y: 0, w: cs.w, h: cs.h }, containerSize, 24, {
        minScale: 1,
      }),
    );
  }, [commitViewport, containerSize]);

  const fitSelection = useCallback(() => {
    if (containerSize.w <= 0 || containerSize.h <= 0) return;
    const box = selectionBoxRef.current;
    if (!box) return;
    // Pad the selection box so the selected shape isn't flush against the
    // viewport edge — makes the "snap to selection" feel less cramped.
    const pad = 60;
    commitViewport(
      fitRect(
        { x: box.x - pad, y: box.y - pad, w: box.w + pad * 2, h: box.h + pad * 2 },
        containerSize,
      ),
    );
  }, [commitViewport, containerSize]);

  const zoomBy = useCallback(
    (dir: 'in' | 'out' | 'reset') => {
      commitViewport(zoomStep(viewportRef.current, dir, containerSize));
    },
    [commitViewport, containerSize],
  );

  // Fit-to-content on first mount if no viewport was persisted.
  useEffect(() => {
    if (didInitFitRef.current) return;
    if (persistedViewport !== null) {
      didInitFitRef.current = true;
      return;
    }
    if (containerSize.w === 0 || contentSizeRef.current.w === 0) return;
    fitContent();
    didInitFitRef.current = true;
  }, [containerSize, persistedViewport, fitContent, tick]);

  // Publish imperative handles so the global keyboard shortcut handler can
  // reach the zoom/pan functions without a direct component reference.
  useEffect(() => {
    setGraphActions({
      zoomIn: () => zoomBy('in'),
      zoomOut: () => zoomBy('out'),
      zoomReset: () => zoomBy('reset'),
      fitContent,
      fitSelection,
    });
    return () => setGraphActions(null);
  }, [setGraphActions, zoomBy, fitContent, fitSelection]);

  // Depend on `tick` (bumped by store subscriptions above) rather than on the
  // collection sizes, which stay constant when existing spans mutate in place
  // (e.g. endMs/status updates). A stale layout would leave `isRunning`
  // true after a span completes and leave return arrows missing — manifesting
  // as a wrong "active agent" indicator and misaligned arrows.
  const layout = useMemo(
    () => computeSequence(watch.store),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [watch.store, tick],
  );

  // How much to pad the left of each agent column for the pre-strip. Zero if
  // the mode is ghost, if tasks are hidden, or if there are no plans at all.
  const hasPlans = watch.store.tasks.size > 0;
  const stripEnabled = taskPlanVisible && hasPlans && (taskPlanMode === 'pre-strip' || taskPlanMode === 'hybrid');
  const stripWidth = stripEnabled
    ? (taskPlanMode === 'hybrid' ? TASK_STRIP_HYBRID_W : TASK_STRIP_W)
    : 0;

  const handleAgentClick = useCallback(
    (agentId: string) => {
      const spans = watch.store.spans
        .queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER)
        .filter((s) => s.agentId === agentId && s.kind === 'INVOCATION')
        .sort((a, b) => b.startMs - a.startMs);
      if (spans[0]) selectSpan(spans[0].id);
    },
    [watch.store, selectSpan],
  );

  // Auto-poll STATUS_QUERY for agents with running activations.
  //
  // The list of running agent ids is derived fresh on every store tick
  // (because `layout` churns with `tick`), so we can't depend on
  // `layout.agentIds` / `layout.activations` directly — their array
  // identities change every render and would re-run this effect at
  // ~60Hz during a busy run, firing sendStatusQuery() once per tick
  // (see harmonograf GraphView status-query thrash: ~222 downstream
  // events/sec observed in production). Instead, derive a stable
  // scalar signature from the *set* of running agent ids and depend on
  // that: the effect only re-runs when the set actually changes, and
  // the polling loop ticks on its own 8s schedule in between.
  const runningAgentIds = useMemo(
    () =>
      layout.agentIds.filter((_agentId, idx) =>
        layout.activations.some((a) => a.agentIdx === idx && a.isRunning),
      ),
    [layout],
  );
  const runningAgentSig = useMemo(
    () => [...runningAgentIds].sort().join('|'),
    [runningAgentIds],
  );
  const runningAgentIdsRef = useRef<string[]>(runningAgentIds);
  runningAgentIdsRef.current = runningAgentIds;

  useEffect(() => {
    if (!sessionId || runningAgentSig === '') return;

    const poll = () => {
      const ids = runningAgentIdsRef.current;
      for (const agentId of ids) {
        sendStatusQuery(sessionId, agentId).catch(() => {});
      }
    };

    // Fire once immediately on mount / whenever the running-agent set
    // actually changes, then every 8 seconds.
    poll();
    const interval = setInterval(poll, 8000);
    return () => clearInterval(interval);
  }, [sessionId, runningAgentSig]);

  if (!sessionId) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Agent Graph</h2>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">
            No session selected. Open the session picker (⌘K) to pick one.
          </div>
        </div>
      </section>
    );
  }

  const agentCount = watch.store.agents.size;

  if (agentCount === 0) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Agent Graph</h2>
          <span className="hg-panel__hint">0 agents</span>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">No agents registered for this session yet.</div>
        </div>
      </section>
    );
  }

  const nowMs = watch.store.nowMs;
  const { agentIds, arrows, activations, totalMs, glyphs, steerArrows } =
    layout;

  // Scale: pixels per millisecond
  const effectiveTotalMs = Math.max(totalMs, 1000);
  const rawPxPerMs = MIN_PX_PER_SEC / 1000;
  // Ensure plot is at least MIN_PLOT_H, at most MAX_PLOT_H
  const rawPlotH = effectiveTotalMs * rawPxPerMs;
  const plotH = Math.min(MAX_PLOT_H, Math.max(MIN_PLOT_H, rawPlotH));
  const pxPerMs = plotH / effectiveTotalMs;

  // Effective column pitch: each agent gets COL_W for its activations plus
  // stripWidth reserved on the left. colCx points to the activation column's
  // center; chips now anchor directly off colCx, no separate strip-left helper.
  const colPitch = COL_W + stripWidth;
  const svgW = Math.max(600, TIME_LABEL_W + agentIds.length * colPitch);
  const svgH = HEADER_H + plotH + 40;
  // Expose content dimensions to the top-level viewport handlers through a
  // ref so they can read the latest size without a dependency wire-up.
  contentSizeRef.current = { w: svgW, h: svgH };

  // Column center x for each agent (for activation boxes / lifelines / arrows).
  const colCx = (idx: number) =>
    TIME_LABEL_W + idx * colPitch + stripWidth + COL_W / 2;
  const timeY = (ms: number) => HEADER_H + ms * pxPerMs;

  // Time label positions: at each arrow + at regular intervals
  const labelMsSet = new Set<number>();
  for (const arr of arrows) labelMsSet.add(arr.yMs);
  // Also add interval marks every ~100px
  const intervalMs = Math.ceil(100 / pxPerMs / 1000) * 1000;
  for (let t = 0; t <= effectiveTotalMs; t += intervalMs) labelMsSet.add(t);
  const labelMsList = Array.from(labelMsSet).sort((a, b) => a - b);

  // Filter out labels that are too close together (< 20px apart)
  const filteredLabels: number[] = [];
  for (const ms of labelMsList) {
    const y = timeY(ms);
    if (filteredLabels.length === 0 || y - timeY(filteredLabels[filteredLabels.length - 1]) >= 20) {
      filteredLabels.push(ms);
    }
  }

  // ── Task plan layout ─────────────────────────────────────────────────────
  // Build per-task rectangles for the strip and/or ghost boxes. Each entry
  // holds the rendering coordinates plus the source task/plan so the click
  // handler can look up bound spans or show the tooltip.
  interface TaskRect {
    taskId: string;
    planId: string;
    task: Task;
    plan: TaskPlan;
    agentIdx: number;
    mode: 'strip' | 'ghost';
    x: number;
    y: number;
    w: number;
    h: number;
  }
  const plans = watch.store.tasks.listPlans();
  const taskRectsById = new Map<string, TaskRect[]>(); // taskId → rects (strip+ghost)
  const stripRects: TaskRect[] = [];
  const ghostRects: TaskRect[] = [];
  const taskToAgentIdx = new Map<string, number>();

  // Pre-compute: all tasks across all plans, with a stable order inside each
  // agent's strip (plan order → task order within plan).
  if (taskPlanVisible && hasPlans) {
    // Build per-agent strip stacks.
    if (taskPlanMode === 'pre-strip' || taskPlanMode === 'hybrid') {
      const boxH = taskPlanMode === 'hybrid' ? TASK_BOX_H_HYBRID : TASK_BOX_H;
      const stripInnerW = stripWidth - 6;
      const stripY0 = HEADER_H + 6;
      const perAgentCursor = new Map<number, number>();
      for (const plan of plans) {
        for (const task of plan.tasks) {
          const aidx = agentIds.indexOf(task.assigneeAgentId);
          if (aidx < 0) continue;
          const yCursor = perAgentCursor.get(aidx) ?? stripY0;
          // Anchor chips immediately to the LEFT of the target agent's
          // activation column so they visually "belong" to that agent.
          // Previously they were pinned to the far-left of the column pitch,
          // which put them in the gap between columns rather than next to
          // the assigned agent.
          const activationLeft = colCx(aidx) - ACT_W / 2;
          const rect: TaskRect = {
            taskId: task.id,
            planId: plan.id,
            task,
            plan,
            agentIdx: aidx,
            mode: 'strip',
            x: activationLeft - stripInnerW - 4,
            y: yCursor,
            w: stripInnerW,
            h: boxH,
          };
          stripRects.push(rect);
          taskToAgentIdx.set(task.id, aidx);
          const arr = taskRectsById.get(task.id) ?? [];
          arr.push(rect);
          taskRectsById.set(task.id, arr);
          perAgentCursor.set(aidx, yCursor + boxH + TASK_BOX_GAP);
        }
      }
    }
    // Ghost activation boxes.
    if (taskPlanMode === 'ghost' || taskPlanMode === 'hybrid') {
      for (const plan of plans) {
        for (const task of plan.tasks) {
          const aidx = agentIds.indexOf(task.assigneeAgentId);
          if (aidx < 0) continue;
          const ghostStart = task.predictedStartMs;
          const ghostDur = Math.max(100, task.predictedDurationMs);
          const y1 = timeY(ghostStart);
          const y2 = timeY(ghostStart + ghostDur);
          const rect: TaskRect = {
            taskId: task.id,
            planId: plan.id,
            task,
            plan,
            agentIdx: aidx,
            mode: 'ghost',
            x: colCx(aidx) - ACT_W / 2,
            y: y1,
            w: ACT_W,
            h: Math.max(4, y2 - y1),
          };
          ghostRects.push(rect);
          taskToAgentIdx.set(task.id, aidx);
          const arr = taskRectsById.get(task.id) ?? [];
          arr.push(rect);
          taskRectsById.set(task.id, arr);
        }
      }
    }
  }

  // Task dependency edges: pair each (fromTaskId, toTaskId) with the first
  // rect pair available (strip↔strip in strip/hybrid, ghost↔ghost in ghost).
  interface TaskEdgeLine {
    id: string;
    x1: number; y1: number; x2: number; y2: number;
  }
  const taskEdgeLines: TaskEdgeLine[] = [];
  if (taskPlanVisible && hasPlans) {
    const preferStrip = taskPlanMode === 'pre-strip' || taskPlanMode === 'hybrid';
    for (const plan of plans) {
      for (const edge of plan.edges) {
        const fromRects = taskRectsById.get(edge.fromTaskId);
        const toRects = taskRectsById.get(edge.toTaskId);
        if (!fromRects || !toRects) continue;
        const pickMode: TaskRect['mode'] = preferStrip ? 'strip' : 'ghost';
        const from = fromRects.find((r) => r.mode === pickMode) ?? fromRects[0];
        const to = toRects.find((r) => r.mode === pickMode) ?? toRects[0];
        if (!from || !to) continue;
        taskEdgeLines.push({
          id: `te-${plan.id}-${edge.fromTaskId}-${edge.toTaskId}`,
          x1: from.x + from.w / 2,
          y1: from.y + from.h / 2,
          x2: to.x + to.w / 2,
          y2: to.y + to.h / 2,
        });
      }
    }
  }

  // Click handler shared by strip + ghost task boxes.
  const handleTaskClick = (rect: TaskRect, e: React.MouseEvent) => {
    e.stopPropagation();
    selectTask(rect.task.id);
    if (rect.task.boundSpanId) {
      selectSpan(rect.task.boundSpanId);
      return;
    }
    setHoveredTask({ task: rect.task, plan: rect.plan, x: rect.x + rect.w + 8, y: rect.y });
  };

  const transferCount = arrows.filter((a) => a.kind === 'transfer').length;
  const delegCount = arrows.filter((a) => a.kind === 'delegation').length;
  const returnCount = arrows.filter((a) => a.kind === 'return').length;

  // ── Current selection bounding box (content coords) ─────────────────────
  // Used by the "fit selection" button. Null means no selection is available
  // to snap to in the current view. Prefer span selection (activation box)
  // over task selection (task rect).
  let selectionBox: ViewRect | null = null;
  if (selectedSpanId) {
    const act = activations.find((a) => a.spanId === selectedSpanId);
    if (act) {
      const y1 = timeY(act.startMs);
      const y2 = timeY(act.endMs ?? Math.max(nowMs, act.startMs + 100));
      selectionBox = {
        x: colCx(act.agentIdx) - ACT_W / 2,
        y: y1,
        w: ACT_W,
        h: Math.max(4, y2 - y1),
      };
    }
  } else if (selectedTaskId) {
    const rects = taskRectsById.get(selectedTaskId);
    const first = rects?.[0];
    if (first) {
      selectionBox = { x: first.x, y: first.y, w: first.w, h: first.h };
    }
  }
  selectionBoxRef.current = selectionBox;

  // ── Transform strings and derived minimap geometry ──────────────────────
  const vp = viewport;
  const transform = `matrix(${vp.scale} 0 0 ${vp.scale} ${vp.tx} ${vp.ty})`;
  const contentBounds: ViewRect = { x: 0, y: 0, w: svgW, h: svgH };
  const visibleRect = visibleContentRect(vp, containerSize);
  const minimapRect = minimapViewportRect(
    visibleRect,
    contentBounds,
    { w: MINIMAP_W - MINIMAP_PAD * 2, h: MINIMAP_H - MINIMAP_PAD * 2 },
  );
  const minimapScale = Math.min(
    (MINIMAP_W - MINIMAP_PAD * 2) / Math.max(1, svgW),
    (MINIMAP_H - MINIMAP_PAD * 2) / Math.max(1, svgH),
  );

  const handleMinimapSeek = (clientX: number, clientY: number) => {
    const canvas = minimapRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = clientX - rect.left - MINIMAP_PAD;
    const my = clientY - rect.top - MINIMAP_PAD;
    const content = minimapPointToContent(
      mx,
      my,
      contentBounds,
      { w: MINIMAP_W - MINIMAP_PAD * 2, h: MINIMAP_H - MINIMAP_PAD * 2 },
    );
    commitViewport(centerOn(viewportRef.current, content.x, content.y, containerSize));
  };

  // Cursor hint for the viewport container — `grab` when space is held,
  // default otherwise. While actively panning we swap to `grabbing` inline.
  const viewportCursor = spaceHeld ? 'grab' : 'default';

  return (
    <section className="hg-panel" data-testid="graph-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Agent Graph</h2>
        <span className="hg-panel__hint">
          {agentCount} agent{agentCount !== 1 ? 's' : ''}
          {transferCount > 0 && ` · ${transferCount} transfer${transferCount !== 1 ? 's' : ''}`}
          {delegCount > 0 && ` · ${delegCount} delegation${delegCount !== 1 ? 's' : ''}`}
          {returnCount > 0 && ` · ${returnCount} return${returnCount !== 1 ? 's' : ''}`}
          {hasPlans && ` · ${plans.length} plan${plans.length !== 1 ? 's' : ''}`}
        </span>
        <span
          style={{
            marginLeft: 'auto',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 11,
            color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
          }}
        >
          <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <input
              type="checkbox"
              checked={taskPlanVisible}
              onChange={toggleTaskPlanVisible}
            />
            Plans
          </label>
          <select
            value={taskPlanMode}
            onChange={(e) => setTaskPlanMode(e.target.value as TaskPlanMode)}
            disabled={!taskPlanVisible}
            style={{
              fontSize: 11,
              padding: '2px 4px',
              background: 'var(--md-sys-color-surface, #10131a)',
              color: 'var(--md-sys-color-on-surface, #e3e6ef)',
              border: '1px solid var(--md-sys-color-outline-variant, #2a2f3a)',
              borderRadius: 4,
            }}
          >
            <option value="pre-strip">Pre-strip</option>
            <option value="ghost">Ghost</option>
            <option value="hybrid">Hybrid</option>
          </select>
        </span>
      </header>
      <div
        className="hg-panel__body"
        style={{
          overflow: 'hidden',
          position: 'relative',
          display: 'flex',
          flexDirection: 'column',
        }}
        onClickCapture={handleClickCapture}
      >
        {/* Legend */}
        <div style={{
          display: 'flex', gap: 20, padding: '0 0 10px',
          fontSize: 11, color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
          flex: '0 0 auto',
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#e8953a" strokeWidth={2.5} />
            </svg>
            Transfer
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#5b8def" strokeWidth={1.5} strokeDasharray="6 3" />
            </svg>
            Delegation
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#888" strokeWidth={1.2} strokeDasharray="4 4" />
            </svg>
            Return
          </span>
          {arrows.length === 0 && agentCount > 0 && (
            <span style={{ marginLeft: 8, opacity: 0.6 }}>
              No agent interactions detected yet.
            </span>
          )}
        </div>

        <div
          ref={containerRef}
          className="hg-graph__viewport"
          style={{
            flex: 1,
            position: 'relative',
            minHeight: 0,
            overflow: 'hidden',
            background: 'var(--md-sys-color-surface, #10131a)',
            borderRadius: 6,
            cursor: viewportCursor,
            touchAction: 'none',
          }}
          role="application"
          aria-label="Agent sequence diagram — scroll to zoom, drag to pan"
          tabIndex={0}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
        >
          {/* Toolbar */}
          <div
            style={{
              position: 'absolute',
              top: 8,
              right: 8,
              zIndex: 20,
              display: 'flex',
              gap: 4,
            }}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={fitContent}
              title="Fit diagram to viewport"
              aria-label="Fit diagram to viewport"
              style={toolbarBtnStyle}
            >
              ⊡ Fit
            </button>
            <button
              type="button"
              onClick={fitSelection}
              disabled={!selectionBox}
              title="Fit to selection"
              aria-label="Fit to selection"
              style={{ ...toolbarBtnStyle, opacity: selectionBox ? 1 : 0.45 }}
            >
              ⊙ Fit selection
            </button>
            <button
              type="button"
              onClick={() => zoomBy('in')}
              title="Zoom in (Ctrl + =)"
              aria-label="Zoom in"
              style={toolbarBtnStyle}
              disabled={viewport.scale >= MAX_SCALE}
            >
              +
            </button>
            <button
              type="button"
              onClick={() => zoomBy('out')}
              title="Zoom out (Ctrl + -)"
              aria-label="Zoom out"
              style={toolbarBtnStyle}
              disabled={viewport.scale <= MIN_SCALE}
            >
              −
            </button>
            <button
              type="button"
              onClick={() => zoomBy('reset')}
              title="Reset zoom (Ctrl + 0)"
              aria-label="Reset zoom"
              style={toolbarBtnStyle}
            >
              1:1
            </button>
            <span
              style={{
                fontSize: 10,
                alignSelf: 'center',
                padding: '0 4px',
                color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
                fontVariantNumeric: 'tabular-nums',
              }}
              aria-live="polite"
            >
              {Math.round(vp.scale * 100)}%
            </span>
          </div>

        <svg
          width="100%"
          height="100%"
          style={{ display: 'block' }}
          preserveAspectRatio="xMinYMin meet"
        >
          <g transform={transform}>
          <defs>
            <marker id="arr-transfer" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={7} markerHeight={7} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#e8953a" />
            </marker>
            <marker id="arr-delegation" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#5b8def" opacity={0.8} />
            </marker>
            <marker id="arr-return" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#888" />
            </marker>
            <marker id="arr-task-dep" viewBox="0 0 10 10" refX={9} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa3b4" opacity={0.5} />
            </marker>
          </defs>

          {/* Time labels + horizontal tick lines */}
          {filteredLabels.map((ms) => {
            const y = timeY(ms);
            return (
              <g key={`tl-${ms}`}>
                <line
                  x1={TIME_LABEL_W} y1={y} x2={svgW} y2={y}
                  stroke="var(--md-sys-color-outline-variant, #2a2f3a)"
                  strokeWidth={0.5} opacity={0.4}
                />
                <text
                  x={TIME_LABEL_W - 8} y={y}
                  textAnchor="end" dominantBaseline="middle"
                  fill="var(--md-sys-color-on-surface-variant, #9da3b4)"
                  fontSize={10}
                >
                  {fmtTime(ms)}
                </text>
              </g>
            );
          })}

          {/* Agent header boxes + lifelines */}
          {agentIds.map((agentId, idx) => {
            const agent = watch.store.agents.get(agentId);
            if (!agent) return null;
            const cx = colCx(idx);
            const color = colorForAgent(agentId);
            const hasRunning = activations.some((a) => a.agentIdx === idx && a.isRunning);
            // Only surface "stuck" in the UI if the agent is flagged AND it
            // currently has an open (RUNNING) INVOCATION. A flagged agent
            // whose invocation already ended cleanly is no longer stuck —
            // without this intersection every agent in a completed session
            // would still show the amber stuck border.
            const isStuck = agent.stuck === true && hasRunning;
            const statusDot = isStuck ? '#f59e0b' : (STATUS_COLOR[agent.status] ?? '#777');
            const label = agent.name.length > 18 ? agent.name.slice(0, 17) + '…' : agent.name;
            const hBoxW = COL_W - 24;
            const hBoxX = cx - hBoxW / 2;
            const hBoxY = 4;
            const liveStatus = agent.taskReport || agent.currentActivity || '';
            const hBoxH = liveStatus ? 80 : 66;
            const borderColor = isStuck ? '#f59e0b' : color;
            const isAsking = askingAgents.has(agentId);

            const handleAsk = (e: React.MouseEvent) => {
              e.stopPropagation();
              if (!sessionId || isAsking) return;
              setAskingAgents((prev) => new Set([...prev, agentId]));
              sendStatusQuery(sessionId, agentId).finally(() => {
                setAskingAgents((prev) => {
                  const next = new Set(prev);
                  next.delete(agentId);
                  return next;
                });
              });
            };

            return (
              <g key={agentId}>
                {/* Lifeline */}
                <line
                  x1={cx} y1={HEADER_H} x2={cx} y2={HEADER_H + plotH}
                  stroke={color} strokeWidth={1} opacity={0.25}
                  strokeDasharray="5 5"
                />

                {/* Header box */}
                <g
                  onClick={() => handleAgentClick(agentId)}
                  style={{ cursor: 'pointer' }}
                  role="button"
                  aria-label={agent.name}
                >
                  {(hasRunning || isStuck) && (
                    <rect
                      x={hBoxX - 3} y={hBoxY - 3}
                      width={hBoxW + 6} height={hBoxH + 6}
                      rx={9}
                      fill={isStuck ? '#f59e0b' : color} opacity={0.1}
                      className="hg-graph__pulse"
                    />
                  )}
                  <rect
                    x={hBoxX} y={hBoxY}
                    width={hBoxW} height={hBoxH}
                    rx={7}
                    fill={isStuck ? '#f59e0b' : color}
                    fillOpacity={0.15}
                    stroke={borderColor}
                    strokeWidth={hasRunning || isStuck ? 2 : 1.5}
                    className={isStuck ? 'hg-graph__pulse' : hasRunning ? 'hg-graph__pulse' : undefined}
                  />
                  <text
                    x={cx} y={hBoxY + 18}
                    textAnchor="middle" dominantBaseline="central"
                    fill={isStuck ? '#f59e0b' : color} fontSize={12} fontWeight={700}
                  >
                    {label}
                  </text>
                  <text
                    x={cx} y={hBoxY + 34}
                    textAnchor="middle" dominantBaseline="central"
                    fill={isStuck ? '#f59e0b' : 'var(--md-sys-color-on-surface-variant, #9da3b4)'}
                    fontSize={10}
                  >
                    {isStuck ? '⚠ stuck' : (agent.framework !== 'UNKNOWN' ? agent.framework : '')}
                  </text>
                  {/* Task report line */}
                  {liveStatus && (
                    <foreignObject
                      x={hBoxX + 4} y={hBoxY + 48}
                      width={hBoxW - 8} height={30}
                      style={{ pointerEvents: 'none', overflow: 'visible' }}
                    >
                      <div
                        style={{
                          fontSize: 9,
                          color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
                          lineHeight: 1.4,
                          overflow: 'hidden',
                          display: '-webkit-box',
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: 'vertical' as const,
                          wordBreak: 'break-word',
                        } as React.CSSProperties}
                        title={liveStatus}
                      >
                        {liveStatus}
                      </div>
                    </foreignObject>
                  )}
                  {/* Status dot */}
                  <circle cx={hBoxX + hBoxW - 10} cy={hBoxY + 10} r={4} fill={statusDot} />
                </g>
                {/* Ask ? button (foreignObject so we get a real HTML button) */}
                <foreignObject
                  x={hBoxX + hBoxW - 52} y={hBoxY + hBoxH - 22}
                  width={48} height={18}
                  style={{ pointerEvents: 'all' }}
                >
                  <button
                    onClick={handleAsk}
                    disabled={isAsking}
                    title="Ask agent what it's working on"
                    style={{
                      fontSize: 9,
                      padding: '1px 5px',
                      cursor: isAsking ? 'default' : 'pointer',
                      border: `1px solid ${color}`,
                      borderRadius: 4,
                      background: 'var(--md-sys-color-surface, #10131a)',
                      color: color,
                      width: '100%',
                      height: '100%',
                      opacity: isAsking ? 0.6 : 1,
                    }}
                  >
                    {isAsking ? '…' : '↻ Status'}
                  </button>
                </foreignObject>
              </g>
            );
          })}

          {/* Activation boxes */}
          {activations.map((act, i) => {
            const cx = colCx(act.agentIdx);
            const color = colorForAgent(agentIds[act.agentIdx] ?? '');
            const y1 = timeY(act.startMs);
            const endMs = act.endMs ?? Math.max(nowMs, act.startMs + 100);
            const y2 = timeY(endMs);
            const boxH = Math.max(4, y2 - y1);
            return (
              <g key={`act-${i}`}>
                <rect
                  x={cx - ACT_W / 2}
                  y={y1}
                  width={ACT_W}
                  height={boxH}
                  rx={3}
                  fill={color}
                  fillOpacity={act.isRunning ? 0.85 : 0.55}
                  stroke={color}
                  strokeWidth={act.isRunning ? 1.5 : 0}
                  className={act.isRunning ? 'hg-graph__pulse' : undefined}
                />
                {act.isRunning && act.thinking && (
                  <circle
                    cx={cx}
                    cy={y1 + Math.min(12, boxH / 2)}
                    r={3}
                    fill="#a8c8ff"
                    className="hg-graph__pulse"
                  />
                )}
              </g>
            );
          })}

          {/* Task dependency edges (drawn below transfer arrows) */}
          {taskPlanVisible && taskEdgeLines.map((e) => {
            const dx = e.x2 - e.x1;
            const midY = (e.y1 + e.y2) / 2;
            const c1x = e.x1 + dx * 0.25;
            const c2x = e.x1 + dx * 0.75;
            return (
              <path
                key={e.id}
                d={`M ${e.x1} ${e.y1} C ${c1x} ${midY}, ${c2x} ${midY}, ${e.x2} ${e.y2}`}
                stroke="#9aa3b4"
                strokeWidth={1}
                strokeDasharray="3 3"
                fill="none"
                opacity={0.5}
                markerEnd="url(#arr-task-dep)"
              />
            );
          })}

          {/* Pre-strip task boxes */}
          {taskPlanVisible && stripRects.map((r) => {
            const color = colorForAgent(agentIds[r.agentIdx] ?? '');
            const s = r.task.status;
            const isPending = s === 'PENDING' || s === 'UNSPECIFIED';
            const isRunning = s === 'RUNNING';
            const isDone = s === 'COMPLETED';
            const isFailed = s === 'FAILED';
            const isCancelled = s === 'CANCELLED';
            const isSelected = selectedTaskId === r.taskId;
            const fill = isRunning ? color
              : isDone ? color
              : isFailed ? 'transparent'
              : 'var(--md-sys-color-surface-container, #1a1f2a)';
            const fillOpacity = isRunning ? 0.85 : isDone ? 0.55 : isPending ? 0.25 : 1;
            const stroke = isFailed ? '#e06070' : color;
            const hybrid = taskPlanMode === 'hybrid';
            const label = r.task.title.length > (hybrid ? 4 : 8)
              ? r.task.title.slice(0, hybrid ? 3 : 7) + '…'
              : r.task.title;
            return (
              <g
                key={`tr-${r.planId}-${r.taskId}`}
                style={{ cursor: 'pointer' }}
                onClick={(e) => handleTaskClick(r, e)}
              >
                {isSelected && (
                  <rect
                    x={r.x - 2} y={r.y - 2}
                    width={r.w + 4} height={r.h + 4}
                    rx={4}
                    fill="none"
                    stroke="var(--md-sys-color-primary, #a8c8ff)"
                    strokeWidth={2}
                  />
                )}
                <rect
                  x={r.x} y={r.y} width={r.w} height={r.h}
                  rx={3}
                  fill={fill}
                  fillOpacity={fillOpacity}
                  stroke={isSelected ? 'var(--md-sys-color-primary, #a8c8ff)' : stroke}
                  strokeWidth={isSelected ? 1.5 : 1}
                  strokeDasharray={isFailed ? '2 2' : undefined}
                />
                {!hybrid && (
                  <text
                    x={r.x + 4} y={r.y + r.h / 2}
                    dominantBaseline="middle"
                    fill={isPending
                      ? 'var(--md-sys-color-on-surface-variant, #9da3b4)'
                      : '#fff'}
                    fontSize={9}
                    style={{
                      textDecoration: isCancelled ? 'line-through' : undefined,
                      pointerEvents: 'none',
                    }}
                  >
                    {label}
                  </text>
                )}
                {isDone && (
                  <text
                    x={r.x + r.w - 6} y={r.y + r.h / 2}
                    textAnchor="end" dominantBaseline="middle"
                    fill="#fff" fontSize={9}
                    style={{ pointerEvents: 'none' }}
                  >
                    ✓
                  </text>
                )}
                <title>
                  {`${r.task.title}\n${r.task.description}\nStatus: ${r.task.status}`}
                </title>
              </g>
            );
          })}

          {/* Ghost activation boxes for tasks */}
          {taskPlanVisible && ghostRects.map((r) => {
            const color = colorForAgent(agentIds[r.agentIdx] ?? '');
            const isSelected = selectedTaskId === r.taskId;
            return (
              <g key={`gh-${r.planId}-${r.taskId}`}>
                {isSelected && (
                  <rect
                    x={r.x - 2} y={r.y - 2}
                    width={r.w + 4} height={r.h + 4}
                    rx={4}
                    fill="none"
                    stroke="var(--md-sys-color-primary, #a8c8ff)"
                    strokeWidth={2}
                  />
                )}
                <rect
                  x={r.x} y={r.y} width={r.w} height={r.h}
                  rx={3}
                  fill={color}
                  fillOpacity={0.25}
                  stroke={isSelected ? 'var(--md-sys-color-primary, #a8c8ff)' : color}
                  strokeWidth={isSelected ? 1.5 : 1}
                  strokeDasharray="2 2"
                  style={{ cursor: 'pointer' }}
                  onClick={(e) => handleTaskClick(r, e)}
                >
                  <title>{`${r.task.title} (predicted)`}</title>
                </rect>
              </g>
            );
          })}

          {/* Arrows */}
          {arrows.map((arrow) => {
            const x1 = colCx(arrow.fromCol);
            const x2 = colCx(arrow.toCol);
            const y = timeY(arrow.yMs);
            const isLeft = x2 < x1;

            let stroke: string;
            let strokeWidth: number;
            let strokeDasharray: string | undefined;
            let markerId: string;

            if (arrow.kind === 'transfer') {
              stroke = '#e8953a';
              strokeWidth = 2.5;
              strokeDasharray = undefined;
              markerId = 'url(#arr-transfer)';
            } else if (arrow.kind === 'delegation') {
              stroke = '#5b8def';
              strokeWidth = 1.5;
              strokeDasharray = '6 3';
              markerId = 'url(#arr-delegation)';
            } else {
              stroke = '#888';
              strokeWidth = 1.2;
              strokeDasharray = '4 4';
              markerId = 'url(#arr-return)';
            }

            // Offset start/end by activation box half-width
            const startX = x1 + (isLeft ? -ACT_W / 2 : ACT_W / 2);
            const endX = x2 + (isLeft ? ACT_W / 2 : -ACT_W / 2);

            // Label position: above the arrow, centered
            const labelX = (startX + endX) / 2;
            const labelY = y - 8;
            const truncLabel = arrow.label.length > 22 ? arrow.label.slice(0, 21) + '…' : arrow.label;

            return (
              <g key={arrow.id}>
                <line
                  x1={startX} y1={y} x2={endX} y2={y}
                  stroke={stroke} strokeWidth={strokeWidth}
                  strokeDasharray={strokeDasharray}
                  markerEnd={markerId}
                />
                {/* Label background */}
                <rect
                  x={labelX - 50} y={labelY - 8}
                  width={100} height={14}
                  rx={3}
                  fill="var(--md-sys-color-surface, #10131a)"
                  opacity={0.8}
                />
                <text
                  x={labelX} y={labelY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill={arrow.kind === 'return' ? '#888' : stroke}
                  fontSize={9.5} fontStyle={arrow.kind === 'return' ? 'italic' : undefined}
                >
                  {truncLabel}
                </text>
              </g>
            );
          })}

          {/* ── Goldfive steering arrows (fix B of harmonograf#goldfive-unify) ── */}
          {/* Distinct style from delegation/transfer arrows so the viewer can */}
          {/* tell a call-edge apart from an orchestrator-authored plan change. */}
          {/* Dashed line; amber for warning, red for critical, grey for info. */}
          {steerArrows.map((sa) => {
            const x1 = colCx(sa.fromCol);
            const x2 = colCx(sa.toCol);
            const y = timeY(sa.yMs);
            const isLeft = x2 < x1;
            const sevColor =
              sa.severity === 'critical' ? '#e06070'
              : sa.severity === 'warning' ? '#f59e0b'
              : sa.severity === 'info' ? '#8aa6d6'
              : sa.kind === 'user-steer' ? '#d0bcff' : '#80deea';
            const startX = x1 + (isLeft ? -ACT_W / 2 : ACT_W / 2);
            const endX = x2 + (isLeft ? ACT_W / 2 : -ACT_W / 2);
            const labelX = (startX + endX) / 2;
            const labelY = y - 8;
            const truncLabel = sa.label.length > 24 ? sa.label.slice(0, 23) + '…' : sa.label;
            return (
              <g
                key={sa.id}
                style={{ cursor: 'pointer' }}
                data-testid={`steering-arrow-${sa.id}`}
                data-severity={sa.severity || undefined}
                onClick={(e) => {
                  e.stopPropagation();
                  if (sa.revisionIndex > 0) {
                    setSteeringSelection({
                      kind: 'revision',
                      revision: sa.revisionIndex,
                    });
                  }
                }}
              >
                <line
                  x1={startX} y1={y} x2={endX} y2={y}
                  stroke={sevColor} strokeWidth={2}
                  strokeDasharray="5 3"
                />
                {/* Arrow head */}
                <polygon
                  points={`${endX - (isLeft ? -8 : 8)},${y - 4} ${endX},${y} ${endX - (isLeft ? -8 : 8)},${y + 4}`}
                  fill={sevColor}
                />
                <rect
                  x={labelX - 54} y={labelY - 8}
                  width={108} height={14}
                  rx={3}
                  fill="var(--md-sys-color-surface, #10131a)"
                  opacity={0.85}
                />
                <text
                  x={labelX} y={labelY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill={sevColor}
                  fontSize={9.5}
                >
                  {truncLabel}
                </text>
                <title>
                  {`${sa.label}${sa.severity ? ' · ' + sa.severity : ''}`}
                </title>
              </g>
            );
          })}

          {/* ── Intervention glyphs on goldfive's / user's timeline ── */}
          {/* Small diamond anchored on the actor's lifeline at drift.recordedAtMs. */}
          {/* Color by severity; click opens the SteeringDetailPanel if the drift */}
          {/* triggered a plan revision, otherwise shows the hover tooltip only. */}
          {glyphs.map((g) => {
            const cx = colCx(g.agentIdx);
            const cy = timeY(g.yMs);
            const sevColor =
              g.severity === 'critical' ? '#e06070'
              : g.severity === 'warning' ? '#f59e0b'
              : g.severity === 'info' ? '#8aa6d6'
              : '#9aa3b4';
            const clickable = g.revisionIndex > 0;
            if (g.variant === 'cancel') {
              // Cancel glyph: a circle with a diagonal slash (the ⊘ stop
              // symbol) so it reads as a terminal marker distinct from
              // the drift diamond. Operator-only — no click-through to
              // a separate panel beyond the hover tooltip (the cancel
              // detail lives alongside the drift drawer when a drift
              // backs it; see InterventionsList for click routing).
              const r = 6;
              return (
                <g
                  key={g.id}
                  data-testid={`cancel-glyph-${Math.abs(g.driftSeq) - 1}`}
                  data-severity={g.severity || undefined}
                  data-variant="cancel"
                >
                  <circle
                    cx={cx}
                    cy={cy}
                    r={r}
                    fill="#e05e4a"
                    stroke="var(--md-sys-color-surface, #10131a)"
                    strokeWidth={1.2}
                  />
                  <line
                    x1={cx - r + 1.4}
                    y1={cy + r - 1.4}
                    x2={cx + r - 1.4}
                    y2={cy - r + 1.4}
                    stroke="#0b0d12"
                    strokeWidth={1.6}
                  />
                  <title>
                    {`CANCELLED${g.kind ? ' · ' + g.kind : ''}${g.severity ? ' · ' + g.severity : ''}${g.detail ? '\n' + g.detail : ''}`}
                  </title>
                </g>
              );
            }
            const r = 5;
            return (
              <g
                key={g.id}
                data-testid={`drift-glyph-${g.driftSeq}`}
                data-severity={g.severity || undefined}
                data-authored-by={g.authoredBy || undefined}
                style={{ cursor: clickable ? 'pointer' : 'default' }}
                onClick={(e) => {
                  if (!clickable) return;
                  e.stopPropagation();
                  setSteeringSelection({
                    kind: 'revision',
                    revision: g.revisionIndex,
                  });
                }}
              >
                {/* Diamond glyph — visually distinct from the circular */}
                {/* thinking-dot and square activation boxes. */}
                <polygon
                  points={`${cx},${cy - r} ${cx + r},${cy} ${cx},${cy + r} ${cx - r},${cy}`}
                  fill={sevColor}
                  stroke="var(--md-sys-color-surface, #10131a)"
                  strokeWidth={1}
                />
                <title>
                  {`${g.kind}${g.severity ? ' · ' + g.severity : ''}${g.detail ? '\n' + g.detail : ''}`}
                </title>
              </g>
            );
          })}
          </g>
        </svg>

          {/* Minimap */}
          <div
            ref={minimapRef}
            onPointerDown={(e) => {
              e.stopPropagation();
              minimapDragRef.current = true;
              (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
              handleMinimapSeek(e.clientX, e.clientY);
            }}
            onPointerMove={(e) => {
              if (!minimapDragRef.current) return;
              e.stopPropagation();
              handleMinimapSeek(e.clientX, e.clientY);
            }}
            onPointerUp={(e) => {
              minimapDragRef.current = false;
              try {
                (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
              } catch {
                /* ignore */
              }
            }}
            onPointerCancel={() => {
              minimapDragRef.current = false;
            }}
            onWheel={(e) => e.stopPropagation()}
            onClickCapture={(e) => e.stopPropagation()}
            style={{
              position: 'absolute',
              right: 12,
              bottom: 12,
              width: MINIMAP_W,
              height: MINIMAP_H,
              background: 'var(--md-sys-color-surface-container, #1a1f2a)',
              border: '1px solid var(--md-sys-color-outline-variant, #2a2f3a)',
              borderRadius: 6,
              boxShadow: '0 4px 14px rgba(0,0,0,0.35)',
              overflow: 'hidden',
              cursor: 'crosshair',
              zIndex: 15,
              touchAction: 'none',
            }}
            role="region"
            aria-label="Agent diagram minimap — click or drag to pan"
          >
            <svg
              width={MINIMAP_W}
              height={MINIMAP_H}
              style={{ display: 'block' }}
            >
              <g transform={`translate(${MINIMAP_PAD}, ${MINIMAP_PAD}) scale(${minimapScale})`}>
                {/* Full-content background */}
                <rect
                  x={0}
                  y={0}
                  width={svgW}
                  height={svgH}
                  fill="var(--md-sys-color-surface, #10131a)"
                  opacity={0.6}
                />
                {/* Agent lifelines */}
                {agentIds.map((_id, idx) => (
                  <line
                    key={`mm-life-${idx}`}
                    x1={colCx(idx)}
                    y1={HEADER_H}
                    x2={colCx(idx)}
                    y2={HEADER_H + plotH}
                    stroke={colorForAgent(agentIds[idx] ?? '')}
                    strokeWidth={1 / Math.max(minimapScale, 0.01)}
                    opacity={0.4}
                  />
                ))}
                {/* Activation boxes */}
                {activations.map((act, i) => {
                  const color = colorForAgent(agentIds[act.agentIdx] ?? '');
                  const y1 = timeY(act.startMs);
                  const endMs = act.endMs ?? Math.max(nowMs, act.startMs + 100);
                  const y2 = timeY(endMs);
                  return (
                    <rect
                      key={`mm-act-${i}`}
                      x={colCx(act.agentIdx) - ACT_W / 2}
                      y={y1}
                      width={ACT_W}
                      height={Math.max(2, y2 - y1)}
                      fill={color}
                      fillOpacity={0.7}
                    />
                  );
                })}
                {/* Agent header dots */}
                {agentIds.map((agentId, idx) => (
                  <circle
                    key={`mm-hd-${idx}`}
                    cx={colCx(idx)}
                    cy={HEADER_H - 8}
                    r={6 / Math.max(minimapScale, 0.01)}
                    fill={colorForAgent(agentId)}
                    opacity={0.85}
                  />
                ))}
              </g>
              {/* Viewport rectangle (in minimap pixel space). */}
              <rect
                x={Math.max(0, Math.min(MINIMAP_W - MINIMAP_PAD * 2, minimapRect.x)) + MINIMAP_PAD}
                y={Math.max(0, Math.min(MINIMAP_H - MINIMAP_PAD * 2, minimapRect.y)) + MINIMAP_PAD}
                width={Math.max(
                  3,
                  Math.min(MINIMAP_W - MINIMAP_PAD * 2 - Math.max(0, minimapRect.x), minimapRect.w),
                )}
                height={Math.max(
                  3,
                  Math.min(MINIMAP_H - MINIMAP_PAD * 2 - Math.max(0, minimapRect.y), minimapRect.h),
                )}
                fill="rgba(100,140,255,0.15)"
                stroke="rgba(140,170,255,0.85)"
                strokeWidth={1}
              />
            </svg>
          </div>
        {steeringSelection && (() => {
          // Locate the plan that owns this revision. The Graph view is
          // plan-agnostic (it spans all agents in the session), so we
          // walk the PlanHistoryRegistry plan-by-plan until we find one
          // carrying the selected revision. Single-plan sessions resolve
          // on the first iteration.
          let plan: TaskPlan | null = null;
          let history: readonly import('../../../state/planHistoryStore').PlanRevisionRecord[] = [];
          let supersedes: Map<string, import('../../../state/planHistoryStore').SupersessionLink> = new Map();
          for (const pid of watch.store.planHistory.planIds()) {
            const recs = watch.store.planHistory.historyFor(pid);
            if (recs.some((r) => r.revision === steeringSelection.revision)) {
              plan = watch.store.tasks.getPlan(pid) ?? null;
              history = recs;
              supersedes = watch.store.planHistory.supersedesMap(pid);
              break;
            }
          }
          return (
            <SteeringDetailPanel
              selection={steeringSelection}
              plan={plan}
              history={history}
              supersedes={supersedes}
              store={watch.store}
              onClose={() => setSteeringSelection(null)}
              onJumpToGantt={() => setSteeringSelection(null)}
            />
          );
        })()}
        {hoveredTask && (
          <div
            style={{
              position: 'absolute',
              left: hoveredTask.x * vp.scale + vp.tx,
              top: hoveredTask.y * vp.scale + vp.ty,
              maxWidth: 260,
              padding: '8px 10px',
              background: 'var(--md-sys-color-surface-container-high, #1a1f2a)',
              border: '1px solid var(--md-sys-color-outline-variant, #2a2f3a)',
              borderRadius: 6,
              boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              fontSize: 11,
              color: 'var(--md-sys-color-on-surface, #e3e6ef)',
              zIndex: 10,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ fontWeight: 600, marginBottom: 2 }}>
              {hoveredTask.task.title}
            </div>
            <div style={{ opacity: 0.8, marginBottom: 4 }}>
              {hoveredTask.task.description || <em>No description</em>}
            </div>
            <div
              style={{ fontSize: 10, opacity: 0.7 }}
              title={hoveredTask.task.assigneeAgentId || undefined}
            >
              Assignee:{' '}
              {hoveredTask.task.assigneeAgentId
                ? watch.store?.agents.get(hoveredTask.task.assigneeAgentId)
                    ?.name ||
                  bareAgentName(hoveredTask.task.assigneeAgentId) ||
                  hoveredTask.task.assigneeAgentId
                : '(unassigned)'}
            </div>
            <div style={{ fontSize: 10, opacity: 0.7 }}>
              Status: {hoveredTask.task.status}
            </div>
            {hoveredTask.plan.edges.some(
              (e) => e.toTaskId === hoveredTask.task.id,
            ) && (
              <div style={{ fontSize: 10, opacity: 0.7, marginTop: 2 }}>
                Depends on:{' '}
                {hoveredTask.plan.edges
                  .filter((e) => e.toTaskId === hoveredTask.task.id)
                  .map((e) => e.fromTaskId)
                  .join(', ')}
              </div>
            )}
          </div>
        )}
        </div>
      </div>
    </section>
  );
}

const toolbarBtnStyle: React.CSSProperties = {
  padding: '4px 8px',
  fontSize: 11,
  background: 'var(--md-sys-color-surface-container, #1a1f2a)',
  color: 'var(--md-sys-color-on-surface, #e3e6ef)',
  border: '1px solid var(--md-sys-color-outline-variant, #2a2f3a)',
  borderRadius: 4,
  cursor: 'pointer',
};
