// harmonograf#197 — judge-detail resolver + judge-span discriminator.
//
// Integration-flavoured: feeds a ReasoningJudgeInvoked event through
// applyGoldfiveEvent, then queries the synthesized span and runs it
// through resolveJudgeDetail. Verifies the six sections (header meta,
// context, reasoning input, verdict, raw response, steering outcome)
// land correctly, plus the steered-plan lookup when a PlanRevised's
// trigger_event_id matches the judge event id.

import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  PlanSubmittedSchema,
  PlanRevisedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  DriftKind,
  DriftSeverity,
  TaskStatus,
} from '../../pb/goldfive/v1/types_pb';
import { isJudgeSpan, resolveJudgeDetail } from '../../lib/interventionDetail';
import { GOLDFIVE_ACTOR_ID } from '../../theme/agentColors';
import type { Span, TaskPlan } from '../../gantt/types';

function ts(seconds: number) {
  return create(TimestampSchema, { seconds: BigInt(seconds), nanos: 0 });
}

function pbTask(id: string, agent = 'agent-a') {
  return create(TaskSchema, {
    id,
    title: `task ${id}`,
    description: '',
    assigneeAgentId: agent,
    status: TaskStatus.PENDING,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function pbPlan(id: string, taskIds: string[], revisionIndex = 0) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: `plan ${id}`,
    tasks: taskIds.map((t) => pbTask(t)),
    edges: [],
    revisionReason: '',
    revisionKind: DriftKind.UNSPECIFIED,
    revisionSeverity: DriftSeverity.UNSPECIFIED,
    revisionIndex,
  });
}

// ReasoningJudgeInvoked doesn't have a bufbuild schema import in this test
// file (the test lives alongside interventionDetail.test.ts which hand-
// builds the judge payload via the oneof's string-case escape hatch).
// We do the same here so the test is tolerant of pre-merge stubs.
function judgeEvent(
  eventId: string,
  atSeconds: number,
  fields: Record<string, unknown>,
) {
  const event = create(EventSchema, {
    eventId,
    runId: 'run-1',
    sequence: 0n,
    emittedAt: ts(atSeconds),
  });
  (event as unknown as Record<string, unknown>).payload = {
    case: 'reasoningJudgeInvoked',
    value: fields,
  };
  return event;
}

function listJudgeSpans(store: SessionStore): Span[] {
  const spans: Span[] = [];
  store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans);
  return spans.filter((s) => isJudgeSpan(s));
}

function collectAllPlans(store: SessionStore): TaskPlan[] {
  const out: TaskPlan[] = [];
  const seen = new Set<TaskPlan>();
  for (const live of store.tasks.listPlans()) {
    for (const snap of store.tasks.allRevsForPlan(live.id)) {
      if (seen.has(snap)) continue;
      seen.add(snap);
      out.push(snap);
    }
  }
  return out;
}

describe('judge-span routing (harmonograf#197)', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('synthesized judge span carries kind=judge as a discriminator', () => {
    applyGoldfiveEvent(
      judgeEvent('ev-judge', 30, {
        onTask: true,
        reason: 'looks good',
        reasoningInput: 'the agent said: I will search',
        rawResponse: '{"on_task": true, "reason": "looks good"}',
        model: 'haiku',
        elapsedMs: 200,
        subjectAgentId: 'agent-a',
        taskId: 't1',
      }),
      store,
      0,
    );
    const judges = listJudgeSpans(store);
    expect(judges).toHaveLength(1);
    expect(judges[0].attributes['judge.kind']).toEqual({
      kind: 'string',
      value: 'judge',
    });
    expect(judges[0].attributes['judge.event_id']).toEqual({
      kind: 'string',
      value: 'ev-judge',
    });
    expect(isJudgeSpan(judges[0])).toBe(true);
  });

  it('isJudgeSpan returns false for non-judge spans', () => {
    // Seed a regular drift span on the goldfive lane.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-drift',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(10),
        payload: {
          case: 'driftDetected',
          value: {
            kind: DriftKind.LOOPING_REASONING,
            severity: DriftSeverity.WARNING,
            detail: 'loop',
            currentTaskId: 't1',
            currentAgentId: 'agent-a',
            id: 'd-1',
          },
        },
      }),
      store,
      0,
    );
    const spans: Span[] = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans);
    // At least the drift span should be present; none are judge spans.
    expect(spans.length).toBeGreaterThan(0);
    for (const s of spans) expect(isJudgeSpan(s)).toBe(false);
  });

  it('resolveJudgeDetail surfaces the header + context + verdict for on_task', () => {
    applyGoldfiveEvent(
      judgeEvent('ev-j1', 45, {
        onTask: true,
        severity: '',
        reason: 'on track',
        reasoningInput: 'I will search',
        rawResponse: '{"on_task": true}',
        model: 'haiku',
        elapsedMs: 175,
        subjectAgentId: 'agent-a',
        taskId: 't1',
      }),
      store,
      0,
    );
    const judge = listJudgeSpans(store)[0];
    const detail = resolveJudgeDetail(judge, []);
    expect(detail.verdictBucket).toBe('on_task');
    expect(detail.onTask).toBe(true);
    expect(detail.reason).toBe('on track');
    expect(detail.reasoningInput).toBe('I will search');
    expect(detail.model).toBe('haiku');
    expect(detail.elapsedMs).toBe(175);
    expect(detail.subjectAgentId).toBe('agent-a');
    expect(detail.taskId).toBe('t1');
    expect(detail.steeredPlan).toBeNull();
  });

  it('resolveJudgeDetail returns off_task bucket with severity + reason', () => {
    applyGoldfiveEvent(
      judgeEvent('ev-off', 80, {
        onTask: false,
        severity: 'warning',
        reason: 'agent paraphrasing',
        reasoningInput: 'agent repeated itself',
        rawResponse: '{"on_task": false, "severity":"warning"}',
        model: 'haiku',
        elapsedMs: 310,
        subjectAgentId: 'agent-a',
        taskId: 't1',
      }),
      store,
      0,
    );
    const judge = listJudgeSpans(store)[0];
    const detail = resolveJudgeDetail(judge, []);
    expect(detail.verdictBucket).toBe('off_task');
    expect(detail.severity).toBe('warning');
    expect(detail.reason).toBe('agent paraphrasing');
  });

  it('resolveJudgeDetail returns no_verdict when the judge emitted raw but no parsed fields', () => {
    applyGoldfiveEvent(
      judgeEvent('ev-bad', 90, {
        // No onTask / verdict / severity — malformed judge output.
        rawResponse: '{ bad json',
        reasoningInput: 'agent thinking',
      }),
      store,
      0,
    );
    const judge = listJudgeSpans(store)[0];
    const detail = resolveJudgeDetail(judge, []);
    expect(detail.verdictBucket).toBe('no_verdict');
    expect(detail.rawResponse).toContain('bad json');
  });

  it('links a matching PlanRevised as the steering outcome (by trigger_event_id)', () => {
    // Seed an initial plan so the PlanRevised has a prior revision to
    // chain from. Then emit the judge event AND a PlanRevised whose
    // trigger_event_id points at the judge event id.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-init',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: pbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      judgeEvent('ev-judge-steer', 100, {
        onTask: false,
        severity: 'warning',
        reason: 'drift',
        reasoningInput: 'agent stuck',
        rawResponse: '{"on_task": false}',
        subjectAgentId: 'agent-a',
        taskId: 't1',
      }),
      store,
      0,
    );
    const rev = pbPlan('p1', ['t1', 't2-new'], 2);
    rev.revisionReason = 'recover from reasoning drift';
    rev.revisionKind = DriftKind.LOOPING_REASONING;
    rev.revisionTriggerEventId = 'ev-judge-steer';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 2n,
        emittedAt: ts(101),
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: rev,
            reason: 'recover from reasoning drift',
            revisionIndex: 2,
          }),
        },
      }),
      store,
      0,
    );

    const judge = listJudgeSpans(store)[0];
    const plans = collectAllPlans(store);
    const detail = resolveJudgeDetail(judge, plans);
    expect(detail.steeredPlan).not.toBeNull();
    expect(detail.steeredPlan?.id).toBe('p1');
    expect(detail.steeredPlan?.revisionIndex).toBe(2);
    expect(detail.steeringSummary).toContain('recover from reasoning drift');
    // Task summaries include at least the new task title.
    const titles = detail.taskSummaries.join('|');
    expect(titles).toContain('task t2-new');
  });

  it('no matching PlanRevised leaves steeredPlan null (ladder did not escalate)', () => {
    applyGoldfiveEvent(
      judgeEvent('ev-orphan', 200, {
        onTask: false,
        severity: 'info',
        reason: 'mild drift',
        reasoningInput: 'thinking',
        rawResponse: '{}',
        subjectAgentId: 'agent-a',
        taskId: 't1',
      }),
      store,
      0,
    );
    const judge = listJudgeSpans(store)[0];
    const detail = resolveJudgeDetail(judge, collectAllPlans(store));
    expect(detail.steeredPlan).toBeNull();
    expect(detail.taskSummaries).toHaveLength(0);
  });
});
