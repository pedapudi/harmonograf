// Regression: the zicato gantt zoom/pan viewport actually changes the rendered
// window. Clicking + (zoom in) must move a span's x; fit must restore it.
// Pure render test (no RPC/browser) so it can't be fooled by a stale dist.

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { GanttViewZ } from '../../components/zicato/GanttViewZ';
import { EMPTY_SESSION, type ZSession } from '../../components/zicato/adapter';

function mkSession(): ZSession {
  return {
    ...EMPTY_SESSION,
    id: 'zoom-test',
    T: 100,
    now: 100,
    empty: false,
    agents: [{ id: 'a', label: 'coder', ordinal: 1, synthetic: null }],
    // t0=30,t1=50 stays inside the +-zoom window [20,80] so the bar is visible
    // before AND after, and its x must shift.
    spans: [
      {
        id: 's1',
        agent: 'a',
        kind: 'llm-call',
        status: 'completed',
        gf: null,
        t0: 30,
        t1: 50,
        label: 'work',
        hasReasoning: false,
        reasoning: null,
      },
    ],
  };
}

const barX = (): string | null =>
  (document.querySelector('[data-span="s1"]') as SVGRectElement | null)?.getAttribute('x') ?? null;

describe('zicato gantt zoom viewport', () => {
  it('zoom in (+) shifts a span; fit restores it', () => {
    render(<GanttViewZ z={mkSession()} />);
    const x0 = barX();
    expect(x0).not.toBeNull();

    fireEvent.click(screen.getByLabelText('zoom in'));
    const x1 = barX();
    expect(x1).not.toBe(x0); // the visible window narrowed → the bar moved

    fireEvent.click(screen.getByLabelText('fit to range'));
    expect(barX()).toBe(x0); // back to the full range
  });

  it('fit is disabled at the full range and enabled once zoomed', () => {
    render(<GanttViewZ z={mkSession()} />);
    expect(screen.getByLabelText('fit to range')).toBeDisabled();
    fireEvent.click(screen.getByLabelText('zoom in'));
    expect(screen.getByLabelText('fit to range')).not.toBeDisabled();
  });

  it('fit snaps to the content range, skipping an agent-startup lead-in', () => {
    // The session ends at T=100 but the first span does not start until t=33s
    // (agents take time to connect + emit). "Fit" must show [33, 100] — the
    // span sits at the LEFT plot edge — NOT [0, 100], which would push it a
    // third of the way across behind an empty band.
    const z: ZSession = {
      ...EMPTY_SESSION,
      id: 'lead-in',
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
          t0: 33,
          t1: 100,
          label: 'work',
          hasReasoning: false,
          reasoning: null,
        },
      ],
    };
    render(<GanttViewZ z={z} />);
    const x = Number(
      (document.querySelector('[data-span="s1"]') as SVGRectElement).getAttribute('x'),
    );
    // padL (76) = the left plot edge. Fit=[33,100] → X(33)=76. The old [0,100]
    // fit would have placed it at ~356 (76 + 850·33/100).
    expect(x).toBeGreaterThan(70);
    expect(x).toBeLessThan(85);
  });
});
