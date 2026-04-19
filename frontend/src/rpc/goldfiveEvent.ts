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
import type { TaskPlan, Task, TaskStatus } from '../gantt/types';
import type {
  Plan as GoldfivePlan,
  Task as GoldfiveTask,
} from '../pb/goldfive/v1/types_pb.js';
import {
  DriftKind as GoldfiveDriftKindEnum,
  DriftSeverity as GoldfiveDriftSeverityEnum,
} from '../pb/goldfive/v1/types_pb.js';
import type { Event as GoldfiveEvent } from '../pb/goldfive/v1/events_pb.js';

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
    case 'taskProgress':
    case 'driftDetected':
    case 'runStarted':
    case 'goalDerived':
    case 'runCompleted':
    case 'runAborted':
      // No-op. task_progress / drift / run_* are observable on the wire
      // (useful for analytics or a future drift timeline), but the
      // Gantt renderer does not consume them today.
      return;
  }
}
