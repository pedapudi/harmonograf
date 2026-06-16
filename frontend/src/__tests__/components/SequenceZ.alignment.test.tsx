// SequenceZ alignment + return-label declutter (fix #B).
//
// Messages must sit at their TRUE time position (yT(m.t)) — NOT on min-gap rows —
// so a delegate arrow lines up with the TOP of the target's activation bar and a
// return lines up with its BOTTOM (both share the yT(time) scale). And the noisy,
// repeated "return" text labels are dropped (the dashed line + leftward arrowhead
// already reads as a return), while delegate / transfer / user / agent labels stay.

import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SequenceZ } from '../../components/zicato/SequenceZ';
import { EMPTY_SESSION, type ZSession } from '../../components/zicato/adapter';

const W = 940;
const H = 420;
const padT = 52;
// yT mirrors SequenceZ's internal time→y mapping for the chosen W/H/T.
const yT = (t: number, T: number): number => padT + (t / (T > 0 ? T : 1)) * (H - padT - 26);

// Two real agents (coder delegates to reviewer and gets a return) on a 30s clock.
function fixture(): ZSession {
  return {
    ...EMPTY_SESSION,
    id: 'sess-align',
    goal: 'ship the feature now',
    empty: false,
    T: 30,
    now: 20,
    agents: [
      { id: 'c:coder', label: 'coder', ordinal: 1, synthetic: null },
      { id: 'c:reviewer', label: 'reviewer', ordinal: 2, synthetic: null },
    ],
    spans: [
      // reviewer's activation bar runs 8s..16s — delegate lands on its TOP (8s),
      // the return leaves from its BOTTOM (16s).
      {
        id: 'sp-review',
        agent: 'c:reviewer',
        kind: 'llm-call',
        status: 'running',
        gf: null,
        t0: 8,
        t1: 16,
        label: 'review changes',
        hasReasoning: false,
        reasoning: null,
      },
    ],
    edges: [
      { t: 8, from: 'c:coder', to: 'c:reviewer', kind: 'delegation' },
      { t: 16, from: 'c:reviewer', to: 'c:coder', kind: 'return' },
    ],
  };
}

function lineY1(el: Element): number {
  return Number(el.getAttribute('y1'));
}

describe('SequenceZ — message alignment + return declutter (fix #B)', () => {
  it('places each message at its true time (yT(t)), aligning with the activation bar', () => {
    const z = fixture();
    const { container } = render(<SequenceZ z={z} W={W} H={H} />);

    const msgLines = Array.from(container.querySelectorAll('line.sq-msg'));
    expect(msgLines).toHaveLength(2);

    // The target activation bar (top = yT(8), bottom = yT(8)+height).
    const bar = container.querySelector('rect.sq-act[data-span="sp-review"]')!;
    const barTop = Number(bar.getAttribute('y'));
    const barBottom = barTop + Number(bar.getAttribute('height'));

    // Delegate arrow (t=8) lines up with the TOP of the activation bar.
    const delegate = msgLines.find((l) => l.getAttribute('stroke-dasharray') == null)!;
    expect(lineY1(delegate)).toBeCloseTo(yT(8, z.T), 5);
    expect(lineY1(delegate)).toBeCloseTo(barTop, 5);

    // Return arrow (t=16, dashed) lines up with the BOTTOM of the activation bar.
    const ret = msgLines.find((l) => l.getAttribute('stroke-dasharray') === '4 3')!;
    expect(lineY1(ret)).toBeCloseTo(yT(16, z.T), 5);
    expect(lineY1(ret)).toBeCloseTo(barBottom, 5);

    // And they are NOT collapsed onto consecutive min-gap rows (true Δ = yT(16)-yT(8)).
    expect(lineY1(ret) - lineY1(delegate)).toBeCloseTo(yT(16, z.T) - yT(8, z.T), 5);
  });

  it('drops the "return" text label but keeps the delegate label', () => {
    const z = fixture();
    const { container } = render(<SequenceZ z={z} W={W} H={H} />);

    const labels = Array.from(container.querySelectorAll('text.sq-lbl')).map(
      (t) => t.textContent,
    );
    expect(labels).toContain('delegate');
    expect(labels).not.toContain('return');
  });
});
