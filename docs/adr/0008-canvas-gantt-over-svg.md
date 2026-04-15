# ADR 0008 — Canvas rendering for the Gantt chart

## Status

Accepted.

## Context

The Gantt view is the primary surface of harmonograf. It renders:

- One row per agent.
- One block per span, positioned by start/end time on the X axis.
- Live updates — blocks grow as spans run, animate as status changes.
- Cross-agent arrows for `LINK_INVOKED` edges (transfers, tool invocations
  across agents).
- Zoom, pan, scrub, live-tail cursor, minimap, popovers, inspector drawer
  interactions, keyboard navigation.
- A viewport that can easily contain tens of thousands of span blocks
  across a multi-agent session without the frame rate collapsing.

Three options for the render layer:

1. **DOM + CSS** — every span is a `<div>`. React-friendly, easy to
   hit-test, accessible, slow beyond a few thousand elements.
2. **SVG** — every span is a `<rect>`. Declarative, React-friendly, also
   DOM-backed, also slow beyond a few thousand elements. Animations
   composite poorly once the tree is large.
3. **Canvas 2D** — imperative draw on every frame. One HTMLCanvasElement
   for the whole chart, manual hit-testing via a spatial index, manual
   redraw driven by a render loop.
4. **WebGL** — maximum performance, maximum complexity. Overkill for
   2D axis-aligned rectangles at v0 scales.

## Decision

Render the Gantt on **Canvas 2D**, with a spatial index
(`frontend/src/gantt/spatialIndex.ts`) for hit-testing and a custom
render loop in `frontend/src/gantt/renderer.ts`. React owns the
surrounding app shell (drawer, transport bar, AppBar, popover) but does
not own the Gantt interior — the canvas element is a stable leaf that
React mounts once, and the renderer redraws on its own RAF loop.

Interaction points that *need* DOM for accessibility or text input
(popover on click, inspector drawer, floating tooltips) live as React
components positioned over the canvas using DOM coordinates derived
from the renderer's transform.

## Consequences

**Good.**
- Scales to tens of thousands of spans without React reconciliation
  cost dominating the frame. This is the real reason. Early iterations
  used SVG and visibly stuttered past a few thousand spans.
- One redraw path. Live-tail, scroll, zoom, animation all flow through
  the same RAF loop in `renderer.ts`, which is straightforward to
  reason about — no partial React re-renders racing a CSS animation.
- The spatial index (see `spatialIndex.ts`) gives us hit-testing at
  arbitrary viewport scales, including the dedup fix in commit
  `c2801d2 frontend(gantt): dedupe SpanIndex.append by span id`.
- Canvas hands us a clean seam for the minimap — it renders the same
  model at a different scale using the same draw routines.

**Bad.**
- **Accessibility is worse by default.** Screen readers cannot read a
  canvas. Harmonograf has to reimplement keyboard navigation and
  expose interactive targets through ARIA-labeled overlay DOM, and a
  strict reading of WCAG is not met by the canvas view. This is a
  known v0 cost. The inspector drawer and popover are DOM and
  accessible; the timeline proper is not yet.
- **Hit-testing is manual.** Every click and keyboard navigation has to
  look up a span via the spatial index. This is well-contained but is
  code we own rather than code the DOM gives us.
- **Text rendering is worse than DOM.** Canvas text is pixel-aligned
  and does not get browser-native subpixel AA. Zooming to fractional
  scales looks slightly fuzzier than the equivalent SVG rendering.
- **Debugging with browser devtools is limited.** The element inspector
  cannot reach individual spans; developers debug by reading model
  state in React devtools and correlating to the spatial index.
- **Animation is manual.** Span growth, status transitions, and
  attention-attracting highlights are all coded in the render loop
  rather than delegated to CSS. This is fine at the current scope
  but any elaborate animation (e.g., easing curves on many elements
  at once) has to be hand-written.

The decision is forced by scale. DOM/SVG could not keep up with the
target workload, and the accessibility cost is real but mitigable with
effort in the interaction layer. The renderer abstraction keeps the
Gantt decoupled from React enough that swapping to WebGL later is not a
rewrite.
