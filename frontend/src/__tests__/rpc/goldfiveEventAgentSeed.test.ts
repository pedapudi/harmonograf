import { describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { SessionStore, PLACEHOLDER_AGENT_FLAG } from '../../gantt/index';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  PlanSubmittedSchema,
  PlanRevisedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  TaskStatus,
  DriftKind,
  DriftSeverity,
} from '../../pb/goldfive/v1/types_pb';

// Integration coverage for harmonograf#133: seeding the agent registry
// from plan content so tasks whose assignee hasn't emitted a span yet
// still resolve to a bare display name instead of the raw compound id.

const CLIENT = 'presentation-orchestrated-9b2b3a9c7289';

function pbTask(id: string, bareAgent: string) {
  return create(TaskSchema, {
    id,
    title: `task ${id}`,
    description: '',
    // Assignee id comes off the wire in its compound form (the canonical
    // storage form — see HarmonografSink._compound on the client side).
    assigneeAgentId: `${CLIENT}:${bareAgent}`,
    status: TaskStatus.PENDING,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function pbPlan(
  id: string,
  entries: Array<[string, string]>,
  revisionIndex = 0,
) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: `plan ${id}`,
    tasks: entries.map(([tid, bare]) => pbTask(tid, bare)),
    edges: [],
    revisionReason: '',
    revisionKind: DriftKind.UNSPECIFIED,
    revisionSeverity: DriftSeverity.UNSPECIFIED,
    revisionIndex,
  });
}

describe('planSubmitted seeds the agent registry from task assignees', () => {
  it('creates a placeholder row for every unique assignee, keyed by compound id', () => {
    const store = new SessionStore();
    const event = create(EventSchema, {
      eventId: 'ev-seed',
      runId: 'run-1',
      sequence: 0n,
      payload: {
        case: 'planSubmitted',
        value: create(PlanSubmittedSchema, {
          plan: pbPlan('p1', [
            ['t1', 'research_agent'],
            ['t2', 'reviewer_agent'],
            ['t3', 'debugger_agent'],
          ]),
        }),
      },
    });

    applyGoldfiveEvent(event, store, 0);

    // All three rows exist under their compound ids — that is what
    // `task.assigneeAgentId` lookup on the resolver side uses.
    expect(store.agents.size).toBe(3);
    for (const bare of ['research_agent', 'reviewer_agent', 'debugger_agent']) {
      const row = store.agents.get(`${CLIENT}:${bare}`);
      expect(row, `agent row for ${bare}`).toBeDefined();
      expect(row!.name).toBe(bare);
      expect(row!.metadata[PLACEHOLDER_AGENT_FLAG]).toBe('1');
    }
  });

  it('standard registry-lookup resolver now returns bare names', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-seed-2',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: pbPlan('p1', [
              ['t1', 'research_agent'],
              ['t2', 'reviewer_agent'],
            ]),
          }),
        },
      }),
      store,
      0,
    );

    // This matches GanttView.agentNameFor / DelegationTooltip resolvers:
    // `store.agents.get(id)?.name ?? id`. Before #133 this would have
    // returned the raw compound wire id.
    const resolve = (id: string) => store.agents.get(id)?.name ?? id;
    expect(resolve(`${CLIENT}:research_agent`)).toBe('research_agent');
    expect(resolve(`${CLIENT}:reviewer_agent`)).toBe('reviewer_agent');
  });

  it('planRevised also seeds newly-announced agents', () => {
    const store = new SessionStore();
    // Initial plan assigns t1/t2 to research + reviewer.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-init',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: pbPlan('p1', [
              ['t1', 'research_agent'],
              ['t2', 'reviewer_agent'],
            ]),
          }),
        },
      }),
      store,
      0,
    );
    expect(store.agents.size).toBe(2);

    // Revision introduces a third task assigned to an unseen agent.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 1n,
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: pbPlan(
              'p1',
              [
                ['t1', 'research_agent'],
                ['t2', 'reviewer_agent'],
                ['t3', 'verify_agent'],
              ],
              1,
            ),
          }),
        },
      }),
      store,
      0,
    );

    expect(store.agents.get(`${CLIENT}:verify_agent`)?.name).toBe(
      'verify_agent',
    );
    // harmonograf#196: PlanRevised now synthesizes the goldfive actor row so
    // the refine span has a lane to land on. Count the three plan-seeded
    // rows directly rather than rely on size.
    expect(store.agents.size).toBe(4);
    expect(store.agents.get('__goldfive__')).toBeTruthy();
  });

  it('skips tasks with an empty assignee (no synthetic empty-id row)', () => {
    const store = new SessionStore();
    const taskWithoutAssignee = create(TaskSchema, {
      id: 't1',
      title: 'unassigned',
      description: '',
      assigneeAgentId: '',
      status: TaskStatus.PENDING,
      predictedStartMs: 0n,
      predictedDurationMs: 0n,
    });
    const plan = create(PlanSchema, {
      id: 'p1',
      runId: 'run-1',
      summary: '',
      tasks: [taskWithoutAssignee, pbTask('t2', 'research_agent')],
      edges: [],
      revisionReason: '',
      revisionKind: DriftKind.UNSPECIFIED,
      revisionSeverity: DriftSeverity.UNSPECIFIED,
      revisionIndex: 0,
    });
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev',
        runId: 'run-1',
        sequence: 0n,
        payload: { case: 'planSubmitted', value: create(PlanSubmittedSchema, { plan }) },
      }),
      store,
      0,
    );
    expect(store.agents.size).toBe(1);
    expect(store.agents.get('')).toBeUndefined();
    expect(store.agents.get(`${CLIENT}:research_agent`)).toBeDefined();
  });
});
