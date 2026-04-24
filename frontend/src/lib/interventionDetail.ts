// Intervention detail resolver (harmonograf#196).
//
// Given a drift record, a plan revision, or the task cancel-reason slot,
// compute the three-part detail that the Trajectory DetailPane (and any
// other intervention-aware UI) renders:
//
//   * trigger  — the input that triggered goldfive's detection
//                (drift reasoning text / activity summary / detail)
//   * steering — what goldfive published (refine-plan reason / cancel
//                reason / inject-prompt text)
//   * target   — the agent id goldfive's steering applies to
//
// Tree-agnostic: this helper never inspects kind vocabularies. New drift
// or revision kinds emitted server-side surface correctly without code
// changes. The shape mirrors the three sections the UI renders — the
// view-layer is free to hide any section whose value is empty.
//
// Forward-compat with goldfive#feat/judge-observability-events:
//   DriftDetected.trigger_input and PlanRevised.refine_input_summary /
//   refine_output_summary / target_agent_id are read through the
//   synthesized span attributes goldfiveEvent.ts stamps (absent today,
//   present post-merge). We do NOT add optional fields to DriftRecord /
//   TaskPlan themselves — pre-merge the stubs don't carry the values so
//   there is nothing to wire. Post-merge the ingest layer fills the span
//   attributes and this resolver surfaces them.

import type { DriftRecord, SessionStore } from '../gantt/index';
import type { Span, TaskPlan, Task } from '../gantt/types';

export interface InterventionDetail {
  // Section 1: what triggered goldfive's detection. For drifts this is
  // the drift detail plus (post-merge) the trigger_input reasoning text.
  // Empty string when neither is known — view callers should hide the
  // section.
  trigger: string;
  // Section 2: what goldfive published as steering. For plan revisions
  // this is the revision reason plus (post-merge) the refine input /
  // output summaries. For task cancellations (selected from the delta
  // list) this is the task.cancelReason. Empty string ⇒ not a steering.
  steering: string;
  // Section 3: target agent id. Derived from the drift's current_agent_id
  // or (post-merge) PlanRevised.target_agent_id. Empty string when
  // goldfive did not point at a specific agent.
  targetAgentId: string;
  // Target task id, when known. Surfaced alongside the agent in the
  // detail pane so the "Target" section can link to the task lane too.
  targetTaskId: string;
  // Forward-compat: DriftDetected.authored_by ("user" / "goldfive" / "").
  // Empty string on legacy events or non-drift selections — the view
  // hides the label in that case.
  authoredBy?: string;
}

// Pull a string attribute off a Span, handling absent / wrong-kind gracefully.
function readStringAttr(span: Span | null, key: string): string {
  if (!span) return '';
  const attr = span.attributes[key];
  if (!attr) return '';
  if (attr.kind !== 'string') return '';
  return attr.value;
}

// Synthetic "refine" span minted by goldfiveEvent.synthesizeRefineSpan.
// Matched by `refine.index == revisionIndex` — the most reliable key
// since the synth span's startMs is the event's emittedAt (not the
// plan's createdAt, which can differ by clock skew).
function findRefineSpan(
  store: SessionStore | null,
  plan: TaskPlan,
): Span | null {
  if (!store) return null;
  const revIdx = plan.revisionIndex ?? 0;
  if (revIdx <= 0) return null;
  const spans: Span[] = [];
  store.spans.queryAgent(
    '__goldfive__',
    0,
    Number.POSITIVE_INFINITY,
    spans,
  );
  for (const s of spans) {
    if (!s.name.startsWith('refine:')) continue;
    if (readStringAttr(s, 'refine.index') === String(revIdx)) return s;
  }
  return null;
}

// Synthetic "drift" span minted by goldfiveEvent.synthesizeDriftSpan.
function findDriftSpan(
  store: SessionStore | null,
  drift: DriftRecord,
): Span | null {
  if (!store) return null;
  const actor = drift.kind && drift.kind.startsWith('user_') ? '__user__' : '__goldfive__';
  const spans: Span[] = [];
  store.spans.queryAgent(
    actor,
    drift.recordedAtMs - 5,
    drift.recordedAtMs + 5,
    spans,
  );
  for (const s of spans) {
    if (
      readStringAttr(s, 'drift.kind') === drift.kind &&
      s.startMs === drift.recordedAtMs
    ) {
      return s;
    }
  }
  return null;
}

// Resolve detail for a drift selection. Pairs the drift row with its
// triggering PlanRevised (if any) by walking store.tasks' rev list for a
// plan whose triggerEventId matches this drift's driftId. That lookup is
// cheap — sessions have few plan revs — and keeps the resolver stateless.
export function resolveDriftDetail(
  drift: DriftRecord,
  plans: readonly TaskPlan[],
  store: SessionStore | null,
): InterventionDetail {
  const driftSpan = findDriftSpan(store, drift);
  const triggerInput = readStringAttr(driftSpan, 'drift.trigger_input');
  const triggerParts: string[] = [];
  if (drift.detail) triggerParts.push(drift.detail);
  if (triggerInput && triggerInput !== drift.detail) triggerParts.push(triggerInput);
  // Find a plan rev triggered by this drift to compose the steering text.
  // Tier-1 strict id match (see lib/interventions.ts).
  const triggeredPlan = drift.driftId
    ? plans.find((p) => p.triggerEventId === drift.driftId)
    : undefined;
  let steeringText = '';
  let refineSpan: Span | null = null;
  if (triggeredPlan) {
    refineSpan = findRefineSpan(store, triggeredPlan);
    const steeringParts: string[] = [];
    if (triggeredPlan.revisionReason) steeringParts.push(triggeredPlan.revisionReason);
    const inSum = readStringAttr(refineSpan, 'refine.input_summary');
    const outSum = readStringAttr(refineSpan, 'refine.output_summary');
    if (inSum) steeringParts.push(`input: ${inSum}`);
    if (outSum) steeringParts.push(`output: ${outSum}`);
    steeringText = steeringParts.join('\n\n');
  }
  // Target: prefer the refine span's target_agent_id (post-merge wire
  // field), else the drift's current_agent_id, else empty.
  const refineTarget = readStringAttr(refineSpan, 'refine.target_agent_id');
  const targetAgentId = refineTarget || drift.agentId || '';
  const targetTaskId = drift.taskId || '';
  // authored_by is forward-compat (goldfive /tmp/goldfive-steer-unify).
  // Prefer the drift record field (populated from the wire) and fall
  // back to the synthesized drift span's attribute (stamped by the
  // ingest layer) so either carrying path surfaces the label.
  const authoredBy =
    drift.authoredBy || readStringAttr(driftSpan, 'drift.authored_by') || '';
  return {
    trigger: triggerParts.join('\n\n'),
    steering: steeringText,
    targetAgentId,
    targetTaskId,
    authoredBy,
  };
}

// Resolve detail when a plan revision is selected directly (no drift).
// Used for goldfive-authored revisions that have no preceding user /
// drift row (cascade_cancel, refine_retry, etc).
export function resolvePlanRevisionDetail(
  plan: TaskPlan,
  store: SessionStore | null,
): InterventionDetail {
  if ((plan.revisionIndex ?? 0) <= 0) {
    return { trigger: '', steering: '', targetAgentId: '', targetTaskId: '' };
  }
  const refineSpan = findRefineSpan(store, plan);
  const steeringParts: string[] = [];
  if (plan.revisionReason) steeringParts.push(plan.revisionReason);
  const inSum = readStringAttr(refineSpan, 'refine.input_summary');
  const outSum = readStringAttr(refineSpan, 'refine.output_summary');
  if (inSum) steeringParts.push(`input: ${inSum}`);
  if (outSum) steeringParts.push(`output: ${outSum}`);
  return {
    trigger: '',
    steering: steeringParts.join('\n\n'),
    targetAgentId: readStringAttr(refineSpan, 'refine.target_agent_id'),
    targetTaskId: '',
  };
}

// Verdict bucket used by the judge detail panel to pick the badge + colour.
// Kept stable across pre-merge / post-merge wire shapes: callers inspect
// `onTask` / `verdict` / `rawResponse` in that order to decide the bucket.
export type JudgeVerdictBucket = 'on_task' | 'off_task' | 'no_verdict';

// Fine-grained verdict tone used by the popover banner and the drawer's
// verdict badge. Derived from `verdictBucket` + `severity`:
//   on_task           → green banner "On task"
//   off_task + info   → blue   banner "Off task (info)"
//   off_task + warn   → amber  banner "Off task (warning)"
//   off_task + crit   → red    banner "Off task (critical)"
//   no_verdict / ''   → grey   banner "No verdict"
// Kept as a separate field (rather than reconstructed in each view) so
// the popover and drawer agree on colour without duplicating the ladder.
export type JudgeVerdictTone =
  | 'on_task'
  | 'off_task_info'
  | 'off_task_warning'
  | 'off_task_critical'
  | 'no_verdict';

export interface JudgeDetail {
  // Span that triggered the detail lookup. Held so the view can key off
  // its id (copy-to-clipboard, deep-link) without a second query.
  spanId: string;
  eventId: string;
  recordedAtMs: number;
  model: string;
  elapsedMs: number;
  subjectAgentId: string;
  taskId: string;
  targetAgentId: string; // usually == subjectAgentId for reasoning judges
  verdictBucket: JudgeVerdictBucket;
  verdictTone: JudgeVerdictTone;
  // Parsed verdict fields.
  onTask: boolean;
  severity: string; // empty when on_task or no_verdict
  reason: string;   // judge's short explanation
  reasoningInput: string; // what the judge saw (chain-of-thought)
  rawResponse: string;    // raw LLM output before JSON parse
  // True when the judge's response parsed cleanly to on_task + severity.
  // False when the parser could not extract an on_task boolean — the drawer
  // surfaces this with a diagnostic line under the parsed fields.
  parseSuccessful: boolean;
  // Optional richer context sourced from the pending goldfive.* sibling
  // attributes (harmonograf#234+). When absent these stay empty and the
  // drawer falls back to the plain judge.* fields.
  inputPreview: string;    // goldfive.input_preview
  outputPreview: string;   // goldfive.output_preview
  decisionSummary: string; // goldfive.decision_summary
  // Steering outcome: the plan-revision this judge invocation triggered,
  // if any. Resolved via PlanRevised.trigger_event_id == this event id.
  steeredPlan: TaskPlan | null;
  steeringSummary: string; // human-readable summary of the plan rev
  taskSummaries: string[]; // short bullet points for new / changed tasks
}

function classifyVerdict(
  onTask: boolean,
  verdict: string,
  rawResponse: string,
): JudgeVerdictBucket {
  if (onTask || verdict === 'on_task') return 'on_task';
  // "no verdict" covers malformed / error / null cases where the judge
  // didn't produce an on_task boolean but emitted raw response text.
  // We detect this by the absence of a verdict string + the presence of
  // raw output (otherwise we'd hide empty panels).
  if (!verdict && rawResponse) return 'no_verdict';
  if (!verdict && !rawResponse) return 'no_verdict';
  return 'off_task';
}

function verdictToneFor(
  bucket: JudgeVerdictBucket,
  severity: string,
): JudgeVerdictTone {
  if (bucket === 'on_task') return 'on_task';
  if (bucket === 'no_verdict') return 'no_verdict';
  const s = (severity || '').toLowerCase();
  if (s === 'critical') return 'off_task_critical';
  if (s === 'warning' || s === 'warn') return 'off_task_warning';
  // info / missing-severity / anything else on an off-task verdict treat
  // as "info" rather than upgrading silently to warning.
  return 'off_task_info';
}

// Resolve detail for a judge-span click. The caller finds the clicked
// span by id; this helper reads its attributes + scans the plan list
// for a PlanRevised whose trigger_event_id matches the judge event id.
export function resolveJudgeDetail(
  span: Span,
  plans: readonly TaskPlan[],
): JudgeDetail {
  const eventId = readStringAttr(span, 'judge.event_id');
  const verdict = readStringAttr(span, 'judge.verdict');
  const onTaskAttr = span.attributes['judge.on_task'];
  const parseSuccessful =
    onTaskAttr?.kind === 'bool' || verdict === 'on_task' || verdict !== '';
  const onTask =
    onTaskAttr?.kind === 'bool'
      ? onTaskAttr.value
      : verdict === 'on_task';
  const severity = readStringAttr(span, 'judge.severity');
  const reason = readStringAttr(span, 'judge.reason')
    || readStringAttr(span, 'judge.reasoning');
  // Prefer the richer `goldfive.input_preview` (harmonograf#234+) when
  // present — it carries the agent's reasoning with preamble trimmed and
  // is shorter than `judge.reasoning_input` for the popover preview. The
  // drawer still renders the full `judge.reasoning_input` for fidelity.
  const inputPreview = readStringAttr(span, 'goldfive.input_preview');
  const outputPreview = readStringAttr(span, 'goldfive.output_preview');
  const decisionSummary = readStringAttr(span, 'goldfive.decision_summary');
  const reasoningInput = readStringAttr(span, 'judge.reasoning_input')
    || readStringAttr(span, 'judge.reasoning');
  const rawResponse = readStringAttr(span, 'judge.raw_response');
  const model = readStringAttr(span, 'judge.model');
  const elapsedAttr = span.attributes['judge.elapsed_ms'];
  let elapsedMs = 0;
  if (elapsedAttr?.kind === 'int') {
    elapsedMs = Number(elapsedAttr.value) || 0;
  } else if (elapsedAttr?.kind === 'double') {
    elapsedMs = Number(elapsedAttr.value) || 0;
  } else {
    const elapsedStr = readStringAttr(span, 'judge.elapsed_ms');
    elapsedMs = elapsedStr ? Number(elapsedStr) || 0 : 0;
  }
  const subjectAgentId = readStringAttr(span, 'judge.subject_agent_id')
    || readStringAttr(span, 'judge.target_agent_id');
  const targetAgentId = readStringAttr(span, 'judge.target_agent_id')
    || subjectAgentId;
  const taskId = readStringAttr(span, 'judge.target_task_id');
  const verdictBucket = classifyVerdict(onTask, verdict, rawResponse);
  const verdictTone = verdictToneFor(verdictBucket, severity);

  let steeredPlan: TaskPlan | null = null;
  if (eventId && verdictBucket === 'off_task') {
    for (const p of plans) {
      if ((p.revisionIndex ?? 0) <= 0) continue;
      if (p.triggerEventId === eventId) {
        // Prefer the latest revision when multiple match (chained refines).
        if (!steeredPlan || p.createdAtMs > steeredPlan.createdAtMs) {
          steeredPlan = p;
        }
      }
    }
  }

  let steeringSummary = '';
  const taskSummaries: string[] = [];
  if (steeredPlan) {
    steeringSummary = steeredPlan.revisionReason
      || `Plan revised to r${steeredPlan.revisionIndex}`;
    // Stash new-task titles for the view to list. Keep the list short:
    // the detail panel is a peek surface, not the full plan diff.
    const maxTasks = 6;
    for (const t of steeredPlan.tasks.slice(0, maxTasks)) {
      const label = t.title || t.id || '';
      if (!label) continue;
      const flag =
        t.status === 'CANCELLED'
          ? 'cancelled: '
          : t.status === 'FAILED'
            ? 'failed: '
            : '';
      taskSummaries.push(`${flag}${label}`);
    }
    if (steeredPlan.tasks.length > maxTasks) {
      taskSummaries.push(`… +${steeredPlan.tasks.length - maxTasks} more`);
    }
  }

  return {
    spanId: span.id,
    eventId,
    recordedAtMs: span.startMs,
    model,
    elapsedMs,
    subjectAgentId,
    targetAgentId,
    taskId,
    verdictBucket,
    verdictTone,
    onTask,
    severity,
    reason,
    reasoningInput,
    rawResponse,
    parseSuccessful,
    inputPreview,
    outputPreview,
    decisionSummary,
    steeredPlan,
    steeringSummary,
    taskSummaries,
  };
}

// Returns true when the given span represents a goldfive LLM-as-judge
// invocation. Click handlers route to the judge detail card when this
// is true. The discriminator is an explicit `judge.kind = "judge"`
// attribute stamped by the harmonograf client sink when it translates
// a ReasoningJudgeInvoked event to a span (Option X, harmonograf#N).
// Do NOT match on the span name — display-name renames would silently
// break routing.
export function isJudgeSpan(span: Span | null | undefined): boolean {
  if (!span) return false;
  const attr = span.attributes['judge.kind'];
  if (attr && attr.kind === 'string' && attr.value === 'judge') return true;
  // Back-compat for spans produced before the `judge.kind` attribute
  // existed (pre-Option-X synthesizer named spans `judge: ...`). Keep
  // matching so older sessions replayed from the server don't lose the
  // routing.
  return span.agentId === '__goldfive__' && span.name.startsWith('judge:');
}

// Resolve detail for a task cancellation (terminal status + cancelReason).
// Surfaced when the task-delta list row is selected; the "steering" text
// is the structured cancel reason itself.
export function resolveTaskCancelDetail(task: Task): InterventionDetail {
  if (task.status !== 'CANCELLED' && task.status !== 'FAILED') {
    return { trigger: '', steering: '', targetAgentId: '', targetTaskId: '' };
  }
  return {
    trigger: '',
    steering: task.cancelReason || '',
    targetAgentId: task.assigneeAgentId || '',
    targetTaskId: task.id,
  };
}
