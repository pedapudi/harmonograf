import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Span, AttributeValue } from '../../gantt/types';

let mockStore: SessionStore | undefined = undefined;
vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? mockStore : undefined),
}));

import { OrchestrationTimeline } from '../../components/OrchestrationTimeline/OrchestrationTimeline';

const str = (v: string): AttributeValue => ({ kind: 'string', value: v });
const bool = (v: boolean): AttributeValue => ({ kind: 'bool', value: v });

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

describe('OrchestrationTimeline thinking preview', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    // Ensure the agent registry knows about the agent so the timeline's
    // thinking-map builder finds spans via store.agents.list.
    mockStore.agents.upsert({
      id: 'agent-a',
      name: 'agent-a',
      framework: 'ADK',
      capabilities: [],
      status: 'CONNECTED',
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {},
    });
  });
  afterEach(() => {
    mockStore = undefined;
  });

  it('renders a 🧠 preview on reporting-tool rows whose span has reasoning', () => {
    // LLM span carrying reasoning. The thinking-map is keyed by span.id, so
    // the reporting-tool row must share the same span.id as the LLM span to
    // surface the preview (in real life an orchestration event is keyed by
    // the report_task_* span, so the reasoning must live there). The helper
    // collects reasoning from every span in the agent's index.
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-report',
        name: 'report_task_progress',
        kind: 'TOOL_CALL',
        startMs: 100,
        attributes: {
          tool_args_preview: str(JSON.stringify({ task_id: 't1', detail: 'halfway' })),
          'llm.reasoning': str(
            'The user wants me to summarize three documents so I should start with the first.',
          ),
          has_reasoning: bool(true),
        },
      }),
    );

    render(<OrchestrationTimeline sessionId="s1" />);

    const preview = screen.getByTestId('orchestration-thinking-preview');
    expect(preview.textContent).toMatch(/summarize three documents/);
  });

  it('omits the preview when the span has no thinking content', () => {
    mockStore!.spans.append(
      mkSpan({
        id: 'sp-no-think',
        name: 'report_task_started',
        kind: 'TOOL_CALL',
        startMs: 10,
        attributes: {
          tool_args_preview: str(JSON.stringify({ task_id: 't2', detail: 'go' })),
        },
      }),
    );

    render(<OrchestrationTimeline sessionId="s1" />);

    expect(
      screen.queryByTestId('orchestration-thinking-preview'),
    ).toBeNull();
  });
});
