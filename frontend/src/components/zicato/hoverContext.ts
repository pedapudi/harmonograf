// hoverContext.ts — routes a Gantt span hover up to the console's quick-look
// hovercard WITHOUT threading a prop through the GanttViewZ / Fig layers.
// ZicatoConsole provides the handler via <SpanHoverContext.Provider>; GanttZ
// consumes it with useSpanHover() and reports `(spanId, rect)` on pointer-enter
// and clears on pointer-leave. No-op defaults keep GanttZ safe when rendered
// standalone (tests, the minimap, future call sites outside the console).
//
// Mirrors steerContext.ts. Lives in its own non-component module so the
// component files (ZicatoConsole.tsx, SpanHovercardZ.tsx) stay
// Fast-Refresh-clean (a file that exports a React component may not also export
// a hook).

import { createContext, useContext } from 'react';

/** The hovered span's identity + its live on-screen box (client coords). */
export interface SpanHoverPayload {
  spanId: string;
  /** getBoundingClientRect of the hovered span <rect> (viewport coords). */
  rect: DOMRect;
}

export interface SpanHoverHandlers {
  /** Report a span as hovered, anchored at its on-screen rect. */
  report: (spanId: string, rect: DOMRect) => void;
  /** Dismiss the hovered span (pointer-leave / drag start). */
  clear: () => void;
}

const NOOP_HANDLERS: SpanHoverHandlers = {
  report: () => {},
  clear: () => {},
};

export const SpanHoverContext =
  createContext<SpanHoverHandlers>(NOOP_HANDLERS);

/** GanttZ's hover reporter — the console's hovercard opener/closer. */
export function useSpanHover(): SpanHoverHandlers {
  return useContext(SpanHoverContext);
}
