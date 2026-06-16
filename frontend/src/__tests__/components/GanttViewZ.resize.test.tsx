// TASK #5 — the gantt vertical resize pill. The gantt fits ALL agent lanes, so a
// many-lane session is very tall; the centred drag pill above the minimap caps
// the gantt scroll container to a px height (and scrolls when content overflows).
//
// We render GanttViewZ, grab the resize pill (role=separator / aria-label), and
// drive a pointerdown → pointermove → pointerup gesture. The FIRST drag seeds the
// base height from the scroll container's clientHeight, which jsdom reports as 0,
// so we stub clientHeight to a natural height. After a downward drag (dy>0) the
// scroll container must carry an inline height that grew by dy.

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { GanttViewZ } from '../../components/zicato/GanttViewZ';
import { EMPTY_SESSION, type ZSession } from '../../components/zicato/adapter';

const NATURAL_H = 400; // pretend the auto-laid-out gantt is 400px tall.

function mkSession(): ZSession {
  return {
    ...EMPTY_SESSION,
    id: 'resize-test',
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

const pill = (): HTMLElement => screen.getByLabelText('resize gantt height');
const scroll = (): HTMLElement =>
  document.querySelector('.zk-gantt-scroll') as HTMLElement;

let clientHeightSpy: PropertyDescriptor | undefined;

beforeAll(() => {
  // jsdom reports clientHeight as 0; the first-drag seed reads it, so stub a
  // realistic natural height onto every element.
  clientHeightSpy = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    'clientHeight',
  );
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() {
      return NATURAL_H;
    },
  });
});

afterAll(() => {
  if (clientHeightSpy) {
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', clientHeightSpy);
  }
});

describe('GanttViewZ — vertical resize pill', () => {
  it('starts with no inline height (natural/auto layout)', () => {
    render(<GanttViewZ z={mkSession()} />);
    const sc = scroll();
    expect(sc).toBeTruthy();
    // null state → no height / overflow applied; the gantt fits all lanes.
    expect(sc.style.height).toBe('');
    expect(sc.style.overflowY).toBe('');
  });

  it('dragging the pill DOWN seeds from clientHeight and grows the gantt by dy', () => {
    render(<GanttViewZ z={mkSession()} />);
    const handle = pill();
    fireEvent.pointerDown(handle, { clientY: 100, pointerId: 7 });
    fireEvent.pointerMove(handle, { clientY: 160, pointerId: 7 }); // dy = +60
    fireEvent.pointerUp(handle, { clientY: 160, pointerId: 7 });
    const sc = scroll();
    // base (clientHeight=400) + dy(60) = 460, and overflow becomes scrollable.
    expect(sc.style.height).toBe('460px');
    expect(sc.style.overflowY).toBe('auto');
  });

  it('dragging UP shrinks the gantt height', () => {
    render(<GanttViewZ z={mkSession()} />);
    const handle = pill();
    fireEvent.pointerDown(handle, { clientY: 200, pointerId: 7 });
    fireEvent.pointerMove(handle, { clientY: 100, pointerId: 7 }); // dy = -100
    fireEvent.pointerUp(handle, { clientY: 100, pointerId: 7 });
    expect(scroll().style.height).toBe('300px'); // 400 - 100
  });

  it('clamps the height to the [120, 2000] range', () => {
    render(<GanttViewZ z={mkSession()} />);
    const handle = pill();
    // Drag far UP: 400 - 1000 = -600 → clamped to the 120 floor.
    fireEvent.pointerDown(handle, { clientY: 1000, pointerId: 7 });
    fireEvent.pointerMove(handle, { clientY: 0, pointerId: 7 });
    fireEvent.pointerUp(handle, { clientY: 0, pointerId: 7 });
    expect(scroll().style.height).toBe('120px');
  });

  it('a pointermove with no active drag does NOT change the height', () => {
    render(<GanttViewZ z={mkSession()} />);
    const handle = pill();
    // Move without a preceding pointerdown — dragRef is null, must no-op.
    fireEvent.pointerMove(handle, { clientY: 999, pointerId: 7 });
    expect(scroll().style.height).toBe('');
  });
});
