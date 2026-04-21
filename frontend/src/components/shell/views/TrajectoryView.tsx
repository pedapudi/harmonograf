import './views.css';
import { useEffect, useState } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import type { Task, TaskEdge, TaskPlan, TaskStatus } from '../../../gantt/types';
import type {
  DelegationRecord,
  DriftRecord,
  SessionStore,
} from '../../../gantt/index';

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
  const drifts = [...store.drifts.list()];
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
    return () => {
      un1();
      un2();
      un3();
      un4();
    };
  }, [store]);

  // No memoization: buildViewModel is a small O(revs + drifts) walk and the
  // store is a stable object reference, so tick-driven re-renders need to
  // recompute to pick up the latest registry contents.
  const vm = buildViewModel(store);

  // Latest rev tracked live; user can pin a specific rev. `null` → live.
  const [pinnedRevIdx, setPinnedRevIdx] = useState<number | null>(null);
  const [compareRevIdx, setCompareRevIdx] = useState<number | null>(null);
  const [selection, setSelection] = useState<Selection>(null);

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
            <Ribbon
              revs={vm.revs}
              driftsByRev={vm.driftsByRev}
              selectedRevIdx={revIdx}
              comparedRevIdx={compareRevIdx}
              onSelectRev={onRevChange}
              onSelectDrift={(seq) => setSelection({ kind: 'drift', seq })}
            />
            <div className="hg-traj__split">
              <DagPane
                plan={currentRev}
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
              />
              <DetailPane
                selection={selection}
                plan={currentRev}
                drifts={vm.allDrifts}
                store={store}
              />
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
              {drifts.map((d) => {
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
  marks: DiffMarks | null;
  compareRev: TaskPlan | null;
  driftsOnRev: DriftRecord[];
  delegations: DelegationRecord[];
  selection: Selection;
  onSelectTask: (taskId: string) => void;
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

function DagPane(props: DagProps) {
  const {
    plan,
    marks,
    compareRev,
    driftsOnRev,
    delegations,
    selection,
    onSelectTask,
  } = props;
  const layout = plan ? layoutDag(plan) : null;
  const driftsByTaskId = buildDriftsByTaskId(driftsOnRev);
  const delegationsByTaskId = buildDelegationsByTaskId(delegations);

  if (!plan || !layout) {
    return <div className="hg-traj__dag hg-traj__dag--empty">No plan to render.</div>;
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
        </defs>

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
            return (
              <g
                key={t.id}
                className="hg-traj__node"
                data-selected={isSelected}
                data-added={isAdded}
                data-modified={isMod}
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
                <text className="hg-traj__node-sub" x={16} y={42}>
                  {t.status.toLowerCase()}
                  {t.assigneeAgentId ? ` · ${truncate(t.assigneeAgentId, 14)}` : ''}
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
    </div>
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
          <dd>{t.assigneeAgentId || '—'}</dd>
          <dt>bound span</dt>
          <dd>{t.boundSpanId ? t.boundSpanId.slice(0, 8) : '—'}</dd>
          {span && (
            <>
              <dt>started</dt>
              <dd>{fmtTime(span.startMs)}</dd>
            </>
          )}
        </dl>
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
        {d.detail && <p className="hg-traj__detail-desc">{d.detail}</p>}
        <dl className="hg-traj__detail-meta">
          <dt>observed</dt>
          <dd>{fmtTime(d.recordedAtMs)}</dd>
          <dt>task</dt>
          <dd>{d.taskId || '—'}</dd>
          <dt>agent</dt>
          <dd>{d.agentId || '—'}</dd>
        </dl>
      </aside>
    );
  }
  return null;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + '…';
}
