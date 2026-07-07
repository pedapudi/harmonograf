// Hover-state controller + selection helper for the zicato span hovercard.
//
// Split out of SpanHovercardZ.tsx so that component file exports ONLY the
// component (react-refresh/only-export-components) while the pure pin
// helper and the stateful hook stay unit-testable in isolation.

import { useEffect, useRef, useState } from 'react';

/** What the console tracks for the currently-hovered span. */
export interface HoveredSpan {
  spanId: string;
  rect: DOMRect;
}

/**
 * The span whose hovercard should be shown, given the selection + hover state.
 *
 * A SELECTED span (the user clicked it → drawer open) PINS its hovercard: it
 * wins over the transient hover and stays put until deselected. With nothing
 * selected we fall back to the hovered span (the original transient behaviour),
 * or null when neither is set.
 *
 * Kept pure (no React) so the console's pin logic is unit-testable in isolation.
 */
export function displayedSpanId(
  selectedSpanId: string | null,
  hovered: HoveredSpan | null,
): string | null {
  return selectedSpanId ?? hovered?.spanId ?? null;
}

/**
 * A tiny stateful hook the console uses to debounce hover enter/leave with a
 * grace delay so the card doesn't vanish the instant the pointer slides off a
 * thin (4px) bar. Enter is immediate; leave is delayed by ~120ms and can be
 * cancelled by a re-enter. Timers are cleared on unmount.
 */
export function useHoverController(): {
  hovered: HoveredSpan | null;
  report: (spanId: string, rect: DOMRect) => void;
  clear: () => void;
} {
  const [hovered, setHovered] = useState<HoveredSpan | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelLeave = (): void => {
    if (leaveTimer.current != null) {
      clearTimeout(leaveTimer.current);
      leaveTimer.current = null;
    }
  };

  const report = (spanId: string, rect: DOMRect): void => {
    cancelLeave();
    setHovered({ spanId, rect });
  };

  const clear = (): void => {
    cancelLeave();
    leaveTimer.current = setTimeout(() => {
      setHovered(null);
      leaveTimer.current = null;
    }, 120);
  };

  // Clean up the pending timer on unmount.
  useEffect(() => cancelLeave, []);

  return { hovered, report, clear };
}
