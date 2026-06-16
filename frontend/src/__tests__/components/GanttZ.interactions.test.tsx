// Wheel-zoom + minimap-seek coverage for the zicato gantt. These are the two
// interactions the user reports "broken"; the existing zoom test only clicks
// the +/− buttons. Both handlers map clientX → time via getBoundingClientRect,
// which jsdom reports as all-zeros (width 0) → the handlers early-return and
// never run. So we MOCK the rect to a real width and drive the native wheel
// listener + the minimap pointer seek, asserting a span actually moves.

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { GanttViewZ } from '../../components/zicato/GanttViewZ';
import { EMPTY_SESSION, type ZSession } from '../../components/zicato/adapter';

const W = 940; // <Fig> fallback width in jsdom (no ResizeObserver).

function mkSession(): ZSession {
  return {
    ...EMPTY_SESSION,
    id: 'interact-test',
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
        t0: 30,
        t1: 50,
        label: 'work',
        hasReasoning: false,
        reasoning: null,
      },
    ],
  };
}

const barX = (): number =>
  Number(
    (document.querySelector('[data-span="s1"]') as SVGRectElement).getAttribute('x'),
  );

let origRect: typeof Element.prototype.getBoundingClientRect;

beforeEach(() => {
  // Map the SVG to a real on-screen box so clientX → svg-x → time works. width
  // = W so 1 client px = 1 viewBox unit (the figure renders at 1:1 via <Fig>).
  origRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (): DOMRect {
    return {
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: W,
      bottom: 300,
      width: W,
      height: 300,
      toJSON: () => ({}),
    } as DOMRect;
  };
});

afterEach(() => {
  Element.prototype.getBoundingClientRect = origRect;
});

describe('zicato gantt — wheel zoom', () => {
  it('scroll-up zooms in toward the cursor and moves a span', () => {
    render(<GanttViewZ z={mkSession()} />);
    const x0 = barX();
    const svg = screen.getByLabelText(/execution gantt/i);
    // Native non-passive listener (React onWheel is passive in 19) → fireEvent
    // dispatches a real WheelEvent the listener catches.
    fireEvent.wheel(svg, { deltaY: -120, clientX: 200 });
    expect(barX()).not.toBe(x0); // window narrowed → the bar shifted
    // The view is no longer the full range, so "fit" re-enables.
    expect(screen.getByLabelText('fit to range')).not.toBeDisabled();
  });

  it('scroll-down from a zoomed window widens it back out', () => {
    render(<GanttViewZ z={mkSession()} />);
    fireEvent.click(screen.getByLabelText('zoom in'));
    const zoomed = barX();
    const svg = screen.getByLabelText(/execution gantt/i);
    fireEvent.wheel(svg, { deltaY: 120, clientX: 200 }); // zoom OUT
    expect(barX()).not.toBe(zoomed);
  });
});

describe('zicato gantt — drag the timeline to scroll', () => {
  it('dragging the gantt with the button HELD pans (scrolls) a zoomed window', () => {
    render(<GanttViewZ z={mkSession()} />);
    fireEvent.click(screen.getByLabelText('zoom in')); // zoom so there's room to pan
    const before = barX();
    const svg = screen.getByLabelText(/execution gantt/i);
    fireEvent.pointerDown(svg, { button: 0, buttons: 1, clientX: 500, pointerId: 1 });
    // buttons:1 = left button held → this is a drag.
    fireEvent.pointerMove(svg, { buttons: 1, clientX: 380, pointerId: 1 });
    fireEvent.pointerUp(svg, { clientX: 380, pointerId: 1 });
    expect(barX()).not.toBe(before); // the visible window scrolled
  });

  it('moving the mouse with NO button does NOT pan (only drags pan)', () => {
    // Regression: after a drag the drag-state lingers (kept alive for the
    // trailing click), and the pointer is not captured — so plain hover moves
    // fire on the SVG. Without the button guard those button-less moves would
    // pan the gantt, making the spans impossible to navigate.
    render(<GanttViewZ z={mkSession()} />);
    fireEvent.click(screen.getByLabelText('zoom in'));
    const svg = screen.getByLabelText(/execution gantt/i);
    // Do a real drag first so drag-state exists and lingers past pointerup.
    fireEvent.pointerDown(svg, { button: 0, buttons: 1, clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(svg, { buttons: 1, clientX: 420, pointerId: 1 });
    fireEvent.pointerUp(svg, { clientX: 420, pointerId: 1 });
    const settled = barX();
    // Now just move the mouse around with NO button held — must NOT pan.
    fireEvent.pointerMove(svg, { buttons: 0, clientX: 300, pointerId: 1 });
    fireEvent.pointerMove(svg, { buttons: 0, clientX: 700, pointerId: 1 });
    expect(barX()).toBe(settled); // the window did not move on button-less hover
  });
});

describe('zicato gantt — minimap seek + brush-zoom', () => {
  it('a plain click on the minimap pans a zoomed window (recenters)', () => {
    render(<GanttViewZ z={mkSession()} />);
    // Zoom in first so the window is narrower than the full range; only then is
    // there anything to pan to (a full window can't move). A click = down+up.
    fireEvent.click(screen.getByLabelText('zoom in'));
    const beforeSeek = barX();
    const mm = screen.getByLabelText(/gantt minimap/i);
    fireEvent.pointerDown(mm, { button: 0, clientX: 880, pointerId: 1 });
    fireEvent.pointerUp(mm, { button: 0, clientX: 880, pointerId: 1 });
    expect(barX()).not.toBe(beforeSeek);
  });

  it('a plain minimap click at the full range is a no-op (nothing to pan)', () => {
    render(<GanttViewZ z={mkSession()} />);
    const x0 = barX(); // fit (full range)
    const mm = screen.getByLabelText(/gantt minimap/i);
    fireEvent.pointerDown(mm, { button: 0, clientX: 470, pointerId: 1 });
    fireEvent.pointerUp(mm, { button: 0, clientX: 470, pointerId: 1 });
    expect(barX()).toBe(x0);
  });

  it('dragging a region on the minimap zooms the gantt into that section', () => {
    render(<GanttViewZ z={mkSession()} />);
    const x0 = barX();
    expect(screen.getByLabelText('fit to range')).toBeDisabled(); // starts at fit
    const mm = screen.getByLabelText(/gantt minimap/i);
    fireEvent.pointerDown(mm, { button: 0, clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(mm, { clientX: 600, pointerId: 1 }); // brush a region
    // The live selection rectangle is drawn while dragging.
    expect(document.querySelector('[data-testid="zk-mm-brush"]')).not.toBeNull();
    fireEvent.pointerUp(mm, { clientX: 600, pointerId: 1 });
    expect(barX()).not.toBe(x0); // window zoomed to the brushed region
    expect(screen.getByLabelText('fit to range')).not.toBeDisabled();
    // The brush rectangle clears on release.
    expect(document.querySelector('[data-testid="zk-mm-brush"]')).toBeNull();
  });
});
