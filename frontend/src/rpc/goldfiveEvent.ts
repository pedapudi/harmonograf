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
): void {
  const spanKind: SpanKind = actorId === USER_ACTOR_ID ? 'USER_MESSAGE' : 'CUSTOM';
  const id = `drift-${actorId}-${recordedAtMs}-${kind}`;
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
    attributes: {
      'drift.kind': { kind: 'string', value: kind },
      'drift.severity': { kind: 'string', value: severity },
      'drift.detail': { kind: 'string', value: detail },
      'drift.target_task_id': { kind: 'string', value: taskId },
      'drift.target_agent_id': { kind: 'string', value: targetAgentId },
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
        store.tasks.upsertPlan(convertGoldfivePlan(plan, sessionStartMs));
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
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'FAILED');
      return;
    case 'taskBlocked':
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'BLOCKED');
      return;
    case 'taskCancelled':
      store.tasks.updateTaskStatusByTaskId(payload.value.taskId, 'CANCELLED');
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
      const emittedMs = event.emittedAt
        ? Number(event.emittedAt.seconds) * 1000 +
          Math.floor(event.emittedAt.nanos / 1_000_000) -
          sessionStartMs
        : 0;
      const kindStr = driftKindToString(d.kind as unknown as number);
      const sevStr = driftSeverityToString(d.severity as unknown as number);
      store.drifts.append({
        kind: kindStr,
        severity: sevStr,
        detail: d.detail,
        taskId: d.currentTaskId,
        agentId: d.currentAgentId,
        recordedAtMs: emittedMs,
        // goldfive#176: user-control drifts carry the source annotation_id
        // so harmonograf#75's deduper can merge them into the annotation
        // row. Empty string for autonomous drifts.
        annotationId: d.annotationId || '',
        // goldfive#199 / harmonograf#99: goldfive-minted drift id,
        // always non-empty. Used as the strict join key when merging a
        // subsequent PlanRevised (whose trigger_event_id == this id)
        // onto the drift row.
        driftId: d.id || '',
      });
      // Attribute the drift to an actor row so it shows up in gantt / graph /
      // trajectory without those views having to special-case drift events.
      const actorId = USER_DRIFT_KINDS.has(kindStr)
        ? USER_ACTOR_ID
        : GOLDFIVE_ACTOR_ID;
      ensureSyntheticActor(store, actorId);
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
      );
      return;
    }
    case 'taskProgress':
    case 'runStarted':
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
    case 'delegationObserved': {
      // DelegationObserved carries the coordinator→sub_agent edge that the
      // telemetry plugin only records as a generic TOOL_CALL span on the
      // coordinator row. Feed the registry; the Gantt renderer's delegation
      // edge pass synthesizes a cross-agent curve from each record.
      const d = payload.value;
      const observedMs = event.emittedAt
        ? Number(event.emittedAt.seconds) * 1000 +
          Math.floor(event.emittedAt.nanos / 1_000_000) -
          sessionStartMs
        : 0;
      store.delegations.append({
        fromAgentId: d.fromAgent,
        toAgentId: d.toAgent,
        taskId: d.taskId,
        invocationId: d.invocationId,
        observedAtMs: observedMs,
      });
      return;
    }
  }
}
