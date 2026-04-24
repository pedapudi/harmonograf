// Mutable, non-React data store for the Gantt. React components subscribe for
// presence (agent list, counts) but the hot rendering path reads directly from
// these stores every frame — no setState in the data path.

import type { Agent, ContextWindowSample, Task, TaskPlan, TaskStatus } from './types';
import { SpanIndex, type DirtyRect } from './spatialIndex';
import { hasThinking } from '../lib/thinking';
import { PlanHistoryRegistry } from '../state/planHistoryStore';

export type { DirtyRect } from './spatialIndex';
export { SpanIndex } from './spatialIndex';
export type {
  Agent,
  ContextWindowSample,
  Span,
  SpanKind,
  SpanStatus,
  SpanLink,
  Capability,
  Task,
  TaskEdge,
  TaskPlan,
  TaskStatus,
} from './types';

// Metadata key used to mark an agent row as a placeholder — seeded from
// plan content (task.assignee_agent_id) before any real span has landed
// for that agent. The renderer does NOT style placeholders differently
// today, but consumers can branch on this marker if they need to (e.g.
// a dimmer lane header, a "not yet active" tooltip).
//
// Value is "1" when the row originated from plan seeding and has never
// been upgraded by a real telemetry upsert. As soon as a real agent row
// (Hello / AgentsListed / span-emission path) lands, AgentRegistry.upsert
// clobbers the placeholder and the flag is dropped.
export const PLACEHOLDER_AGENT_FLAG = 'harmonograf.announced_only';

// Strip a compound agent id (`<client>:<bare>`) down to its bare form
// by lopping off everything up to and including the last `":"`. Matches
// the inverse of `HarmonografSink._compound()` on the client side — a
// bare id (no `:`) is returned unchanged, as is the empty string.
export function bareAgentName(agentId: string): string {
  if (!agentId) return agentId;
  const idx = agentId.lastIndexOf(':');
  if (idx < 0) return agentId;
  return agentId.slice(idx + 1);
}

// Registry of agents in a session. Order is join time (stable) — doc 04 §5.1.
export class AgentRegistry {
  private agents: Agent[] = [];
  private byId = new Map<string, Agent>();
  private listeners = new Set<() => void>();

  get list(): readonly Agent[] {
    return this.agents;
  }

  get size(): number {
    return this.agents.length;
  }

  get(id: string): Agent | undefined {
    return this.byId.get(id);
  }

  indexOf(id: string): number {
    const a = this.byId.get(id);
    if (!a) return -1;
    return this.agents.indexOf(a);
  }

  upsert(agent: Agent): void {
    const existing = this.byId.get(agent.id);
    if (existing) {
      // A real upsert (span-emission / Hello / AgentsListed) always wins
      // over a placeholder — drop the placeholder marker so downstream
      // consumers stop treating this row as "announced but unobserved".
      // Preserve metadata keys the caller didn't supply so metadata set
      // by plan seeding (none today besides the flag) isn't silently
      // dropped.
      const prevMeta = existing.metadata ?? {};
      const nextMeta = { ...prevMeta, ...(agent.metadata ?? {}) };
      delete nextMeta[PLACEHOLDER_AGENT_FLAG];
      Object.assign(existing, agent);
      existing.metadata = nextMeta;
    } else {
      this.byId.set(agent.id, agent);
      this.agents.push(agent);
      this.agents.sort((a, b) => a.connectedAtMs - b.connectedAtMs);
    }
    this.emit();
  }

  // Upsert a minimal placeholder row for an agent announced on the wire
  // (today: via task.assignee_agent_id on a PlanSubmitted / PlanRevised)
  // but not yet observed via any span, Hello, or AgentsListed frame.
  //
  // Idempotent: a second call with the same args is a no-op. When the
  // row already exists and is NOT a placeholder (i.e. a real upsert has
  // landed), this method leaves it entirely alone — plan seeding must
  // never clobber authoritative agent metadata (connection status,
  // framework, capabilities, task reports, …).
  //
  // Returns `true` when a new placeholder was inserted, `false` when a
  // pre-existing row was left untouched. Callers typically ignore the
  // return; it's exposed for tests.
  ensureAgent(agentId: string, bareName: string): boolean {
    if (!agentId) return false;
    const existing = this.byId.get(agentId);
    if (existing) {
      // Leave real rows alone. Also leave existing placeholders alone —
      // the name we'd derive is a pure function of the id, so re-seeding
      // with the same inputs would be a no-op write anyway.
      return false;
    }
    const placeholder: Agent = {
      id: agentId,
      name: bareName || agentId,
      framework: 'UNKNOWN',
      capabilities: [],
      // DISCONNECTED is the cleanest existing status for "announced but
      // not yet observed" — a real CONNECTED frame upgrades it via
      // upsert() (which also strips the placeholder flag).
      status: 'DISCONNECTED',
      // connectedAtMs drives row ordering. 0 sorts to the top alongside
      // any other placeholders; real agents with a real connect time
      // will sort in below once they land.
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {
        [PLACEHOLDER_AGENT_FLAG]: '1',
      },
    };
    this.byId.set(agentId, placeholder);
    this.agents.push(placeholder);
    this.agents.sort((a, b) => a.connectedAtMs - b.connectedAtMs);
    this.emit();
    return true;
  }

  setStatus(id: string, status: Agent['status']): void {
    const a = this.byId.get(id);
    if (!a || a.status === status) return;
    a.status = status;
    this.emit();
  }

  setActivityAndStuck(id: string, currentActivity: string, stuck: boolean): void {
    const a = this.byId.get(id);
    if (!a) return;
    a.currentActivity = currentActivity;
    a.stuck = stuck;
    this.emit();
  }

  setTaskReport(agentId: string, report: string, recordedAt: number): void {
    const a = this.byId.get(agentId);
    if (!a) return;
    a.taskReport = report;
    a.taskReportAt = recordedAt;
    this.emit();
  }

  clearTaskReport(agentId: string): void {
    const a = this.byId.get(agentId);
    if (!a || !a.taskReport) return;
    a.taskReport = '';
    this.emit();
  }

  clear(): void {
    this.agents = [];
    this.byId.clear();
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}

// Registry of TaskPlans for the current session. Like AgentRegistry, this is a
// plain mutable store — the renderer reads the current snapshot per frame and
// React chrome subscribes via subscribe() to know when to redraw.
export type PlanDiffFieldChange =
  | 'title'
  | 'description'
  | 'assignee'
  | 'status';

export interface PlanDiff {
  added: Task[];
  // Removed entries keep the title so UI can still render it after the task
  // is gone from the current plan snapshot.
  removed: Array<{ id: string; title: string }>;
  modified: Array<{ id: string; title: string; changes: PlanDiffFieldChange[] }>;
  edgesChanged: boolean;
}

export interface PlanRevision {
  revisedAtMs: number;
  reason: string;
  diff: PlanDiff;
}

export function computePlanDiff(
  prev: TaskPlan | undefined,
  next: TaskPlan,
): PlanDiff {
  const prevTasks = new Map<string, Task>();
  for (const t of prev?.tasks ?? []) prevTasks.set(t.id, t);
  const nextIds = new Set<string>();
  const added: Task[] = [];
  const modified: PlanDiff['modified'] = [];

  for (const t of next.tasks) {
    nextIds.add(t.id);
    const p = prevTasks.get(t.id);
    if (!p) {
      added.push(t);
      continue;
    }
    const changes: PlanDiffFieldChange[] = [];
    if (p.title !== t.title) changes.push('title');
    if (p.description !== t.description) changes.push('description');
    if (p.assigneeAgentId !== t.assigneeAgentId) changes.push('assignee');
    if (p.status !== t.status) changes.push('status');
    if (changes.length > 0) {
      modified.push({ id: t.id, title: t.title || p.title, changes });
    }
  }
  const removed: PlanDiff['removed'] = [];
  for (const [id, t] of prevTasks) {
    if (!nextIds.has(id)) removed.push({ id, title: t.title });
  }

  // Edge equality is order-insensitive: treat the edge set as "from->to" keys
  // and flag changed if either side has a key the other doesn't.
  const prevEdges = new Set<string>();
  for (const e of prev?.edges ?? []) {
    prevEdges.add(`${e.fromTaskId}->${e.toTaskId}`);
  }
  const nextEdges = new Set<string>();
  for (const e of next.edges) {
    nextEdges.add(`${e.fromTaskId}->${e.toTaskId}`);
  }
  let edgesChanged = prevEdges.size !== nextEdges.size;
  if (!edgesChanged) {
    for (const k of nextEdges) {
      if (!prevEdges.has(k)) {
        edgesChanged = true;
        break;
      }
    }
  }

  return { added, removed, modified, edgesChanged };
}

function clonePlan(p: TaskPlan): TaskPlan {
  return {
    ...p,
    tasks: p.tasks.map((t) => ({ ...t })),
    edges: p.edges.map((e) => ({ ...e })),
  };
}

export class TaskRegistry {
  private plans: TaskPlan[] = [];
  private byId = new Map<string, TaskPlan>();
  private listeners = new Set<() => void>();
  private _revisionsByPlan = new Map<string, PlanRevision[]>();
  private _lastReasonByPlan = new Map<string, string>();
  // Snapshots of each past plan rev, oldest-first, keyed by plan_id. Captured
  // at the moment a new rev replaces an old one; the current rev is _not_
  // duplicated here (callers read it via getPlan/listPlans instead). These
  // snapshots are deep-cloned so later status mutations on the live plan
  // don't leak into history.
  private _snapshotsByPlan = new Map<string, TaskPlan[]>();

  listPlans(): readonly TaskPlan[] {
    return this.plans;
  }

  getPlan(id: string): TaskPlan | undefined {
    return this.byId.get(id);
  }

  // Add a plan or replace an existing one. On re-upsert the plan object is
  // swapped wholesale rather than mutated in place — a new reference is
  // required so downstream consumers using React memoization (useMemo keyed
  // on `plan`) invalidate when a refine arrives. The server always sends a
  // complete snapshot, so merging fields would be unsafe anyway.
  upsertPlan(plan: TaskPlan): void {
    // Defensive de-dup: if another plan with the same invocationSpanId
    // already exists under a different plan_id, treat the incoming one as
    // a replacement for it. Belt-and-suspenders against client-side
    // double-submission producing two plan_ids for the same invocation.
    let prevPlan: TaskPlan | undefined;
    if (plan.invocationSpanId) {
      const sibling = this.plans.find(
        (p) =>
          p.invocationSpanId === plan.invocationSpanId && p.id !== plan.id,
      );
      if (sibling) {
        prevPlan = sibling;
        this.byId.delete(sibling.id);
        const sidx = this.plans.indexOf(sibling);
        if (sidx >= 0) this.plans.splice(sidx, 1);
      }
    }
    const existing = this.byId.get(plan.id);
    if (existing) {
      prevPlan = prevPlan ?? existing;
      // A new revisionIndex means this is a true refine — snapshot the old
      // plan before we overwrite it. Re-upserts with the same rev index
      // (e.g. stream reconnects) don't produce a snapshot.
      const existingRev = existing.revisionIndex ?? 0;
      const incomingRev = plan.revisionIndex ?? 0;
      if (existingRev !== incomingRev) {
        const snaps = this._snapshotsByPlan.get(plan.id) ?? [];
        snaps.push(clonePlan(existing));
        this._snapshotsByPlan.set(plan.id, snaps);
      }
      this.byId.set(plan.id, plan);
      const idx = this.plans.indexOf(existing);
      if (idx >= 0) {
        this.plans[idx] = plan;
      } else {
        this.plans.push(plan);
        this.plans.sort((a, b) => a.createdAtMs - b.createdAtMs);
      }
    } else {
      this.byId.set(plan.id, plan);
      this.plans.push(plan);
      this.plans.sort((a, b) => a.createdAtMs - b.createdAtMs);
    }
    const nextReason = plan.revisionReason || '';
    const prevReason = this._lastReasonByPlan.get(plan.id) || '';
    if (nextReason && nextReason !== prevReason) {
      const arr = this._revisionsByPlan.get(plan.id) ?? [];
      arr.push({
        revisedAtMs: Date.now(),
        reason: nextReason,
        diff: computePlanDiff(prevPlan, plan),
      });
      if (arr.length > 20) arr.shift();
      this._revisionsByPlan.set(plan.id, arr);
      this._lastReasonByPlan.set(plan.id, nextReason);
    }
    this.emit();
  }

  revisionsForPlan(planId: string): ReadonlyArray<PlanRevision> {
    return this._revisionsByPlan.get(planId) ?? [];
  }

  // Past plan snapshots, oldest-first. Does NOT include the current live plan
  // — combine with getPlan(planId) to get the full rev history.
  snapshotsForPlan(planId: string): ReadonlyArray<TaskPlan> {
    return this._snapshotsByPlan.get(planId) ?? [];
  }

  // Full rev sequence: every past snapshot plus the current live plan at the
  // tail. Returns [] if the plan id is unknown. Used by the trajectory view
  // to walk rev 0 → rev N in order.
  allRevsForPlan(planId: string): ReadonlyArray<TaskPlan> {
    const snaps = this._snapshotsByPlan.get(planId) ?? [];
    const current = this.byId.get(planId);
    if (!current) return snaps;
    return [...snaps, current];
  }

  // Delta update: change one task's status and/or bound span id.
  updateTaskStatus(
    planId: string,
    taskId: string,
    status: TaskStatus,
    boundSpanId: string,
  ): void {
    const plan = this.byId.get(planId);
    if (!plan) return;
    const task = plan.tasks.find((t) => t.id === taskId);
    if (!task) return;
    task.status = status;
    if (boundSpanId) task.boundSpanId = boundSpanId;
    this.emit();
  }

  // Goldfive task_* events carry only the task_id (task ids are unique
  // across plans in a run). Search every plan; silently no-op if the
  // task hasn't been introduced yet (a plan_submitted event may arrive
  // out of order with a task_started on a reconnect).
  updateTaskStatusByTaskId(
    taskId: string,
    status: TaskStatus,
    boundSpanId?: string,
    cancelReason?: string,
  ): void {
    for (const plan of this.plans) {
      const task = plan.tasks.find((t) => t.id === taskId);
      if (!task) continue;
      task.status = status;
      if (boundSpanId) task.boundSpanId = boundSpanId;
      // harmonograf#110 / goldfive#205: stamp the structured cancel
      // reason when the transition carried one. Preserve an already-
      // stored reason across non-cancel transitions (e.g. a late
      // BLOCKED ping should not blank a prior CANCELLED reason).
      if (cancelReason) task.cancelReason = cancelReason;
      this.emit();
      return;
    }
  }

  // Flattened helper: every task (across all plans) assigned to an agent.
  // Used by the renderer to build the per-column pre-strip.
  tasksForAgent(agentId: string): Task[] {
    const out: Task[] = [];
    for (const plan of this.plans) {
      for (const task of plan.tasks) {
        if (task.assigneeAgentId === agentId) out.push(task);
      }
    }
    return out;
  }

  // Find the plan that owns a given task id (useful for click-through).
  findPlanForTask(taskId: string): { plan: TaskPlan; task: Task } | undefined {
    for (const plan of this.plans) {
      const task = plan.tasks.find((t) => t.id === taskId);
      if (task) return { plan, task };
    }
    return undefined;
  }

  get size(): number {
    return this.plans.length;
  }

  clear(): void {
    this.plans = [];
    this.byId.clear();
    this._revisionsByPlan.clear();
    this._lastReasonByPlan.clear();
    this._snapshotsByPlan.clear();
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(): void {
    for (const fn of this.listeners) fn();
  }
}

// Harmonograf reporting tools — TOOL_CALL spans whose `name` matches one of
// these are surfaced in the Orchestration timeline as explicit orchestration
// signals rather than generic tool invocations.
export const ORCHESTRATION_TOOL_NAMES = [
  'report_task_started',
  'report_task_progress',
  'report_task_completed',
  'report_task_failed',
  'report_task_blocked',
  'report_new_work_discovered',
  'report_plan_divergence',
] as const;

export type OrchestrationEventKind =
  | 'started'
  | 'progress'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'discovered'
  | 'divergence';

export interface OrchestrationEvent {
  spanId: string;
  agentId: string;
  kind: OrchestrationEventKind;
  toolName: string;
  startMs: number;
  taskId: string;
  title: string;
  detail: string;
  recoverable: boolean | null;
}

const TOOL_KIND_MAP: Record<string, OrchestrationEventKind> = {
  report_task_started: 'started',
  report_task_progress: 'progress',
  report_task_completed: 'completed',
  report_task_failed: 'failed',
  report_task_blocked: 'blocked',
  report_new_work_discovered: 'discovered',
  report_plan_divergence: 'divergence',
};

function parseArgsPreview(
  raw: string | undefined,
): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function pickString(
  obj: Record<string, unknown> | null,
  key: string,
): string {
  if (!obj) return '';
  const v = obj[key];
  return typeof v === 'string' ? v : '';
}

// Per-agent context-window sample series. Samples are monotonic by tMs
// within an agent; the renderer walks them linearly per frame clipping to
// the viewport window.
//
// Kept separate from AgentRegistry so an agent with zero samples doesn't
// allocate an empty array, and so the subscribe channel fans out per-agent
// (chrome can observe one agent's header chip without rerendering on every
// other agent's heartbeat).
export class ContextSeriesRegistry {
  private byAgent = new Map<string, ContextWindowSample[]>();
  private listeners = new Set<(agentId: string) => void>();

  append(agentId: string, sample: ContextWindowSample): void {
    // Drop samples where both tokens and limit are zero (unknown). The server
    // already filters these; this is a belt-and-suspenders guard for tests
    // and mock data.
    if (sample.tokens === 0 && sample.limitTokens === 0) return;
    let arr = this.byAgent.get(agentId);
    if (!arr) {
      arr = [];
      this.byAgent.set(agentId, arr);
    }
    // Preserve monotonic order even if the wire happens to deliver slightly
    // out-of-order samples (e.g. replay burst after the first live sample).
    // Linear walk from the tail is fine — the burst is bounded to ~200.
    let i = arr.length;
    while (i > 0 && arr[i - 1].tMs > sample.tMs) i--;
    if (i === arr.length) {
      arr.push(sample);
    } else {
      arr.splice(i, 0, sample);
    }
    this.emit(agentId);
  }

  forAgent(agentId: string): readonly ContextWindowSample[] {
    return this.byAgent.get(agentId) ?? EMPTY_SAMPLES;
  }

  latest(agentId: string): ContextWindowSample | null {
    const arr = this.byAgent.get(agentId);
    if (!arr || arr.length === 0) return null;
    return arr[arr.length - 1];
  }

  hasAny(): boolean {
    for (const arr of this.byAgent.values()) {
      if (arr.length > 0) return true;
    }
    return false;
  }

  clear(): void {
    if (this.byAgent.size === 0) return;
    this.byAgent.clear();
    // Broadcast a wildcard clear so observers can drop cached derived state.
    this.emit('');
  }

  subscribe(fn: (agentId: string) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(agentId: string): void {
    for (const fn of this.listeners) fn(agentId);
  }
}

const EMPTY_SAMPLES: readonly ContextWindowSample[] = Object.freeze([]);

// A captured goldfive DriftDetected event. The trajectory view anchors these
// to their owning plan rev (by position in the event stream) and task.
export interface DriftRecord {
  seq: number;            // monotonic across the session, assigned on arrival
  kind: string;           // lowercase DriftKind, e.g. 'user_steer'
  severity: string;       // 'info' | 'warning' | 'critical' | ''
  detail: string;
  taskId: string;
  agentId: string;
  // Session-relative ms — derived from recordedAtAbsoluteMs on ingest and
  // refreshed by DriftRegistry.rebase(sessionStartMs) once the session's
  // wall-clock start lands on the frontend. Readers consume this directly.
  recordedAtMs: number;
  // Authoritative wall-clock timestamp in ms (from Event.emitted_at).
  // Stored separately so that if the 'session' update races the first
  // drift event on the live path, we can recompute recordedAtMs correctly
  // once the session start is known (harmonograf#127 — kills a whole
  // class of session-relative-at-ingest races that would otherwise
  // misalign drift markers on the Gantt / Trajectory timelines).
  recordedAtAbsoluteMs: number;
  // Non-empty for USER_STEER / USER_CANCEL drifts minted by goldfive
  // from a ControlMessage carrying a bridge-supplied annotation_id
  // (goldfive#176). Used by the intervention deriver (harmonograf#75)
  // to collapse the drift row into the source annotation row so a
  // single user STEER renders as one card, not three.
  annotationId: string;
  // goldfive#199 / harmonograf#99: goldfive-minted drift id (UUID4),
  // always non-empty. Used as the strict join key when merging a
  // PlanRevised row triggered by this autonomous drift onto the drift
  // row (the PlanRevised.trigger_event_id will match).
  driftId: string;
  // Forward-compat (goldfive/tmp/goldfive-steer-unify): drift authorship.
  // "user" (user_steer / user_cancel / user_pause), "goldfive" (anything
  // the orchestrator minted itself), or "" when the producing goldfive
  // build predates the field. Surfaced in the intervention detail pane.
  authoredBy?: string;
}

// Drift registry — in-memory list of DriftDetected events received during
// the session. Kept separate from TaskRegistry so the trajectory view can
// subscribe to drift arrivals without re-rendering on every plan task
// status change.
export class DriftRegistry {
  private drifts: DriftRecord[] = [];
  private listeners = new Set<() => void>();
  private nextSeq = 0;

  list(): readonly DriftRecord[] {
    return this.drifts;
  }

  append(
    d: Omit<DriftRecord, 'seq' | 'recordedAtAbsoluteMs'> &
      Partial<Pick<DriftRecord, 'recordedAtAbsoluteMs'>>,
  ): void {
    // Default absolute to relative for callers that haven't been migrated
    // (older tests, synthetic fixtures). Rebase is a no-op when
    // wallClockStartMs is 0, so these records retain their original
    // recordedAtMs — matching pre-#127 semantics.
    const recordedAtAbsoluteMs = d.recordedAtAbsoluteMs ?? d.recordedAtMs;
    this.drifts.push({
      ...d,
      recordedAtAbsoluteMs,
      seq: this.nextSeq++,
    });
    this.emit();
  }

  // Recompute every drift's session-relative `recordedAtMs` from its
  // authoritative `recordedAtAbsoluteMs` using the newly-known session
  // start. Called from SessionStore.rebaseRelativeTimestamps when the
  // 'session' SessionUpdate finally delivers `created_at` to the
  // frontend — previously, a live-delivered DriftDetected that landed
  // before the session update was recorded with a bogus wall-clock-scale
  // recordedAtMs (sessionStartMs was still 0 at ingest) and the marker
  // rendered miles off-axis on the Gantt / Trajectory timelines.
  rebase(sessionStartMs: number): void {
    if (this.drifts.length === 0) return;
    let changed = false;
    for (const d of this.drifts) {
      // Records ingested before this PR (or with no absolute ms) fall back
      // to whatever was stored — don't mutate so we don't regress legacy
      // paths that already stored a usable relative ms.
      if (!d.recordedAtAbsoluteMs) continue;
      const next = d.recordedAtAbsoluteMs - sessionStartMs;
      if (d.recordedAtMs !== next) {
        d.recordedAtMs = next;
        changed = true;
      }
    }
    if (changed) this.emit();
  }

  clear(): void {
    if (this.drifts.length === 0) return;
    this.drifts = [];
    this.nextSeq = 0;
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}

// A captured goldfive DelegationObserved event. Emitted when a coordinator
// agent invokes `AgentTool(sub_agent)` — goldfive observes the tool call on
// its registry-dispatch side and fans out a DelegationObserved event so
// sinks can render an explicit from→to edge that the telemetry plugin's
// generic TOOL_CALL span on the coordinator row would otherwise leave
// implicit.
export interface DelegationRecord {
  seq: number;             // monotonic across the session, assigned on arrival
  fromAgentId: string;     // coordinator (the observer-side "from")
  toAgentId: string;       // sub-agent the coordinator delegated to
  taskId: string;          // empty when the host agent has no bound task
  invocationId: string;    // ADK invocation id for the delegation
  // Session-relative ms. Derived from observedAtAbsoluteMs on ingest and
  // refreshed by DelegationRegistry.rebase(sessionStartMs) once the
  // session's wall-clock start lands. All renderers (Gantt delegation
  // edge pass, GraphView arrow projection) read this field directly.
  observedAtMs: number;
  // Authoritative wall-clock ms from Event.emitted_at. Kept alongside
  // the relative field so that live-delivered DelegationObserved events
  // that arrive before the 'session' SessionUpdate (sessionStartMs=0 at
  // ingest) can be re-anchored once the session start is known — the
  // refresh-path replay worked only because the burst ordered the
  // 'session' frame first, which set sessionStartMs before the first
  // delegation_observed event was replayed. harmonograf#127.
  observedAtAbsoluteMs: number;
}

// Delegation registry — in-memory list of DelegationObserved events received
// during the session. Shape mirrors DriftRegistry so consumers can subscribe
// without special-casing and the Gantt's delegation-edge render pass can
// walk a single array per frame.
export class DelegationRegistry {
  private delegations: DelegationRecord[] = [];
  private listeners = new Set<() => void>();
  private nextSeq = 0;
  // Dedup key set — harmonograf#117's WatchSession can deliver the same
  // DelegationObserved event via both the initial-burst replay of persisted
  // events AND the live bus publish during the subscription-setup race,
  // which previously caused two ↪↪ arrows to render per delegation. The key
  // is (fromAgentId|toAgentId|invocationId) when an invocationId is present
  // (unique per delegation), else a timestamp-based fallback for older
  // goldfive versions that don't set invocation_id.
  private seen = new Set<string>();

  list(): readonly DelegationRecord[] {
    return this.delegations;
  }

  append(
    d: Omit<DelegationRecord, 'seq' | 'observedAtAbsoluteMs'> &
      Partial<Pick<DelegationRecord, 'observedAtAbsoluteMs'>>,
  ): void {
    // Default absolute to relative for callers that haven't been migrated
    // (older tests, synthetic fixtures). Rebase is a no-op when
    // wallClockStartMs is 0, so these records keep their original
    // observedAtMs — which matches pre-#127 behavior.
    const observedAtAbsoluteMs = d.observedAtAbsoluteMs ?? d.observedAtMs;
    const key = d.invocationId
      ? `${d.fromAgentId}|${d.toAgentId}|inv:${d.invocationId}`
      : `${d.fromAgentId}|${d.toAgentId}|ts:${
          // Prefer the absolute ms for the dedup fallback key so a
          // record ingested before `sessionStartMs` lands (relative
          // would be ~wall-clock scale) and the rebased same record
          // (relative much smaller) still dedup.
          observedAtAbsoluteMs || d.observedAtMs
        }`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.delegations.push({
      ...d,
      observedAtAbsoluteMs,
      seq: this.nextSeq++,
    });
    this.emit();
  }

  // Recompute every delegation's session-relative `observedAtMs` from its
  // authoritative `observedAtAbsoluteMs` using the newly-known session
  // start. See harmonograf#127: on the live path, the first
  // delegation_observed event can race the 'session' SessionUpdate that
  // seeds `store.wallClockStartMs`; without this rebase, the delegation
  // is permanently stamped with a wall-clock-scale observedAtMs and
  // renders miles off-axis on the Gantt and GraphView. The refresh path
  // happened to work because the server's initial burst orders the
  // 'session' frame before any replayed delegation_observed.
  rebase(sessionStartMs: number): void {
    if (this.delegations.length === 0) return;
    let changed = false;
    for (const d of this.delegations) {
      if (!d.observedAtAbsoluteMs) continue;
      const next = d.observedAtAbsoluteMs - sessionStartMs;
      if (d.observedAtMs !== next) {
        d.observedAtMs = next;
        changed = true;
      }
    }
    if (changed) this.emit();
  }

  clear(): void {
    if (this.delegations.length === 0) return;
    this.delegations = [];
    this.nextSeq = 0;
    this.seen.clear();
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}

// A Session couples an AgentRegistry, a SpanIndex, and a TaskRegistry. The
// renderer and chrome read directly from all three.
export class SessionStore {
  readonly agents = new AgentRegistry();
  readonly spans = new SpanIndex();
  readonly tasks = new TaskRegistry();
  readonly drifts = new DriftRegistry();
  readonly delegations = new DelegationRegistry();
  readonly contextSeries = new ContextSeriesRegistry();
  // Accumulator for every revision of every plan observed in this
  // session. Fed by the `planSubmitted` / `planRevised` event handlers
  // and by the optional `GetSessionPlanHistory` RPC loader so the Task
  // / Plan panel and Trajectory view can render plans as evolving
  // artifacts (generation badges, annotated supersedes edges, historic
  // nodes) without mining the rolling snapshot buffer on `tasks`.
  readonly planHistory = new PlanHistoryRegistry();

  // Scan the span index for TOOL_CALL spans whose name is one of the
  // harmonograf reporting tools and project them as OrchestrationEvents.
  // Returns newest-first; caller can trim/slice.
  listOrchestrationEvents(limit = 200): OrchestrationEvent[] {
    const out: OrchestrationEvent[] = [];
    for (const span of this.spans.all()) {
      if (span.kind !== 'TOOL_CALL') continue;
      const kind = TOOL_KIND_MAP[span.name];
      if (!kind) continue;
      const previewAttr = span.attributes['tool_args_preview'];
      const preview =
        previewAttr && previewAttr.kind === 'string' ? previewAttr.value : '';
      const parsed = parseArgsPreview(preview);
      const taskId =
        pickString(parsed, 'task_id') || pickString(parsed, 'parent_task_id');
      let detail = '';
      if (parsed) {
        detail =
          pickString(parsed, 'detail') ||
          pickString(parsed, 'summary') ||
          pickString(parsed, 'reason') ||
          pickString(parsed, 'blocker') ||
          pickString(parsed, 'note') ||
          pickString(parsed, 'description') ||
          '';
      }
      const title = pickString(parsed, 'title');
      let recoverable: boolean | null = null;
      if (parsed && typeof parsed['recoverable'] === 'boolean') {
        recoverable = parsed['recoverable'] as boolean;
      }
      out.push({
        spanId: span.id,
        agentId: span.agentId,
        kind,
        toolName: span.name,
        startMs: span.startMs,
        taskId,
        title,
        detail,
        recoverable,
      });
    }
    out.sort((a, b) => b.startMs - a.startMs);
    if (out.length > limit) out.length = limit;
    return out;
  }

  // Flattened view: the "currently active" task across every plan in this
  // session. Preference order:
  //   1. a task whose status is RUNNING (across any plan),
  //   2. else the most recently completed task (by plan order),
  //   3. else null.
  // Used by the Drawer "Current task" section and the CurrentTaskStrip.
  //
  // When the task is RUNNING, the result is enriched with live context from
  // the span index: the most recent in-flight TOOL_CALL on the assignee agent
  // and whether any in-flight LLM_CALL on that agent carries reasoning
  // (per lib/thinking.hasThinking — has_reasoning flag or inline text).
  // These fields let the CurrentTaskStrip surface a tool badge and thinking
  // dot without the component having to crawl the span index itself.
  getCurrentTask(): {
    task: Task;
    plan: TaskPlan;
    inFlightTool?: { name: string; startedAtMs: number };
    isThinking: boolean;
  } | null {
    const plans = this.tasks.listPlans();
    let found: { task: Task; plan: TaskPlan } | null = null;
    let lastDone: { task: Task; plan: TaskPlan } | null = null;
    for (const plan of plans) {
      for (const task of plan.tasks) {
        if (task.status === 'RUNNING') {
          found = { task, plan };
          break;
        }
        if (
          task.status === 'COMPLETED' ||
          task.status === 'FAILED' ||
          task.status === 'CANCELLED'
        ) {
          lastDone = { task, plan };
        }
      }
      if (found) break;
    }
    const picked = found ?? lastDone;
    if (!picked) return null;

    // Only enrich with live span context while the task is still RUNNING —
    // once it's done, the strip shows the outcome without any in-flight
    // indicators (which would be stale by definition).
    if (picked.task.status !== 'RUNNING' || !picked.task.assigneeAgentId) {
      return { ...picked, isThinking: false };
    }

    let inFlightTool: { name: string; startedAtMs: number } | undefined;
    let isThinking = false;
    const agentSpans = this.spans.queryAgent(
      picked.task.assigneeAgentId,
      0,
      Number.POSITIVE_INFINITY,
    );
    for (const span of agentSpans) {
      if (span.endMs != null) continue;
      if (span.kind === 'TOOL_CALL') {
        if (!inFlightTool || span.startMs > inFlightTool.startedAtMs) {
          inFlightTool = { name: span.name, startedAtMs: span.startMs };
        }
      } else if (span.kind === 'LLM_CALL') {
        // Route through lib/thinking.hasThinking() so we honor both the
        // has_reasoning bool flag and any reasoning text carrier (#107).
        if (hasThinking(span)) {
          isThinking = true;
        }
      }
    }
    return { ...picked, inFlightTool, isThinking };
  }
  // Session start timestamp (wall clock ms). startMs/endMs in spans are
  // session-relative, so this only matters for display formatting.
  wallClockStartMs = 0;

  // Refresh every session-relative timestamp cached on a child registry
  // so that records whose `*AtAbsoluteMs` was captured before
  // `wallClockStartMs` landed (live-path race, harmonograf#127) pick up
  // the now-correct session start. Today, only DriftRegistry and
  // DelegationRegistry participate — spans carry absolute times on the
  // wire and are relativized at ingest by `convertSpan`, which reads
  // `origin.startMs` that the transport layer (rpc/hooks.ts) guards
  // against by defaulting to the cached origin before subscribing.
  //
  // Idempotent: safe to call whenever `wallClockStartMs` changes,
  // including on re-delivery of a 'session' SessionUpdate. Each
  // registry short-circuits when nothing moves.
  rebaseRelativeTimestamps(sessionStartMs: number): void {
    this.drifts.rebase(sessionStartMs);
    this.delegations.rebase(sessionStartMs);
  }

  // Current wall-clock "now" relative to session start. Advanced by the
  // renderer each frame (or by the transport when paused).
  nowMs = 0;

  clear(): void {
    this.agents.clear();
    this.spans.clear();
    this.tasks.clear();
    this.drifts.clear();
    this.delegations.clear();
    this.contextSeries.clear();
    this.planHistory.clear();
    this.nowMs = 0;
  }
}

export function emptyDirty(): DirtyRect {
  return { agentId: null, t0: 0, t1: 0 };
}
