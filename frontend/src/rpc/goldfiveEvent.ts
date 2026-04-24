// Goldfive event → SessionStore routing. Extracted from the WatchSession
// oneof dispatch in hooks.ts so the dispatch is testable without having
// to stand up the whole streaming hook (which would need a mocked
// Connect transport).
//
// The server delivers every plan/task/drift/run delta on the same
// `goldfive.v1.Event` envelope — both during the initial burst (replayed
// from persisted state) and on the live tail (fanned out from the Phase
// B bus). This module maps the envelope's payload oneof onto the
// existing gantt stores without introducing any new store concepts.

import type { SessionStore } from '../gantt/index';
import { bareAgentName } from '../gantt/index';
import type { Span, SpanKind, TaskPlan, Task, TaskStatus } from '../gantt/types';
import type {
  Plan as GoldfivePlan,
  Task as GoldfiveTask,
} from '../pb/goldfive/v1/types_pb.js';
import {
  DriftKind as GoldfiveDriftKindEnum,
  DriftSeverity as GoldfiveDriftSeverityEnum,
} from '../pb/goldfive/v1/types_pb.js';
import type { Event as GoldfiveEvent } from '../pb/goldfive/v1/events_pb.js';
import { useApprovalsStore } from '../state/approvalsStore';
import {
  USER_ACTOR_ID,
  GOLDFIVE_ACTOR_ID,
} from '../theme/agentColors';

// Drift kinds that represent a user-authored intervention. Anything else is
// a model-authored or executor-authored signal and is attributed to the
// goldfive orchestrator actor.
const USER_DRIFT_KINDS = new Set<string>([
  'user_steer',
  'user_cancel',
  'user_pause',
]);

function ensureSyntheticActor(store: SessionStore, actorId: string): void {
  if (store.agents.get(actorId)) return;
  store.agents.upsert({
    id: actorId,
    name: actorId === USER_ACTOR_ID ? 'user' : 'goldfive',
    framework: 'CUSTOM',
    capabilities: [],
    status: 'CONNECTED',
    // connectedAtMs drives row ordering. Setting it to a tiny positive
    // value (not zero, which would be 'not set') keeps real agents —
    // which connect later — below the actor rows.
    connectedAtMs: 1,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {
      'harmonograf.synthetic_actor': '1',
    },
  });
}

function synthesizeDriftSpan(
  store: SessionStore,
  sessionId: string | null,
  actorId: string,
  kind: string,
  severity: string,
  detail: string,
  taskId: string,
  targetAgentId: string,
  recordedAtMs: number,
  triggerInput?: string,
  authoredBy?: string,
  driftEventId?: string,
): void {
  const spanKind: SpanKind = actorId === USER_ACTOR_ID ? 'USER_MESSAGE' : 'CUSTOM';
  const id = `drift-${actorId}-${recordedAtMs}-${kind}`;
  const attributes: Span['attributes'] = {
    'drift.kind': { kind: 'string', value: kind },
    'drift.severity': { kind: 'string', value: severity },
    'drift.detail': { kind: 'string', value: detail },
    'drift.target_task_id': { kind: 'string', value: taskId },
    'drift.target_agent_id': { kind: 'string', value: targetAgentId },
    'harmonograf.synthetic_span': { kind: 'bool', value: true },
  };
  // harmonograf#196 forward-compat: goldfive#feat/judge-observability-events
  // adds DriftDetected.trigger_input — the reasoning snippet / activity
  // summary that triggered the judge. Carry it on the synthesized span as
  // a string attribute when present so the intervention detail panel can
  // surface richer context post-submodule-bump. Attribute is simply absent
  // (not empty-string) when the field is missing on the pre-merge wire.
  if (triggerInput) {
    attributes['drift.trigger_input'] = { kind: 'string', value: triggerInput };
  }
  // harmonograf forward-compat: goldfive's /tmp/goldfive-steer-unify branch
  // adds DriftDetected.authored_by ("user" / "goldfive" / ""). Pre-merge
  // the field is absent; surface it on the span so the intervention detail
  // pane can label rows as "Authored by: goldfive" vs "Authored by: user"
  // once the submodule bumps. Absent / empty-string ⇒ no attribute so the
  // UI hides the label.
  if (authoredBy) {
    attributes['drift.authored_by'] = { kind: 'string', value: authoredBy };
  }
  if (driftEventId) {
    attributes['drift.event_id'] = { kind: 'string', value: driftEventId };
  }
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: actorId,
    parentSpanId: null,
    kind: spanKind,
    status: 'COMPLETED',
    name: kind || 'drift',
    startMs: recordedAtMs,
    endMs: recordedAtMs,
    links: [],
    attributes,
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
}

// Synthesize a "refine" span on the goldfive actor row for each PlanRevised
// event so the Gantt + Trajectory views surface goldfive's decision-making
// as first-class activity — not just an implicit new plan revision. Pairs
// with the DriftDetected span (same row) and the forthcoming
// ReasoningJudgeInvoked span (same row) to build a readable timeline of
// orchestrator decisions.
//
// The span name reads `refine: <kind>` (or `refine: <reason prefix>`
// if the drift kind is empty). Target agent / task attribution is pulled
// from (a) the forward-compat ``targetAgentId`` field if present on the
// generated stubs, otherwise (b) the revisionKind alone — which the UI
// will treat as "refine with no explicit target" and skip the steering
// arrow for.
function synthesizeRefineSpan(
  store: SessionStore,
  sessionId: string | null,
  revisionIndex: number,
  revisionKind: string,
  revisionSeverity: string,
  revisionReason: string,
  recordedAtMs: number,
  targetAgentId: string,
  refineInputSummary: string,
  refineOutputSummary: string,
): void {
  const id = `refine-${recordedAtMs}-${revisionIndex}`;
  const nameCore = revisionKind || 'refine';
  const attributes: Span['attributes'] = {
    'refine.index': { kind: 'string', value: String(revisionIndex) },
    'refine.kind': { kind: 'string', value: revisionKind },
    'refine.severity': { kind: 'string', value: revisionSeverity },
    'refine.reason': { kind: 'string', value: revisionReason },
    'refine.target_agent_id': { kind: 'string', value: targetAgentId },
    'harmonograf.synthetic_span': { kind: 'bool', value: true },
  };
  if (refineInputSummary) {
    attributes['refine.input_summary'] = {
      kind: 'string',
      value: refineInputSummary,
    };
  }
  if (refineOutputSummary) {
    attributes['refine.output_summary'] = {
      kind: 'string',
      value: refineOutputSummary,
    };
  }
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: GOLDFIVE_ACTOR_ID,
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: `refine: ${nameCore}`,
    startMs: recordedAtMs,
    endMs: recordedAtMs,
    links: [],
    attributes,
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
}

// Synthesize a USER_MESSAGE span on the user actor row for each RunStarted
// event, carrying the goal summary as the span name. This is the best
// pre-merge substitute for a dedicated conversation-turn event —
// `RunStarted.goal_summary` is a single-line natural-language distillation
// of what the user asked for. When a richer per-turn event ships later
// the same row can absorb those too; the user-lane pattern already exists.
function synthesizeUserGoalSpan(
  store: SessionStore,
  sessionId: string | null,
  runId: string,
  goalSummary: string,
  recordedAtMs: number,
): void {
  if (!goalSummary) return;
  const id = `user-goal-${runId || recordedAtMs}`;
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: USER_ACTOR_ID,
    parentSpanId: null,
    kind: 'USER_MESSAGE',
    status: 'COMPLETED',
    name: goalSummary,
    startMs: recordedAtMs,
    endMs: recordedAtMs,
    links: [],
    attributes: {
      'user.goal_summary': { kind: 'string', value: goalSummary },
      'user.run_id': { kind: 'string', value: runId },
      'harmonograf.synthetic_span': { kind: 'bool', value: true },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
}

// Parameters for the judge-span synthesizer. Grouped into an options
// object so new forward-compat fields added on the wire (reason,
// reasoning_input, raw_response, elapsed_ms, model, subject_agent_id)
// don't balloon the positional arg list. All fields are optional —
// callers supply what the event carried; missing fields render as empty
// attributes so the detail panel can hide sections cleanly.
interface JudgeSpanInput {
  sessionId: string | null;
  eventId: string;
  recordedAtMs: number;
  // Parsed-verdict fields (post-merge ReasoningJudgeInvoked).
  onTask: boolean;
  verdict: string; // 'on_task' | 'drift' | '' when malformed
  severity: string;
  reason: string;
  // Raw judge payload + metadata.
  reasoningInput: string;
  rawResponse: string;
  elapsedMs: number;
  model: string;
  subjectAgentId: string;
  currentTaskId: string;
}

// Forward-compat: synthesize a "judge" span on the goldfive actor row
// when a goldfive.v1.ReasoningJudgeInvoked event arrives. The stub may
// not exist on the pre-merge submodule — callers guard the call path
// with a string-case check on the oneof so a missing case does not crash
// ingest. Once the submodule bumps the generated ``Event.payload.case``
// union includes 'reasoningJudgeInvoked' and TS narrows automatically.
//
// The span carries ``judge.kind = "judge"`` as an explicit discriminator
// so click handlers can route to JudgeInvocationDetail without having
// to match on the span name. All the event's structured fields land as
// string attributes on the span — the detail panel reads them back.
function synthesizeJudgeSpan(store: SessionStore, input: JudgeSpanInput): void {
  const {
    sessionId,
    eventId,
    recordedAtMs,
    onTask,
    verdict,
    severity,
    reason,
    reasoningInput,
    rawResponse,
    elapsedMs,
    model,
    subjectAgentId,
    currentTaskId,
  } = input;
  const id = `judge-${recordedAtMs}-${verdict || (onTask ? 'on_task' : 'unspec')}`;
  const name =
    verdict && verdict !== 'on_task'
      ? `judge: ${verdict}${severity ? ` (${severity})` : ''}`
      : 'judge: on_task';
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: GOLDFIVE_ACTOR_ID,
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name,
    startMs: recordedAtMs,
    endMs: recordedAtMs,
    links: [],
    attributes: {
      // Discriminator — read by click handlers to route to the judge
      // detail card. Backed by an explicit attribute (not the span name)
      // so renames to the display name don't accidentally break routing.
      'judge.kind': { kind: 'string', value: 'judge' },
      'judge.event_id': { kind: 'string', value: eventId },
      'judge.verdict': { kind: 'string', value: verdict },
      'judge.on_task': { kind: 'bool', value: onTask },
      'judge.severity': { kind: 'string', value: severity },
      'judge.reason': { kind: 'string', value: reason },
      'judge.reasoning_input': { kind: 'string', value: reasoningInput },
      'judge.raw_response': { kind: 'string', value: rawResponse },
      'judge.elapsed_ms': { kind: 'string', value: String(elapsedMs) },
      'judge.model': { kind: 'string', value: model },
      'judge.subject_agent_id': { kind: 'string', value: subjectAgentId },
      'judge.target_agent_id': { kind: 'string', value: subjectAgentId },
      'judge.target_task_id': { kind: 'string', value: currentTaskId },
      // Back-compat alias: pre-rework tests and the existing detail
      // resolvers still read ``judge.reasoning`` (judge's one-sentence
      // reason / explanation). Keep both until nothing reads it.
      'judge.reasoning': { kind: 'string', value: reason || reasoningInput },
      'harmonograf.synthetic_span': { kind: 'bool', value: true },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
}

// goldfive.v1.TaskStatus enum values are identical to harmonograf's
// TaskStatus strings at indices 0..5, plus BLOCKED = 6 (goldfive-only).
const TASK_STATUS_STRINGS: TaskStatus[] = [
  'UNSPECIFIED',
  'PENDING',
  'RUNNING',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'BLOCKED',
];

export function taskStatusFromInt(n: number): TaskStatus {
  return TASK_STATUS_STRINGS[n] ?? 'UNSPECIFIED';
}

function tsToMsAbs(t: { seconds: bigint; nanos: number } | undefined): number {
  if (!t) return 0;
  return Number(t.seconds) * 1000 + Math.floor(t.nanos / 1_000_000);
}

// DriftKind / DriftSeverity wire as enum ints; harmonograf persisted them
// (and the gantt TaskPlan carries them) as lowercase strings so existing
// chrome keeps rendering the same labels. UNSPECIFIED → empty string so
// the plan revision UI treats the initial (pre-drift) plan as "no kind".
function driftKindToString(n: number): string {
  const name = GoldfiveDriftKindEnum[n];
  if (!name || name === 'UNSPECIFIED') return '';
  return name.toLowerCase();
}

function driftSeverityToString(n: number): string {
  const name = GoldfiveDriftSeverityEnum[n];
  if (!name || name === 'UNSPECIFIED') return '';
  return name.toLowerCase();
}

export function convertGoldfiveTask(t: GoldfiveTask): Task {
  return {
    id: t.id,
    title: t.title,
    description: t.description,
    assigneeAgentId: t.assigneeAgentId,
    status: taskStatusFromInt(t.status as unknown as number),
    predictedStartMs: Number(t.predictedStartMs),
    predictedDurationMs: Number(t.predictedDurationMs),
    boundSpanId: t.boundSpanId ?? '',
  };
}

// goldfive.v1.Plan → gantt TaskPlan. The goldfive message has no
// invocationSpanId / plannerAgentId (those are harmonograf-specific
// session state, not in the goldfive event); they default to empty
// string so downstream consumers that optionally display them just
// render blanks.
export function convertGoldfivePlan(
  p: GoldfivePlan,
  sessionStartMs: number,
): TaskPlan {
  const createdAbs = tsToMsAbs(p.createdAt);
  return {
    id: p.id,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: createdAbs ? createdAbs - sessionStartMs : 0,
    summary: p.summary,
    tasks: p.tasks.map(convertGoldfiveTask),
    edges: p.edges.map((e) => ({
      fromTaskId: e.fromTaskId,
      toTaskId: e.toTaskId,
    })),
    revisionReason: p.revisionReason || '',
    revisionKind: driftKindToString(p.revisionKind as unknown as number),
    revisionSeverity: driftSeverityToString(
      p.revisionSeverity as unknown as number,
    ),
    revisionIndex: Number(p.revisionIndex ?? 0),
    // harmonograf#99 / goldfive#199: carry the trigger_event_id off the
    // wire so lib/interventions.ts can strict-id-merge plan revisions
    // onto their originating annotation or drift.
    triggerEventId: p.revisionTriggerEventId || '',
  };
}

// Dispatch one goldfive Event onto the session's stores. Called from the
// WatchSession hook for every `goldfive_event` oneof case. Task-status
// events carry only a task_id (not a plan_id) so they route through
// TaskRegistry.updateTaskStatusByTaskId, which scans every plan.
export function applyGoldfiveEvent(
  event: GoldfiveEvent,
  store: SessionStore,
  sessionStartMs: number,
  sessionId: string | null = null,
): void {
  const payload = event.payload;
  if (!payload.case) return;
  switch (payload.case) {
    case 'planSubmitted':
    case 'planRevised': {
      const plan = payload.value.plan;
      if (plan) {
        const converted = convertGoldfivePlan(plan, sessionStartMs);
        store.tasks.upsertPlan(converted);
        // harmonograf#133: seed the agent registry from plan content so
        // tasks whose assignee hasn't emitted a span yet still resolve
        // to a bare display name (e.g. `reviewer_agent`) instead of the
        // raw compound id (`<client>:reviewer_agent`). The registry was
        // previously populated only as spans arrived; agents listed in
        // the plan but not yet invoked had no row, so every display
        // resolver fell back to rendering the compound wire id.
        for (const task of converted.tasks) {
          const id = task.assigneeAgentId;
          if (!id) continue;
          store.agents.ensureAgent(id, bareAgentName(id));
        }
      }
      // harmonograf#196 Gantt lane: synthesize a "refine: <kind>" span on
      // the goldfive actor row for every PlanRevised event so the lane
      // reads as a readable orchestrator timeline (drift → refine → judge)
      // instead of only drift rows. Skip the initial plan (planSubmitted /
      // revision_index == 0) — that's the baseline, not a steering move.
      if (payload.case === 'planRevised') {
        const pr = payload.value;
        ensureSyntheticActor(store, GOLDFIVE_ACTOR_ID);
        const emittedAbsMs = tsToMsAbs(event.emittedAt);
        const emittedMs = emittedAbsMs ? emittedAbsMs - sessionStartMs : 0;
        const revKind = driftKindToString(pr.driftKind as unknown as number);
        const revSev = driftSeverityToString(pr.severity as unknown as number);
        // Forward-compat reads: goldfive#feat/judge-observability-events
        // adds PlanRevised.target_agent_id / refine_input_summary /
        // refine_output_summary. Pre-merge these fields are undefined on
        // the generated stubs, so read through `unknown` + a string guard
        // instead of relying on TS to know about them. When the stubs
        // regenerate with the new fields the reads land naturally.
        const pru = pr as unknown as Record<string, unknown>;
        const targetAgent =
          typeof pru.targetAgentId === 'string' ? (pru.targetAgentId as string) : '';
        const refineInput =
          typeof pru.refineInputSummary === 'string'
            ? (pru.refineInputSummary as string)
            : '';
        const refineOutput =
          typeof pru.refineOutputSummary === 'string'
            ? (pru.refineOutputSummary as string)
            : '';
        synthesizeRefineSpan(
          store,
          sessionId,
          pr.revisionIndex,
          revKind,
          revSev,
          pr.reason || '',
          emittedMs,
          targetAgent,
          refineInput,
          refineOutput,
        );
      }
      return;
    }
    case 'taskStarted':
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'RUNNING');
      return;
    case 'taskCompleted':
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'COMPLETED');
      return;
    case 'taskFailed':
      // harmonograf#110 / goldfive#205: carry the structured cancel
      // reason onto the task so the pre-strip tooltip, Drawer overview,
      // and Trajectory task-delta list can render "why?".
      store.tasks.updateTaskStatusByTaskId(
        payload.value.taskId,
        'FAILED',
        undefined,
        payload.value.reason || '',
      );
      return;
    case 'taskBlocked':
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'BLOCKED');
      return;
    case 'taskCancelled':
      store.tasks.updateTaskStatusByTaskId(
        payload.value.taskId,
        'CANCELLED',
        undefined,
        payload.value.reason || '',
      );
      return;
    case 'approvalRequested': {
      if (!sessionId) return;
      const r = payload.value;
      // Best-effort binding back to the originating span: if goldfive has
      // already emitted a TaskStarted for the same task_id, the plan's task
      // row has a boundSpanId set — use that agent/span so APPROVE/REJECT
      // routes to the exact executor. Falls back to empty strings when the
      // span isn't known yet; the server-side ControlChannel bridge accepts
      // session-level approvals without a specific agentId/spanId.
      const found = r.taskId ? store.tasks.findPlanForTask(r.taskId) : null;
      const boundSpanId = found?.task.boundSpanId ?? '';
      const span = boundSpanId ? store.spans.get(boundSpanId) : null;
      const requestedAtMs = event.emittedAt
        ? Number(event.emittedAt.seconds) * 1000 +
          Math.floor(event.emittedAt.nanos / 1_000_000) -
          sessionStartMs
        : 0;
      useApprovalsStore.getState().request({
        sessionId,
        targetId: r.targetId,
        kind: r.kind,
        prompt: r.prompt,
        taskId: r.taskId,
        metadata: { ...r.metadata },
        requestedAtMs,
        agentId: span?.agentId ?? found?.task.assigneeAgentId ?? '',
        spanId: boundSpanId,
      });
      return;
    }
    case 'approvalGranted':
    case 'approvalRejected': {
      if (!sessionId) return;
      useApprovalsStore
        .getState()
        .resolve(sessionId, payload.value.targetId);
      return;
    }
    case 'driftDetected': {
      const d = payload.value;
      // Store the authoritative wall-clock ms alongside the relative form.
      // If the live-path delivery races the 'session' SessionUpdate that
      // seeds `sessionStartMs`, the relative value here will be wall-clock
      // scale (garbage) — SessionStore.rebaseRelativeTimestamps is called
      // from the 'session' handler to rewrite it once the start is known.
      // See DriftRecord.recordedAtAbsoluteMs and harmonograf#127.
      const emittedAbsMs = tsToMsAbs(event.emittedAt);
      const emittedMs = emittedAbsMs ? emittedAbsMs - sessionStartMs : 0;
      const kindStr = driftKindToString(d.kind as unknown as number);
      const sevStr = driftSeverityToString(d.severity as unknown as number);
      // Forward-compat read for DriftDetected.authored_by
      // (goldfive /tmp/goldfive-steer-unify). Empty on pre-merge events.
      const duEarly = d as unknown as Record<string, unknown>;
      const authoredByForRecord =
        typeof duEarly.authoredBy === 'string' ? (duEarly.authoredBy as string) : '';
      store.drifts.append({
        kind: kindStr,
        severity: sevStr,
        detail: d.detail,
        taskId: d.currentTaskId,
        agentId: d.currentAgentId,
        recordedAtMs: emittedMs,
        recordedAtAbsoluteMs: emittedAbsMs,
        // goldfive#176: user-control drifts carry the source annotation_id
        // so harmonograf#75's deduper can merge them into the annotation
        // row. Empty string for autonomous drifts.
        annotationId: d.annotationId || '',
        // goldfive#199 / harmonograf#99: goldfive-minted drift id,
        // always non-empty. Used as the strict join key when merging a
        // subsequent PlanRevised (whose trigger_event_id == this id)
        // onto the drift row.
        driftId: d.id || '',
        authoredBy: authoredByForRecord,
      });
      // Attribute the drift to an actor row so it shows up in gantt / graph /
      // trajectory without those views having to special-case drift events.
      const actorId = USER_DRIFT_KINDS.has(kindStr)
        ? USER_ACTOR_ID
        : GOLDFIVE_ACTOR_ID;
      ensureSyntheticActor(store, actorId);
      // Forward-compat: goldfive#feat/judge-observability-events adds
      // DriftDetected.trigger_input (the reasoning snippet / activity
      // summary that produced the drift). Read through `unknown` + string
      // guard so ingest stays green on the pre-merge submodule pin. Once
      // the stubs regenerate the field lands naturally.
      const du = d as unknown as Record<string, unknown>;
      const triggerInput =
        typeof du.triggerInput === 'string' ? (du.triggerInput as string) : '';
      const authoredBy =
        typeof du.authoredBy === 'string' ? (du.authoredBy as string) : '';
      synthesizeDriftSpan(
        store,
        sessionId,
        actorId,
        kindStr,
        sevStr,
        d.detail,
        d.currentTaskId,
        d.currentAgentId,
        emittedMs,
        triggerInput,
        authoredBy,
        event.eventId || '',
      );
      return;
    }
    case 'runStarted': {
      // harmonograf#196 user lane: synthesize a USER_MESSAGE span on the
      // user actor row carrying `goal_summary` — the pre-merge substitute
      // for a per-turn user-prompt event. Once goldfive ships a dedicated
      // ConversationTurn event with per-turn user text, the same row can
      // absorb those too.
      const r = payload.value;
      if (r.goalSummary) {
        ensureSyntheticActor(store, USER_ACTOR_ID);
        const emittedAbsMs = tsToMsAbs(event.emittedAt);
        const emittedMs = emittedAbsMs ? emittedAbsMs - sessionStartMs : 0;
        synthesizeUserGoalSpan(
          store,
          sessionId,
          r.runId || '',
          r.goalSummary,
          emittedMs,
        );
      }
      return;
    }
    case 'taskProgress':
    case 'goalDerived':
    case 'runCompleted':
    case 'runAborted':
    case 'conversationStarted':
    case 'conversationEnded':
      return;
    // goldfive 2986775+ registry-dispatch events. agentInvocationStarted /
    // agentInvocationCompleted remain deliberate no-ops: the existing
    // HarmonografTelemetryPlugin already emits per-agent INVOCATION spans,
    // so Gantt / Trajectory see those invocations without duplication.
    case 'agentInvocationStarted':
    case 'agentInvocationCompleted':
      return;
    // harmonograf#196 forward-compat: goldfive#feat/judge-observability-events
    // adds a `reasoningJudgeInvoked` oneof case (the orchestrator's LLM-as-
    // judge fires on each reasoning step and classifies it on-task / drift).
    // The case string is matched as a wide string because the generated
    // union on the current submodule pin doesn't include it yet — once the
    // submodule bumps the `payload.case` type narrows automatically and this
    // branch becomes reachable through the normal switch.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    case 'reasoningJudgeInvoked' as any: {
      ensureSyntheticActor(store, GOLDFIVE_ACTOR_ID);
      const emittedAbsMs = tsToMsAbs(event.emittedAt);
      const emittedMs = emittedAbsMs ? emittedAbsMs - sessionStartMs : 0;
      const ju = payload.value as unknown as Record<string, unknown>;
      // `verdict` is a harmonograf-local string synthesized from
      // `on_task`/`severity` since the wire message doesn't expose a
      // single-string verdict field. Pre-merge test fixtures that set
      // ``verdict`` directly still flow through (the explicit string
      // wins over the on_task derivation).
      const onTaskRaw = ju.onTask;
      const onTask = typeof onTaskRaw === 'boolean' ? onTaskRaw : false;
      const severityRaw = ju.severity;
      const severity =
        typeof severityRaw === 'string'
          ? severityRaw
          : typeof severityRaw === 'number'
            ? driftSeverityToString(severityRaw)
            : '';
      let verdict = typeof ju.verdict === 'string' ? (ju.verdict as string) : '';
      if (!verdict) {
        verdict = onTask ? 'on_task' : severity ? severity : '';
      }
      // Pre-merge tests pass `reasoning` as the single reasoning string;
      // post-merge the wire separates `reasoning_input` (what the judge
      // saw) from `reason` (the judge's explanation). Accept both and
      // prefer the richer pair when both are set.
      const reasoning =
        typeof ju.reasoning === 'string' ? (ju.reasoning as string) : '';
      const reason = typeof ju.reason === 'string' ? (ju.reason as string) : reasoning;
      const reasoningInput =
        typeof ju.reasoningInput === 'string'
          ? (ju.reasoningInput as string)
          : reasoning;
      const rawResponse =
        typeof ju.rawResponse === 'string' ? (ju.rawResponse as string) : '';
      const elapsedRaw = ju.elapsedMs;
      let elapsedMs = 0;
      if (typeof elapsedRaw === 'number') elapsedMs = elapsedRaw;
      else if (typeof elapsedRaw === 'bigint') elapsedMs = Number(elapsedRaw);
      const model = typeof ju.model === 'string' ? (ju.model as string) : '';
      // Post-merge field is `subject_agent_id`; pre-merge test fixtures
      // used `currentAgentId`. Read both so neither wire shape breaks.
      const subjectAgentId =
        typeof ju.subjectAgentId === 'string'
          ? (ju.subjectAgentId as string)
          : typeof ju.currentAgentId === 'string'
            ? (ju.currentAgentId as string)
            : '';
      const currentTaskId =
        typeof ju.taskId === 'string'
          ? (ju.taskId as string)
          : typeof ju.currentTaskId === 'string'
            ? (ju.currentTaskId as string)
            : '';
      synthesizeJudgeSpan(store, {
        sessionId,
        eventId: event.eventId || '',
        recordedAtMs: emittedMs,
        onTask,
        verdict,
        severity,
        reason,
        reasoningInput,
        rawResponse,
        elapsedMs,
        model,
        subjectAgentId,
        currentTaskId,
      });
      return;
    }
    case 'delegationObserved': {
      // DelegationObserved carries the coordinator→sub_agent edge that the
      // telemetry plugin only records as a generic TOOL_CALL span on the
      // coordinator row. Feed the registry; the Gantt renderer's delegation
      // edge pass synthesizes a cross-agent curve from each record.
      //
      // Store both the authoritative wall-clock ms and the relative form.
      // harmonograf#127: on the live path, this event can arrive BEFORE the
      // 'session' SessionUpdate has set `store.wallClockStartMs`, so
      // `sessionStartMs` here is 0 and the naive subtract stamps the
      // record with wall-clock-scale observedAtMs — which made the Gantt
      // / Graph arrows land miles off-axis. The refresh path worked only
      // because the server's initial burst orders 'session' first. When
      // the 'session' case fires, hooks.ts now calls
      // `store.rebaseRelativeTimestamps(startMs)` which walks the
      // delegation registry and rewrites observedAtMs from the absolute.
      const d = payload.value;
      const observedAbsMs = tsToMsAbs(event.emittedAt);
      const observedMs = observedAbsMs ? observedAbsMs - sessionStartMs : 0;
      store.delegations.append({
        fromAgentId: d.fromAgent,
        toAgentId: d.toAgent,
        taskId: d.taskId,
        invocationId: d.invocationId,
        observedAtMs: observedMs,
        observedAtAbsoluteMs: observedAbsMs,
      });
      return;
    }
  }
}
