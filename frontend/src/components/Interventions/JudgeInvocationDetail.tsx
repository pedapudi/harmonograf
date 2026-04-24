// Click-through detail panel for goldfive judge spans (harmonograf#197).
//
// Rendered when the user clicks a judge span on the goldfive lane. The
// span arrives via the normal span transport — the harmonograf client
// sink translates ReasoningJudgeInvoked goldfive events into
// SpanStart/SpanEnd frames under Option X (harmonograf#N). The panel
// reads `judge.*` attributes directly off the span; nothing here
// depends on the event-side synthesizer that Option X retired.
//
// Surfaces the six questions an operator asks about an LLM-as-judge
// invocation:
//
//   1. when did it fire, which model, how long did it take
//   2. which agent + task was being judged
//   3. what reasoning did the judge see (reasoning_input, collapsible)
//   4. what did it decide (on_task / off_task + severity + reason)
//   5. raw LLM response, for debugging malformed verdicts (collapsible)
//   6. did the verdict actually trigger steering (PlanRevised lookup)
//
// The component is deliberately layout-light — it reuses the existing
// CSS language from InterventionsList (data-severity / swatches / tiny
// uppercase labels) so it slots into the goldfive panel without its own
// theme. The parent container (currently SpanPopover) supplies the box
// chrome; this component contributes the inner sections.

import { useState } from 'react';
import type { JudgeDetail } from '../../lib/interventionDetail';
import { bareAgentName } from '../../gantt/index';
import './JudgeInvocationDetail.css';

interface Props {
  detail: JudgeDetail;
  // Optional: agent-id → display-name resolver (SessionStore agents).
  // When omitted the component falls back to `bareAgentName(id)`.
  resolveAgentName?: (agentId: string) => string;
  // Optional: scroll-to / highlight callbacks — the Gantt can pass
  // handlers here so the header's context links (agent / task) pan the
  // canvas. When omitted the links render as plain text.
  onFocusAgent?: (agentId: string) => void;
  onFocusTask?: (taskId: string) => void;
  // Optional: click handler for the steered-plan link. Lets the parent
  // route the click into the existing intervention-detail pane (the one
  // that renders Trigger / Steering / Target for PlanRevised rows).
  onOpenSteering?: (planId: string, revisionIndex: number) => void;
}

function fmtTime(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number): string => n.toString().padStart(2, '0');
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}

function copyToClipboard(text: string): void {
  if (!text) return;
  void navigator.clipboard?.writeText(text).catch(() => {});
}

export function JudgeInvocationDetail({
  detail,
  resolveAgentName,
  onFocusAgent,
  onFocusTask,
  onOpenSteering,
}: Props) {
  const [inputOpen, setInputOpen] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);

  const displayAgent = (id: string): string => {
    if (!id) return '';
    if (resolveAgentName) return resolveAgentName(id) || bareAgentName(id) || id;
    return bareAgentName(id) || id;
  };

  const verdictLabel =
    detail.verdictBucket === 'on_task'
      ? 'On task'
      : detail.verdictBucket === 'off_task'
        ? 'Off task'
        : 'No verdict';

  return (
    <div
      className="hg-judge-detail"
      data-testid="judge-invocation-detail"
      data-verdict={detail.verdictBucket}
    >
      <header className="hg-judge-detail__header">
        <div className="hg-judge-detail__title">Judge invocation</div>
        <div className="hg-judge-detail__meta">
          {detail.recordedAtMs >= 0 && (
            <span className="hg-judge-detail__at" title="Session-relative time">
              {fmtTime(detail.recordedAtMs)}
            </span>
          )}
          {detail.elapsedMs > 0 && (
            <span
              className="hg-judge-detail__elapsed"
              title="Wall-clock duration of the judge call"
            >
              {detail.elapsedMs}ms
            </span>
          )}
          {detail.model && (
            <span className="hg-judge-detail__model" title="Judge model">
              {detail.model}
            </span>
          )}
        </div>
        <div className="hg-judge-detail__badges">
          <span
            className="hg-judge-detail__verdict"
            data-testid="judge-detail-verdict"
            data-bucket={detail.verdictBucket}
          >
            {verdictLabel}
          </span>
          {detail.verdictBucket === 'off_task' && detail.severity && (
            <span
              className="hg-judge-detail__severity"
              data-testid="judge-detail-severity"
              data-severity={detail.severity}
            >
              {detail.severity}
            </span>
          )}
        </div>
      </header>

      {(detail.subjectAgentId || detail.taskId) && (
        <section
          className="hg-judge-detail__context"
          data-testid="judge-detail-context"
        >
          <span className="hg-judge-detail__section-label">Context</span>
          {detail.subjectAgentId && (
            <ContextLink
              label="agent"
              value={displayAgent(detail.subjectAgentId)}
              title={detail.subjectAgentId}
              testId="judge-detail-agent"
              onClick={
                onFocusAgent
                  ? () => onFocusAgent(detail.subjectAgentId)
                  : undefined
              }
            />
          )}
          {detail.taskId && (
            <ContextLink
              label="task"
              value={detail.taskId}
              title={detail.taskId}
              testId="judge-detail-task"
              onClick={onFocusTask ? () => onFocusTask(detail.taskId) : undefined}
              mono
            />
          )}
        </section>
      )}

      {(detail.reasoningInput || detail.verdictBucket === 'no_verdict') && (
        <Collapsible
          label="Reasoning input"
          open={inputOpen}
          onToggle={() => setInputOpen((v) => !v)}
          testId="judge-detail-reasoning"
          onCopy={
            detail.reasoningInput
              ? () => copyToClipboard(detail.reasoningInput)
              : undefined
          }
        >
          {detail.reasoningInput ? (
            <pre
              className="hg-judge-detail__pre"
              data-testid="judge-detail-reasoning-body"
            >
              {detail.reasoningInput}
            </pre>
          ) : (
            <div className="hg-judge-detail__empty">No reasoning captured.</div>
          )}
        </Collapsible>
      )}

      <section
        className="hg-judge-detail__verdict-section"
        data-testid="judge-detail-response"
      >
        <span className="hg-judge-detail__section-label">Judge response</span>
        {detail.verdictBucket === 'on_task' && (
          <>
            {detail.reason ? (
              <div className="hg-judge-detail__reason">{detail.reason}</div>
            ) : (
              <div className="hg-judge-detail__empty">
                No explanation provided.
              </div>
            )}
          </>
        )}
        {detail.verdictBucket === 'off_task' && (
          <div className="hg-judge-detail__reason hg-judge-detail__reason--off">
            {detail.reason || '(no explanation from judge)'}
          </div>
        )}
        {detail.verdictBucket === 'no_verdict' && (
          <div
            className="hg-judge-detail__reason hg-judge-detail__reason--none"
            data-testid="judge-detail-no-verdict"
          >
            The judge returned no parseable verdict. Raw response below.
          </div>
        )}
      </section>

      {detail.rawResponse && (
        <Collapsible
          label="Raw LLM response"
          open={rawOpen}
          onToggle={() => setRawOpen((v) => !v)}
          testId="judge-detail-raw"
          onCopy={() => copyToClipboard(detail.rawResponse)}
        >
          <pre
            className="hg-judge-detail__pre"
            data-testid="judge-detail-raw-body"
          >
            {detail.rawResponse}
          </pre>
        </Collapsible>
      )}

      {detail.verdictBucket === 'off_task' && (
        <section
          className="hg-judge-detail__steering"
          data-testid="judge-detail-steering"
        >
          <span className="hg-judge-detail__section-label">Steering outcome</span>
          {detail.steeredPlan ? (
            <>
              <button
                type="button"
                className="hg-judge-detail__steering-link"
                data-testid="judge-detail-steering-link"
                onClick={
                  onOpenSteering && detail.steeredPlan
                    ? () =>
                        onOpenSteering(
                          detail.steeredPlan!.id,
                          detail.steeredPlan!.revisionIndex ?? 0,
                        )
                    : undefined
                }
                disabled={!onOpenSteering}
                title="Open the plan-revision detail"
              >
                Goldfive steering: refined plan → r
                {detail.steeredPlan.revisionIndex ?? 0}
              </button>
              {detail.steeringSummary && (
                <div className="hg-judge-detail__steering-summary">
                  {detail.steeringSummary}
                </div>
              )}
              {detail.taskSummaries.length > 0 && (
                <ul
                  className="hg-judge-detail__tasks"
                  data-testid="judge-detail-steering-tasks"
                >
                  {detail.taskSummaries.map((t, i) => (
                    <li key={`${i}:${t}`}>{t}</li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <div
              className="hg-judge-detail__empty"
              data-testid="judge-detail-no-steering"
            >
              No steering applied (ladder did not escalate or suppression
              fired).
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function ContextLink({
  label,
  value,
  title,
  testId,
  onClick,
  mono,
}: {
  label: string;
  value: string;
  title?: string;
  testId?: string;
  onClick?: () => void;
  mono?: boolean;
}) {
  const body = mono ? <code>{value}</code> : value;
  if (onClick) {
    return (
      <button
        type="button"
        className="hg-judge-detail__ctx-link"
        data-testid={testId}
        onClick={onClick}
        title={title}
      >
        <span className="hg-judge-detail__ctx-label">{label}</span>
        {body}
      </button>
    );
  }
  return (
    <span
      className="hg-judge-detail__ctx-plain"
      data-testid={testId}
      title={title}
    >
      <span className="hg-judge-detail__ctx-label">{label}</span>
      {body}
    </span>
  );
}

function Collapsible({
  label,
  open,
  onToggle,
  onCopy,
  testId,
  children,
}: {
  label: string;
  open: boolean;
  onToggle: () => void;
  onCopy?: () => void;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="hg-judge-detail__collapsible"
      data-testid={testId}
      data-open={open ? 'true' : 'false'}
    >
      <div className="hg-judge-detail__collapsible-header">
        <button
          type="button"
          className="hg-judge-detail__collapsible-toggle"
          data-testid={testId ? `${testId}-toggle` : undefined}
          aria-expanded={open}
          onClick={onToggle}
        >
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
          {label}
        </button>
        {onCopy && (
          <button
            type="button"
            className="hg-judge-detail__copy"
            data-testid={testId ? `${testId}-copy` : undefined}
            onClick={onCopy}
            title="Copy to clipboard"
          >
            copy
          </button>
        )}
      </div>
      {open && <div className="hg-judge-detail__collapsible-body">{children}</div>}
    </section>
  );
}
