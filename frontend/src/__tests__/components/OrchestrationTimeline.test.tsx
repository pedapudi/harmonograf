import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Span } from '../../gantt/types';

// The hook reaches into rpc/hooks.getSessionStore — mock it to return our
// fixture store keyed by id so the timeline can subscribe + read from the
// same instance we mutate in the tests.
let mockStore: SessionStore | undefined = undefined;
vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? mockStore : undefined),
}));

import { OrchestrationTimeline } from '../../components/OrchestrationTimeline/OrchestrationTimeline';

function mkSpan(partial: Partial<Span> & { id: string; name: string }): Span {
  return {
    id: partial.id,
    sessionId: 's',
    agentId: partial.agentId ?? 'agent-a',
    parentSpanId: null,
    kind: partial.kind ?? 'TOOL_CALL',
    status: partial.status ?? 'COMPLETED',
    name: partial.name,
    startMs: partial.startMs ?? 0,
    endMs: partial.endMs ?? (partial.startMs ?? 0) + 10,
    links: [],
    attributes: partial.attributes ?? {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

function previewAttr(obj: unknown) {
  return {
    tool_args_preview: {
      kind: 'string' as const,
      value: JSON.stringify(obj),
    },
  };
}

describe('<OrchestrationTimeline />', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
  });
  afterEach(() => {
    mockStore = undefined;
  });

  it('renders an empty state when no reporting-tool spans exist', () => {
    render(<OrchestrationTimeline sessionId="s1" />);
    expect(
      screen.getByTestId('orchestration-timeline-empty'),
    ).toBeInTheDocument();
  });

  it('filters to reporting-tool TOOL_CALL spans and renders them newest-first', () => {
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-llm',
        name: 'gemini-llm',
        kind: 'LLM_CALL',
        startMs: 10,
      }),
    );
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-tool-unrelated',
        name: 'search_web',
        kind: 'TOOL_CALL',
        startMs: 20,
        attributes: previewAttr({ query: 'harmonograf' }),
      }),
    );
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-started',
        name: 'report_task_started',
        kind: 'TOOL_CALL',
        startMs: 100,
        attributes: previewAttr({
          task_id: 't1',
          detail: 'kicking off research',
        }),
      }),
    );
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-completed',
        name: 'report_task_completed',
        kind: 'TOOL_CALL',
        startMs: 500,
        attributes: previewAttr({
          task_id: 't1',
          summary: 'found 3 sources',
        }),
      }),
    );

    render(<OrchestrationTimeline sessionId="s1" />);

    const rows = screen.getAllByTestId('orchestration-event');
    expect(rows).toHaveLength(2);
    // Newest first: completed (500ms) then started (100ms).
    expect(rows[0]).toHaveAttribute('data-kind', 'completed');
    expect(rows[1]).toHaveAttribute('data-kind', 'started');
    expect(rows[0]).toHaveTextContent('found 3 sources');
    expect(rows[1]).toHaveTextContent('kicking off research');
    expect(rows[0]).toHaveTextContent('#t1');
  });

  it('live-updates when a new reporting-tool span is appended', () => {
    render(<OrchestrationTimeline sessionId="s1" />);
    expect(
      screen.getByTestId('orchestration-timeline-empty'),
    ).toBeInTheDocument();

    act(() => {
      mockStore!.spans.append(
        mkSpan({
          id: 'sp-fail',
          name: 'report_task_failed',
          kind: 'TOOL_CALL',
          startMs: 200,
          attributes: previewAttr({
            task_id: 't-doom',
            reason: 'model refused',
            recoverable: false,
          }),
        }),
      );
    });

    const row = screen.getByTestId('orchestration-event');
    expect(row).toHaveAttribute('data-kind', 'failed');
    expect(row).toHaveTextContent('model refused');
    expect(row).toHaveTextContent('fatal');
  });

  it('respects the limit prop', () => {
    for (let i = 0; i < 25; i++) {
      mockStore!.spans.append(
        mkSpan({
          id: `sp-${i}`,
          name: 'report_task_progress',
          kind: 'TOOL_CALL',
          startMs: i * 10,
          attributes: previewAttr({ task_id: `t${i}`, detail: `step ${i}` }),
        }),
      );
    }
    render(<OrchestrationTimeline sessionId="s1" limit={5} />);
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(5);
  });

  it('toggles long details with the show more button', () => {
    const longDetail = 'a'.repeat(400);
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-long',
        name: 'report_task_blocked',
        kind: 'TOOL_CALL',
        startMs: 1,
        attributes: previewAttr({
          task_id: 't1',
          blocker: longDetail,
        }),
      }),
    );
    render(<OrchestrationTimeline sessionId="s1" />);
    const toggle = screen.getByRole('button', { name: /show more/i });
    fireEvent.click(toggle);
    expect(
      screen.getByRole('button', { name: /show less/i }),
    ).toBeInTheDocument();
  });

  it('returns empty for a null sessionId', () => {
    render(<OrchestrationTimeline sessionId={null} />);
    expect(
      screen.getByTestId('orchestration-timeline-empty'),
    ).toBeInTheDocument();
  });

  // ------------------------------------------------------------------
  // Filter / grouping controls (task #14)
  // ------------------------------------------------------------------

  function seedMixed() {
    // 2 tasks, 2 agents, a handful of kinds at different times.
    const seed: Array<Parameters<typeof mkSpan>[0]> = [
      {
        id: 'sp-started-a-t1',
        name: 'report_task_started',
        kind: 'TOOL_CALL',
        agentId: 'agent-a',
        startMs: 100,
        attributes: previewAttr({ task_id: 't1', detail: 'kick off t1' }),
      },
      {
        id: 'sp-progress-a-t1-1',
        name: 'report_task_progress',
        kind: 'TOOL_CALL',
        agentId: 'agent-a',
        startMs: 200,
        attributes: previewAttr({ task_id: 't1', detail: 'tick 1' }),
      },
      {
        id: 'sp-progress-a-t1-2',
        name: 'report_task_progress',
        kind: 'TOOL_CALL',
        agentId: 'agent-a',
        startMs: 300,
        attributes: previewAttr({ task_id: 't1', detail: 'tick 2' }),
      },
      {
        id: 'sp-completed-a-t1',
        name: 'report_task_completed',
        kind: 'TOOL_CALL',
        agentId: 'agent-a',
        startMs: 400,
        attributes: previewAttr({ task_id: 't1', summary: 't1 done' }),
      },
      {
        id: 'sp-started-b-t2',
        name: 'report_task_started',
        kind: 'TOOL_CALL',
        agentId: 'agent-b',
        startMs: 500_000, // far in the future so "last 30s" excludes t1
        attributes: previewAttr({ task_id: 't2', detail: 'kick off t2' }),
      },
      {
        id: 'sp-failed-b-t2',
        name: 'report_task_failed',
        kind: 'TOOL_CALL',
        agentId: 'agent-b',
        startMs: 510_000,
        attributes: previewAttr({
          task_id: 't2',
          reason: 'oh no',
          recoverable: false,
        }),
      },
    ];
    for (const s of seed) mockStore!.spans.append(mkSpan(s));
  }

  it('filters by event kind via the kind chips', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);
    // Start: all 6 events are shown.
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(6);

    // Deselect "progress" → the 2 progress events disappear.
    fireEvent.click(screen.getByTestId('orch-kind-chip-progress'));
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(4);

    // Deselect every remaining kind — empty-with-filter message.
    for (const k of [
      'started',
      'completed',
      'failed',
      'blocked',
      'discovered',
      'divergence',
    ]) {
      fireEvent.click(screen.getByTestId(`orch-kind-chip-${k}`));
    }
    expect(
      screen.getByTestId('orchestration-timeline-empty'),
    ).toHaveTextContent(/No events match/);
  });

  it('filters by agent via the agent chips', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(6);

    // Deselect agent-a → only agent-b's 2 events remain.
    fireEvent.click(screen.getByTestId('orch-agent-chip-agent-a'));
    const rows = screen.getAllByTestId('orchestration-event');
    expect(rows).toHaveLength(2);
    for (const r of rows) {
      expect(r).toHaveTextContent('agent-b');
    }

    // Reset via the "all" button → back to 6.
    fireEvent.click(screen.getByTestId('orch-agent-reset'));
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(6);
  });

  it('time window "last 30s" is relative to the newest event', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);
    fireEvent.click(screen.getByTestId('orch-window-30s'));
    // Newest event is at 510_000ms; window cutoff is 480_000ms. Only t2's
    // two events (500_000, 510_000) survive.
    const rows = screen.getAllByTestId('orchestration-event');
    expect(rows).toHaveLength(2);
    for (const r of rows) {
      expect(r).toHaveTextContent(/t2/);
    }
  });

  it('hides noise by collapsing consecutive progress events per task', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);

    // Before: 6 rows (2 progress).
    expect(screen.getAllByTestId('orchestration-event')).toHaveLength(6);

    fireEvent.click(screen.getByTestId('orch-hide-noise').querySelector('input')!);

    // After: 5 rows — one progress collapsed into the representative.
    const rows = screen.getAllByTestId('orchestration-event');
    expect(rows).toHaveLength(5);
    const collapsed = screen.getByTestId('orchestration-collapsed-count');
    expect(collapsed).toHaveTextContent(/\+1 progress updates/);
  });

  it('group by task creates a group per task_id', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);
    fireEvent.click(screen.getByTestId('orch-group-task'));

    const groups = screen.getAllByTestId('orchestration-group');
    // Two tasks → two groups.
    expect(groups).toHaveLength(2);
    const keys = groups.map((g) => g.getAttribute('data-group-key'));
    expect(keys).toContain('task:t1');
    expect(keys).toContain('task:t2');
  });

  it('group by agent creates a group per agent', () => {
    seedMixed();
    render(<OrchestrationTimeline sessionId="s1" limit={50} />);
    fireEvent.click(screen.getByTestId('orch-group-agent'));
    const groups = screen.getAllByTestId('orchestration-group');
    expect(groups).toHaveLength(2);
    const keys = groups.map((g) => g.getAttribute('data-group-key'));
    expect(keys).toContain('agent:agent-a');
    expect(keys).toContain('agent:agent-b');
  });
});
