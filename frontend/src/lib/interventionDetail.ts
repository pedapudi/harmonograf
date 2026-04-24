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
  return {
    trigger: triggerParts.join('\n\n'),
    steering: steeringText,
    targetAgentId,
    targetTaskId,
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
