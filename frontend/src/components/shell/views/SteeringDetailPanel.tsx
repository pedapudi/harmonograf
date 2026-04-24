// SteeringDetailPanel — side panel opened when the operator clicks a
// steering arrow or a supersedes edge on the Trajectory DAG. Renders the
// three questions the user explicitly asked for:
//
//   1. Trigger  — what goldfive observed (drift kind + reason; or for
//                 user steers, the annotation body).
//   2. Steering — what goldfive decided (refine reason; decision_summary
//                 from the refine span when the sibling goldfive PR has
//                 landed and stamped that attribute).
//   3. Target   — the agent + task that got steered. The target agent
//                 is named here AND on the arrow label — so it's
//                 visible without a click.
//
// Reads optional refine-span attributes (input_preview, output_preview,
// decision_summary, target_agent_id) that the sibling goldfive-render
// and sink-stamp worktreees stamp. Degrades to the bare PlanRevised
// reason + drift detail when those attrs are absent.
//
// Refactor (harmonograf — floating-drawer): the content is split into a
// pure `<SteeringDetailBody>` that renders the three sections + jump
// footer, and a thin `<SteeringDetailPanel>` adapter that wraps the body
// in a `<TrajectoryFloatingDrawer>` (backward-compat for existing call
// sites and tests that expect a panel with data-testid
// "steering-detail-panel"). New layouts should mount SteeringDetailBody
// inside their own TrajectoryFloatingDrawer.

import type React from 'react';
import type { SessionStore } from '../../../gantt/index';
import { bareAgentName } from '../../../gantt/index';
import type { Span, Task, TaskPlan } from '../../../gantt/types';
import type {
  PlanRevisionRecord,
  SupersessionLink,
} from '../../../state/planHistoryStore';
import { TrajectoryFloatingDrawer } from './TrajectoryFloatingDrawer';

export interface SteeringSelection {
  kind: 'revision' | 'supersedes';
  revision: number;
  oldTaskId?: string;
  targetTaskId?: string;
}

export interface SteeringDetailBodyProps {
  selection: SteeringSelection;
  plan: TaskPlan | null;
  history: readonly PlanRevisionRecord[];
  supersedes: Map<string, SupersessionLink>;
  store: SessionStore | null;
  onJumpToGantt: (atMs: number | null, driftId: string) => void;
}

export interface SteeringDetailPanelProps {
  selection: SteeringSelection | null;
  plan: TaskPlan | null;
  history: readonly PlanRevisionRecord[];
  supersedes: Map<string, SupersessionLink>;
  store: SessionStore | null;
  onClose: () => void;
  onJumpToGantt: (atMs: number | null, driftId: string) => void;
}

function readStringAttr(span: Span | null, key: string): string {
  if (!span) return '';
  const attr = span.attributes[key];
  if (!attr || attr.kind !== 'string') return '';
  return attr.value;
}

function findRefineSpan(store: SessionStore | null, revision: number): Span | null {
  if (!store || revision <= 0) return null;
  const spans: Span[] = [];
  // Resolve the canonical goldfive actor id at query time: after the
  // harmonograf#goldfive-unify merge, refine spans live on either the
  // legacy `__goldfive__` row or the compound `<client>:goldfive` one,
  // whichever survived the alias collapse.
  store.spans.queryAgent(
    store.resolveGoldfiveActorId(),
    0,
    Number.POSITIVE_INFINITY,
    spans,
  );
  for (const s of spans) {
    if (!s.name.startsWith('refine:')) continue;
    const attr = s.attributes['refine.index'];
    if (attr && attr.kind === 'string' && attr.value === String(revision)) return s;
  }
  return null;
}

function taskById(plan: TaskPlan | null, id: string | undefined): Task | null {
  if (!plan || !id) return null;
  return plan.tasks.find((t) => t.id === id) ?? null;
}

function agentDisplayName(store: SessionStore | null, id: string): string {
  if (!id) return '';
  return store?.agents.get(id)?.name || bareAgentName(id) || id;
}

export function SteeringDetailBody(
  props: SteeringDetailBodyProps,
): React.ReactElement {
  const { selection, plan, history, supersedes, store, onJumpToGantt } = props;

  const record: PlanRevisionRecord | undefined = history.find(
    (r) => r.revision === selection.revision,
  );
  const link: SupersessionLink | undefined =
    selection.kind === 'supersedes' && selection.oldTaskId
      ? supersedes.get(selection.oldTaskId)
      : undefined;

  const refineSpan = findRefineSpan(store, selection.revision);
  const decisionSummary = readStringAttr(refineSpan, 'goldfive.decision_summary');
  const inputPreview = readStringAttr(refineSpan, 'goldfive.input_preview');
  const outputPreview = readStringAttr(refineSpan, 'goldfive.output_preview');
  const targetAgentFromSpan = readStringAttr(refineSpan, 'refine.target_agent_id');

  const reason = record?.reason || link?.reason || '';
  const kind = record?.kind || link?.kind || '';
  const triggerEventId = record?.triggerEventId || link?.triggerEventId || '';
  const authoredBy = kind.startsWith('user_') ? 'user' : 'goldfive';

  const targetTask =
    taskById(plan, selection.targetTaskId) ??
    taskById(plan, link?.newTaskId) ??
    (targetAgentFromSpan
      ? plan?.tasks.find((t) => t.assigneeAgentId === targetAgentFromSpan) ?? null
      : null);
  const targetAgentId = targetTask?.assigneeAgentId || targetAgentFromSpan || '';
  const targetAgentName = agentDisplayName(store, targetAgentId);

  let triggerDriftAtMs: number | null = null;
  if (store && triggerEventId) {
    for (const d of store.drifts.list()) {
      if (d.driftId === triggerEventId || d.annotationId === triggerEventId) {
        triggerDriftAtMs = d.recordedAtMs;
        break;
      }
    }
  }

  return (
    <div
      className="hg-traj__steering-panel-inner"
      data-testid="steering-detail-body"
    >
      <div className="hg-traj__steering-panel-kicker-row">
        <span className="hg-traj__steering-panel-kicker" data-authored-by={authoredBy}>
          {authoredBy === 'user' ? 'user steer' : 'goldfive steer'} · rev {selection.revision}
        </span>
        <span className="hg-traj__steering-panel-kind">{kind || 'plan revised'}</span>
      </div>

      <section
        className="hg-traj__steering-panel-section"
        data-testid="steering-detail-trigger"
      >
        <h4>Trigger</h4>
        {kind && <div><strong>kind:</strong> {kind}</div>}
        {reason && <div className="hg-traj__steering-panel-body">{reason}</div>}
        {inputPreview && (
          <div className="hg-traj__steering-panel-preview">{inputPreview}</div>
        )}
      </section>

      <section
        className="hg-traj__steering-panel-section"
        data-testid="steering-detail-steering"
      >
        <h4>Steering</h4>
        <div className="hg-traj__steering-panel-body">
          {decisionSummary || reason || '(no summary recorded)'}
        </div>
        {outputPreview && (
          <div className="hg-traj__steering-panel-preview">{outputPreview}</div>
        )}
      </section>

      <section
        className="hg-traj__steering-panel-section"
        data-testid="steering-detail-target"
      >
        <h4>Target</h4>
        <div data-testid="steering-detail-target-agent" title={targetAgentId}>
          <strong>agent:</strong> {targetAgentName || '(unknown)'}
        </div>
        {targetTask && (
          <div data-testid="steering-detail-target-task">
            <strong>task:</strong> {targetTask.title || targetTask.id}
          </div>
        )}
        {link?.oldTaskId && (
          <div>
            <strong>supersedes:</strong> <code>{link.oldTaskId}</code>
            {link.newTaskId && (
              <>
                {' → '}
                <code>{link.newTaskId}</code>
              </>
            )}
          </div>
        )}
      </section>

      <div className="hg-traj__steering-panel-foot">
        <button
          type="button"
          className="hg-traj__steering-panel-jump"
          data-testid="steering-detail-jump-gantt"
          disabled={!triggerEventId}
          onClick={() => onJumpToGantt(triggerDriftAtMs, triggerEventId)}
        >
          Jump to drift in Gantt
        </button>
      </div>
    </div>
  );
}

/**
 * Backward-compat adapter: mounts SteeringDetailBody inside a
 * TrajectoryFloatingDrawer and forwards close/jump. Existing call sites
 * (TrajectoryView today, plus the evolution tests) keep working
 * unchanged — testids like `steering-detail-panel` and
 * `steering-detail-close` are re-stamped on the drawer wrapper.
 */
export function SteeringDetailPanel(
  props: SteeringDetailPanelProps,
): React.ReactElement | null {
  const { selection, onClose, ...bodyRest } = props;
  const title = selection
    ? `${selection.kind === 'supersedes' ? 'supersedes' : 'revision'} · rev ${selection.revision}`
    : undefined;
  return (
    <TrajectoryFloatingDrawer
      open={selection !== null}
      onClose={onClose}
      title={title}
      testId="steering-detail-panel"
      closeTestId="steering-detail-close"
    >
      {selection && (
        <SteeringDetailBody
          selection={selection}
          plan={bodyRest.plan}
          history={bodyRest.history}
          supersedes={bodyRest.supersedes}
          store={bodyRest.store}
          onJumpToGantt={bodyRest.onJumpToGantt}
        />
      )}
    </TrajectoryFloatingDrawer>
  );
}
