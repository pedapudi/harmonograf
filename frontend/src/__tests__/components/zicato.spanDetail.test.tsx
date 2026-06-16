// Span detail surfaces in the zicato console: the quick-look HOVERCARD content
// and the click→drawer SELECTION path (a plain click selects; a drag does not).
// These lock in the two features the user reported missing.

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { SpanHovercardZ } from '../../components/zicato/SpanHovercardZ';
import { GanttZ } from '../../components/zicato/GanttZ';
import { EMPTY_SESSION, type ZSession } from '../../components/zicato/adapter';
import type { Span } from '../../gantt/types';

function mkSpan(over: Partial<Span> & Pick<Span, 'id' | 'agentId'>): Span {
  return {
    sessionId: 's',
    parentSpanId: null,
    kind: 'LLM_CALL',
    status: 'COMPLETED',
    name: over.name ?? over.id,
    startMs: 0,
    endMs: 8000,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
    ...over,
  };
}

const mkRect = (over: Partial<DOMRect> = {}): DOMRect =>
  ({
    x: 100,
    y: 100,
    left: 100,
    top: 100,
    right: 140,
    bottom: 112,
    width: 40,
    height: 12,
    toJSON: () => ({}),
    ...over,
  }) as DOMRect;

describe('SpanHovercardZ — quick-look content', () => {
  it('renders title, kind/status pills, duration, and the 🧠 preview when reasoning is present', () => {
    const span = mkSpan({
      id: 'sp1',
      agentId: 'client:coder',
      name: 'do the thing',
      endMs: 8000,
      attributes: {
        'llm.reasoning': { kind: 'string', value: 'I will read the file first, then edit it.' },
      },
    });
    render(
      <SpanHovercardZ
        span={span}
        anchor={mkRect()}
        containerRect={mkRect({ left: 0, top: 0, width: 1200, height: 700 })}
      />,
    );
    expect(screen.getByTestId('zk-hovercard-title').textContent).toBe('do the thing');
    expect(screen.getByText('LLM_CALL')).toBeTruthy();
    expect(screen.getByText('completed')).toBeTruthy();
    expect(screen.getByTestId('zk-hovercard-duration').textContent).toBe('8.0s');
    const reasoning = screen.getByTestId('zk-hovercard-reasoning');
    expect(reasoning.textContent).toContain('🧠');
    expect(reasoning.textContent).toContain('read the file');
  });

  it('omits the reasoning block for a plain span', () => {
    const span = mkSpan({ id: 'sp2', agentId: 'client:coder', name: 'plain' });
    render(
      <SpanHovercardZ
        span={span}
        anchor={mkRect()}
        containerRect={mkRect({ width: 1200, height: 700 })}
      />,
    );
    expect(screen.queryByTestId('zk-hovercard-reasoning')).toBeNull();
    expect(screen.getByText('click for full detail')).toBeTruthy();
  });
});

function mkSession(): ZSession {
  return {
    ...EMPTY_SESSION,
    id: 'click-test',
    T: 100,
    now: 100,
    empty: false,
    agents: [{ id: 'a', label: 'coder', ordinal: 1, synthetic: null }],
    spans: [
      {
        id: 's1',
        agent: 'a',
        kind: 'llm-call',
        status: 'completed',
        gf: null,
        t0: 10,
        t1: 60,
        label: 'work',
        hasReasoning: false,
        reasoning: null,
      },
    ],
  };
}

describe('GanttZ — span click selects (opens drawer); drag does not', () => {
  it('a plain click on a span calls onSpanSelect', () => {
    const onSpanSelect = vi.fn();
    const { container } = render(
      <GanttZ
        z={mkSession()}
        view={{ t0: 10, t1: 60 }}
        onViewChange={() => {}}
        onSpanSelect={onSpanSelect}
      />,
    );
    fireEvent.click(container.querySelector('[data-span="s1"]')!);
    expect(onSpanSelect).toHaveBeenCalledWith('s1');
  });

  it('a drag (button held, moved past threshold) does NOT select the span', () => {
    const onSpanSelect = vi.fn();
    const { container } = render(
      <GanttZ
        z={mkSession()}
        view={{ t0: 10, t1: 60 }}
        onViewChange={() => {}}
        onSpanSelect={onSpanSelect}
      />,
    );
    const svg = container.querySelector('svg.fig')!;
    fireEvent.pointerDown(svg, { button: 0, buttons: 1, clientX: 300, pointerId: 1 });
    fireEvent.pointerMove(svg, { buttons: 1, clientX: 180, pointerId: 1 }); // 120px drag
    fireEvent.pointerUp(svg, { clientX: 180, pointerId: 1 });
    // The trailing click on the span is swallowed because the gesture was a drag.
    fireEvent.click(container.querySelector('[data-span="s1"]')!);
    expect(onSpanSelect).not.toHaveBeenCalled();
  });
});
