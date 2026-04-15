---
name: hgraf-add-renderer-overlay
description: Add a new canvas-layer overlay to the Gantt renderer — RAF draw order, layer composition, viewport transform, hit testing.
---

# hgraf-add-renderer-overlay

## When to use

You need to draw something on the Gantt canvas that is **not a span** — a highlight, a guide line, a badge, a warning icon, a selection outline, a context-window chart, a minimap cursor. This is renderer-layer-only: no proto changes, no server work.

**If you also need new data from the server**, see `hgraf-add-gantt-overlay.md` (batch 1), which covers the full client→proto→server→storage→frontend loop. This skill specializes on the canvas/draw-loop side.

## Prerequisites

1. Read `frontend/src/gantt/renderer.ts:1-150` — the `GanttRenderer` class, the `draw()` method, the layer ordering.
2. Read `frontend/src/gantt/contextOverlay.ts` — it's the canonical example of a compact, composable overlay with its own state and RAF draw contribution.
3. Read `frontend/src/gantt/viewport.ts` — world ↔ screen coordinate transforms. Your overlay will receive `(ctx, viewport, layout)` and must not mutate them.
4. Understand the layer order: background → grid → plan ghosts → span bars → row separators → overlays → labels → selection → cursor. Overlays draw **after** spans, **before** labels.

## Step-by-step

### 1. Sketch the overlay's contract

A clean overlay looks like:

```ts
// frontend/src/gantt/myOverlay.ts
import type { GanttLayout } from './layout';
import type { Viewport } from './viewport';

export interface MyOverlayState {
  visible: boolean;
  highlightSpanId: string | null;
}

export function drawMyOverlay(
  ctx: CanvasRenderingContext2D,
  vp: Viewport,
  layout: GanttLayout,
  state: MyOverlayState,
): void {
  if (!state.visible || !state.highlightSpanId) return;
  const bar = layout.barsBySpanId.get(state.highlightSpanId);
  if (!bar) return;
  ctx.save();
  ctx.strokeStyle = 'var(--hg-overlay-highlight, #ffcc00)';
  ctx.lineWidth = 2;
  ctx.strokeRect(bar.x, bar.y, bar.w, bar.h);
  ctx.restore();
}
```

Key rules:

- **Read-only inputs.** Never mutate `vp`, `layout`, or the source store.
- **`ctx.save()` / `ctx.restore()`** every call. Leaking canvas state between overlays is the most common overlay bug.
- **No React imports.** The renderer runs outside React; overlays must be pure draw functions plus a plain state object.

### 2. Hook into the renderer's draw loop

`GanttRenderer.draw()` in `frontend/src/gantt/renderer.ts` walks a fixed sequence of draw phases. Grep `this._drawSpanBars` and `this._drawLabels` to find the slot between them and insert your call:

```ts
// Inside draw()
this._drawSpanBars(ctx, vp, layout);
this._drawRowSeparators(ctx, vp, layout);
drawMyOverlay(ctx, vp, layout, this._myOverlayState);
this._drawLabels(ctx, vp, layout);
this._drawSelection(ctx, vp, layout);
```

Order matters. Overlays that should appear *underneath* span bars go before `_drawSpanBars`; highlights that should appear *over* labels go at the very end.

### 3. Store the overlay state on the renderer

Add a field on `GanttRenderer`:

```ts
private _myOverlayState: MyOverlayState = { visible: false, highlightSpanId: null };

setMyOverlayState(next: Partial<MyOverlayState>): void {
  this._myOverlayState = { ...this._myOverlayState, ...next };
  this._requestDraw();
}
```

Always call `this._requestDraw()` (the RAF scheduler) after any mutation — the renderer does not re-draw on state change unless you ask it to.

### 4. Expose a control handle

If UI code (a drawer tab, a toolbar toggle, a keyboard shortcut) needs to toggle the overlay, put the handle on `uiStore` rather than passing the renderer directly:

```ts
// frontend/src/state/uiStore.ts
ganttRenderer: GanttRenderer | null;  // already exists
toggleMyOverlay(visible: boolean): void {
  set({ myOverlayVisible: visible });
  get().ganttRenderer?.setMyOverlayState({ visible });
},
```

### 5. Hit testing (only if the overlay is interactive)

If clicks on the overlay should do something, plumb a hit-test into `GanttRenderer.spatialIndex` — see `frontend/src/gantt/spatialIndex.ts`. For static decorations, skip this entirely. Interactive overlays should re-use the existing `handleClick` path in `GanttCanvas.tsx` by consulting the spatial index first, then falling back to overlay hit logic.

### 6. Theme the overlay

Use CSS custom properties for colors. Grep `--hg-` in `frontend/src/theme/themes.ts` to see the existing variable namespace and add a new one there so both light and dark themes provide a value. Never hard-code a hex color in an overlay — it will invert badly across themes.

### 7. Performance budget

The renderer runs the full `draw()` cycle every RAF tick while the viewport animates. Your overlay runs inside that budget — aim for <0.5ms per draw on a 10k-span session:

- **Prefer `fillRect` / `strokeRect` / `arc`** — primitive paths are fast.
- **Avoid `ctx.measureText`** inside draw loops; it forces a font-shaping round trip. Measure once and cache.
- **Don't iterate all spans.** Use `layout.barsBySpanId.get(id)` or `layout.rowsByAgentId.get(id)` for O(1) lookups.
- **Don't allocate inside the draw function** if you can help it. Reuse a small scratch object on the renderer instance for intermediate math.

Benchmark with the stress page: `frontend/src/gantt/StressPage.tsx` renders large sessions; toggle your overlay and watch the frame meter.

### 8. Tests

- `frontend/src/__tests__/gantt/renderer.test.ts` — snapshot test that the overlay draws the expected pixels on a fixture layout. Use the existing canvas mock.
- If you used a theme variable, mock `getComputedStyle` to return a known color and assert the overlay used it.

### 9. Verification

```bash
cd frontend && pnpm test -- --run renderer
cd frontend && pnpm typecheck
cd frontend && pnpm dev  # visually verify overlay toggles on/off, animates, and survives theme switches
```

## Common pitfalls

- **Leaking canvas state**: forgetting `ctx.save()` / `ctx.restore()` poisons every later overlay. Wrap every overlay body in save/restore even if it seems unnecessary.
- **Drawing without `_requestDraw()`**: setting state outside the draw loop does nothing visible until the next external event causes a redraw. Always call the scheduler.
- **Coordinate space confusion**: `layout.barsBySpanId` uses screen coordinates already transformed by the viewport. Do **not** apply `vp.worldToScreen` on top of that — you'll double-transform.
- **React-driven overlays**: an overlay that reads `useUiStore` directly breaks the renderer's isolation. Keep the overlay pure and push state in from the outside via `setMyOverlayState`.
- **Allocation thrashing**: building a new array every frame is fine for hundreds of items, brutal for tens of thousands. Pre-allocate on a renderer field and reuse.
- **Wrong draw order**: overlays that need to be under the selection outline must go before `_drawSelection`. Check the order against `hgraf-add-gantt-overlay.md`'s data-layer example which draws at the ghost-plan layer, well before spans.
