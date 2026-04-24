// harmonograf#197 — judge-detail resolver + judge-span discriminator.
//
// Option X update (harmonograf#N): ReasoningJudgeInvoked is now
// translated to SpanStart/SpanEnd at the harmonograf client sink (see
// harmonograf_client/sink.py). The frontend sees judge invocations as
// ordinary spans on the goldfive lane carrying the documented
// `judge.*` attributes — this test seeds such spans directly and
// exercises the resolver against them.

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
import type { AttributeValue, Span, TaskPlan } from '../../gantt/types';

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

// Seed a judge span directly onto the goldfive lane — mirroring what the
// harmonograf client sink emits after translating a ReasoningJudgeInvoked
// event under Option X. Only the `judge.*` attributes that the resolver
// reads are populated; callers override via `attrs`.
interface JudgeSpanSeed {
  eventId: string;
  atMs: number;
  onTask?: boolean;
  verdict?: string;
  severity?: string;
  reason?: string;
  reasoningInput?: string;
  rawResponse?: string;
  model?: string;
  elapsedMs?: number;
  subjectAgentId?: string;
  taskId?: string;
}

function seedJudgeSpan(store: SessionStore, seed: JudgeSpanSeed): Span {
  const attrs: Record<string, AttributeValue> = {
    'judge.kind': { kind: 'string', value: 'judge' },
    'judge.event_id': { kind: 'string', value: seed.eventId },
  };
  if (seed.onTask !== undefined) {
    attrs['judge.on_task'] = { kind: 'bool', value: seed.onTask };
  }
  if (seed.verdict !== undefined) {
    attrs['judge.verdict'] = { kind: 'string', value: seed.verdict };
  }
  if (seed.severity !== undefined) {
    attrs['judge.severity'] = { kind: 'string', value: seed.severity };
  }
  if (seed.reason !== undefined) {
    attrs['judge.reason'] = { kind: 'string', value: seed.reason };
  }
  if (seed.reasoningInput !== undefined) {
    attrs['judge.reasoning_input'] = {
      kind: 'string',
      value: seed.reasoningInput,
    };
  }
  if (seed.rawResponse !== undefined) {
    attrs['judge.raw_response'] = { kind: 'string', value: seed.rawResponse };
  }
  if (seed.model !== undefined) {
    attrs['judge.model'] = { kind: 'string', value: seed.model };
  }
  if (seed.elapsedMs !== undefined) {
    attrs['judge.elapsed_ms'] = {
      kind: 'string',
      value: String(seed.elapsedMs),
    };
  }
  if (seed.subjectAgentId !== undefined) {
    attrs['judge.subject_agent_id'] = {
      kind: 'string',
      value: seed.subjectAgentId,
    };
  }
  if (seed.taskId !== undefined) {
    attrs['judge.target_task_id'] = { kind: 'string', value: seed.taskId };
  }
  const span: Span = {
    id: `judge-${seed.eventId}`,
    sessionId: 'sess-1',
    agentId: GOLDFIVE_ACTOR_ID,
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: `judge: ${seed.verdict || (seed.onTask ? 'on_task' : 'unspec')}`,
    startMs: seed.atMs,
    endMs: seed.atMs,
    links: [],
    attributes: attrs,
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
  store.spans.append(span);
  return span;
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

describe('judge-span routing (harmonograf#197, Option X)', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('judge span (as emitted by the sink translator) is discoverable via isJudgeSpan', () => {
    seedJudgeSpan(store, {
      eventId: 'ev-judge',
      atMs: 30,
      onTask: true,
      reason: 'looks good',
      reasoningInput: 'the agent said: I will search',
      rawResponse: '{"on_task": true, "reason": "looks good"}',
      model: 'haiku',
      elapsedMs: 200,
      subjectAgentId: 'agent-a',
      taskId: 't1',
    });
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

  it('isJudgeSpan returns false for non-judge spans on the goldfive lane', () => {
    // Seed a regular drift span on the goldfive lane (drift synthesis
    // stays under Option X — only LLM-call synthesis was retired).
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
    expect(spans.length).toBeGreaterThan(0);
    for (const s of spans) expect(isJudgeSpan(s)).toBe(false);
  });

  it('resolveJudgeDetail surfaces the header + context + verdict for on_task', () => {
    const span = seedJudgeSpan(store, {
      eventId: 'ev-j1',
      atMs: 45,
      onTask: true,
      severity: '',
      reason: 'on track',
      reasoningInput: 'I will search',
      rawResponse: '{"on_task": true}',
      model: 'haiku',
      elapsedMs: 175,
      subjectAgentId: 'agent-a',
      taskId: 't1',
    });
    const detail = resolveJudgeDetail(span, []);
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
    const span = seedJudgeSpan(store, {
      eventId: 'ev-off',
      atMs: 80,
      onTask: false,
      verdict: 'warning',
      severity: 'warning',
      reason: 'agent paraphrasing',
      reasoningInput: 'agent repeated itself',
      rawResponse: '{"on_task": false, "severity":"warning"}',
      model: 'haiku',
      elapsedMs: 310,
      subjectAgentId: 'agent-a',
      taskId: 't1',
    });
    const detail = resolveJudgeDetail(span, []);
    expect(detail.verdictBucket).toBe('off_task');
    expect(detail.severity).toBe('warning');
    expect(detail.reason).toBe('agent paraphrasing');
  });

  it('resolveJudgeDetail returns no_verdict when the judge emitted raw but no parsed fields', () => {
    const span = seedJudgeSpan(store, {
      eventId: 'ev-bad',
      atMs: 90,
      // No onTask / verdict / severity — malformed judge output.
      rawResponse: '{ bad json',
      reasoningInput: 'agent thinking',
    });
    const detail = resolveJudgeDetail(span, []);
    expect(detail.verdictBucket).toBe('no_verdict');
    expect(detail.rawResponse).toContain('bad json');
  });

  it('links a matching PlanRevised as the steering outcome (by trigger_event_id)', () => {
    // Seed an initial plan so the PlanRevised has a prior revision to
    // chain from. Then seed a judge span AND a PlanRevised whose
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
    const judgeSpan = seedJudgeSpan(store, {
      eventId: 'ev-judge-steer',
      atMs: 100,
      onTask: false,
      verdict: 'warning',
      severity: 'warning',
      reason: 'drift',
      reasoningInput: 'agent stuck',
      rawResponse: '{"on_task": false}',
      subjectAgentId: 'agent-a',
      taskId: 't1',
    });
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

    const plans = collectAllPlans(store);
    const detail = resolveJudgeDetail(judgeSpan, plans);
    expect(detail.steeredPlan).not.toBeNull();
    expect(detail.steeredPlan?.id).toBe('p1');
    expect(detail.steeredPlan?.revisionIndex).toBe(2);
    expect(detail.steeringSummary).toContain('recover from reasoning drift');
    // Task summaries include at least the new task title.
    const titles = detail.taskSummaries.join('|');
    expect(titles).toContain('task t2-new');
  });

  it('no matching PlanRevised leaves steeredPlan null (ladder did not escalate)', () => {
    const span = seedJudgeSpan(store, {
      eventId: 'ev-orphan',
      atMs: 200,
      onTask: false,
      verdict: 'info',
      severity: 'info',
      reason: 'mild drift',
      reasoningInput: 'thinking',
      rawResponse: '{}',
      subjectAgentId: 'agent-a',
      taskId: 't1',
    });
    const detail = resolveJudgeDetail(span, collectAllPlans(store));
    expect(detail.steeredPlan).toBeNull();
    expect(detail.taskSummaries).toHaveLength(0);
  });
});
