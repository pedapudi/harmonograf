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
// and sink-stamp worktrees stamp. Degrades to the bare PlanRevised
// reason + drift detail when those attrs are absent.

import type React from 'react';
import { useEffect } from 'react';
import type { SessionStore } from '../../../gantt/index';
import { bareAgentName } from '../../../gantt/index';
import type { Span, Task, TaskPlan } from '../../../gantt/types';
import type {
  PlanRevisionRecord,
  SupersessionLink,
} from '../../../state/planHistoryStore';

export interface SteeringSelection {
  kind: 'revision' | 'supersedes';
  revision: number;
  oldTaskId?: string;
  targetTaskId?: string;
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
  store.spans.queryAgent('__goldfive__', 0, Number.POSITIVE_INFINITY, spans);
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

export function SteeringDetailPanel(
  props: SteeringDetailPanelProps,
): React.ReactElement | null {
  const { selection, plan, history, supersedes, store, onClose, onJumpToGantt } = props;

  // Esc closes the panel.
  useEffect(() => {
    if (!selection) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selection, onClose]);

  if (!selection) return null;

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
    <aside
      className="hg-traj__steering-panel"
      data-testid="steering-detail-panel"
      role="dialog"
      aria-label="Steering decision detail"
    >
      <header className="hg-traj__steering-panel-head">
        <div>
          <span className="hg-traj__steering-panel-kicker" data-authored-by={authoredBy}>
            {authoredBy === 'user' ? 'user steer' : 'goldfive steer'} · rev {selection.revision}
          </span>
          <h3 className="hg-traj__steering-panel-title">{kind || 'plan revised'}</h3>
        </div>
        <button
          type="button"
          className="hg-traj__steering-panel-close"
          onClick={onClose}
          aria-label="Close steering detail"
          data-testid="steering-detail-close"
        >
          ×
        </button>
      </header>

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

      <footer className="hg-traj__steering-panel-foot">
        <button
          type="button"
          className="hg-traj__steering-panel-jump"
          data-testid="steering-detail-jump-gantt"
          disabled={!triggerEventId}
          onClick={() => onJumpToGantt(triggerDriftAtMs, triggerEventId)}
        >
          Jump to drift in Gantt
        </button>
      </footer>
    </aside>
  );
}
