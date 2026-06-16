// Fig — render a figure at its container's TRUE pixel width.
//
// The zicato figures draw into a fixed viewBox (e.g. `0 0 940 H`) with the SVG
// at `width:100%`. On a wide pane that upscales the whole drawing — text, bars,
// strokes — by container/viewBox (≈2× at 1800px), so "everything looks too
// large". Measuring the container and passing that width as the viewBox width
// keeps 1 viewBox unit = 1 CSS px, so labels/strokes render at their intended
// size at any width (the figure still fills the pane horizontally).

import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';

export function useMeasuredWidth(
  fallback: number,
): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    // jsdom (tests) and very old browsers lack ResizeObserver — fall back to the
    // provided width so the figure still renders (and the smoke test passes).
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const cw = entries[0]?.contentRect.width ?? 0;
      if (cw > 0) setW(Math.round(cw));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export function Fig({
  fallback = 940,
  children,
}: {
  fallback?: number;
  children: (w: number) => ReactNode;
}) {
  const [ref, w] = useMeasuredWidth(fallback);
  return (
    <div ref={ref} style={{ width: '100%' }}>
      {children(w)}
    </div>
  );
}
