// Component tests for the goldfive-aware SpanPopover (harmonograf#157).
//
// Drives the popover with a seeded SessionStore + a minimal renderer
// stub (implements only ``rectFor``) + the popover Zustand store so we
// can assert the goldfive section renders its headline, context row,
// input/output disclosures, and hides the Steer/Annotate action row.

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The popover imports RPC hooks for live-agent lookups and control
// sends. In the test environment we stub them so the render doesn't
// attach to a streaming transport.
vi.mock('../../rpc/hooks', async () => {
  const actual = await vi.importActual<object>('../../rpc/hooks');
  return {
    ...actual,
    useAgentLive: () => ({ name: 'goldfive', taskReport: '', currentActivity: '' }),
    usePostAnnotation: () => () => Promise.resolve(),
    useSendControl: () => () => Promise.resolve(),
  };
});

import { SpanPopover } from '../../components/Interaction/SpanPopover';
import { SessionStore } from '../../gantt/index';
import type { AttributeValue, Span } from '../../gantt/types';
import { usePopoverStore } from '../../state/popoverStore';
import type { GanttRenderer } from '../../gantt/renderer';

function mkCtx(store: SessionStore): {
  renderer: GanttRenderer;
  store: SessionStore;
  widthCss: number;
  heightCss: number;
  tick: number;
} {
  // Minimal renderer stub — the popover only calls ``rectFor`` to anchor
  // the card. A canned rectangle is enough for the render assertions.
  const renderer = {
    rectFor: () => ({ x: 100, y: 80, w: 120, h: 20 }),
  } as unknown as GanttRenderer;
  return { renderer, store, widthCss: 1024, heightCss: 600, tick: 0 };
}

function attr(value: string): AttributeValue {
  return { kind: 'string', value };
}

function mkGoldfiveSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'gf-refine-1',
    sessionId: 'sess',
    agentId: 'client-42:goldfive',
    parentSpanId: null,
    kind: 'LLM_CALL',
    name: 'refine_steer',
    status: 'COMPLETED',
    startMs: 1_000,
    endMs: 2_300,
    lane: 0,
    attributes: {
      'goldfive.call_name': attr('refine_steer'),
      'goldfive.decision_summary': attr(
        'refined plan in response to OFF_TOPIC drift on research_solar',
      ),
      'goldfive.target_agent_id': attr('client-42:research_agent'),
      'goldfive.target_task_id': attr('t-research-solar'),
      'goldfive.input_preview': attr('drift=OFF_TOPIC\nprevious plan summary'),
      'goldfive.output_preview': attr('new plan: research_solar_corrected'),
    },
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

function seedGoldfiveAgent(store: SessionStore): void {
  store.agents.upsert({
    id: 'client-42:goldfive',
    name: 'goldfive',
    framework: 'CUSTOM',
    status: 'CONNECTED',
    capabilities: [],
    connectedAtMs: 1,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
}

beforeEach(() => {
  usePopoverStore.getState().closeAll();
});

afterEach(() => {
  usePopoverStore.getState().closeAll();
  cleanup();
});

describe('<SpanPopover /> — goldfive spans', () => {
  it('shows the decision summary as the popover summary line', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    const span = mkGoldfiveSpan();
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);

    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    expect(screen.getByTestId('span-popover-title').textContent).toBe('refine_steer');
    expect(screen.getByTestId('span-popover-summary').textContent).toMatch(
      /refined plan in response to OFF_TOPIC drift/,
    );
  });

  it('shows the target agent (bare) and target task in the context row', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    const span = mkGoldfiveSpan();
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);
    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    const targetAgent = screen.getByTestId('span-popover-goldfive-target-agent');
    expect(targetAgent.textContent).toMatch(/research_agent/);
    expect(targetAgent.textContent).not.toMatch(/^client-42:/);

    const targetTask = screen.getByTestId('span-popover-goldfive-target-task');
    expect(targetTask.textContent).toMatch(/t-research-solar/);
  });

  it('renders collapsed Input and Output preview disclosures and expands on click', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    const span = mkGoldfiveSpan();
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);
    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    const input = screen.getByTestId('span-popover-goldfive-input');
    expect(input.getAttribute('data-open')).toBe('false');
    expect(screen.queryByTestId('span-popover-goldfive-input-body')).toBeNull();

    fireEvent.click(screen.getByTestId('span-popover-goldfive-input-toggle'));
    expect(screen.getByTestId('span-popover-goldfive-input-body').textContent).toMatch(
      /drift=OFF_TOPIC/,
    );

    fireEvent.click(screen.getByTestId('span-popover-goldfive-output-toggle'));
    expect(screen.getByTestId('span-popover-goldfive-output-body').textContent).toMatch(
      /research_solar_corrected/,
    );
  });

  it('hides the Steer and Annotate buttons but keeps Copy id + Open drawer', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    const span = mkGoldfiveSpan();
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);
    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    expect(screen.queryByText('Steer')).toBeNull();
    expect(screen.queryByText('Annotate')).toBeNull();
    expect(screen.getByText('Copy id')).toBeTruthy();
    expect(screen.getByText('Open drawer')).toBeTruthy();
  });

  it('degrades gracefully on a goldfive span with no new attributes (pre-merge session)', () => {
    // Minimal legacy shape — on __goldfive__ row, no goldfive.* attrs.
    const store = new SessionStore();
    store.agents.upsert({
      id: '__goldfive__',
      name: 'goldfive',
      framework: 'CUSTOM',
      status: 'CONNECTED',
      capabilities: [],
      connectedAtMs: 1,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {},
    });
    const span = mkGoldfiveSpan({
      id: 'gf-legacy',
      agentId: '__goldfive__',
      name: 'refine: looping',
      attributes: {},
    });
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);
    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    // No crash; summary falls back to the synthesized decision summary.
    expect(screen.getByTestId('span-popover-summary').textContent).toMatch(
      /goldfive: refine: looping/,
    );
    // No previews rendered when no attributes present.
    expect(screen.queryByTestId('span-popover-goldfive-input')).toBeNull();
    expect(screen.queryByTestId('span-popover-goldfive-output')).toBeNull();
    // Still hides Steer/Annotate because the span is still a goldfive span.
    expect(screen.queryByText('Steer')).toBeNull();
  });

  it('non-goldfive spans keep the legacy popover shape', () => {
    const store = new SessionStore();
    store.agents.upsert({
      id: 'agent-a',
      name: 'agent-a',
      framework: 'ADK',
      status: 'CONNECTED',
      capabilities: [],
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {},
    });
    const span: Span = {
      id: 'sp-llm',
      sessionId: 'sess',
      agentId: 'agent-a',
      parentSpanId: null,
      kind: 'LLM_CALL',
      name: 'gemini-2.0',
      status: 'COMPLETED',
      startMs: 1_000,
      endMs: 2_000,
      lane: 0,
      attributes: {},
      payloadRefs: [],
      links: [],
      replaced: false,
      error: null,
    };
    store.spans.append(span);

    usePopoverStore.getState().openForSpan(span.id, 100, 80);
    render(<SpanPopover ctx={mkCtx(store)} sessionId="sess" />);

    expect(screen.getByText('Steer')).toBeTruthy();
    expect(screen.getByText('Annotate')).toBeTruthy();
    expect(screen.queryByTestId('span-popover-goldfive-input')).toBeNull();
    expect(screen.queryByTestId('span-popover-goldfive-context')).toBeNull();
  });
});
