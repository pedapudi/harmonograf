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
//
// Note (Option X, harmonograf#N): goldfive LLM-call events
// (goldfive_llm_call_start, goldfive_llm_call_end, reasoning_judge_invoked)
// are now translated to SpanStart/SpanEnd at the harmonograf client
// sink (see harmonograf_client/sink.py). They arrive as ordinary spans
// via the span transport and are rendered by the normal span-ingest
// path; no frontend-side synthesis needed. If you see a synthesize*Span
// helper in this file, it's probably for user-lane synthesis (user
// goal message from RunStarted) or the drift/refine visual-marker
// synthesis — NOT part of Option X, those stay.

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
import type {
  Event as GoldfiveEvent,
  InvocationCancelled as InvocationCancelledPb,
  TaskTransitioned as TaskTransitionedPb,
} from '../pb/goldfive/v1/events_pb.js';
import type {
  RefineAttempted as RefineAttemptedPb,
  RefineFailed as RefineFailedPb,
  UserMessageReceived as UserMessageReceivedPb,
} from '../pb/harmonograf/v1/telemetry_pb.js';
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

// Resolve the id the goldfive synthetic-actor row should use. For the
// user actor this is always the legacy `__user__` constant. For the
// goldfive actor, if a sink-translated `<client>:goldfive` row has
// already been registered (via span-ingest or Hello), synthesis lands
// on that compound id so we don't double-render the orchestrator. Else
// we fall back to the legacy `__goldfive__` constant; a subsequent
// compound row arriving later triggers SessionStore.mergeGoldfiveAlias
// which collapses the legacy row into it.
function resolveActorId(store: SessionStore, actorId: string): string {
  if (actorId !== GOLDFIVE_ACTOR_ID) return actorId;
  return store.resolveGoldfiveActorId(GOLDFIVE_ACTOR_ID);
}

function ensureSyntheticActor(store: SessionStore, actorId: string): string {
  const resolved = resolveActorId(store, actorId);
  if (store.agents.get(resolved)) return resolved;
  store.agents.upsert({
    id: resolved,
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
  // Collapse any legacy `__goldfive__` row we may have created earlier
  // — if the resolver returned a compound id, this is a no-op; if it
  // returned the legacy id and a compound row arrives later, the
  // upsert path will re-run mergeGoldfiveAlias.
  if (actorId === GOLDFIVE_ACTOR_ID) {
    store.mergeGoldfiveAlias(GOLDFIVE_ACTOR_ID);
  }
  return resolved;
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
  // `actorId` may be the legacy `__goldfive__` / `__user__` constant or
  // (post-goldfive-unify) the compound `<client>:goldfive` id returned
  // by ensureSyntheticActor. `spanKind` / id prefix still hinge on the
  // semantic distinction between user vs goldfive, so branch on whether
  // the id is the user constant.
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
  goldfiveActorId: string,
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
    agentId: goldfiveActorId,
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
    // goldfive#237: authoritative supersession link set by the refine LLM
    // on replacement tasks. Defaults to '' on original / legacy plans.
    supersedes: t.supersedes ?? '',
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
        // Plan-history accumulator: append this revision to the
        // per-plan registry so the Task/Plan panel + Trajectory view
        // can render cumulative / per-rev / supersedes views. The
        // registry dedups on (plan_id, revision_number), so this is
        // safe to call on stream reconnect replays too.
        const emittedAbsMsHist = tsToMsAbs(event.emittedAt);
        store.planHistory.append({
          revision: Number(plan.revisionIndex ?? 0),
          plan: converted,
          reason: plan.revisionReason || '',
          kind: driftKindToString(plan.revisionKind as unknown as number),
          triggerEventId: plan.revisionTriggerEventId || '',
          emittedAtMs: emittedAbsMsHist,
        });
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
        const goldfiveId = ensureSyntheticActor(store, GOLDFIVE_ACTOR_ID);
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
          goldfiveId,
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
      const isUserDrift = USER_DRIFT_KINDS.has(kindStr);
      const resolvedActorId = ensureSyntheticActor(
        store,
        isUserDrift ? USER_ACTOR_ID : GOLDFIVE_ACTOR_ID,
      );
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
        resolvedActorId,
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
    // Option X (harmonograf#N): reasoningJudgeInvoked is now translated
    // to SpanStart/SpanEnd at the harmonograf client sink (see
    // harmonograf_client/sink.py). Judge spans arrive via the normal
    // span transport; no frontend-side synthesis needed. Treat as a
    // no-op if one reaches here (e.g. replay from an old-format
    // recording that still used the goldfive-event channel).
    case 'reasoningJudgeInvoked':
      return;
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
    case 'invocationCancelled': {
      // Operator-observability marker (goldfive#262 / Stream C of #251).
      // Promoted from a dict envelope (harmonograf PR#187 ingested it via
      // a placeholder ``harmonograf.v1.InvocationCancelled`` carried on
      // its own ``SessionUpdate`` oneof slot) to a typed
      // ``goldfive.v1.InvocationCancelled`` payload variant on the
      // standard ``Event`` envelope. Envelope metadata (run_id,
      // emitted_at) reads off the parent ``event``; the payload carries
      // the cancel-specific fields.
      applyInvocationCancelled(payload.value, event, store, sessionStartMs, sessionId);
      return;
    }
    case 'taskTransitioned': {
      // Operator-observability marker (goldfive#267 / #251 R4). Every
      // plan-state transition emits a TaskTransitioned record with
      // source attribution; the deriver in ``lib/interventions.ts``
      // filters down to the user-meaningful subset (terminal to_status
      // + meaningful source) before surfacing as an intervention row.
      // We do NOT synthesize a span on Gantt / Graph — these events are
      // too fine-grained for those views; the intervention list is
      // their only surface.
      applyTaskTransitioned(payload.value, event, store, sessionStartMs);
      return;
    }
  }
}

// Synthesize a cancel marker span on the cancelled agent's lane so the
// Gantt renderer shows a stop glyph at the cancellation time without
// special-casing the cancel registry in the renderer. The span name
// reads "cancelled: <reason>" so the default Gantt label path reads
// naturally, and ``harmonograf.cancel_marker = true`` lets downstream
// consumers (custom renderers) pick these out if needed.
//
// Same pattern as synthesizeDriftSpan (drifts land as synthesized
// spans on the __goldfive__ row). The difference: cancels land on the
// CANCELLED AGENT's row, not on the goldfive row — because a cancel is
// an event that happened to a specific agent's invocation, and the UI
// reads better when the marker sits on that lane.
function synthesizeCancelSpan(
  store: SessionStore,
  sessionId: string | null,
  agentId: string,
  reason: string,
  severity: string,
  driftKind: string,
  detail: string,
  toolName: string,
  invocationId: string,
  driftId: string,
  recordedAtMs: number,
): void {
  if (!agentId) return;
  const id = `cancel-${agentId}-${recordedAtMs}-${invocationId || 'x'}`;
  const attributes: Span['attributes'] = {
    'cancel.reason': { kind: 'string', value: reason },
    'cancel.severity': { kind: 'string', value: severity },
    'cancel.drift_kind': { kind: 'string', value: driftKind },
    'cancel.detail': { kind: 'string', value: detail },
    'cancel.invocation_id': { kind: 'string', value: invocationId },
    'cancel.drift_id': { kind: 'string', value: driftId },
    'harmonograf.synthetic_span': { kind: 'bool', value: true },
    'harmonograf.cancel_marker': { kind: 'bool', value: true },
  };
  if (toolName) {
    attributes['cancel.tool_name'] = { kind: 'string', value: toolName };
  }
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId,
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: reason ? `cancelled: ${reason}` : 'cancelled',
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

// Operator-observability: an InvocationCancelled payload landed on the
// WatchSession stream as part of a ``goldfive.v1.Event`` envelope
// (goldfive#262). The cancel was previously delivered on a dedicated
// ``SessionUpdate.invocation_cancelled`` slot carrying a placeholder
// ``harmonograf.v1.InvocationCancelled`` message (harmonograf PR#187),
// because the goldfive proto envelope had no ``InvocationCancelled``
// variant yet. With the typed promotion both routes converge on this
// helper — it is now invoked from the ``invocationCancelled`` case in
// :func:`applyGoldfiveEvent`, and reads envelope metadata (``run_id``,
// ``emitted_at``) off the parent ``Event`` rather than the payload.
//
// Translated straight into an InvocationCancelRecord on the session
// store so the Trajectory / Gantt / Graph views can render a cancel
// marker without crawling spans.
//
// Agent-agnostic: ``agentId`` is read verbatim from the payload (already
// canonicalized <client>:<bare> by the client sink). No special-casing
// of any particular agent ("coordinator", etc.) — the renderer looks
// up the agent's lane/lifeline by id.
export function applyInvocationCancelled(
  payload: InvocationCancelledPb,
  event: GoldfiveEvent,
  store: SessionStore,
  sessionStartMs: number,
  sessionId: string | null = null,
): void {
  const recordedAbsMs = tsToMsAbs(event.emittedAt);
  // When the envelope didn't carry emitted_at (shouldn't happen on a
  // well-formed event but tolerated for replays of legacy recordings),
  // fall back to "now" so the marker renders at the ingest moment
  // rather than the session start.
  const abs = recordedAbsMs || Date.now();
  const recordedMs = abs - sessionStartMs;
  const agentId = payload.agentName || '';
  store.invocationCancels.append({
    runId: event.runId || '',
    invocationId: payload.invocationId || '',
    agentId,
    reason: payload.reason || '',
    severity: payload.severity || '',
    driftId: payload.driftId || '',
    driftKind: payload.driftKind || '',
    detail: payload.detail || '',
    toolName: payload.toolName || '',
    recordedAtMs: recordedMs,
    recordedAtAbsoluteMs: abs,
  });
  // Ensure the cancelled-agent's row exists so the Gantt lane renders
  // even if no spans have been emitted for that agent yet (the cancel
  // could have fired at the before_agent_callback checkpoint, before
  // the first span). Bare name is derived from the compound id so the
  // row label reads cleanly — the agent may be an ADK sub-agent whose
  // real framework-emitted spans land via the normal plugin path.
  if (agentId) {
    store.agents.ensureAgent(agentId, bareAgentName(agentId));
  }
  synthesizeCancelSpan(
    store,
    sessionId,
    agentId,
    payload.reason || '',
    payload.severity || '',
    payload.driftKind || '',
    payload.detail || '',
    payload.toolName || '',
    payload.invocationId || '',
    payload.driftId || '',
    recordedMs,
  );
}

// Synthesize a failed-refine marker span on the goldfive actor row. Same
// pattern as ``synthesizeRefineSpan`` (which lands on plan_revised) but
// rendered with a distinct ``harmonograf.refine_failed = true`` flag and
// a ``refine.failure_kind`` attribute so the Gantt renderer can pick the
// failed-refine glyph (a crossed-out refine symbol in #179's lifeline
// glyph infrastructure) without scanning per-span. Lives on the goldfive
// lane next to the drift / successful-refine spans so the lane reads as
// a chronological timeline of orchestrator decisions.
//
// ``attemptId`` is stamped as an attribute so the click-through detail
// pane can correlate the synthesized span back to the failure record on
// :class:`RefineFailureRegistry`.
function synthesizeFailedRefineSpan(
  store: SessionStore,
  sessionId: string | null,
  attemptId: string,
  driftId: string,
  triggerKind: string,
  triggerSeverity: string,
  failureKind: string,
  reason: string,
  detail: string,
  recordedAtMs: number,
  goldfiveActorId: string,
): void {
  const id = `refine-failed-${attemptId || `${recordedAtMs}-${driftId || 'x'}`}`;
  const attributes: Span['attributes'] = {
    'refine.attempt_id': { kind: 'string', value: attemptId },
    'refine.drift_id': { kind: 'string', value: driftId },
    'refine.trigger_kind': { kind: 'string', value: triggerKind },
    'refine.trigger_severity': { kind: 'string', value: triggerSeverity },
    'refine.failure_kind': { kind: 'string', value: failureKind },
    'refine.reason': { kind: 'string', value: reason },
    'refine.detail': { kind: 'string', value: detail },
    'harmonograf.synthetic_span': { kind: 'bool', value: true },
    // Flag the renderer reads to pick the failed-refine glyph variant.
    // Distinct from ``harmonograf.cancel_marker`` (cancel uses a stop
    // glyph; failed refine uses a crossed-out refine glyph).
    'harmonograf.refine_failed': { kind: 'bool', value: true },
  };
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: goldfiveActorId,
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: failureKind ? `refine failed: ${failureKind}` : 'refine failed',
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

// Operator-observability: a ``RefineAttempted`` envelope landed on the
// WatchSession stream (goldfive#264). Append to the per-session
// :class:`RefineAttemptRegistry` so the deriver in
// ``lib/interventions.ts`` can correlate the attempt with its terminal
// counterpart (a successful ``PlanRevised`` or a ``RefineFailed``)
// by ``attemptId``.
//
// We deliberately do NOT synthesize a span here. The successful-refine
// path already mints a span via ``synthesizeRefineSpan`` on
// ``plan_revised``; the failed-refine path mints one via
// ``synthesizeFailedRefineSpan`` on ``refine_failed``. Synthesizing a
// span on every attempted-event would double-render the successful
// refines (one before, one after the terminal arrives).
export function applyRefineAttempted(
  event: RefineAttemptedPb,
  store: SessionStore,
  sessionStartMs: number,
): void {
  const recordedAbsMs = tsToMsAbs(event.emittedAt);
  const abs = recordedAbsMs || Date.now();
  const recordedMs = abs - sessionStartMs;
  store.refineAttempts.append({
    runId: event.runId || '',
    attemptId: event.attemptId || '',
    driftId: event.driftId || '',
    triggerKind: (event.triggerKind || '').toLowerCase(),
    triggerSeverity: (event.triggerSeverity || '').toLowerCase(),
    taskId: event.currentTaskId || '',
    agentId: event.currentAgentId || '',
    recordedAtMs: recordedMs,
    recordedAtAbsoluteMs: abs,
  });
}

// Operator-observability: a ``RefineFailed`` envelope landed on the
// WatchSession stream (goldfive#264). Append to the per-session
// :class:`RefineFailureRegistry` AND synthesize a failed-refine marker
// span on the goldfive actor row so the Gantt + Graph views render the
// failure as a first-class glyph (rather than only a row in the
// intervention list). Pairs with the existing
// :func:`synthesizeRefineSpan` on the success path.
export function applyRefineFailed(
  event: RefineFailedPb,
  store: SessionStore,
  sessionStartMs: number,
  sessionId: string | null = null,
): void {
  const recordedAbsMs = tsToMsAbs(event.emittedAt);
  const abs = recordedAbsMs || Date.now();
  const recordedMs = abs - sessionStartMs;
  const triggerKind = (event.triggerKind || '').toLowerCase();
  const triggerSeverity = (event.triggerSeverity || '').toLowerCase();
  const failureKind = (event.failureKind || '').toLowerCase();
  store.refineFailures.append({
    runId: event.runId || '',
    attemptId: event.attemptId || '',
    driftId: event.driftId || '',
    triggerKind,
    triggerSeverity,
    failureKind,
    reason: event.reason || '',
    detail: event.detail || '',
    taskId: event.currentTaskId || '',
    agentId: event.currentAgentId || '',
    recordedAtMs: recordedMs,
    recordedAtAbsoluteMs: abs,
  });
  // Synthesize the marker span on the goldfive actor row so the Gantt
  // + Graph views surface the failure on the orchestrator lane next to
  // its drift + successful-refine siblings.
  const goldfiveId = ensureSyntheticActor(store, GOLDFIVE_ACTOR_ID);
  synthesizeFailedRefineSpan(
    store,
    sessionId,
    event.attemptId || '',
    event.driftId || '',
    triggerKind,
    triggerSeverity,
    failureKind,
    event.reason || '',
    event.detail || '',
    recordedMs,
    goldfiveId,
  );
}

// Synthesize a USER_MESSAGE span on the user actor row carrying the
// verbatim operator text. The span name is the message body (clipped
// to 120 chars for tooltip readability); the full text rides on
// ``user.content`` for the detail panel. Distinct from
// :func:`synthesizeUserGoalSpan` (which renders the RunStarted goal
// summary): that's goldfive's distillation; this is the raw input.
function synthesizeUserMessageSpan(
  store: SessionStore,
  sessionId: string | null,
  recordedAtMs: number,
  content: string,
  author: string,
  midTurn: boolean,
  invocationId: string,
): void {
  if (!content) return;
  const id = `user-msg-${recordedAtMs}-${author || 'user'}`;
  const headline = content.length > 120 ? content.slice(0, 117) + '…' : content;
  const span: Span = {
    id,
    sessionId: sessionId ?? '',
    agentId: USER_ACTOR_ID,
    parentSpanId: null,
    kind: 'USER_MESSAGE',
    status: 'COMPLETED',
    name: headline,
    startMs: recordedAtMs,
    endMs: recordedAtMs,
    links: [],
    attributes: {
      'user.content': { kind: 'string', value: content },
      'user.author': { kind: 'string', value: author || 'user' },
      'user.mid_turn': { kind: 'bool', value: midTurn },
      'user.invocation_id': {
        kind: 'string',
        value: invocationId || '',
      },
      'harmonograf.synthetic_span': { kind: 'bool', value: true },
      // Renderer flag: pick the user-message glyph variant on the
      // user lane instead of the generic span style.
      'harmonograf.user_message_marker': { kind: 'bool', value: true },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
}

// Operator-observability: a ``UserMessageReceived`` envelope landed
// on the WatchSession stream (harmonograf user-message UX gap).
// Append to the per-session :class:`UserMessageRegistry` so the
// deriver in ``lib/interventions.ts`` can surface the operator's
// words as a first-class intervention row, AND synthesize a
// USER_MESSAGE span on the user actor row so Gantt + Graph render
// the marker without crawling the registry directly.
export function applyUserMessage(
  event: UserMessageReceivedPb,
  store: SessionStore,
  sessionStartMs: number,
  sessionId: string | null = null,
): void {
  const recordedAbsMs = tsToMsAbs(event.emittedAt);
  const abs = recordedAbsMs || Date.now();
  const recordedMs = abs - sessionStartMs;
  const content = event.content || '';
  const author = event.author || 'user';
  const midTurn = Boolean(event.midTurn);
  const invocationId = event.invocationId || '';
  store.userMessages.append({
    runId: event.runId || '',
    content,
    author,
    midTurn,
    invocationId,
    recordedAtMs: recordedMs,
    recordedAtAbsoluteMs: abs,
  });
  // Ensure the user actor row exists so the Gantt lane renders
  // even on a fresh stream that hasn't fired runStarted yet.
  ensureSyntheticActor(store, USER_ACTOR_ID);
  synthesizeUserMessageSpan(
    store,
    sessionId,
    recordedMs,
    content,
    author,
    midTurn,
    invocationId,
  );
}

// Operator-observability: a ``TaskTransitioned`` payload landed on the
// WatchSession stream as part of a ``goldfive.v1.Event`` envelope
// (goldfive#267 / #251 R4). Append to the per-session
// :class:`TaskTransitionRegistry` so the deriver in
// ``lib/interventions.ts`` can pick the user-meaningful subset.
//
// We deliberately do NOT synthesize a span on any actor row. These
// events are fine-grained (every plan-state transition emits one); the
// intervention list is the only surface. Adding glyphs to Gantt /
// Graph would be visual noise. If operators want them on Gantt later
// that's a separate UX decision.
//
// We also do NOT mutate task status here — the existing TaskStarted /
// TaskCompleted / TaskFailed / TaskCancelled handlers above are still
// the authoritative path for the gantt task store. TaskTransitioned is
// a *parallel* observability stream alongside those, not a replacement.
export function applyTaskTransitioned(
  payload: TaskTransitionedPb,
  event: GoldfiveEvent,
  store: SessionStore,
  sessionStartMs: number,
): void {
  const recordedAbsMs = tsToMsAbs(event.emittedAt);
  // When emitted_at is missing on a replay of legacy data, fall back to
  // "now" so the record still has a usable timestamp to render.
  const abs = recordedAbsMs || Date.now();
  const recordedMs = abs - sessionStartMs;
  store.taskTransitions.append({
    runId: event.runId || '',
    sequence: Number(event.sequence ?? 0n),
    taskId: payload.taskId || '',
    // ``from_status`` / ``to_status`` arrive as bare uppercase strings on
    // the wire (per goldfive#267 events.proto comment). Coerce
    // defensively in case a non-conforming emitter ships lowercase.
    fromStatus: (payload.fromStatus || '').toUpperCase(),
    toStatus: (payload.toStatus || '').toUpperCase(),
    source: payload.source || '',
    revisionStamp: payload.revisionStamp || 0,
    agentName: payload.agentName || '',
    invocationId: payload.invocationId || '',
    recordedAtMs: recordedMs,
    recordedAtAbsoluteMs: abs,
  });
}
