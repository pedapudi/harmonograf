import { describe, it, expect } from 'vitest';
import {
  DEFAULT_VIEWPORT,
  MIN_SCALE,
  MAX_SCALE,
  clampScale,
  zoomAt,
  panBy,
  fitRect,
  containerToContent,
  visibleContentRect,
  centerOn,
  minimapViewportRect,
  minimapPointToContent,
  zoomStep,
  wheelZoomFactor,
} from '../../components/shell/views/graphViewport';

describe('graphViewport — clampScale', () => {
  it('clamps below the minimum', () => {
    expect(clampScale(0.1)).toBe(MIN_SCALE);
  });
  it('clamps above the maximum', () => {
    expect(clampScale(10)).toBe(MAX_SCALE);
  });
  it('passes values in range through unchanged', () => {
    expect(clampScale(1.5)).toBe(1.5);
  });
  it('guards against NaN/Infinity', () => {
    expect(clampScale(Number.NaN)).toBe(1);
    expect(clampScale(Number.POSITIVE_INFINITY)).toBe(MAX_SCALE);
  });
});

describe('graphViewport — zoomAt', () => {
  it('keeps the cursor-anchored content point stationary', () => {
    // Start from identity. Cursor sits at container (200, 100), which is also
    // content (200, 100). After zooming 2× on that point, the same content
    // should still land on (200, 100) in container space.
    const vp = DEFAULT_VIEWPORT;
    const after = zoomAt(vp, 2, 200, 100);
    expect(after.scale).toBe(2);
    // Content point (200, 100) in container space after transform:
    const containerX = 200 * after.scale + after.tx;
    const containerY = 100 * after.scale + after.ty;
    expect(containerX).toBeCloseTo(200, 6);
    expect(containerY).toBeCloseTo(100, 6);
  });

  it('keeps the cursor-anchored point stationary from a non-identity start', () => {
    const vp = { scale: 1.5, tx: 40, ty: -20 };
    const { x: cx, y: cy } = containerToContent(vp, 150, 80);
    const after = zoomAt(vp, 0.5, 150, 80);
    const containerX = cx * after.scale + after.tx;
    const containerY = cy * after.scale + after.ty;
    expect(containerX).toBeCloseTo(150, 6);
    expect(containerY).toBeCloseTo(80, 6);
  });

  it('returns the input unchanged when clamped scale does not move', () => {
    const vp = { scale: MAX_SCALE, tx: 10, ty: 20 };
    expect(zoomAt(vp, 3, 0, 0)).toBe(vp);
  });

  it('clamps scale at both ends', () => {
    expect(zoomAt(DEFAULT_VIEWPORT, 100, 0, 0).scale).toBe(MAX_SCALE);
    expect(zoomAt(DEFAULT_VIEWPORT, 0.001, 0, 0).scale).toBe(MIN_SCALE);
  });
});

describe('graphViewport — panBy', () => {
  it('adds to the translation without changing scale', () => {
    const vp = { scale: 2, tx: 10, ty: 20 };
    expect(panBy(vp, 5, -3)).toEqual({ scale: 2, tx: 15, ty: 17 });
  });
});

describe('graphViewport — fitRect', () => {
  it('fits content smaller than the container with scale clamped to MAX', () => {
    const vp = fitRect({ x: 0, y: 0, w: 100, h: 50 }, { w: 800, h: 600 }, 20);
    expect(vp.scale).toBe(MAX_SCALE);
    // Content should be centered inside the padded area.
    const center = { x: 50, y: 25 };
    const cx = center.x * vp.scale + vp.tx;
    const cy = center.y * vp.scale + vp.ty;
    expect(cx).toBeCloseTo(400, 6);
    expect(cy).toBeCloseTo(300, 6);
  });

  it('fits content larger than the container by shrinking', () => {
    // 2000×1000 content into 800×600 with 24px padding: available is 752×552,
    // so the tightest axis is width and the scale works out to 752/2000 ≈ 0.376.
    const vp = fitRect({ x: 0, y: 0, w: 2000, h: 1000 }, { w: 800, h: 600 }, 24);
    const availW = 800 - 48;
    const expectedScale = availW / 2000;
    expect(vp.scale).toBeCloseTo(expectedScale, 6);
    // Left edge of content sits at the left padding after centering horizontally.
    const leftEdgeContainer = 0 * vp.scale + vp.tx;
    expect(leftEdgeContainer).toBeCloseTo(24, 6);
  });

  it('clamps tiny fit-scales up to the minimum', () => {
    // Content way larger than the container — the raw fit ratio would be
    // below MIN_SCALE, so fitRect should clamp to MIN_SCALE rather than
    // returning an unreachable viewport.
    const vp = fitRect({ x: 0, y: 0, w: 10000, h: 5000 }, { w: 400, h: 300 }, 20);
    expect(vp.scale).toBe(0.25);
  });

  it('clamps the fit-scale up to ``minScale`` when content is larger than the container', () => {
    // Item 2 of UX cleanup batch: the GraphView's initial-fit was leaving
    // the canvas at 0.37× when the DAG was wider than the viewport,
    // which read as broken / mostly empty. The fitRect ``minScale``
    // option clamps the floor so big DAGs open at 1.0× (centered) and
    // operators can pan from there.
    const vp = fitRect(
      { x: 0, y: 0, w: 2000, h: 1000 },
      { w: 800, h: 600 },
      24,
      { minScale: 1 },
    );
    expect(vp.scale).toBe(1);
  });

  it('does not change behaviour when ``minScale`` is below the natural fit', () => {
    // Sanity: minScale only acts as a floor. Tiny content that fits
    // comfortably should still hit MAX_SCALE clamping.
    const vp = fitRect(
      { x: 0, y: 0, w: 100, h: 50 },
      { w: 800, h: 600 },
      20,
      { minScale: 1 },
    );
    expect(vp.scale).toBe(MAX_SCALE);
  });

  it('handles a non-origin content rect', () => {
    const vp = fitRect({ x: 50, y: 100, w: 200, h: 100 }, { w: 400, h: 300 }, 10);
    // Content top-left should sit at (pad + extraX, pad + extraY).
    const left = 50 * vp.scale + vp.tx;
    const top = 100 * vp.scale + vp.ty;
    const right = 250 * vp.scale + vp.tx;
    const bottom = 200 * vp.scale + vp.ty;
    // Centered horizontally and vertically within the padded box.
    expect((left + right) / 2).toBeCloseTo(200, 6);
    expect((top + bottom) / 2).toBeCloseTo(150, 6);
  });
});

describe('graphViewport — containerToContent / visibleContentRect', () => {
  it('inverts the affine', () => {
    const vp = { scale: 2, tx: 30, ty: -10 };
    const { x, y } = containerToContent(vp, 130, 90);
    expect(x).toBe((130 - 30) / 2);
    expect(y).toBe((90 - -10) / 2);
  });

  it('visibleContentRect matches the inverse of all four corners', () => {
    const vp = { scale: 0.5, tx: 10, ty: 20 };
    const rect = visibleContentRect(vp, { w: 400, h: 300 });
    expect(rect.x).toBe(-20);
    expect(rect.y).toBe(-40);
    expect(rect.w).toBe(800);
    expect(rect.h).toBe(600);
  });
});

describe('graphViewport — centerOn', () => {
  it('places the given content point at the container center', () => {
    const vp = { scale: 1.5, tx: 0, ty: 0 };
    const after = centerOn(vp, 200, 100, { w: 800, h: 400 });
    const cx = 200 * after.scale + after.tx;
    const cy = 100 * after.scale + after.ty;
    expect(cx).toBe(400);
    expect(cy).toBe(200);
    expect(after.scale).toBe(1.5);
  });
});

describe('graphViewport — minimap coordinate conversions', () => {
  const bounds = { x: 0, y: 0, w: 2000, h: 1000 };
  const minimap = { w: 200, h: 100 };

  it('maps the full content bounds onto the full minimap rect', () => {
    const r = minimapViewportRect(bounds, bounds, minimap);
    expect(r).toEqual({ x: 0, y: 0, w: 200, h: 100 });
  });

  it('projects a sub-rectangle of visible content', () => {
    const r = minimapViewportRect(
      { x: 500, y: 250, w: 500, h: 250 },
      bounds,
      minimap,
    );
    expect(r.x).toBe(50);
    expect(r.y).toBe(25);
    expect(r.w).toBe(50);
    expect(r.h).toBe(25);
  });

  it('round-trips a minimap point back to content coordinates', () => {
    const minimapPoint = { x: 100, y: 50 };
    const content = minimapPointToContent(minimapPoint.x, minimapPoint.y, bounds, minimap);
    expect(content.x).toBe(1000);
    expect(content.y).toBe(500);
    // And projecting a visible rect centered on that content point lines up.
    const r = minimapViewportRect(
      { x: content.x - 100, y: content.y - 50, w: 200, h: 100 },
      bounds,
      minimap,
    );
    expect(r.x + r.w / 2).toBeCloseTo(minimapPoint.x, 6);
    expect(r.y + r.h / 2).toBeCloseTo(minimapPoint.y, 6);
  });

  it('handles a content origin offset', () => {
    const offset = { x: 100, y: 50, w: 400, h: 200 };
    const point = minimapPointToContent(100, 50, offset, minimap);
    // 100/200 of w=400 = 200 → + origin 100 = 300. Same for y.
    expect(point.x).toBe(300);
    expect(point.y).toBe(150);
  });
});

describe('graphViewport — zoomStep', () => {
  it('zooms in centered on the container midpoint', () => {
    const container = { w: 400, h: 200 };
    const vp = zoomStep(DEFAULT_VIEWPORT, 'in', container);
    expect(vp.scale).toBeGreaterThan(1);
    // Middle of container should still map back to middle in content coords.
    const { x, y } = containerToContent(vp, 200, 100);
    expect(x).toBeCloseTo(200, 6);
    expect(y).toBeCloseTo(100, 6);
  });

  it('resets to identity', () => {
    const vp = { scale: 3, tx: 55, ty: -40 };
    expect(zoomStep(vp, 'reset', { w: 100, h: 50 })).toEqual(DEFAULT_VIEWPORT);
  });

  it('zooming out then in returns to the original scale', () => {
    const container = { w: 400, h: 200 };
    const after = zoomStep(zoomStep(DEFAULT_VIEWPORT, 'out', container), 'in', container);
    expect(after.scale).toBeCloseTo(1, 6);
  });
});

describe('graphViewport — wheelZoomFactor', () => {
  it('returns a factor > 1 when scrolling up (deltaY negative)', () => {
    expect(wheelZoomFactor(-40)).toBeGreaterThan(1);
  });
  it('returns a factor < 1 when scrolling down', () => {
    expect(wheelZoomFactor(40)).toBeLessThan(1);
  });
  it('is monotonic in deltaY', () => {
    expect(wheelZoomFactor(-80)).toBeGreaterThan(wheelZoomFactor(-40));
    expect(wheelZoomFactor(80)).toBeLessThan(wheelZoomFactor(40));
  });
  it('clamps extreme deltas', () => {
    expect(wheelZoomFactor(100000)).toBe(wheelZoomFactor(100));
    expect(wheelZoomFactor(-100000)).toBe(wheelZoomFactor(-100));
  });
});
