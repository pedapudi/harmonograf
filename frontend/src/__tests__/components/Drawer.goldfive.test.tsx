// Drawer integration tests for goldfive-aware span detail (harmonograf#157).
//
// Covers three routing questions:
//
//   1. Generic goldfive spans render GoldfiveSpanDetail inside SummaryTab
//      with all their sections (header, input/output previews, context).
//   2. Judge spans (judge.kind=judge) keep using JudgeInvocationDetail so
//      the verdict badge + reasoning sections still show up.
//   3. Spans that are not goldfive at all keep the vanilla SummaryTab.
//
// Uses the same usePayload mock style as Drawer.reasoning.test.tsx so
// the Reasoning section never fetches.

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const usePayloadSpy = vi.fn<(digest: string | null) => {
  bytes: Uint8Array | null;
  mimeType: string;
  loading: boolean;
  error: string | null;
}>();

let mockStore: unknown = undefined;

vi.mock('../../rpc/hooks', async () => {
  const actual = await vi.importActual<object>('../../rpc/hooks');
  return {
    ...actual,
    usePayload: (digest: string | null) => usePayloadSpy(digest),
    getSessionStore: () => mockStore,
  };
});

import { SummaryTab } from '../../components/shell/Drawer';
import { SessionStore } from '../../gantt/index';
import type { AttributeValue, Span } from '../../gantt/types';

function attr(value: string): AttributeValue {
  return { kind: 'string', value };
}
function boolAttr(value: boolean): AttributeValue {
  return { kind: 'bool', value };
}

function mkSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'sp-gf',
    sessionId: 'sess',
    agentId: 'client-1:goldfive',
    parentSpanId: null,
    kind: 'LLM_CALL',
    name: 'refine_steer',
    status: 'COMPLETED',
    startMs: 100,
    endMs: 300,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  usePayloadSpy.mockReset();
  usePayloadSpy.mockReturnValue({
    bytes: null,
    mimeType: '',
    loading: false,
    error: null,
  });
  const store = new SessionStore();
  store.agents.upsert({
    id: 'client-1:goldfive',
    name: 'goldfive',
    framework: 'CUSTOM',
    status: 'CONNECTED',
    capabilities: [],
    connectedAtMs: 0,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
  mockStore = store;
});

afterEach(() => {
  vi.clearAllMocks();
  mockStore = undefined;
  cleanup();
});

describe('Drawer SummaryTab — goldfive routing', () => {
  it('renders GoldfiveSpanDetail for a refine_* goldfive span', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.decision_summary': attr(
          'refined plan in response to OFF_TOPIC drift',
        ),
        'goldfive.target_agent_id': attr('client-1:research_agent'),
        'goldfive.input_preview': attr('input text'),
        'goldfive.output_preview': attr('output text'),
      },
    });
    render(<SummaryTab span={span} />);
    const section = screen.getByTestId('drawer-goldfive-section');
    expect(section.getAttribute('data-mode')).toBe('generic');
    expect(screen.getByTestId('goldfive-span-detail')).toBeTruthy();
    expect(
      screen.getByTestId('goldfive-span-detail-header').textContent,
    ).toMatch(/OFF_TOPIC drift/);
    expect(
      screen.getByTestId('goldfive-span-detail-input-body').textContent,
    ).toBe('input text');
  });

  it('renders JudgeInvocationDetail for a judge span (judge.kind=judge)', () => {
    const span = mkSpan({
      id: 'sp-judge',
      name: 'judge_reasoning',
      attributes: {
        'goldfive.call_name': attr('judge_reasoning'),
        'judge.kind': attr('judge'),
        'judge.on_task': boolAttr(true),
        'judge.reason': attr('agent is on task'),
      },
    });
    render(<SummaryTab span={span} />);
    const section = screen.getByTestId('drawer-goldfive-section');
    expect(section.getAttribute('data-mode')).toBe('judge');
    expect(screen.getByTestId('judge-invocation-detail')).toBeTruthy();
    // And the generic panel does NOT render (routing is exclusive).
    expect(screen.queryByTestId('goldfive-span-detail')).toBeNull();
  });

  it('does not render a goldfive section for non-goldfive spans', () => {
    const span: Span = mkSpan({
      id: 'sp-plain',
      agentId: 'client-1:research_agent',
      kind: 'LLM_CALL',
      name: 'gemini-2.0',
      attributes: {},
    });
    render(<SummaryTab span={span} />);
    expect(screen.queryByTestId('drawer-goldfive-section')).toBeNull();
    expect(screen.queryByTestId('goldfive-span-detail')).toBeNull();
    expect(screen.queryByTestId('judge-invocation-detail')).toBeNull();
  });

  it('graceful-degrades when a goldfive span carries no new attributes', () => {
    // Legacy __goldfive__ span — isGoldfiveSpan still returns true so the
    // GoldfiveSpanDetail renders, but the sections show the empty-state
    // placeholders instead of crashing.
    const span = mkSpan({
      id: 'sp-legacy',
      agentId: '__goldfive__',
      name: 'refine: drift',
      attributes: {},
    });
    render(<SummaryTab span={span} />);
    expect(screen.getByTestId('drawer-goldfive-section')).toBeTruthy();
    expect(
      screen.getByTestId('goldfive-span-detail-input').textContent,
    ).toMatch(/No input preview captured/);
    expect(
      screen.getByTestId('goldfive-span-detail-output').textContent,
    ).toMatch(/No output preview captured/);
  });

  it('links a refine_* span to its PlanRevised by target_task_id + time proximity', () => {
    // Seed a plan revision (rev 2) whose task list includes the span's
    // target_task_id and whose createdAtMs is slightly after the span's
    // startMs — the expected pairing.
    const store = mockStore as SessionStore;
    store.tasks.upsertPlan({
      id: 'p1',
      invocationSpanId: '',
      plannerAgentId: '',
      createdAtMs: 180,
      summary: '',
      tasks: [
        {
          id: 't-research-corrected',
          title: 'Research corrected topic',
          description: '',
          assigneeAgentId: 'client-1:research_agent',
          status: 'PENDING',
          predictedStartMs: 0,
          predictedDurationMs: 0,
          boundSpanId: '',
        },
      ],
      edges: [],
      revisionReason: 'OFF_TOPIC drift',
      revisionIndex: 2,
    });

    const span = mkSpan({
      startMs: 150,
      endMs: 170,
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.target_task_id': attr('t-research-corrected'),
      },
    });
    render(<SummaryTab span={span} />);
    const linked = screen.getByTestId('goldfive-span-detail-linked-plan-link');
    expect(linked.textContent).toMatch(/Plan refined → r2/);
    expect(linked.textContent).toMatch(/OFF_TOPIC drift/);
  });

  it('does not link a refine_* span when no plan revisions exist', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.target_task_id': attr('t-missing'),
      },
    });
    render(<SummaryTab span={span} />);
    expect(
      screen.queryByTestId('goldfive-span-detail-linked-plan'),
    ).toBeNull();
  });
});
