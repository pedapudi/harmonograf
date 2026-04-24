// Click-through detail panel for goldfive judge spans (harmonograf#197).
//
// Rendered when the user clicks a judge span on the goldfive lane. The
// span arrives via the normal span transport — the harmonograf client
// sink translates ReasoningJudgeInvoked goldfive events into
// SpanStart/SpanEnd frames under Option X (harmonograf#N). The panel
// reads `judge.*` attributes directly off the span; nothing here
// depends on the event-side synthesizer that Option X retired.
//
// Two layout variants share the same data model:
//
//   * `variant="popover"` — quick-look card that sits in SpanPopover.
//     Leads with a large colour-coded verdict banner, then the reason as
//     a lead sentence, then a compact context row (agent / task / model /
//     elapsed), then a collapsed-by-default "Reasoning under review"
//     preview (first ~200 chars of the judge's input).
//
//   * `variant="drawer"` — full "what was judged / what the judgement
//     is / steering outcome" breakdown rendered inside the Inspector
//     Drawer. Section A surfaces the full reasoning input + task context
//     + goals; Section B the verdict + raw response + parse diagnostic;
//     Section C the plan-revised link (when the verdict actually drove
//     steering).
//
// Fallbacks are explicit: missing reasoning_input / raw_response / goals
// render as subdued placeholders rather than blank space, so pre-#234
// sessions still tell the operator "we don't have that recorded" without
// looking broken.

import { useState } from 'react';
import type { JudgeDetail } from '../../lib/interventionDetail';
import { bareAgentName } from '../../gantt/index';
import './JudgeInvocationDetail.css';

type Variant = 'popover' | 'drawer';

interface Props {
  detail: JudgeDetail;
  // Popover (compact) or drawer (full). Defaults to `drawer` so existing
  // call sites (tests that rely on the pre-#234 layout) continue to work.
  variant?: Variant;
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
  // Drawer-only contextual fields. Provided by the Drawer container,
  // which resolves them from the SessionStore. Omitted on the popover
  // variant — those sections simply don't render there.
  taskTitle?: string;
  taskDescription?: string;
  // Session-level goals (the user's run goal summaries). Each entry is a
  // short sentence; the drawer lists them verbatim under "Goals".
  goals?: readonly string[];
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

// Maps the verdict tone to a short banner label. Used by both variants;
// the popover renders it in a big banner, the drawer in a pill badge.
function verdictBannerLabel(detail: JudgeDetail): string {
  switch (detail.verdictTone) {
    case 'on_task':
      return 'On task';
    case 'off_task_info':
      return 'Off task (info)';
    case 'off_task_warning':
      return 'Off task (warning)';
    case 'off_task_critical':
      return 'Off task (critical)';
    case 'no_verdict':
    default:
      return 'No verdict';
  }
}

// Popover input-preview chars. Kept short because the popover is a
// quick-look surface; the full text is one click away in the drawer.
const POPOVER_PREVIEW_CHARS = 200;

// Popover reason fallback chars — when the judge didn't emit a `reason`
// we pull from the raw response so the popover isn't empty.
const POPOVER_REASON_FALLBACK_CHARS = 140;

export function JudgeInvocationDetail({
  detail,
  variant = 'drawer',
  resolveAgentName,
  onFocusAgent,
  onFocusTask,
  onOpenSteering,
  taskTitle,
  taskDescription,
  goals,
}: Props) {
  if (variant === 'popover') {
    return (
      <PopoverVariant
        detail={detail}
        resolveAgentName={resolveAgentName}
      />
    );
  }
  return (
    <DrawerVariant
      detail={detail}
      resolveAgentName={resolveAgentName}
      onFocusAgent={onFocusAgent}
      onFocusTask={onFocusTask}
      onOpenSteering={onOpenSteering}
      taskTitle={taskTitle}
      taskDescription={taskDescription}
      goals={goals}
    />
  );
}

// ---------------------------------------------------------------------
// Popover variant — compact quick-look card.
// ---------------------------------------------------------------------

function PopoverVariant({
  detail,
  resolveAgentName,
}: {
  detail: JudgeDetail;
  resolveAgentName?: (id: string) => string;
}) {
  const [inputOpen, setInputOpen] = useState(false);

  const displayAgent = (id: string): string => {
    if (!id) return 'Unknown agent';
    if (resolveAgentName) return resolveAgentName(id) || bareAgentName(id) || id;
    return bareAgentName(id) || id;
  };

  // Lead sentence: prefer the judge's explicit reason. Fall back to the
  // first chunk of the raw response so the popover never shows a blank
  // lead on malformed verdicts.
  const leadSource = detail.reason
    || detail.decisionSummary
    || (detail.rawResponse
      ? detail.rawResponse.slice(0, POPOVER_REASON_FALLBACK_CHARS)
      : '');
  const lead = leadSource.trim();

  // Input preview — prefer the richer goldfive.input_preview (already
  // trimmed upstream) and fall back to the raw reasoning input.
  const rawInput = detail.inputPreview || detail.reasoningInput;
  const inputExcerpt = rawInput
    ? rawInput.slice(0, POPOVER_PREVIEW_CHARS)
    : '';
  const inputTruncated = rawInput.length > POPOVER_PREVIEW_CHARS;

  return (
    <div
      className="hg-judge-detail hg-judge-detail--popover"
      data-testid="judge-invocation-detail"
      data-variant="popover"
      data-verdict={detail.verdictBucket}
      data-tone={detail.verdictTone}
    >
      {/* Top: verdict banner — large, colour-coded. */}
      <div
        className="hg-judge-detail__banner"
        data-testid="judge-popover-banner"
        data-tone={detail.verdictTone}
        role="status"
        aria-live="polite"
      >
        <span className="hg-judge-detail__banner-label">
          {verdictBannerLabel(detail)}
        </span>
      </div>

      {/* Lead sentence — judge's short explanation. */}
      {lead ? (
        <div
          className="hg-judge-detail__lead"
          data-testid="judge-popover-lead"
        >
          {lead}
        </div>
      ) : (
        <div
          className="hg-judge-detail__lead hg-judge-detail__lead--empty"
          data-testid="judge-popover-lead-empty"
        >
          Judge returned no explanation.
        </div>
      )}

      {/* Context row. */}
      <div
        className="hg-judge-detail__context-row"
        data-testid="judge-popover-context"
      >
        <ContextChip
          label="Judging"
          value={displayAgent(detail.subjectAgentId)}
          title={detail.subjectAgentId || undefined}
          testId="judge-popover-subject"
        />
        {detail.taskId && (
          <ContextChip
            label="Task"
            value={detail.taskId}
            mono
            testId="judge-popover-task"
          />
        )}
        {detail.model && (
          <ContextChip
            label="Model"
            value={detail.model}
            testId="judge-popover-model"
          />
        )}
        {detail.elapsedMs > 0 && (
          <ContextChip
            label="Elapsed"
            value={`${detail.elapsedMs}ms`}
            testId="judge-popover-elapsed"
          />
        )}
      </div>

      {/* Input preview — collapsed by default. */}
      <Collapsible
        label="Reasoning under review"
        open={inputOpen}
        onToggle={() => setInputOpen((v) => !v)}
        testId="judge-popover-input"
      >
        {inputExcerpt ? (
          <>
            <pre
              className="hg-judge-detail__pre hg-judge-detail__pre--excerpt"
              data-testid="judge-popover-input-body"
            >
              {inputExcerpt}
              {inputTruncated ? '…' : ''}
            </pre>
            <div
              className="hg-judge-detail__drawer-hint"
              data-testid="judge-popover-drawer-hint"
            >
              See full in drawer.
            </div>
          </>
        ) : (
          <div
            className="hg-judge-detail__empty"
            data-testid="judge-popover-input-empty"
          >
            Reasoning input not recorded.
          </div>
        )}
      </Collapsible>
    </div>
  );
}

// ---------------------------------------------------------------------
// Drawer variant — full A / B / C breakdown.
// ---------------------------------------------------------------------

function DrawerVariant({
  detail,
  resolveAgentName,
  onFocusAgent,
  onFocusTask,
  onOpenSteering,
  taskTitle,
  taskDescription,
  goals,
}: {
  detail: JudgeDetail;
  resolveAgentName?: (id: string) => string;
  onFocusAgent?: (agentId: string) => void;
  onFocusTask?: (taskId: string) => void;
  onOpenSteering?: (planId: string, revisionIndex: number) => void;
  taskTitle?: string;
  taskDescription?: string;
  goals?: readonly string[];
}) {
  const [inputOpen, setInputOpen] = useState(true);
  const [rawOpen, setRawOpen] = useState(true);

  const displayAgent = (id: string): string => {
    if (!id) return 'Unknown agent';
    if (resolveAgentName) return resolveAgentName(id) || bareAgentName(id) || id;
    return bareAgentName(id) || id;
  };

  const verdictLabel =
    detail.verdictBucket === 'on_task'
      ? 'On task'
      : detail.verdictBucket === 'off_task'
        ? 'Off task'
        : 'No verdict';

  const goalsList = (goals ?? []).filter((g) => g && g.trim().length > 0);

  return (
    <div
      className="hg-judge-detail"
      data-testid="judge-invocation-detail"
      data-variant="drawer"
      data-verdict={detail.verdictBucket}
      data-tone={detail.verdictTone}
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
            data-tone={detail.verdictTone}
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

      {/* ================================================================
          Section A — What was being judged
          ================================================================ */}
      <section
        className="hg-judge-detail__section"
        data-testid="judge-drawer-section-a"
      >
        <h3 className="hg-judge-detail__section-heading">
          A. What was being judged
        </h3>

        <div
          className="hg-judge-detail__context"
          data-testid="judge-detail-context"
        >
          <ContextLink
            label="Subject"
            value={displayAgent(detail.subjectAgentId)}
            title={detail.subjectAgentId || 'Unknown agent'}
            testId="judge-detail-agent"
            onClick={
              detail.subjectAgentId && onFocusAgent
                ? () => onFocusAgent(detail.subjectAgentId)
                : undefined
            }
          />
          {detail.taskId && (
            <ContextLink
              label="Task"
              value={detail.taskId}
              title={detail.taskId}
              testId="judge-detail-task"
              onClick={onFocusTask ? () => onFocusTask(detail.taskId) : undefined}
              mono
            />
          )}
        </div>

        {(taskTitle || taskDescription) && (
          <div
            className="hg-judge-detail__task-context"
            data-testid="judge-drawer-task-context"
          >
            {taskTitle && (
              <div className="hg-judge-detail__task-title">{taskTitle}</div>
            )}
            {taskDescription && (
              <div className="hg-judge-detail__task-desc">
                {taskDescription}
              </div>
            )}
          </div>
        )}

        {goalsList.length > 0 && (
          <div
            className="hg-judge-detail__goals"
            data-testid="judge-drawer-goals"
          >
            <span className="hg-judge-detail__section-label">Goals</span>
            <ul className="hg-judge-detail__goals-list">
              {goalsList.map((g, i) => (
                <li key={i} data-testid="judge-drawer-goal">
                  {g}
                </li>
              ))}
            </ul>
          </div>
        )}

        <Collapsible
          label="Reasoning text (judge input)"
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
            <div
              className="hg-judge-detail__empty"
              data-testid="judge-detail-reasoning-empty"
            >
              Reasoning input not recorded.
            </div>
          )}
        </Collapsible>
      </section>

      {/* ================================================================
          Section B — What the judgement is
          ================================================================ */}
      <section
        className="hg-judge-detail__section"
        data-testid="judge-drawer-section-b"
      >
        <h3 className="hg-judge-detail__section-heading">
          B. What the judgement is
        </h3>

        <div
          className="hg-judge-detail__verdict-section"
          data-testid="judge-detail-response"
        >
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
        </div>

        {/* Parsed fields — always render so the user can see what the
            resolver extracted. The diagnostic line below explains whether
            the parse was successful. */}
        <div
          className="hg-judge-detail__parsed"
          data-testid="judge-drawer-parsed"
        >
          <span className="hg-judge-detail__section-label">Parsed fields</span>
          <dl className="hg-judge-detail__parsed-list">
            <dt>on_task</dt>
            <dd data-testid="judge-drawer-parsed-on-task">
              {detail.parseSuccessful ? String(detail.onTask) : '—'}
            </dd>
            <dt>severity</dt>
            <dd data-testid="judge-drawer-parsed-severity">
              {detail.severity || '—'}
            </dd>
          </dl>
          <div
            className={
              detail.parseSuccessful
                ? 'hg-judge-detail__diagnostic'
                : 'hg-judge-detail__diagnostic hg-judge-detail__diagnostic--warn'
            }
            data-testid="judge-drawer-parse-diagnostic"
            data-ok={detail.parseSuccessful ? 'true' : 'false'}
          >
            {detail.parseSuccessful
              ? 'Successfully parsed JSON with on_task + severity.'
              : 'Malformed response — treating as no verdict.'}
          </div>
        </div>

        <Collapsible
          label="Raw LLM response"
          open={rawOpen}
          onToggle={() => setRawOpen((v) => !v)}
          testId="judge-detail-raw"
          onCopy={
            detail.rawResponse
              ? () => copyToClipboard(detail.rawResponse)
              : undefined
          }
        >
          {detail.rawResponse ? (
            <pre
              className="hg-judge-detail__pre"
              data-testid="judge-detail-raw-body"
            >
              {detail.rawResponse}
            </pre>
          ) : (
            <div
              className="hg-judge-detail__empty"
              data-testid="judge-detail-raw-empty"
            >
              Raw response not recorded.
            </div>
          )}
        </Collapsible>
      </section>

      {/* ================================================================
          Section C — Steering outcome (off-task only)
          ================================================================ */}
      {detail.verdictBucket === 'off_task' && (
        <section
          className="hg-judge-detail__section hg-judge-detail__steering"
          data-testid="judge-detail-steering"
        >
          <h3 className="hg-judge-detail__section-heading">
            C. Steering outcome
          </h3>
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
                Goldfive refined the plan → r
                {detail.steeredPlan.revisionIndex ?? 0}
              </button>
              {detail.steeringSummary && (
                <div className="hg-judge-detail__steering-summary">
                  {detail.steeringSummary}
                </div>
              )}
              {detail.decisionSummary
                && detail.decisionSummary !== detail.steeringSummary && (
                <div
                  className="hg-judge-detail__steering-summary"
                  data-testid="judge-drawer-decision-summary"
                >
                  {detail.decisionSummary}
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

// ---------------------------------------------------------------------
// Shared sub-components.
// ---------------------------------------------------------------------

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

// Popover-only chip — denser than ContextLink and always plain-text (the
// popover's context is "at a glance" metadata, not a click-through).
function ContextChip({
  label,
  value,
  title,
  testId,
  mono,
}: {
  label: string;
  value: string;
  title?: string;
  testId?: string;
  mono?: boolean;
}) {
  const body = mono ? <code>{value}</code> : value;
  return (
    <span
      className="hg-judge-detail__ctx-chip"
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
