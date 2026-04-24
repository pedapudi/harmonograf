import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type {
  Agent,
  AttributeValue,
  Span,
  SpanKind,
  SpanStatus,
  Task,
  TaskPlan,
  TaskStatus,
} from '../../gantt/types';

let mockStore: SessionStore | undefined = undefined;
let mockSessionId: string | null = 'session-1';

vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? mockStore : undefined),
}));
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: { currentSessionId: string | null }) => T) =>
    selector({ currentSessionId: mockSessionId }),
}));

import { CurrentTaskStrip } from '../../components/shell/CurrentTaskStrip';

function task(id: string, status: TaskStatus, title = id, agent = 'agent-a'): Task {
  return {
    id,
    title,
    description: '',
    assigneeAgentId: agent,
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes: '',
  };
}

function span(
  id: string,
  agentId: string,
  kind: SpanKind,
  name: string,
  opts: {
    endMs?: number | null;
    status?: SpanStatus;
    startMs?: number;
    attributes?: Record<string, AttributeValue>;
  } = {},
): Span {
  return {
    id,
    sessionId: 'session-1',
    agentId,
    parentSpanId: null,
    kind,
    status: opts.status ?? 'RUNNING',
    name,
    startMs: opts.startMs ?? 0,
    endMs: opts.endMs ?? null,
    links: [],
    attributes: opts.attributes ?? {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

function plan(id: string, tasks: Task[]): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: '',
    tasks,
    edges: [],
    revisionReason: '',
  };
}

describe('<CurrentTaskStrip />', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    mockSessionId = 'session-1';
  });
  afterEach(() => {
    mockStore = undefined;
  });

  it('renders nothing when there is no current task', () => {
    const { container } = render(<CurrentTaskStrip />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when there is no active session', () => {
    mockSessionId = null;
    mockStore!.tasks.upsertPlan(plan('p1', [task('t1', 'RUNNING', 'Do thing')]));
    const { container } = render(<CurrentTaskStrip />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the RUNNING task title, status chip, and assignee', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    const strip = screen.getByTestId('current-task-strip');
    expect(strip).toHaveAttribute('data-running', 'true');
    expect(strip).toHaveTextContent('Analyze logs');
    expect(strip).toHaveTextContent('RUNNING');
    expect(strip).toHaveTextContent('worker-7');
  });

  it('falls back to a completed task when nothing is running', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'COMPLETED', 'Finished work')]),
    );
    render(<CurrentTaskStrip />);
    const strip = screen.getByTestId('current-task-strip');
    expect(strip).toHaveAttribute('data-running', 'false');
    expect(strip).toHaveTextContent('COMPLETED');
    expect(strip).toHaveTextContent('Finished work');
  });

  it('renders an in-flight tool badge for the current agent', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-tool', 'worker-7', 'TOOL_CALL', 'write_webpage', {
        startMs: 100,
      }),
    );
    render(<CurrentTaskStrip />);
    const badge = screen.getByTestId('current-task-strip-tool');
    expect(badge).toHaveTextContent('write_webpage');
  });

  it('prefers the most recent in-flight tool when several overlap', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-a', 'worker-7', 'TOOL_CALL', 'older_tool', { startMs: 100 }),
    );
    mockStore!.spans.append(
      span('s-b', 'worker-7', 'TOOL_CALL', 'newer_tool', { startMs: 250 }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.getByTestId('current-task-strip-tool'),
    ).toHaveTextContent('newer_tool');
  });

  it('ignores completed tool spans when picking the badge', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-done', 'worker-7', 'TOOL_CALL', 'finished_tool', {
        startMs: 100,
        endMs: 200,
        status: 'COMPLETED',
      }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.queryByTestId('current-task-strip-tool'),
    ).toBeNull();
  });

  it('ignores tool spans owned by other agents', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-other', 'other-agent', 'TOOL_CALL', 'not_mine', { startMs: 100 }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.queryByTestId('current-task-strip-tool'),
    ).toBeNull();
  });

  it('renders the thinking dot when an LLM_CALL has has_reasoning=true', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-llm', 'worker-7', 'LLM_CALL', 'gpt', {
        startMs: 100,
        attributes: { has_reasoning: { kind: 'bool', value: true } },
      }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.getByTestId('current-task-strip-thinking'),
    ).toBeTruthy();
  });

  it('omits the thinking dot when has_reasoning is false or absent', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    mockStore!.spans.append(
      span('s-llm', 'worker-7', 'LLM_CALL', 'gpt', {
        startMs: 100,
        attributes: { has_reasoning: { kind: 'bool', value: false } },
      }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.queryByTestId('current-task-strip-thinking'),
    ).toBeNull();
  });

  it('does not enrich with in-flight context for fallback completed tasks', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'COMPLETED', 'Finished work', 'worker-7')]),
    );
    // This tool span is still "in-flight" from the span index's point of
    // view, but the fallback task is not RUNNING — we don't surface stale
    // in-flight context in that case.
    mockStore!.spans.append(
      span('s-ghost', 'worker-7', 'TOOL_CALL', 'ghost', { startMs: 100 }),
    );
    render(<CurrentTaskStrip />);
    expect(
      screen.queryByTestId('current-task-strip-tool'),
    ).toBeNull();
  });

  it('repaints when a new in-flight tool span is appended', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    expect(screen.queryByTestId('current-task-strip-tool')).toBeNull();

    act(() => {
      mockStore!.spans.append(
        span('s-new', 'worker-7', 'TOOL_CALL', 'write_webpage', {
          startMs: 100,
        }),
      );
    });
    expect(
      screen.getByTestId('current-task-strip-tool'),
    ).toHaveTextContent('write_webpage');
  });

  function agent(id: string, metadata: Record<string, string> = {}): Agent {
    return {
      id,
      name: id,
      framework: 'ADK',
      capabilities: [],
      status: 'CONNECTED',
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata,
    };
  }

  it('renders SEQ chip when assignee is in sequential mode', () => {
    mockStore!.agents.upsert(
      agent('worker-7', { 'harmonograf.execution_mode': 'sequential' }),
    );
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    const chip = screen.getByTestId('current-task-strip-mode');
    expect(chip).toHaveTextContent('SEQ');
    expect(chip).toHaveAttribute('data-mode', 'sequential');
  });

  it('renders PAR chip when assignee is in parallel mode', () => {
    mockStore!.agents.upsert(
      agent('worker-7', { 'harmonograf.execution_mode': 'parallel' }),
    );
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    const chip = screen.getByTestId('current-task-strip-mode');
    expect(chip).toHaveTextContent('PAR');
    expect(chip).toHaveAttribute('data-mode', 'parallel');
  });

  it('renders OBS chip when assignee is in delegated mode', () => {
    mockStore!.agents.upsert(
      agent('worker-7', { 'harmonograf.execution_mode': 'delegated' }),
    );
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    const chip = screen.getByTestId('current-task-strip-mode');
    expect(chip).toHaveTextContent('OBS');
    expect(chip).toHaveAttribute('data-mode', 'delegated');
  });

  it('omits the mode chip when the assignee has no execution mode metadata', () => {
    mockStore!.agents.upsert(agent('worker-7'));
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    expect(screen.queryByTestId('current-task-strip-mode')).toBeNull();
  });

  it('omits the mode chip when the execution mode value is unrecognised', () => {
    mockStore!.agents.upsert(
      agent('worker-7', { 'harmonograf.execution_mode': 'bogus' }),
    );
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    expect(screen.queryByTestId('current-task-strip-mode')).toBeNull();
  });

  it('omits the mode chip when no assignee agent is registered', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    expect(screen.queryByTestId('current-task-strip-mode')).toBeNull();
  });

  it('repaints when the assignee agent metadata is upserted later', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'RUNNING', 'Analyze logs', 'worker-7')]),
    );
    render(<CurrentTaskStrip />);
    expect(screen.queryByTestId('current-task-strip-mode')).toBeNull();
    act(() => {
      mockStore!.agents.upsert(
        agent('worker-7', { 'harmonograf.execution_mode': 'parallel' }),
      );
    });
    expect(screen.getByTestId('current-task-strip-mode')).toHaveTextContent(
      'PAR',
    );
  });

  it('repaints when the TaskRegistry mutates', () => {
    mockStore!.tasks.upsertPlan(
      plan('p1', [task('t1', 'PENDING', 'initial')]),
    );
    const { container } = render(<CurrentTaskStrip />);
    expect(container.firstChild).toBeNull();

    act(() => {
      mockStore!.tasks.updateTaskStatus('p1', 't1', 'RUNNING', '');
    });
    expect(screen.getByTestId('current-task-strip')).toHaveTextContent('RUNNING');
  });
});
