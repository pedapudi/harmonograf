// Pinned-hovercard logic for the zicato console: when a span is SELECTED the
// quick-look hovercard pins to THAT span and survives a transient hover clear;
// with nothing selected it tracks the hover as before. We test the pure
// selection helper (displayedSpanId) plus the hover controller's grace-delay
// clear, which together drive ZicatoConsole's `cardSpanId`.

import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  displayedSpanId,
  useHoverController,
  type HoveredSpan,
} from '../../components/zicato/SpanHovercardZ';

const mkRect = (): DOMRect =>
  ({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 40,
    bottom: 12,
    width: 40,
    height: 12,
    toJSON: () => ({}),
  }) as DOMRect;

const mkHovered = (spanId: string): HoveredSpan => ({
  spanId,
  rect: mkRect(),
});

describe('displayedSpanId — selection pins over hover', () => {
  it('returns the SELECTED span even while a different span is hovered', () => {
    expect(displayedSpanId('sel-1', mkHovered('hov-2'))).toBe('sel-1');
  });

  it('falls back to the hovered span when nothing is selected', () => {
    expect(displayedSpanId(null, mkHovered('hov-2'))).toBe('hov-2');
  });

  it('returns null when neither selection nor hover is set', () => {
    expect(displayedSpanId(null, null)).toBeNull();
  });

  it('keeps the SELECTED span as the target after the hover is cleared', () => {
    // The console recomputes cardSpanId on every render; once the hover clears
    // (null) the selection still wins → the card stays pinned.
    expect(displayedSpanId('sel-1', null)).toBe('sel-1');
  });

  it('drops back to null once the span is deselected (and not hovered)', () => {
    expect(displayedSpanId(null, null)).toBeNull();
  });
});

describe('useHoverController — grace-delay clear (transient path)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('reports immediately and clears only after the ~120ms grace delay', () => {
    const { result } = renderHook(() => useHoverController());

    act(() => result.current.report('hov-2', mkRect()));
    expect(result.current.hovered?.spanId).toBe('hov-2');

    act(() => result.current.clear());
    // Still present right after clear() — the leave is debounced.
    expect(result.current.hovered?.spanId).toBe('hov-2');

    act(() => vi.advanceTimersByTime(150));
    expect(result.current.hovered).toBeNull();
  });

  it('a re-enter cancels the pending leave (card does not flicker out)', () => {
    const { result } = renderHook(() => useHoverController());
    act(() => result.current.report('hov-2', mkRect()));
    act(() => result.current.clear());
    act(() => result.current.report('hov-2', mkRect())); // re-enter
    act(() => vi.advanceTimersByTime(150));
    expect(result.current.hovered?.spanId).toBe('hov-2');
  });
});

describe('pin behaviour composed (selection + controller)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('a selected span stays the displayed target across a hover clear, then null when deselected', () => {
    const { result } = renderHook(() => useHoverController());

    // User clicks span "sel-1" → selection drives the displayed span.
    let selectedSpanId: string | null = 'sel-1';
    act(() => result.current.report('sel-1', mkRect()));
    expect(displayedSpanId(selectedSpanId, result.current.hovered)).toBe('sel-1');

    // Pointer leaves the bar → transient hover clears after the grace delay,
    // but the SELECTION still pins the card to sel-1.
    act(() => result.current.clear());
    act(() => vi.advanceTimersByTime(150));
    expect(result.current.hovered).toBeNull();
    expect(displayedSpanId(selectedSpanId, result.current.hovered)).toBe('sel-1');

    // Deselect (closeDrawer / Esc) → nothing pinned, nothing hovered → no card.
    selectedSpanId = null;
    expect(displayedSpanId(selectedSpanId, result.current.hovered)).toBeNull();
  });
});
