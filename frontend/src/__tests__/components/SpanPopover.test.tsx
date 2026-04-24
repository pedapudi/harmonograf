// Judge-span layout tests for SpanPopover (harmonograf#judge-detail-clarify).
//
// Focus: the popover's judge-specific layout — verdict banner, lead
// reason, context row, and input-preview section — composes correctly
// with the generic span-popover chrome (pin / close / open-drawer / copy
// id). Tests feed a real SessionStore with a synthesized judge span and
// mount the SpanPopover overlay with a tiny stub renderer.
//
// Co-routing note (sibling agent /tmp/harmonograf-goldfive-render): the
// SpanPopover routes to the judge layout iff `isJudgeSpan(span)` is
// true. These tests lock that invariant so their GoldfiveSpanDetail
// routing lands alongside without regressing the judge path.

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

// Mock CSS imports and the RPC hooks before importing SpanPopover.
vi.mock('../../components/Interventions/JudgeInvocationDetail.css', () => ({}));

const sendControlSpy = vi.fn().mockResolvedValue(undefined);
const postAnnotationSpy = vi.fn().mockResolvedValue(undefined);
vi.mock('../../rpc/hooks', async () => {
  const actual = await vi.importActual<
    typeof import('../../rpc/hooks')
  >('../../rpc/hooks');
  return {
    ...actual,
    useSendControl: () => sendControlSpy,
    usePostAnnotation: () => postAnnotationSpy,
    useAgentLive: (_sess: string | null, agentId: string | null) => ({
      id: agentId ?? '',
      name: agentId ?? '',
      currentActivity: '',
      taskReport: '',
    }),
  };
});

import { SessionStore } from '../../gantt/index';
import type { Span } from '../../gantt/types';
import { SpanPopover } from '../../components/Interaction/SpanPopover';
import { usePopoverStore } from '../../state/popoverStore';
import { isJudgeSpan } from '../../lib/interventionDetail';
import type { OverlayContext } from '../../gantt/GanttCanvas';

// Tiny stub renderer — SpanPopover only uses rectFor() to reposition
// the card; everything else is unused on the popover path. We return a
// fixed rect so the popover lays out at a predictable coordinate.
function makeStubRenderer() {
  return {
    rectFor: () => ({ x: 100, y: 120, w: 80, h: 18 }),
  };
}

function makeJudgeSpan(over: Partial<Span> = {}): Span {
  return {
    id: 'span-judge-1',
    sessionId: 'sess-1',
    agentId: '__goldfive__',
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: 'judge: reasoning',
    startMs: 1_000,
    endMs: 1_250,
    links: [],
    attributes: {
      'judge.kind': { kind: 'string', value: 'judge' },
      'judge.verdict': { kind: 'string', value: 'reasoning_drift_detected' },
      'judge.on_task': { kind: 'bool', value: false },
      'judge.severity': { kind: 'string', value: 'warning' },
      'judge.reason': {
        kind: 'string',
        value: 'Agent is summarising instead of citing tool output.',
      },
      'judge.reasoning_input': {
        kind: 'string',
        value: 'I will now summarise based on what I remember.',
      },
      'judge.raw_response': {
        kind: 'string',
        value: '{"on_task": false, "severity": "warning"}',
      },
      'judge.model': { kind: 'string', value: 'haiku-4' },
      'judge.elapsed_ms': { kind: 'string', value: '248' },
      'judge.subject_agent_id': { kind: 'string', value: 'client:agent-a' },
      'judge.target_task_id': { kind: 'string', value: 'task-42' },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
    ...over,
  };
}

function makeCtx(store: SessionStore): OverlayContext {
  return {
    // Cast — we only use rectFor on the popover path.
    renderer: makeStubRenderer() as unknown as OverlayContext['renderer'],
    store,
    widthCss: 1200,
    heightCss: 600,
    tick: 0,
  };
}

beforeEach(() => {
  usePopoverStore.setState({ popovers: new Map() });
  sendControlSpy.mockClear();
  postAnnotationSpy.mockClear();
});

describe('<SpanPopover /> — judge span routing', () => {
  it('renders the judge popover layout when the selected span is a judge span', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    // The popover card carries the judge-mode data attribute.
    const card = screen.getByTestId('span-popover');
    expect(card.getAttribute('data-judge')).toBe('true');
    // The judge body subtree is present.
    expect(screen.getByTestId('span-popover-judge-body')).toBeTruthy();
    // The verdict banner renders with the correct tone.
    const banner = screen.getByTestId('judge-popover-banner');
    expect(banner.getAttribute('data-tone')).toBe('off_task_warning');
    expect(banner.textContent).toMatch(/Off task.*warning/i);
  });

  it('shows the lead reason directly below the banner', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    expect(screen.getByTestId('judge-popover-lead').textContent).toMatch(
      /summarising instead of citing/i,
    );
  });

  it('shows the context row with judging-subject + task + model + elapsed', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    expect(
      screen.getByTestId('judge-popover-subject').textContent,
    ).toContain('agent-a');
    expect(screen.getByTestId('judge-popover-task').textContent).toContain(
      'task-42',
    );
    expect(screen.getByTestId('judge-popover-model').textContent).toContain(
      'haiku-4',
    );
    expect(
      screen.getByTestId('judge-popover-elapsed').textContent,
    ).toContain('248ms');
  });

  it('renders the input-preview section collapsed, expandable on click', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    const section = screen.getByTestId('judge-popover-input');
    expect(section.getAttribute('data-open')).toBe('false');
    fireEvent.click(screen.getByTestId('judge-popover-input-toggle'));
    expect(screen.getByTestId('judge-popover-input-body').textContent).toContain(
      'summarise',
    );
  });

  it('does NOT render Steer or Annotate buttons on a judge popover', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    const card = screen.getByTestId('span-popover');
    // Neither button should be present anywhere in the card subtree.
    expect(card.textContent).not.toMatch(/\bSteer\b/);
    expect(card.textContent).not.toMatch(/\bAnnotate\b/);
  });

  it('keeps Open drawer + Copy id action buttons for judge spans', () => {
    const store = new SessionStore();
    const span = makeJudgeSpan();
    store.spans.append(span);
    usePopoverStore.getState().openForSpan(span.id, 100, 120);

    render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

    const card = screen.getByTestId('span-popover');
    expect(card.textContent).toContain('Open drawer');
    expect(card.textContent).toContain('Copy id');
  });

  // Parametrized: verdict-banner color per severity bucket.
  const banners: Array<{
    label: string;
    over: Partial<Span>;
    expectedTone: string;
    expectedLabel: RegExp;
  }> = [
    {
      label: 'on-task (green)',
      over: {
        attributes: {
          'judge.kind': { kind: 'string', value: 'judge' },
          'judge.verdict': { kind: 'string', value: 'on_task' },
          'judge.on_task': { kind: 'bool', value: true },
          'judge.severity': { kind: 'string', value: '' },
          'judge.reason': {
            kind: 'string',
            value: 'Agent is making expected progress.',
          },
        },
      },
      expectedTone: 'on_task',
      expectedLabel: /On task/i,
    },
    {
      label: 'off-task critical (red)',
      over: {
        attributes: {
          'judge.kind': { kind: 'string', value: 'judge' },
          'judge.verdict': {
            kind: 'string',
            value: 'reasoning_drift_detected',
          },
          'judge.on_task': { kind: 'bool', value: false },
          'judge.severity': { kind: 'string', value: 'critical' },
          'judge.reason': { kind: 'string', value: 'Invented tool output.' },
        },
      },
      expectedTone: 'off_task_critical',
      expectedLabel: /Off task.*critical/i,
    },
    {
      label: 'no-verdict (grey)',
      over: {
        attributes: {
          'judge.kind': { kind: 'string', value: 'judge' },
          'judge.raw_response': { kind: 'string', value: '{"bad_json":' },
        },
      },
      expectedTone: 'no_verdict',
      expectedLabel: /No verdict/i,
    },
  ];
  for (const b of banners) {
    it(`renders the ${b.label} banner with the correct tone + label`, () => {
      const store = new SessionStore();
      const span = makeJudgeSpan({ ...b.over, id: `span-${b.expectedTone}` });
      store.spans.append(span);
      usePopoverStore.getState().openForSpan(span.id, 100, 120);

      render(<SpanPopover ctx={makeCtx(store)} sessionId="sess-1" />);

      const banner = screen.getByTestId('judge-popover-banner');
      expect(banner.getAttribute('data-tone')).toBe(b.expectedTone);
      expect(banner.textContent).toMatch(b.expectedLabel);
    });
  }

  it('isJudgeSpan predicate still matches synthesized judge spans (co-routing invariant)', () => {
    // Co-ordination with /tmp/harmonograf-goldfive-render: their
    // GoldfiveSpanDetail branch also touches SpanPopover.tsx and will
    // rebase onto this one. The two paths diverge solely on
    // isJudgeSpan(span) — verify that predicate hasn't regressed.
    const span = makeJudgeSpan();
    expect(isJudgeSpan(span)).toBe(true);
    const nonJudge = makeJudgeSpan({
      id: 'other',
      name: 'tool: search',
      agentId: 'client:agent-b',
      attributes: { 'judge.kind': { kind: 'string', value: 'drift' } },
    });
    expect(isJudgeSpan(nonJudge)).toBe(false);
  });
});
