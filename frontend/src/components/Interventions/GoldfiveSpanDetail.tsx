// Drawer detail panel for goldfive-translated spans (harmonograf#157).
//
// Complements JudgeInvocationDetail: judge spans keep their richer
// verdict/steering panel (on_task / severity / raw response / steered
// plan). Everything else — refine_steer, goal_derive, plan_generate,
// reflective_check, future call names — lands here with a generic but
// high-signal layout:
//
//   Header   — decision summary + call-name / target-agent / target-task
//              badges so the reader sees "what goldfive did" at a glance.
//   Input    — full ``goldfive.input_preview`` (up to 4 KiB) in a
//              monospace pre-block with copy-to-clipboard.
//   Output   — full ``goldfive.output_preview`` in the same shape.
//   Context  — definition list: model, elapsed, run/session/task ids,
//              compound target agent id.
//   Linked plan revision — for refine_* calls, a link to the resulting
//              PlanRevised (resolved via target_task_id + time) so the
//              reader can hop from the steer to the plan diff.
//
// All sections collapse gracefully when their attribute is absent — a
// pre-Option-X session or a goldfive span from before the sibling
// sink-stamp PR lands still renders the header + context list.

import type { Span, TaskPlan } from '../../gantt/types';
import { bareAgentName } from '../../gantt/index';
import type { GoldfiveSpanInfo } from '../../lib/goldfiveSpan';
import './GoldfiveSpanDetail.css';

interface Props {
  span: Span;
  info: GoldfiveSpanInfo;
  /** When set, rendered as the "linked plan revision" block for refine_* calls. */
  linkedPlanRevision?: {
    plan: TaskPlan;
    onOpen?: () => void;
  } | null;
  /** Optional overrides for the two copy-to-clipboard buttons (tests). */
  onCopy?: (text: string) => void;
}

function copyText(text: string, override?: (text: string) => void): void {
  if (!text) return;
  if (override) {
    override(text);
    return;
  }
  void navigator.clipboard?.writeText(text).catch(() => {});
}

function readString(span: Span, key: string): string {
  const v = span.attributes?.[key];
  if (!v) return '';
  if (v.kind !== 'string') return '';
  return v.value;
}

function readInt(span: Span, key: string): number | null {
  const v = span.attributes?.[key];
  if (!v) return null;
  if (v.kind === 'int') return Number(v.value);
  if (v.kind === 'double') return v.value;
  return null;
}

export function GoldfiveSpanDetail({
  span,
  info,
  linkedPlanRevision,
  onCopy,
}: Props) {
  const model =
    readString(span, 'goldfive.model') ||
    readString(span, 'judge.model') ||
    readString(span, 'llm.model');
  const elapsedMs =
    readInt(span, 'goldfive.elapsed_ms') ??
    readInt(span, 'judge.elapsed_ms') ??
    (span.endMs != null ? Math.max(0, Math.round(span.endMs - span.startMs)) : null);
  const runId =
    readString(span, 'goldfive.run_id') || readString(span, 'run_id');
  const taskId =
    info.targetTaskId || readString(span, 'hgraf.task_id');
  const sessionIdAttr = readString(span, 'goldfive.session_id') || span.sessionId;

  return (
    <div
      className="hg-gf-detail"
      data-testid="goldfive-span-detail"
      data-call-name={info.callName}
      data-category={info.category}
    >
      <header
        className="hg-gf-detail__header"
        data-testid="goldfive-span-detail-header"
      >
        <div className="hg-gf-detail__title">{info.decisionSummary}</div>
        <div className="hg-gf-detail__badges">
          <span
            className="hg-gf-detail__badge hg-gf-detail__badge--call"
            data-testid="goldfive-span-detail-call-name"
            data-category={info.category}
          >
            {info.callName}
          </span>
          {info.targetAgentId && (
            <span
              className="hg-gf-detail__badge hg-gf-detail__badge--target"
              data-testid="goldfive-span-detail-target-agent"
              title={info.targetAgentIdRaw}
            >
              → {info.targetAgentId}
            </span>
          )}
          {info.targetTaskId && (
            <span
              className="hg-gf-detail__badge hg-gf-detail__badge--task"
              data-testid="goldfive-span-detail-target-task"
            >
              task {info.targetTaskId}
            </span>
          )}
        </div>
      </header>

      <section
        className="hg-gf-detail__section"
        data-testid="goldfive-span-detail-input"
      >
        <div className="hg-gf-detail__section-head">
          <span className="hg-gf-detail__section-label">Input</span>
          {info.inputPreview && (
            <button
              type="button"
              className="hg-gf-detail__copy"
              data-testid="goldfive-span-detail-input-copy"
              onClick={() => copyText(info.inputPreview, onCopy)}
              title="Copy input to clipboard"
            >
              copy
            </button>
          )}
        </div>
        {info.inputPreview ? (
          <pre
            className="hg-gf-detail__pre"
            data-testid="goldfive-span-detail-input-body"
          >
            {info.inputPreview}
          </pre>
        ) : (
          <div className="hg-gf-detail__empty">
            No input preview captured.
          </div>
        )}
      </section>

      <section
        className="hg-gf-detail__section"
        data-testid="goldfive-span-detail-output"
      >
        <div className="hg-gf-detail__section-head">
          <span className="hg-gf-detail__section-label">Output</span>
          {info.outputPreview && (
            <button
              type="button"
              className="hg-gf-detail__copy"
              data-testid="goldfive-span-detail-output-copy"
              onClick={() => copyText(info.outputPreview, onCopy)}
              title="Copy output to clipboard"
            >
              copy
            </button>
          )}
        </div>
        {info.outputPreview ? (
          <pre
            className="hg-gf-detail__pre"
            data-testid="goldfive-span-detail-output-body"
          >
            {info.outputPreview}
          </pre>
        ) : (
          <div className="hg-gf-detail__empty">
            No output preview captured.
          </div>
        )}
      </section>

      <section
        className="hg-gf-detail__section"
        data-testid="goldfive-span-detail-context"
      >
        <span className="hg-gf-detail__section-label">Context</span>
        <dl className="hg-gf-detail__context-grid">
          {model && (
            <>
              <dt>model</dt>
              <dd data-testid="goldfive-span-detail-ctx-model">{model}</dd>
            </>
          )}
          {elapsedMs !== null && (
            <>
              <dt>elapsed</dt>
              <dd data-testid="goldfive-span-detail-ctx-elapsed">
                {elapsedMs}ms
              </dd>
            </>
          )}
          {runId && (
            <>
              <dt>run</dt>
              <dd data-testid="goldfive-span-detail-ctx-run">
                <code>{runId}</code>
              </dd>
            </>
          )}
          {sessionIdAttr && (
            <>
              <dt>session</dt>
              <dd data-testid="goldfive-span-detail-ctx-session">
                <code>{sessionIdAttr}</code>
              </dd>
            </>
          )}
          {taskId && (
            <>
              <dt>task</dt>
              <dd data-testid="goldfive-span-detail-ctx-task">
                <code>{taskId}</code>
              </dd>
            </>
          )}
          {info.targetAgentIdRaw && (
            <>
              <dt>target agent</dt>
              <dd data-testid="goldfive-span-detail-ctx-target">
                <code>{info.targetAgentIdRaw}</code>
              </dd>
            </>
          )}
        </dl>
      </section>

      {info.category === 'refine' && linkedPlanRevision && (
        <section
          className="hg-gf-detail__section hg-gf-detail__section--linked"
          data-testid="goldfive-span-detail-linked-plan"
        >
          <span className="hg-gf-detail__section-label">Steered</span>
          <button
            type="button"
            className="hg-gf-detail__linked-plan-link"
            data-testid="goldfive-span-detail-linked-plan-link"
            onClick={linkedPlanRevision.onOpen}
            disabled={!linkedPlanRevision.onOpen}
            title="Open the plan revision this steer produced"
          >
            Plan refined → r{linkedPlanRevision.plan.revisionIndex ?? 0}
            {linkedPlanRevision.plan.revisionReason && (
              <span className="hg-gf-detail__linked-plan-reason">
                {' · '}
                {linkedPlanRevision.plan.revisionReason}
              </span>
            )}
          </button>
          {linkedPlanRevision.plan.tasks.length > 0 && (
            <ul
              className="hg-gf-detail__linked-plan-tasks"
              data-testid="goldfive-span-detail-linked-plan-tasks"
            >
              {linkedPlanRevision.plan.tasks.slice(0, 6).map((t) => (
                <li key={t.id}>
                  {t.title || t.id}
                  {t.assigneeAgentId && (
                    <span className="hg-gf-detail__linked-plan-task-agent">
                      {' · '}
                      {bareAgentName(t.assigneeAgentId) || t.assigneeAgentId}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
