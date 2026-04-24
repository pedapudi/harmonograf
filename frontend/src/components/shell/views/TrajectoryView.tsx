import './views.css';
import type React from 'react';
import { useEffect, useMemo, useState } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import type { Span, Task, TaskEdge, TaskPlan, TaskStatus } from '../../../gantt/types';
import type {
  DelegationRecord,
  DriftRecord,
  SessionStore,
} from '../../../gantt/index';
import { bareAgentName } from '../../../gantt/index';
import { useAnnotationStore } from '../../../state/annotationStore';
import {
  deriveInterventionsFromStore,
  SOURCE_COLOR,
  type InterventionRow,
} from '../../../lib/interventions';
import {
  resolveDriftDetail,
  type InterventionDetail,
} from '../../../lib/interventionDetail';
import {
  useCumulativePlan,
  usePlanHistory,
  useSupersedesMap,
} from '../../../state/planHistoryHooks';
import type {
  CumulativePlan,
  CumulativeTaskMeta,
  PlanRevisionRecord,
  SupersessionLink,
} from '../../../state/planHistoryStore';
import { RevisionScrubber } from './RevisionScrubber';
import {
  SteeringDetailPanel,
  type SteeringSelection,
} from './SteeringDetailPanel';

// ── Palette ────────────────────────────────────────────────────────────────
// Aligned with GanttView's status palette so a task's status reads the same
// across panes. Severity palette is a separate axis (warm-cool) so the eye
// can tell a drift badge apart from a status fill.

const STATUS_COLOR: Record<TaskStatus, string> = {
  UNSPECIFIED: '#8d9199',
  PENDING: '#8d9199',
  RUNNING: '#5b8def',
  COMPLETED: '#4caf50',
  FAILED: '#e06070',
  CANCELLED: '#8d9199',
  BLOCKED: '#f59e0b',
};

const SEVERITY_COLOR: Record<string, string> = {
  info: '#5b8def',
  warning: '#f59e0b',
  critical: '#e06070',
  '': '#8d9199',
};

// Kinds we treat as user-authored pivots on the trajectory. These share the
// severity palette but get a distinct glyph so they're visually separate from
// model-authored drifts.
const STEER_KINDS = new Set(['user_steer', 'user_cancel', 'user_pause']);

// Upper bound on how many drift markers a single rev can render in the ribbon.
// goldfive can emit thousands of DRIFT_KIND_UNSPECIFIED drifts in a single
// session from misbehaving status-query paths; rendering a button per drift
// (flex-wrap) produces a wall of dots that pushes the DAG off-screen and
// freezes the browser. We keep the first N by severity+recency and render a
// "+M more" summary chip for the remainder. Empty-kind drifts are dropped
// entirely — goldfiveEvent.ts maps DRIFT_KIND_UNSPECIFIED → ''.
const RIBBON_MAX_MARKERS_PER_REV = 24;

// Filter out drifts whose kind is unknown/unspecified. These are inherently
// noise for the trajectory ribbon — the upstream taxonomy mapper already
// folds DRIFT_KIND_UNSPECIFIED to empty string (rpc/goldfiveEvent.ts
// driftKindToString). Keeps a single filter site so a future "show raw"
// affordance can flip it per-view.
function isRenderableDrift(d: DriftRecord): boolean {
  return !!(d.kind && d.kind.length > 0);
}

// Severity rank used when we have to trim a long drift list down to a
// bounded window: critical > warning > info > unspec. Within a rank we
// keep the most recent by recordedAtMs so the operator still sees the
// trailing activity of a dense session.
const SEVERITY_RANK: Record<string, number> = {
  critical: 3,
  warning: 2,
  info: 1,
  '': 0,
};

function selectTopDrifts(
  drifts: readonly DriftRecord[],
  max: number,
): { shown: DriftRecord[]; hidden: number } {
  if (drifts.length <= max) {
    return { shown: [...drifts], hidden: 0 };
  }
  const ranked = [...drifts].sort((a, b) => {
    const sa = SEVERITY_RANK[a.severity] ?? 0;
    const sb = SEVERITY_RANK[b.severity] ?? 0;
    if (sa !== sb) return sb - sa;
    return b.recordedAtMs - a.recordedAtMs;
  });
  const shown = ranked.slice(0, max);
  // Present in chronological order so the ribbon still reads left-to-right
  // even after we trimmed by severity.
  shown.sort((a, b) => a.recordedAtMs - b.recordedAtMs);
  return { shown, hidden: drifts.length - max };
}

// ── View model ─────────────────────────────────────────────────────────────

interface TrajectoryViewModel {
  planId: string | null;
  revs: TaskPlan[];
  driftsByRev: DriftRecord[][];
  allDrifts: DriftRecord[];
  allDelegations: DelegationRecord[];
}

function buildViewModel(store: SessionStore | null): TrajectoryViewModel {
  const empty: TrajectoryViewModel = {
    planId: null,
    revs: [],
    driftsByRev: [],
    allDrifts: [],
    allDelegations: [],
  };
  if (!store) return empty;
  const plans = store.tasks.listPlans();
  if (plans.length === 0) return empty;
  // Goldfive planners often mint a fresh plan_id on each refine, so we
  // cannot assume the session's plans share an id. The trajectory is the
  // time-ordered sequence of every plan this session has seen, plus all
  // snapshots of each plan_id (for planners that do keep the id stable).
  // Merge both into a single list sorted by createdAtMs, de-duped by
  // object identity.
  const seen = new Set<TaskPlan>();
  const combined: TaskPlan[] = [];
  for (const live of plans) {
    for (const snap of store.tasks.allRevsForPlan(live.id)) {
      if (seen.has(snap)) continue;
      seen.add(snap);
      combined.push(snap);
    }
  }
  combined.sort((a, b) => a.createdAtMs - b.createdAtMs);
  // Primary plan id is the one that produced the first rev — used only
  // for labeling / compare affordances; the trajectory view itself treats
  // every rev uniformly regardless of its plan_id.
  const primaryId = combined[0]?.id ?? plans[0].id;
  // Drop UNSPECIFIED-kind drifts up-front. Goldfive emits tens of thousands
  // of these from a misbehaving status-query path; rendering them in the
  // ribbon buries the DAG under a wall of dots and freezes the browser.
  // They carry no actionable information (no kind, no severity → the
  // ribbon marker would have no color or tooltip anyway).
  const drifts = store.drifts.list().filter(isRenderableDrift);
  const driftsByRev: DriftRecord[][] = combined.map(() => []);
  for (const d of drifts) {
    let idx = 0;
    for (let i = 0; i < combined.length; i++) {
      if (combined[i].createdAtMs <= d.recordedAtMs) idx = i;
      else break;
    }
    driftsByRev[idx].push(d);
  }
  const delegations = [...store.delegations.list()];
  return {
    planId: primaryId,
    revs: combined,
    driftsByRev,
    allDrifts: drifts,
    allDelegations: delegations,
  };
}

// ── DAG layout ─────────────────────────────────────────────────────────────

interface DagNode {
  task: Task;
  layer: number;
  row: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface DagLayout {
  nodes: DagNode[];
  nodeById: Map<string, DagNode>;
  edges: TaskEdge[];
  width: number;
  height: number;
}

const DAG_PAD = 24;
const DAG_COL_W = 176;
const DAG_COL_GAP = 56;
const DAG_BOX_H = 60;
const DAG_ROW_GAP = 20;

function layoutDag(plan: TaskPlan): DagLayout {
  const tasks = plan.tasks;
  const taskById = new Map<string, Task>(tasks.map((t) => [t.id, t]));
  const preds = new Map<string, string[]>();
  const succs = new Map<string, string[]>();
  for (const t of tasks) {
    preds.set(t.id, []);
    succs.set(t.id, []);
  }
  for (const e of plan.edges) {
    if (!taskById.has(e.fromTaskId) || !taskById.has(e.toTaskId)) continue;
    preds.get(e.toTaskId)!.push(e.fromTaskId);
    succs.get(e.fromTaskId)!.push(e.toTaskId);
  }

  // Kahn's topo → layer per task (= longest-path distance from a root).
  const layer = new Map<string, number>();
  const inDeg = new Map<string, number>();
  const queue: string[] = [];
  for (const t of tasks) {
    const n = (preds.get(t.id) ?? []).length;
    inDeg.set(t.id, n);
    if (n === 0) {
      layer.set(t.id, 0);
      queue.push(t.id);
    }
  }
  while (queue.length > 0) {
    const id = queue.shift()!;
    const L = layer.get(id) ?? 0;
    for (const s of succs.get(id) ?? []) {
      layer.set(s, Math.max(layer.get(s) ?? 0, L + 1));
      const d = (inDeg.get(s) ?? 1) - 1;
      inDeg.set(s, d);
      if (d === 0) queue.push(s);
    }
  }
  // Orphans (cycles, or not reachable): drop into layer 0.
  for (const t of tasks) {
    if (!layer.has(t.id)) layer.set(t.id, 0);
  }

  const maxLayer = tasks.reduce((m, t) => Math.max(m, layer.get(t.id) ?? 0), 0);
  const byLayer: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const t of tasks) byLayer[layer.get(t.id)!].push(t.id);
  for (const col of byLayer) col.sort();

  const nodes: DagNode[] = [];
  const nodeById = new Map<string, DagNode>();
  let maxRow = 0;
  for (let L = 0; L < byLayer.length; L++) {
    for (let r = 0; r < byLayer[L].length; r++) {
      const id = byLayer[L][r];
      const t = taskById.get(id)!;
      const node: DagNode = {
        task: t,
        layer: L,
        row: r,
        x: DAG_PAD + L * (DAG_COL_W + DAG_COL_GAP),
        y: DAG_PAD + r * (DAG_BOX_H + DAG_ROW_GAP),
        w: DAG_COL_W,
        h: DAG_BOX_H,
      };
      nodes.push(node);
      nodeById.set(id, node);
      if (r > maxRow) maxRow = r;
    }
  }
  const nCols = Math.max(1, byLayer.length);
  const width =
    DAG_PAD + nCols * DAG_COL_W + (nCols - 1) * DAG_COL_GAP + DAG_PAD;
  const height = DAG_PAD + (maxRow + 1) * (DAG_BOX_H + DAG_ROW_GAP) + DAG_PAD;
  return { nodes, nodeById, edges: plan.edges, width, height };
}

// ── Diff helpers ───────────────────────────────────────────────────────────

interface DiffMarks {
  added: Set<string>;
  removed: Set<string>;
  modified: Set<string>;
}

function diffMarks(prev: TaskPlan | null, curr: TaskPlan): DiffMarks {
  const prevById = new Map<string, Task>();
  for (const t of prev?.tasks ?? []) prevById.set(t.id, t);
  const currById = new Map<string, Task>();
  for (const t of curr.tasks) currById.set(t.id, t);

  const added = new Set<string>();
  const modified = new Set<string>();
  for (const t of curr.tasks) {
    const p = prevById.get(t.id);
    if (!p) {
      added.add(t.id);
      continue;
    }
    if (
      p.title !== t.title ||
      p.description !== t.description ||
      p.assigneeAgentId !== t.assigneeAgentId
    ) {
      modified.add(t.id);
    }
  }
  const removed = new Set<string>();
  for (const [id] of prevById) {
    if (!currById.has(id)) removed.add(id);
  }
  return { added, removed, modified };
}

// ── Time helpers ───────────────────────────────────────────────────────────

function fmtTime(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ── Selection state ────────────────────────────────────────────────────────

type Selection =
  | { kind: 'task'; taskId: string; planId: string }
  | { kind: 'drift'; seq: number }
  | null;

// ── Component ──────────────────────────────────────────────────────────────

export function TrajectoryView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const watch = useSessionWatch(sessionId);
  const store = sessionId ? watch.store : null;

  const [, setTick] = useState(0);
  useEffect(() => {
    if (!store) return;
    const un1 = store.tasks.subscribe(() => setTick((n) => n + 1));
    const un2 = store.drifts.subscribe(() => setTick((n) => n + 1));
    const un3 = store.spans.subscribe(() => setTick((n) => n + 1));
    const un4 = store.delegations.subscribe(() => setTick((n) => n + 1));
    // annotation store is external to SessionStore — subscribe so new
    // user STEERs / HUMAN_RESPONSEs immediately surface as intervention
    // entries in the trajectory.
    const un5 = useAnnotationStore.subscribe(() => setTick((n) => n + 1));
    return () => {
      un1();
      un2();
      un3();
      un4();
      un5();
    };
  }, [store]);

  // No memoization: buildViewModel is a small O(revs + drifts) walk and the
  // store is a stable object reference, so tick-driven re-renders need to
  // recompute to pick up the latest registry contents.
  const vm = buildViewModel(store);

  // Unified intervention history, derived from the same session store the
  // ribbon walks above. One source of truth for the strip in the planning
  // view, the chips in the trajectory ribbon, and the entries block below.
  const annotationsForSession = sessionId
    ? useAnnotationStore.getState().list(sessionId)
    : [];
  const interventions = store
    ? deriveInterventionsFromStore(store, annotationsForSession)
    : [];

  // Latest rev tracked live; user can pin a specific rev. `null` → live.
  const [pinnedRevIdx, setPinnedRevIdx] = useState<number | null>(null);
  const [compareRevIdx, setCompareRevIdx] = useState<number | null>(null);
  const [selection, setSelection] = useState<Selection>(null);

  // harmonograf#197: scrubber pins the cumulative view to a specific
  // revision number (tasks introduced after are hidden); null → "Latest".
  const [pinnedRevision, setPinnedRevision] = useState<number | null>(null);
  // Steering-arrow / supersedes-edge click target. Separate from
  // `selection` so the arrow panel can coexist with a task-detail selection.
  const [steeringSelection, setSteeringSelection] =
    useState<SteeringSelection | null>(null);

  // Plan-history primary id: same fallback as vm.planId. Used to key the
  // cumulative + supersedes + history hooks.
  const primaryPlanId = vm.planId;
  const cumulativePlan = useCumulativePlan(sessionId, primaryPlanId);
  const planHistoryRecords = usePlanHistory(sessionId, primaryPlanId);
  const supersedesMap = useSupersedesMap(sessionId, primaryPlanId);

  // "At this revision" filter on the cumulative plan: when the user pins
  // a revision with the scrubber, tasks introduced later are hidden. This
  // yields a new CumulativePlan with a pruned task+edge list so the DAG
  // layout engine runs unchanged.
  const filteredCumulative = useMemo(() => {
    if (!cumulativePlan) return null;
    if (pinnedRevision === null) return cumulativePlan;
    return filterCumulativeAtRevision(cumulativePlan, pinnedRevision);
  }, [cumulativePlan, pinnedRevision]);

  const latestIdx = Math.max(0, vm.revs.length - 1);
  const revIdx = pinnedRevIdx === null ? latestIdx : Math.min(pinnedRevIdx, latestIdx);
  const currentRev = vm.revs[revIdx] ?? null;
  const compareRev =
    compareRevIdx !== null && compareRevIdx !== revIdx
      ? vm.revs[Math.min(compareRevIdx, latestIdx)] ?? null
      : null;
  // Only produce diff marks when the user has explicitly pinned a compare
  // rev — otherwise every task would be flagged "added" against a null prev.
  const marks =
    currentRev && compareRev ? diffMarks(compareRev, currentRev) : null;

  // Rev-change handler: reset selection inline (not via effect) so stale task
  // detail doesn't linger from a rev that no longer contains that task.
  const onRevChange = (i: number, shiftKey: boolean) => {
    if (shiftKey) {
      setCompareRevIdx((c) => (c === i ? null : i));
      setSelection(null);
    } else {
      setPinnedRevIdx(i === latestIdx ? null : i);
      setCompareRevIdx(null);
      setSelection(null);
    }
  };

  return (
    <section className="hg-panel hg-traj" data-testid="trajectory-view">
      <header className="hg-panel__header hg-traj__header">
        <h2 className="hg-panel__title">Trajectory</h2>
        <span className="hg-panel__hint">
          {vm.revs.length === 0
            ? 'no plan yet'
            : `rev ${revIdx} of ${latestIdx}${compareRev ? ` · comparing to rev ${compareRevIdx}` : ''}`}
        </span>
        {vm.revs.length > 1 && (
          <div className="hg-traj__rev-chips" role="tablist" aria-label="Plan revisions">
            {vm.revs.map((p, i) => {
              const selected = i === revIdx;
              const compared = i === compareRevIdx && compareRevIdx !== revIdx;
              const sev = i > 0 ? (p.revisionSeverity || '') : '';
              return (
                <button
                  key={`${p.id}-${i}`}
                  role="tab"
                  className="hg-traj__chip"
                  aria-selected={selected}
                  data-compared={compared}
                  data-severity={sev || undefined}
                  data-testid={`rev-chip-${i}`}
                  onClick={(e) => onRevChange(i, e.shiftKey)}
                  title={
                    p.revisionReason
                      ? `rev ${i}: ${p.revisionReason}`
                      : `rev ${i}`
                  }
                >
                  <span className="hg-traj__chip-num">rev {i}</span>
                  {sev && <span className="hg-traj__chip-sev" />}
                </button>
              );
            })}
            {compareRev && (
              <button
                className="hg-traj__chip hg-traj__chip--clear"
                onClick={() => setCompareRevIdx(null)}
                aria-label="Clear comparison"
              >
                clear diff
              </button>
            )}
          </div>
        )}
        <span className="hg-traj__hint">
          {vm.revs.length > 1 ? 'shift-click a rev to diff against it' : ''}
        </span>
      </header>
      <div className="hg-panel__body hg-traj__body">
        {!sessionId && (
          <div className="hg-panel__empty">
            No session selected. Open the session picker (⌘K) to pick one.
          </div>
        )}
        {sessionId && vm.revs.length === 0 && (
          <div className="hg-panel__empty">
            No plan has been submitted for this session yet.
          </div>
        )}
        {sessionId && vm.revs.length > 0 && (
          <>
            <RevisionScrubber
              history={planHistoryRecords}
              pinnedRevision={pinnedRevision}
              onPinRevision={(rev) => {
                setPinnedRevision(rev);
                setSteeringSelection(null);
              }}
            />
            <Ribbon
              revs={vm.revs}
              driftsByRev={vm.driftsByRev}
              selectedRevIdx={revIdx}
              comparedRevIdx={compareRevIdx}
              onSelectRev={onRevChange}
              onSelectDrift={(seq) => setSelection({ kind: 'drift', seq })}
            />
            <InterventionEntries
              rows={interventions}
              onJumpToRevision={(revisionIndex) => {
                // Jump: find the rev chip with that revisionIndex and pin it.
                const target = vm.revs.findIndex(
                  (p) => (p.revisionIndex ?? 0) === revisionIndex,
                );
                if (target >= 0) onRevChange(target, false);
              }}
            />
            <div className="hg-traj__split">
              <DagPane
                plan={currentRev}
                cumulative={filteredCumulative}
                history={planHistoryRecords}
                supersedes={supersedesMap}
                pinnedRevision={pinnedRevision}
                marks={marks}
                compareRev={compareRev}
                driftsOnRev={vm.driftsByRev[revIdx] ?? []}
                delegations={vm.allDelegations}
                selection={selection}
                onSelectTask={(taskId) => {
                  if (!currentRev) return;
                  setSelection({ kind: 'task', taskId, planId: currentRev.id });
                  const task = currentRev.tasks.find((t) => t.id === taskId);
                  if (task?.boundSpanId) selectSpan(task.boundSpanId);
                }}
                onSelectSteering={setSteeringSelection}
                store={store}
              />
              {steeringSelection ? (
                <SteeringDetailPanel
                  selection={steeringSelection}
                  plan={filteredCumulative ?? currentRev}
                  history={planHistoryRecords}
                  supersedes={supersedesMap}
                  store={store}
                  onClose={() => setSteeringSelection(null)}
                  onJumpToGantt={() => {
                    // Hand off to the app-level Gantt jump handler. Today
                    // we simply close the panel so the user can eyeball
                    // the drift on the existing ribbon; the sibling
                    // `useUiStore.selectSpan` is the nav primitive most
                    // views use. If the refine span is the closest thing
                    // the view has to "this drift", select it.
                    setSteeringSelection(null);
                  }}
                />
              ) : (
                <DetailPane
                  selection={selection}
                  plan={currentRev}
                  drifts={vm.allDrifts}
                  store={store}
                />
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

// ── Ribbon ─────────────────────────────────────────────────────────────────

interface RibbonProps {
  revs: TaskPlan[];
  driftsByRev: DriftRecord[][];
  selectedRevIdx: number;
  comparedRevIdx: number | null;
  onSelectRev: (i: number, shiftKey: boolean) => void;
  onSelectDrift: (seq: number) => void;
}

function Ribbon(props: RibbonProps) {
  const { revs, driftsByRev, selectedRevIdx, comparedRevIdx } = props;
  return (
    <div className="hg-traj__ribbon" data-testid="trajectory-ribbon">
      {revs.map((rev, i) => {
        const drifts = driftsByRev[i] ?? [];
        const isSelected = i === selectedRevIdx;
        const isCompared = comparedRevIdx !== null && i === comparedRevIdx;
        const sev = i > 0 ? rev.revisionSeverity || '' : '';
        return (
          <div
            key={`seg-${rev.id}-${i}`}
            className="hg-traj__seg"
            data-selected={isSelected}
            data-compared={isCompared}
            data-testid={`rev-segment-${i}`}
          >
            {i > 0 && (
              <div
                className="hg-traj__pivot"
                data-severity={sev || undefined}
                title={`refine → rev ${i}${rev.revisionReason ? `: ${rev.revisionReason}` : ''}`}
              >
                <span className="hg-traj__pivot-glyph">↻</span>
              </div>
            )}
            <button
              className="hg-traj__seg-body"
              type="button"
              onClick={(e) => props.onSelectRev(i, e.shiftKey)}
              aria-label={`rev ${i}`}
            >
              <span className="hg-traj__seg-label">rev {i}</span>
              <span className="hg-traj__seg-summary">
                {rev.summary || `${rev.tasks.length} task${rev.tasks.length === 1 ? '' : 's'}`}
              </span>
            </button>
            <div className="hg-traj__markers">
              {(() => {
                // Cap markers per rev so a pathological drift storm (goldfive
                // can emit thousands in one session) can't overflow the ribbon
                // and push the DAG off-screen. UNSPECIFIED-kind drifts are
                // already dropped in buildViewModel — this cap only kicks in
                // when there are still more legitimate drifts than the ribbon
                // can readably display.
                const { shown, hidden } = selectTopDrifts(
                  drifts,
                  RIBBON_MAX_MARKERS_PER_REV,
                );
                return (
                  <>
                    {shown.map((d) => {
                      const isSteer = STEER_KINDS.has(d.kind);
                      return (
                        <button
                          key={`drift-${d.seq}`}
                          className="hg-traj__marker"
                          type="button"
                          data-steer={isSteer}
                          data-severity={d.severity || undefined}
                          title={`${d.kind}${d.severity ? ` (${d.severity})` : ''}${d.detail ? `: ${d.detail}` : ''}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            props.onSelectDrift(d.seq);
                          }}
                          data-testid={`drift-marker-${d.seq}`}
                        >
                          {isSteer ? '★' : '●'}
                        </button>
                      );
                    })}
                    {hidden > 0 && (
                      <span
                        className="hg-traj__marker--more"
                        data-testid={`drift-more-${i}`}
                        title={`${hidden} more drift${hidden === 1 ? '' : 's'} not shown (ranked by severity + recency)`}
                      >
                        +{hidden}
                      </span>
                    )}
                  </>
                );
              })()}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── DAG ────────────────────────────────────────────────────────────────────

interface DagProps {
  plan: TaskPlan | null;
  // harmonograf#197: cumulative-plan view from PlanHistoryRegistry. When
  // present, the DAG renders the union of tasks across every revision
  // (superseded tasks dimmed, REV-N generation badges). `plan` is kept
  // as a fallback for the one-revision case so the pane renders even
  // before the plan-history RPC wires up.
  cumulative: CumulativePlan | null;
  history: readonly PlanRevisionRecord[];
  supersedes: Map<string, SupersessionLink>;
  /** Null = "Latest" cumulative view; otherwise hide tasks introduced
   *  after this revision number. Controlled by the scrubber. */
  pinnedRevision: number | null;
  marks: DiffMarks | null;
  compareRev: TaskPlan | null;
  driftsOnRev: DriftRecord[];
  delegations: DelegationRecord[];
  selection: Selection;
  onSelectTask: (taskId: string) => void;
  // Click target for steering arrow / supersedes edge. Opens the
  // SteeringDetailPanel.
  onSelectSteering: (selection: SteeringSelection) => void;
  // Optional session handle used only to resolve the assignee agent's
  // bare display name. Passing null preserves the previous fallback
  // (strip the compound prefix; else render the raw id).
  store: SessionStore | null;
}

function buildDriftsByTaskId(
  driftsOnRev: DriftRecord[],
): Map<string, DriftRecord[]> {
  const m = new Map<string, DriftRecord[]>();
  for (const d of driftsOnRev) {
    if (!d.taskId) continue;
    const arr = m.get(d.taskId) ?? [];
    arr.push(d);
    m.set(d.taskId, arr);
  }
  return m;
}

// Bucket delegations by the taskId the coordinator was bound to at the
// moment of delegation. The DAG renders a faint "delegated to: X" line
// under each task card that had at least one delegation observed during
// its execution.
function buildDelegationsByTaskId(
  delegations: DelegationRecord[],
): Map<string, DelegationRecord[]> {
  const m = new Map<string, DelegationRecord[]>();
  for (const d of delegations) {
    if (!d.taskId) continue;
    const arr = m.get(d.taskId) ?? [];
    arr.push(d);
    m.set(d.taskId, arr);
  }
  return m;
}

// harmonograf#196: which task on the current rev should the steering
// arrow land on. Target priority:
//   1. Post-merge goldfive stamps PlanRevised.target_agent_id — pick the
//      first task on the rev whose assignee matches. When multiple
//      tasks share an assignee, prefer the task still RUNNING or
//      PENDING (the one the steering most likely applies to).
//   2. Pre-merge fallback: the drift carried on the same trigger as the
//      revision identifies a current_agent_id — goldfiveEvent.ts stamps
//      that onto the synthesized refine span. Here we accept the plan's
//      revisionKind as a hint when the explicit field is missing: no
//      target → no steering arrow (we degrade gracefully).
function findSteeringTargetTask(
  plan: TaskPlan,
  targetAgentId: string,
): Task | null {
  if (!targetAgentId) return null;
  const matches = plan.tasks.filter((t) => t.assigneeAgentId === targetAgentId);
  if (matches.length === 0) return null;
  // Prefer an active task; otherwise the first task — keeps the arrow stable
  // across status transitions.
  const active = matches.find(
    (t) => t.status === 'RUNNING' || t.status === 'PENDING',
  );
  return active ?? matches[0];
}

// Look up the synthesized "refine" span on the goldfive actor row for a
// given plan revision. The synthesizer (rpc/goldfiveEvent.ts) stamps
// `refine.index = String(revIdx)` so we can pick the exact rev even when
// multiple refines land inside the same ms.
function findRefineSpanForPlan(
  store: SessionStore,
  plan: TaskPlan,
): Span | null {
  const revIdx = plan.revisionIndex ?? 0;
  if (revIdx <= 0) return null;
  const scratch: Span[] = [];
  // Match by `refine.index` on the goldfive row — the synth span's
  // startMs is the event's emittedAt, not the plan's createdAt, so the
  // time window has to be wide enough to tolerate clock skew. Resolve
  // the canonical actor id at query time so post-goldfive-unify
  // sessions (compound `<client>:goldfive`) match the same way.
  store.spans.queryAgent(
    store.resolveGoldfiveActorId(),
    0,
    Number.POSITIVE_INFINITY,
    scratch,
  );
  for (const s of scratch) {
    if (!s.name.startsWith('refine:')) continue;
    const attr = s.attributes['refine.index'];
    if (attr && attr.kind === 'string' && attr.value === String(revIdx)) {
      return s;
    }
  }
  return null;
}

function readRefineAttr(span: Span | null, key: string): string {
  if (!span) return '';
  const attr = span.attributes[key];
  if (!attr || attr.kind !== 'string') return '';
  return attr.value;
}

// The user-goal span is the USER_MESSAGE span the ingest synthesizer
// stamps on the user actor row from RunStarted.goal_summary. There is at
// most one per run; pick the earliest (earliest RunStarted).
function findUserGoalSpan(store: SessionStore): Span | null {
  const scratch: Span[] = [];
  store.spans.queryAgent('__user__', 0, Number.POSITIVE_INFINITY, scratch);
  let chosen: Span | null = null;
  for (const s of scratch) {
    if (s.kind !== 'USER_MESSAGE') continue;
    const marker = s.attributes['user.goal_summary'];
    if (!marker) continue;
    if (!chosen || s.startMs < chosen.startMs) chosen = s;
  }
  return chosen;
}

// ── Cumulative filter + steering events ───────────────────────────────────

interface SteeringEvent {
  /** Revision number the steering produced. */
  revision: number;
  /** "user" for USER_STEER / USER_PAUSE / USER_CANCEL originators,
   *  "goldfive" otherwise. Drives arrow color + gutter glyph. */
  authoredBy: 'user' | 'goldfive';
  /** DriftKind name (e.g. "off_topic", "user_steer"). */
  kind: string;
  /** Short display label for the arrow annotation. */
  kindLabel: string;
  /** Reason text (tooltip body). */
  reason: string;
  /** Task the arrow lands on (the new / replacement task). */
  targetTaskId: string;
  /** Assignee of the target task — named in the arrow label. */
  targetAgentId: string;
  /** Gutter geometry for the revision's origin node. */
  gutterCx: number;
  gutterCy: number;
}

interface SupersedesEdge {
  oldId: string;
  link: SupersessionLink;
}

// Filter a cumulative plan to "at or before" a given revision — used by
// the scrubber. Tasks introduced after `pinnedRevision` are removed;
// their edges are dropped too.
function filterCumulativeAtRevision(
  cumulative: CumulativePlan,
  pinnedRevision: number,
): CumulativePlan {
  const visibleIds = new Set<string>();
  for (const [id, meta] of cumulative.taskRevisionMeta.entries()) {
    if (meta.introducedInRevision <= pinnedRevision) visibleIds.add(id);
  }
  const tasks = cumulative.tasks.filter((t) => visibleIds.has(t.id));
  const edges = cumulative.edges.filter(
    (e) => visibleIds.has(e.fromTaskId) && visibleIds.has(e.toTaskId),
  );
  const meta = new Map<string, CumulativeTaskMeta>();
  for (const [id, m] of cumulative.taskRevisionMeta.entries()) {
    if (visibleIds.has(id)) meta.set(id, m);
  }
  return { ...cumulative, tasks, edges, taskRevisionMeta: meta };
}

// Build per-revision steering events. One arrow per non-zero revision in
// `history` that points at a concrete task on `renderPlan`. Target
// priority (matches the goldfive PlanRevised contract):
//   1. supersedes link's newTaskId — the replacement task.
//   2. refine-span `refine.target_agent_id` → first task with matching
//      assignee on the render plan.
//   3. any task on the revision that wasn't present in the previous rev.
function buildSteeringEvents(
  history: readonly PlanRevisionRecord[],
  supersedes: Map<string, SupersessionLink>,
  renderPlan: TaskPlan,
  pinnedRevision: number | null,
  store: SessionStore | null,
): SteeringEvent[] {
  if (history.length === 0) return [];
  const tasksById = new Map<string, Task>();
  for (const t of renderPlan.tasks) tasksById.set(t.id, t);

  // Invert supersedes map so we can look up by revision → new task id.
  const newTaskByRevision = new Map<number, string>();
  for (const link of supersedes.values()) {
    if (link.newTaskId && !newTaskByRevision.has(link.revision)) {
      newTaskByRevision.set(link.revision, link.newTaskId);
    }
  }

  const events: SteeringEvent[] = [];
  let slot = 0;
  for (const rec of history) {
    if (rec.revision === 0) continue;
    if (pinnedRevision !== null && rec.revision > pinnedRevision) continue;
    let targetTaskId = newTaskByRevision.get(rec.revision) || '';
    let targetAgentId = '';
    if (targetTaskId) {
      targetAgentId = tasksById.get(targetTaskId)?.assigneeAgentId || '';
    }
    if (!targetTaskId && store) {
      const refineSpan = findRefineSpanForPlan(store, rec.plan);
      const agent = readRefineAttr(refineSpan, 'refine.target_agent_id');
      if (agent) {
        const match = findSteeringTargetTask(renderPlan, agent);
        if (match) {
          targetTaskId = match.id;
          targetAgentId = agent;
        }
      }
    }
    // Last-resort fallback: any task in this revision that wasn't in the
    // previous revision. Keeps the arrow drawable on history built from
    // nothing but plan snapshots.
    if (!targetTaskId) {
      const prev =
        history.find((r) => r.revision === rec.revision - 1)?.plan ?? null;
      const prevIds = new Set(prev?.tasks.map((t) => t.id) ?? []);
      for (const t of rec.plan.tasks) {
        if (!prevIds.has(t.id) && tasksById.has(t.id)) {
          targetTaskId = t.id;
          targetAgentId = t.assigneeAgentId;
          break;
        }
      }
    }
    if (!targetTaskId || !tasksById.has(targetTaskId)) continue;
    const authoredBy: 'user' | 'goldfive' =
      rec.kind.startsWith('user_') ? 'user' : 'goldfive';
    const kindLabel = rec.kind
      ? (authoredBy === 'user' ? 'user steer' : rec.kind.toUpperCase())
      : `rev ${rec.revision}`;
    events.push({
      revision: rec.revision,
      authoredBy,
      kind: rec.kind,
      kindLabel,
      reason: rec.reason,
      targetTaskId,
      targetAgentId,
      gutterCx: 12,
      // Stack gutters vertically: skip the user-goal slot (y=16). First
      // steering gutter at y=40, then every 24px. Caps visually at a
      // sensible number but arrows still all draw.
      gutterCy: 40 + slot * 24,
    });
    slot++;
  }
  return events;
}

// Rev-generation palette. Rev 0 is muted, later revs step up the warm
// channel so the operator can tell at a glance how many refines a given
// task has survived. Caps at REV 3+ to keep the palette finite.
function genBadgeFill(rev: number): string {
  if (rev <= 0) return '#5a5e68';
  if (rev === 1) return '#b59340';
  if (rev === 2) return '#d09532';
  return '#e08a24';
}

function DagPane(props: DagProps) {
  const {
    plan,
    cumulative,
    history,
    supersedes,
    pinnedRevision,
    marks,
    compareRev,
    driftsOnRev,
    delegations,
    selection,
    onSelectTask,
    onSelectSteering,
    store,
  } = props;
  // Prefer the cumulative plan — it retains superseded tasks as historical
  // nodes. Falls back to `plan` for pre-history-bootstrap renders.
  const renderPlan: TaskPlan | null = cumulative ?? plan;
  const layout = renderPlan ? layoutDag(renderPlan) : null;
  const driftsByTaskId = buildDriftsByTaskId(driftsOnRev);
  const delegationsByTaskId = buildDelegationsByTaskId(delegations);
  const taskMeta: Map<string, CumulativeTaskMeta> =
    cumulative?.taskRevisionMeta ?? new Map();

  if (!renderPlan || !layout) {
    return <div className="hg-traj__dag hg-traj__dag--empty">No plan to render.</div>;
  }

  // Current-rev steering event (legacy #196 single-arrow path). Retained
  // so the pre-history tests that only seed a single refine synth span
  // still see `trajectory-steer-edges` + `steer-edge-<id>` at the same
  // testids. Under the new history-driven path, the steering-events list
  // below additively renders one arrow per rev.
  const revIdx = renderPlan.revisionIndex ?? 0;
  const refineSpan = store && revIdx > 0 ? findRefineSpanForPlan(store, renderPlan) : null;
  const steerTargetAgent = refineSpan
    ? readRefineAttr(refineSpan, 'refine.target_agent_id')
    : '';
  const steerTargetTask = steerTargetAgent
    ? findSteeringTargetTask(renderPlan, steerTargetAgent)
    : null;

  // Gutter geometry — the left margin of the DAG is a small actor column.
  // User node at the top (seeded the plan), then one goldfive / user-steer
  // node per revision > 0, stacked top-to-bottom. Heights chosen so they
  // never overlap the first-layer task column (which starts at DAG_PAD).
  const GUTTER_X = 12;
  const USER_NODE_CY = 16;
  const USER_NODE = { cx: GUTTER_X, cy: USER_NODE_CY, r: 7 };

  // User → first-task arrow: drawn only on rev 0 (or "Latest" with rev 0
  // present in history).
  const userGoalSpan = store ? findUserGoalSpan(store) : null;
  const userGoalSummary = userGoalSpan?.name || '';
  // The first layer-0 task in the cumulative plan is the user's seed
  // target. We always render the user → first-task arrow when a goal
  // span is present (cumulative view accumulates the first layer too).
  const firstLayerTask =
    plan && (plan.revisionIndex ?? 0) === 0
      ? plan.tasks.find((t) => layout.nodeById.get(t.id)?.layer === 0) ?? null
      : renderPlan.tasks.find((t) => layout.nodeById.get(t.id)?.layer === 0) ?? null;

  // Compute the per-revision steering events that will arrow to a target
  // task node. One entry per non-zero revision in the history the user
  // hasn't scrubbed past.
  const steeringEvents = buildSteeringEvents(
    history,
    supersedes,
    renderPlan,
    pinnedRevision,
    store,
  );

  // Supersedes edges: old task → new task, dashed + muted. The
  // supersedes map keys on the old (superseded) task id and points at
  // the new task id (or "" when the old task was dropped outright).
  const supersedesEdges: SupersedesEdge[] = [];
  for (const [oldId, link] of supersedes.entries()) {
    if (pinnedRevision !== null && link.revision > pinnedRevision) continue;
    supersedesEdges.push({ oldId, link });
  }

  // Removed-task cards (only when in diff/compare mode). These sit to the right
  // of the DAG so the user can see what the refine dropped without corrupting
  // the current-rev layout.
  const removed =
    marks && compareRev
      ? compareRev.tasks.filter((t) => marks.removed.has(t.id))
      : [];

  return (
    <div className="hg-traj__dag-wrap" data-testid="trajectory-dag">
      <svg
        className="hg-traj__dag"
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
      >
        <defs>
          <marker
            id="traj-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="8"
            markerHeight="8"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="#8d9199" />
          </marker>
          {/* Distinct marker for steering edges so it reads different
              from delegation at a glance — same shape, goldfive color. */}
          <marker
            id="traj-steer-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="9"
            markerHeight="9"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="#80deea" />
          </marker>
          {/* User-origin arrow: warm neutral so it reads distinctly from
              both delegation (grey) and steering (goldfive-teal). */}
          <marker
            id="traj-user-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="9"
            markerHeight="9"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="#d0bcff" />
          </marker>
        </defs>

        {/* user → first-layer task arrow: drawn only on rev 0. Represents
            "this plan was seeded by the user's goal". */}
        {firstLayerTask && userGoalSpan && (() => {
          const targetNode = layout.nodeById.get(firstLayerTask.id);
          if (!targetNode) return null;
          const x1 = USER_NODE.cx + USER_NODE.r;
          const y1 = USER_NODE.cy;
          const x2 = targetNode.x;
          const y2 = targetNode.y + targetNode.h / 2;
          const mx = (x1 + x2) / 2;
          const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
          return (
            <g data-testid="trajectory-user-edge">
              <path
                d={d}
                fill="none"
                stroke="#d0bcff"
                strokeWidth={1.5}
                strokeDasharray="5,3"
                opacity={0.7}
                markerEnd="url(#traj-user-arrow)"
              />
              <title>{userGoalSummary ? `user goal: ${userGoalSummary}` : 'user goal'}</title>
            </g>
          );
        })()}
        {userGoalSpan && (
          <g
            className="hg-traj__user-gutter"
            data-testid="trajectory-user-gutter"
          >
            <circle
              cx={USER_NODE.cx}
              cy={USER_NODE.cy}
              r={USER_NODE.r}
              fill="#d0bcff"
              opacity={0.85}
            />
            <text
              x={USER_NODE.cx}
              y={USER_NODE.cy + 3}
              textAnchor="middle"
              fontSize={9}
              fill="#0b0d12"
              fontWeight={700}
            >
              u
            </text>
            <title>{userGoalSummary ? `user: ${userGoalSummary}` : 'user'}</title>
          </g>
        )}

        {/* supersedes edges: dashed muted edge from the superseded (old)
            task node to its replacement. Labeled with the drift kind so
            the DAG itself communicates "why the plan changed here". */}
        {supersedesEdges.length > 0 && (
          <g
            className="hg-traj__supersedes-edges"
            data-testid="trajectory-supersedes-edges"
          >
            {supersedesEdges.map(({ oldId, link }) => {
              const from = layout.nodeById.get(oldId);
              const to = link.newTaskId
                ? layout.nodeById.get(link.newTaskId)
                : null;
              if (!from || !to) return null;
              const x1 = from.x + from.w;
              const y1 = from.y + from.h / 2;
              const x2 = to.x;
              const y2 = to.y + to.h / 2;
              const mx = (x1 + x2) / 2;
              const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
              const label = link.kind
                ? (link.kind.startsWith('user_')
                    ? `user steer`
                    : `goldfive: ${link.kind}`)
                : 'supersedes';
              return (
                <g
                  key={`sup-${oldId}`}
                  data-testid={`supersedes-edge-${oldId}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectSteering({
                      kind: 'supersedes',
                      revision: link.revision,
                      oldTaskId: oldId,
                      targetTaskId: link.newTaskId || undefined,
                    });
                  }}
                  style={{ cursor: 'pointer' }}
                >
                  <path
                    className="hg-traj__supersedes-edge"
                    d={d}
                    fill="none"
                    stroke="#8d9199"
                    strokeWidth={1.25}
                    strokeDasharray="3,3"
                    opacity={0.6}
                    markerEnd="url(#traj-arrow)"
                  />
                  <text
                    className="hg-traj__supersedes-label"
                    x={mx}
                    y={(y1 + y2) / 2 - 4}
                    textAnchor="middle"
                    fontSize={9}
                    fill="#8d9199"
                    opacity={0.75}
                  >
                    <title>
                      {`${label}${link.reason ? `\nreason: ${link.reason}` : ''}`}
                    </title>
                    {truncate(label, 24)}
                  </text>
                </g>
              );
            })}
          </g>
        )}

        {/* Per-revision steering arrows: one entry in steeringEvents per
            PlanRevised in the history. Each arrow originates at a per-rev
            gutter node on the DAG's left edge and lands on the new /
            replacement task node with a short tail label naming the target
            agent. Authored-by-user events use a dashed-purple style; goldfive
            events use the teal steering style. */}
        {steeringEvents.length > 0 && (
          <g
            className="hg-traj__steer-edges"
            data-testid="trajectory-steer-edges"
          >
            {steeringEvents.map((ev) => {
              const targetNode = layout.nodeById.get(ev.targetTaskId);
              if (!targetNode) return null;
              const x1 = ev.gutterCx + 7;
              const y1 = ev.gutterCy;
              const x2 = targetNode.x;
              const y2 = targetNode.y + targetNode.h / 2;
              const mx = (x1 + x2) / 2;
              const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
              const stroke = ev.authoredBy === 'user' ? '#d0bcff' : '#80deea';
              const markerRef = ev.authoredBy === 'user' ? 'traj-user-arrow' : 'traj-steer-arrow';
              // Label: "<kind> → <agent>" — names the target agent in the
              // visual itself, as the user explicitly requested.
              const agentName =
                store?.agents.get(ev.targetAgentId)?.name ||
                bareAgentName(ev.targetAgentId) ||
                ev.targetAgentId ||
                '(agent)';
              const label = `${ev.kindLabel} → ${truncate(agentName, 14)}`;
              const tooltip =
                (ev.authoredBy === 'user'
                  ? `user steered ${agentName}`
                  : `goldfive steered ${agentName}`) +
                (ev.reason ? `\nreason: ${ev.reason}` : '') +
                (ev.kind ? `\nkind: ${ev.kind}` : '') +
                `\nrev: ${ev.revision}`;
              return (
                <g
                  key={`se-${ev.revision}-${ev.targetTaskId}`}
                  data-testid={`steer-edge-${ev.targetTaskId}`}
                  data-authored-by={ev.authoredBy}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectSteering({
                      kind: 'revision',
                      revision: ev.revision,
                      targetTaskId: ev.targetTaskId,
                    });
                  }}
                  style={{ cursor: 'pointer' }}
                >
                  <path
                    className="hg-traj__steer-edge"
                    d={d}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={1.75}
                    strokeDasharray={ev.authoredBy === 'user' ? '6,3' : '5,3'}
                    opacity={0.85}
                    markerEnd={`url(#${markerRef})`}
                  />
                  <text
                    className="hg-traj__steer-label"
                    x={(x1 + x2) / 2}
                    y={(y1 + y2) / 2 - 4}
                    textAnchor="middle"
                    fontSize={10}
                    fill={stroke}
                    opacity={0.95}
                  >
                    <title>{tooltip}</title>
                    {truncate(label, 32)}
                  </text>
                </g>
              );
            })}
          </g>
        )}

        {/* Per-revision gutter nodes — one teal "g5" circle (or purple "u")
            per steering event, stacked along the left edge. Also render
            a single legacy goldfive gutter node for the pre-history path
            so the existing trajectory-goldfive-gutter testid keeps working. */}
        {steeringEvents.length > 0 && (
          <g
            className="hg-traj__gutter-stack"
            data-testid="trajectory-gutter-stack"
          >
            {steeringEvents.map((ev) => (
              <g
                key={`gutter-${ev.revision}`}
                className="hg-traj__gutter-node"
                data-testid={`gutter-node-${ev.revision}`}
                data-authored-by={ev.authoredBy}
              >
                <circle
                  cx={ev.gutterCx}
                  cy={ev.gutterCy}
                  r={7}
                  fill={ev.authoredBy === 'user' ? '#d0bcff' : '#80deea'}
                  opacity={0.9}
                />
                <text
                  x={ev.gutterCx}
                  y={ev.gutterCy + 3}
                  textAnchor="middle"
                  fontSize={9}
                  fill="#0b0d12"
                  fontWeight={700}
                >
                  {ev.authoredBy === 'user' ? 'u' : 'g5'}
                </text>
                <title>
                  {`rev ${ev.revision}: ${ev.authoredBy === 'user' ? 'user steer' : 'goldfive steer'}`}
                </title>
              </g>
            ))}
          </g>
        )}

        {/* Legacy single-gutter compatibility (preserves #196 testid). */}
        {steerTargetTask && (
          <g
            className="hg-traj__goldfive-gutter"
            data-testid="trajectory-goldfive-gutter"
          >
            <circle
              cx={GUTTER_X}
              cy={layout.height / 2}
              r={7}
              fill="#80deea"
              opacity={0.85}
            />
            <text
              x={GUTTER_X}
              y={layout.height / 2 + 3}
              textAnchor="middle"
              fontSize={9}
              fill="#0b0d12"
              fontWeight={700}
            >
              g5
            </text>
          </g>
        )}

        {/* edges behind nodes */}
        <g className="hg-traj__edges">
          {layout.edges.map((e, i) => {
            const from = layout.nodeById.get(e.fromTaskId);
            const to = layout.nodeById.get(e.toTaskId);
            if (!from || !to) return null;
            const x1 = from.x + from.w;
            const y1 = from.y + from.h / 2;
            const x2 = to.x;
            const y2 = to.y + to.h / 2;
            const mx = (x1 + x2) / 2;
            const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
            return (
              <path
                key={`edge-${i}`}
                className="hg-traj__edge"
                d={d}
                markerEnd="url(#traj-arrow)"
                fill="none"
              />
            );
          })}
        </g>

        {/* nodes */}
        <g className="hg-traj__nodes">
          {layout.nodes.map((n) => {
            const t = n.task;
            const fill = STATUS_COLOR[t.status] ?? STATUS_COLOR.UNSPECIFIED;
            const isSelected =
              selection?.kind === 'task' && selection.taskId === t.id;
            const isAdded = marks?.added.has(t.id) ?? false;
            const isMod = marks?.modified.has(t.id) ?? false;
            const drifts = driftsByTaskId.get(t.id) ?? [];
            const taskDelegations = delegationsByTaskId.get(t.id) ?? [];
            // Highest-severity drift drives the pill color (critical > warn > info).
            let worst = '';
            for (const d of drifts) {
              if (d.severity === 'critical') {
                worst = 'critical';
                break;
              }
              if (d.severity === 'warning' && worst !== 'critical') worst = 'warning';
              else if (!worst) worst = d.severity;
            }
            // harmonograf#197: generation metadata from the cumulative plan.
            // Tasks the latest rev dropped render at reduced opacity with a
            // muted border; every node carries a REV-N corner badge.
            const meta = taskMeta.get(t.id);
            const isSuperseded = meta?.isSuperseded ?? false;
            const introducedIn = meta?.introducedInRevision ?? 0;
            return (
              <g
                key={t.id}
                className="hg-traj__node"
                data-selected={isSelected}
                data-added={isAdded}
                data-modified={isMod}
                data-superseded={isSuperseded || undefined}
                data-generation={introducedIn}
                opacity={isSuperseded ? 0.4 : 1}
                transform={`translate(${n.x}, ${n.y})`}
                onClick={() => onSelectTask(t.id)}
                data-testid={`task-node-${t.id}`}
              >
                <rect
                  className="hg-traj__node-bg"
                  x={0}
                  y={0}
                  width={n.w}
                  height={n.h}
                  rx={8}
                  ry={8}
                  stroke={isSuperseded ? '#6b6e76' : undefined}
                  strokeDasharray={isSuperseded ? '4,2' : undefined}
                />
                <rect
                  className="hg-traj__node-bar"
                  x={0}
                  y={0}
                  width={6}
                  height={n.h}
                  rx={3}
                  ry={3}
                  fill={fill}
                />
                <text className="hg-traj__node-title" x={16} y={22}>
                  {truncate(t.title || t.id, 22)}
                </text>
                {/* Generation badge: small text in top-right corner. Muted
                    tint for rev 0, stronger for rev 1, 2, 3+. Shifted
                    left when a drift pill already occupies the corner. */}
                <g
                  className="hg-traj__node-gen"
                  transform={`translate(${drifts.length > 0 ? n.w - 44 : n.w - 24}, 10)`}
                  data-testid={`task-node-${t.id}-rev-badge`}
                  data-rev={introducedIn}
                >
                  <rect
                    x={-2}
                    y={-9}
                    width={24}
                    height={14}
                    rx={3}
                    ry={3}
                    fill={genBadgeFill(introducedIn)}
                    opacity={0.85}
                  />
                  <text
                    x={10}
                    y={1}
                    textAnchor="middle"
                    fontSize={9}
                    fontWeight={600}
                    fill="#0b0d12"
                  >
                    {`R${introducedIn}`}
                  </text>
                </g>
                <text className="hg-traj__node-sub" x={16} y={42}>
                  {t.status.toLowerCase()}
                  {t.assigneeAgentId
                    ? ` · ${truncate(
                        store?.agents.get(t.assigneeAgentId)?.name ||
                          bareAgentName(t.assigneeAgentId) ||
                          t.assigneeAgentId,
                        14,
                      )}`
                    : ''}
                </text>
                {drifts.length > 0 && (
                  <g transform={`translate(${n.w - 14}, 14)`}>
                    <circle
                      r={9}
                      fill={SEVERITY_COLOR[worst] ?? SEVERITY_COLOR['']}
                      className="hg-traj__node-drift"
                    />
                    <text
                      className="hg-traj__node-drift-count"
                      textAnchor="middle"
                      dy={3}
                      fontSize={10}
                    >
                      {drifts.length}
                    </text>
                  </g>
                )}
                {taskDelegations.length > 0 && (
                  <text
                    className="hg-traj__node-delegation"
                    x={16}
                    y={n.h - 6}
                    fontSize={10}
                    data-testid={`task-delegation-${t.id}`}
                  >
                    {/* Native SVG tooltip — mirrors the Gantt delegation
                     * tooltip's fields. Trajectory has no existing nav
                     * pattern into the Gantt so we deliberately don't
                     * synthesize one here; hover + readable summary is
                     * the lightweight equivalent. */}
                    <title>
                      {`Delegation observed\nFrom: ${
                        taskDelegations[0].fromAgentId
                      } → ${taskDelegations[0].toAgentId}\nTask: ${
                        taskDelegations[0].taskId || '(none)'
                      }\nInvocation: ${
                        taskDelegations[0].invocationId || '(none)'
                      }${
                        taskDelegations.length > 1
                          ? `\n+${taskDelegations.length - 1} more`
                          : ''
                      }`}
                    </title>
                    {`↪↪ delegated to: ${truncate(
                      taskDelegations[0].toAgentId,
                      14,
                    )}${taskDelegations.length > 1 ? ` +${taskDelegations.length - 1}` : ''}`}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>
      {removed.length > 0 && (
        <aside className="hg-traj__removed" data-testid="removed-panel">
          <h4>Removed in rev</h4>
          <ul>
            {removed.map((t) => (
              <li key={t.id}>{t.title || t.id}</li>
            ))}
          </ul>
        </aside>
      )}
      <TaskDeltaList plan={renderPlan} />
    </div>
  );
}

// ── TaskDeltaList ──────────────────────────────────────────────────────────
//
// harmonograf#110 / goldfive#205: lists every CANCELLED / FAILED task in
// the currently selected rev with its structured cancel reason. Answers
// "why was this task cancelled?" at a glance — the primary question the
// Trajectory view is expected to answer when a run ends with a cascade
// of cancelled tasks. Sibling to the Intervention list: where the
// intervention list narrates WHY the plan changed, this list narrates
// WHY individual tasks terminated the way they did.

interface TaskDeltaListProps {
  plan: TaskPlan;
}

function TaskDeltaList({ plan }: TaskDeltaListProps) {
  const terminal = plan.tasks.filter(
    (t) => t.status === 'CANCELLED' || t.status === 'FAILED',
  );
  if (terminal.length === 0) return null;
  return (
    <section
      className="hg-traj__task-delta"
      data-testid="trajectory-task-delta"
      aria-label="Task terminal-status delta"
      style={{
        marginTop: 12,
        padding: '8px 12px',
        background: 'rgba(255,255,255,0.03)',
        borderRadius: 6,
        fontSize: 12,
      }}
    >
      <header
        style={{
          fontSize: 10,
          opacity: 0.65,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          marginBottom: 6,
        }}
      >
        Task delta · {terminal.length} task{terminal.length === 1 ? '' : 's'} terminal
      </header>
      <ul
        style={{
          listStyle: 'none',
          margin: 0,
          padding: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        }}
      >
        {terminal.map((t) => (
          <li
            key={t.id}
            data-testid={`task-delta-row-${t.id}`}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(120px, 1fr) 80px 2fr',
              gap: 8,
              alignItems: 'center',
              padding: '2px 0',
            }}
          >
            <span
              title={t.title || t.id}
              style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontWeight: 500,
              }}
            >
              {t.title || t.id}
            </span>
            <span
              data-status={t.status}
              style={{
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: '0.05em',
                color:
                  t.status === 'FAILED'
                    ? 'rgba(255,130,110,0.9)'
                    : 'rgba(255,200,80,0.9)',
              }}
            >
              {t.status}
            </span>
            <code
              style={{
                fontSize: 11,
                opacity: 0.85,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontFamily:
                  'var(--hg-mono, ui-monospace, Menlo, monospace)',
              }}
              title={t.cancelReason || ''}
            >
              {t.cancelReason || '—'}
            </code>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── Detail pane ────────────────────────────────────────────────────────────

interface DetailPaneProps {
  selection: Selection;
  plan: TaskPlan | null;
  drifts: DriftRecord[];
  store: SessionStore | null;
}

function DetailPane(props: DetailPaneProps) {
  const { selection, plan, drifts, store } = props;
  if (!selection) {
    return (
      <aside className="hg-traj__detail hg-traj__detail--empty">
        <span>Select a task or a drift marker for detail.</span>
      </aside>
    );
  }
  if (selection.kind === 'task' && plan) {
    const t = plan.tasks.find((x) => x.id === selection.taskId) ?? null;
    if (!t) {
      return (
        <aside className="hg-traj__detail">
          <span>Task not present in this rev.</span>
        </aside>
      );
    }
    const taskDrifts = drifts.filter((d) => d.taskId === t.id);
    const span =
      t.boundSpanId && store ? store.spans.get(t.boundSpanId) : null;
    return (
      <aside className="hg-traj__detail" data-testid="detail-task">
        <header>
          <span className="hg-traj__detail-kind">task</span>
          <h3>{t.title || t.id}</h3>
          <span className="hg-traj__detail-status" data-status={t.status}>
            {t.status.toLowerCase()}
          </span>
        </header>
        {t.description && <p className="hg-traj__detail-desc">{t.description}</p>}
        <dl className="hg-traj__detail-meta">
          <dt>assignee</dt>
          <dd title={t.assigneeAgentId || undefined}>
            {t.assigneeAgentId
              ? store?.agents.get(t.assigneeAgentId)?.name ||
                bareAgentName(t.assigneeAgentId) ||
                t.assigneeAgentId
              : '—'}
          </dd>
          <dt>bound span</dt>
          <dd>{t.boundSpanId ? t.boundSpanId.slice(0, 8) : '—'}</dd>
          {span && (
            <>
              <dt>started</dt>
              <dd>{fmtTime(span.startMs)}</dd>
            </>
          )}
        </dl>
        {/* harmonograf#110 / goldfive#205: cancel / failure reason on
            terminal tasks. Answers "why was this task cancelled?" —
            the primary question the Trajectory view is expected to
            answer when a run ends with CANCELLED tasks. */}
        {(t.status === 'CANCELLED' || t.status === 'FAILED') && t.cancelReason && (
          <section
            className="hg-traj__detail-cancel-reason"
            data-testid="detail-task-cancel-reason"
            style={{
              marginTop: 8,
              fontSize: 12,
              background: 'rgba(255,200,80,0.08)',
              borderLeft: '3px solid rgba(255,200,80,0.6)',
              borderRadius: 4,
              padding: '6px 10px',
              fontFamily: 'var(--hg-mono, ui-monospace, Menlo, monospace)',
            }}
          >
            <strong style={{ opacity: 0.7, marginRight: 6 }}>
              {t.status === 'FAILED' ? 'failed:' : 'cancelled:'}
            </strong>
            <span>{t.cancelReason}</span>
          </section>
        )}
        {taskDrifts.length > 0 && (
          <section className="hg-traj__detail-drifts">
            <h4>Drifts on this task</h4>
            <ul>
              {taskDrifts.map((d) => (
                <li key={d.seq} data-severity={d.severity || undefined}>
                  <strong>{d.kind}</strong>
                  {d.severity ? ` · ${d.severity}` : ''}
                  {d.detail ? ` — ${d.detail}` : ''}
                </li>
              ))}
            </ul>
          </section>
        )}
      </aside>
    );
  }
  if (selection.kind === 'drift') {
    const d = drifts.find((x) => x.seq === selection.seq) ?? null;
    if (!d) {
      return (
        <aside className="hg-traj__detail">
          <span>Drift not found.</span>
        </aside>
      );
    }
    // Resolve the richer intervention detail so the pane can surface
    // Trigger / Steering / Target sections — the three questions a
    // human operator asks at each drift marker: what did goldfive see,
    // what did it do about it, and which agent got steered.
    const plans = plan ? [plan] : [];
    // Walk every rev visible through the selection context so cross-rev
    // triggerEventId matches work (the trigger-event is stamped on the
    // PlanRevised, not the drift row).
    const allPlans = store ? collectAllPlanRevs(store) : plans;
    const detail = resolveDriftDetail(d, allPlans, store);
    return (
      <aside className="hg-traj__detail" data-testid="detail-drift">
        <header>
          <span className="hg-traj__detail-kind">drift</span>
          <h3>{d.kind}</h3>
          <span
            className="hg-traj__detail-status"
            data-severity={d.severity || undefined}
          >
            {d.severity || 'unspec'}
          </span>
        </header>
        <dl className="hg-traj__detail-meta">
          <dt>observed</dt>
          <dd>{fmtTime(d.recordedAtMs)}</dd>
          <dt>task</dt>
          <dd>{d.taskId || '—'}</dd>
          <dt>agent</dt>
          <dd>{d.agentId || '—'}</dd>
        </dl>
        <InterventionDetailSections detail={detail} store={store} />
      </aside>
    );
  }
  return null;
}

// Walk every plan in the store and gather every revision snapshot. The
// resolver needs the full rev history because a drift's triggering
// PlanRevised can live under any plan_id (goldfive often mints fresh
// plan ids on refine — same behaviour the main VM builder guards
// against).
function collectAllPlanRevs(store: SessionStore): TaskPlan[] {
  const out: TaskPlan[] = [];
  const seen = new Set<TaskPlan>();
  for (const live of store.tasks.listPlans()) {
    for (const snap of store.tasks.allRevsForPlan(live.id)) {
      if (seen.has(snap)) continue;
      seen.add(snap);
      out.push(snap);
    }
  }
  return out;
}

// Trigger / Steering / Target panel (harmonograf#196). Each section is
// hidden when its data is empty — so drift rows that have no matching
// PlanRevised don't render an empty "Steering" block.
function InterventionDetailSections({
  detail,
  store,
}: {
  detail: InterventionDetail;
  store: SessionStore | null;
}): React.ReactNode {
  const agentName = (id: string): string => {
    if (!id) return '';
    return store?.agents.get(id)?.name || bareAgentName(id) || id;
  };
  return (
    <section
      className="hg-traj__detail-intervention"
      data-testid="detail-drift-intervention"
      aria-label="Intervention context"
      style={{ marginTop: 10, fontSize: 12 }}
    >
      {detail.trigger && (
        <DetailSection
          label="Trigger"
          body={detail.trigger}
          testId="detail-drift-trigger"
        />
      )}
      {detail.steering && (
        <DetailSection
          label="Steering"
          body={detail.steering}
          testId="detail-drift-steering"
        />
      )}
      {detail.targetAgentId && (
        <div
          className="hg-traj__detail-target"
          data-testid="detail-drift-target"
          style={{ marginTop: 6, display: 'flex', gap: 6, alignItems: 'baseline' }}
        >
          <strong style={{ opacity: 0.65, textTransform: 'uppercase', fontSize: 10 }}>
            Target
          </strong>
          <span title={detail.targetAgentId}>{agentName(detail.targetAgentId)}</span>
          {detail.targetTaskId && (
            <code style={{ opacity: 0.7, fontSize: 10 }}>{detail.targetTaskId}</code>
          )}
          {detail.authoredBy && (
            <span
              data-testid="detail-drift-authored-by"
              data-authored-by={detail.authoredBy}
              title="Who initiated this intervention"
              style={{
                fontSize: 10,
                opacity: 0.65,
                marginLeft: 'auto',
                textTransform: 'none',
              }}
            >
              Authored by: {detail.authoredBy}
            </span>
          )}
        </div>
      )}
    </section>
  );
}

function DetailSection({
  label,
  body,
  testId,
}: {
  label: string;
  body: string;
  testId: string;
}): React.ReactNode {
  return (
    <div
      data-testid={testId}
      style={{
        marginTop: 6,
        padding: '6px 8px',
        background: 'rgba(255,255,255,0.03)',
        borderRadius: 4,
      }}
    >
      <strong
        style={{
          display: 'block',
          fontSize: 10,
          opacity: 0.65,
          textTransform: 'uppercase',
          marginBottom: 2,
        }}
      >
        {label}
      </strong>
      <div style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{body}</div>
    </div>
  );
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + '…';
}

// ── InterventionEntries ────────────────────────────────────────────────────
// Chronological list of intervention cards rendered below the Ribbon. Each
// card surfaces source, kind, author (if user), body preview, and outcome.
// Tree-agnostic — never inspects kind vocabularies, so new drift/revision
// kinds added on the server show up here automatically.

interface InterventionEntriesProps {
  rows: readonly InterventionRow[];
  onJumpToRevision: (revisionIndex: number) => void;
}

function InterventionEntries({ rows, onJumpToRevision }: InterventionEntriesProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (rows.length === 0) return null;
  const toggle = (key: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  return (
    <section
      className="hg-traj__interventions"
      data-testid="trajectory-interventions"
      aria-label="Intervention history"
    >
      <header className="hg-traj__interventions-head">
        <span className="hg-traj__interventions-title">
          Interventions · {rows.length}
        </span>
      </header>
      <ol className="hg-traj__interventions-list">
        {rows.map((row) => {
          const isOpen = expanded.has(row.key);
          const preview =
            row.bodyOrReason.length > 100 && !isOpen
              ? row.bodyOrReason.slice(0, 100) + '…'
              : row.bodyOrReason;
          const headline =
            row.source === 'user' && row.author
              ? `${row.kind} by ${row.author}`
              : row.source === 'drift'
                ? `${row.kind} drift`
                : row.kind;
          return (
            <li
              key={row.key}
              className="hg-traj__intervention"
              data-source={row.source}
              data-testid={`intervention-entry-${row.key}`}
            >
              <div className="hg-traj__intervention-head">
                <span
                  className="hg-traj__intervention-dot"
                  style={{
                    background: SOURCE_COLOR[row.source] ?? SOURCE_COLOR.goldfive,
                  }}
                />
                <span className="hg-traj__intervention-headline">{headline}</span>
                <span className="hg-traj__intervention-at">{fmtTime(row.atMs)}</span>
                {row.severity && (
                  <span
                    className="hg-traj__intervention-sev"
                    data-severity={row.severity}
                  >
                    {row.severity}
                  </span>
                )}
                <span className="hg-traj__intervention-outcome">
                  {row.outcome || 'pending'}
                </span>
                {row.planRevisionIndex > 0 && (
                  <button
                    type="button"
                    className="hg-traj__intervention-jump"
                    onClick={() => onJumpToRevision(row.planRevisionIndex)}
                    data-testid={`intervention-jump-${row.key}`}
                  >
                    rev {row.planRevisionIndex}
                  </button>
                )}
              </div>
              {row.bodyOrReason && (
                <div className="hg-traj__intervention-body">
                  {preview}
                  {row.bodyOrReason.length > 100 && (
                    <button
                      type="button"
                      className="hg-traj__intervention-show-full"
                      onClick={() => toggle(row.key)}
                    >
                      {isOpen ? 'show less' : 'show full'}
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
