import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InterventionsTimeline } from '../../components/Interventions/InterventionsTimeline';
import type { InterventionRow } from '../../lib/interventions';

vi.mock('../../components/Interventions/InterventionsTimeline.css', () => ({}));

// Parse SVG transform="translate(x, y)" and return cx.
function cxOf(el: Element): number {
  const t = el.getAttribute('transform') || '';
  const m = t.match(/translate\(([-\d.]+),\s*([-\d.]+)\)/);
  if (!m) throw new Error(`no translate in ${t}`);
  return parseFloat(m[1]);
}

function row(over: Partial<InterventionRow>): InterventionRow {
  return {
    key: 'k',
    atMs: 0,
    source: 'drift',
    kind: 'LOOPING_REASONING',
    bodyOrReason: 'detail',
    author: '',
    outcome: '',
    planRevisionIndex: 0,
    severity: 'warning',
    annotationId: '',
    driftKind: 'looping_reasoning',
    triggerEventId: '',
    ...over,
  };
}

describe('<InterventionsTimeline />', () => {
  it('renders one marker per row with the right source attribute', () => {
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 'u1', atMs: 100, source: 'user', kind: 'STEER', annotationId: 'ann_1' }),
          row({ key: 'd1', atMs: 20_000, source: 'drift', kind: 'LOOPING_REASONING' }),
          row({ key: 'g1', atMs: 50_000, source: 'goldfive', kind: 'CASCADE_CANCEL' }),
        ]}
        startMs={0}
        endMs={60_000}
      />,
    );
    const user = screen.getByTestId('intervention-marker-u1');
    const drift = screen.getByTestId('intervention-marker-d1');
    const gold = screen.getByTestId('intervention-marker-g1');
    expect(user.getAttribute('data-source')).toBe('user');
    expect(drift.getAttribute('data-source')).toBe('drift');
    expect(gold.getAttribute('data-source')).toBe('goldfive');
  });

  it('shows the empty-state hint when there are no rows', () => {
    render(<InterventionsTimeline rows={[]} startMs={0} endMs={10} />);
    expect(screen.getByText(/No interventions recorded/i)).toBeTruthy();
  });

  it('opens an expandable card with outcome + body when a marker is clicked', () => {
    render(
      <InterventionsTimeline
        rows={[
          row({
            key: 'd1',
            atMs: 500,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            bodyOrReason: 'agent re-read same doc',
            outcome: 'plan_revised:r3',
            planRevisionIndex: 3,
            severity: 'warning',
          }),
        ]}
        startMs={0}
        endMs={10_000}
      />,
    );
    fireEvent.click(screen.getByTestId('intervention-marker-d1'));
    const card = screen.getByTestId('intervention-card');
    expect(card).toBeTruthy();
    expect(card.textContent).toContain('LOOPING_REASONING');
    expect(card.textContent).toContain('agent re-read same doc');
    expect(card.textContent).toContain('rev 3');
  });

  it('calls onJumpToRevision with the revision index when the button is clicked', () => {
    const onJump = vi.fn();
    render(
      <InterventionsTimeline
        rows={[
          row({
            key: 'd1',
            atMs: 500,
            source: 'drift',
            outcome: 'plan_revised:r5',
            planRevisionIndex: 5,
          }),
        ]}
        revs={[
          {
            id: 'p',
            invocationSpanId: '',
            plannerAgentId: '',
            createdAtMs: 0,
            summary: '',
            tasks: [],
            edges: [],
            revisionReason: '',
            revisionIndex: 5,
          },
        ]}
        startMs={0}
        endMs={10_000}
        onJumpToRevision={onJump}
      />,
    );
    fireEvent.click(screen.getByTestId('intervention-marker-d1'));
    fireEvent.click(screen.getByTestId('intervention-card__jump'));
    expect(onJump).toHaveBeenCalledWith(5);
  });

  // --- #74: stable-anchor invariant -------------------------------------
  //
  // The strip must NOT shift marker X when the parent advances `endMs` on
  // every render (e.g. because a live session's duration ticks up). With
  // the stable-snapshot fix, the marker x stays pinned to the snapshot
  // taken at mount; only a coarse 1s tick may move it.
  it('keeps marker x stable when endMs advances on re-render (stable anchor)', () => {
    const rows = [
      row({ key: 'u1', atMs: 5_000, source: 'user', kind: 'STEER' }),
    ];
    const { rerender } = render(
      <InterventionsTimeline
        rows={rows}
        startMs={0}
        endMs={10_000}
        width={400}
        // Disable the rAF-driven live tick so the test is deterministic.
        _liveTickMs={0}
      />,
    );
    const first = screen.getByTestId('intervention-marker-u1');
    const firstTransform = first.getAttribute('transform');
    // Simulate the parent re-rendering with a much larger endMs (live
    // session has grown). Without the fix, every marker would slide left.
    rerender(
      <InterventionsTimeline
        rows={rows}
        startMs={0}
        endMs={60_000}
        width={400}
        _liveTickMs={0}
      />,
    );
    const after = screen.getByTestId('intervention-marker-u1');
    expect(after.getAttribute('transform')).toBe(firstTransform);
  });

  // --- #74: clustering ---------------------------------------------------
  //
  // Two markers whose centres would land within ~2% of the strip width of
  // each other collapse into a single cluster badge. The badge carries a
  // `data-count` attribute with the cluster size.
  it('clusters dense interventions into a single badge with a count', () => {
    const { container } = render(
      <InterventionsTimeline
        rows={[
          row({ key: 's1', atMs: 5_000, source: 'user', kind: 'STEER' }),
          row({ key: 's2', atMs: 5_050, source: 'user', kind: 'STEER' }),
          row({ key: 's3', atMs: 5_100, source: 'user', kind: 'STEER' }),
        ]}
        startMs={0}
        endMs={60_000}
        width={400}
        _liveTickMs={0}
      />,
    );
    // No individual markers should render for clustered rows.
    expect(screen.queryByTestId('intervention-marker-s1')).toBeNull();
    expect(screen.queryByTestId('intervention-marker-s2')).toBeNull();
    expect(screen.queryByTestId('intervention-marker-s3')).toBeNull();
    // One cluster badge with count=3.
    const cluster = container.querySelector(
      '[data-testid^="intervention-cluster-"]',
    );
    expect(cluster).toBeTruthy();
    expect(cluster?.getAttribute('data-count')).toBe('3');
  });

  it('does not cluster markers that are well-separated', () => {
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 'a', atMs: 5_000, source: 'user', kind: 'STEER' }),
          row({ key: 'b', atMs: 40_000, source: 'drift', kind: 'LOOPING' }),
        ]}
        startMs={0}
        endMs={60_000}
        width={400}
        _liveTickMs={0}
      />,
    );
    expect(screen.getByTestId('intervention-marker-a')).toBeTruthy();
    expect(screen.getByTestId('intervention-marker-b')).toBeTruthy();
  });

  // --- #107: bounded strip width ---------------------------------------
  //
  // The strip has a max-width cap (set in CSS) so it never smears markers
  // across an over-wide Gantt pane. Callers that pass an explicit `width`
  // prop override the cap via an inline style on the wrapper so the strip
  // can still match an intentionally wide container.
  it('applies a max-width cap to the wrapper when no width prop is passed', () => {
    render(
      <InterventionsTimeline
        rows={[row({ key: 'a', atMs: 1_000, source: 'user', kind: 'STEER' })]}
        startMs={0}
        endMs={60_000}
        _liveTickMs={0}
      />,
    );
    const wrapper = screen.getByTestId('interventions-timeline');
    // Inline style only carries width:100% — max-width comes from CSS.
    expect(wrapper.style.width).toBe('100%');
    // Inline style MUST NOT set maxWidth (so the CSS cap applies).
    expect(wrapper.style.maxWidth).toBe('');
  });

  it('lets an explicit width prop override the max-width cap', () => {
    render(
      <InterventionsTimeline
        rows={[row({ key: 'a', atMs: 1_000, source: 'user', kind: 'STEER' })]}
        startMs={0}
        endMs={60_000}
        width={800}
        _liveTickMs={0}
      />,
    );
    const wrapper = screen.getByTestId('interventions-timeline');
    expect(wrapper.style.width).toBe('800px');
    expect(wrapper.style.maxWidth).toBe('800px');
  });

  // --- width-footprint regression (follow-up to #125) -------------------
  //
  // #125 raised the CSS max-width cap from 480px → 1600px so the axis
  // could span a session-wide window honestly. That fix blew up the
  // strip's horizontal footprint on wide panes when content was sparse
  // (one diamond floating 1400px off to the right of an otherwise
  // empty strip). The cap is now tightened back to ~960px — wide
  // enough for a typical 6-stage DAG (~864px), narrow enough to feel
  // intentional on a 1600px page. Callers that know an exact desired
  // width (e.g. GanttView flowing the DAG width down) still override
  // via the `width` prop.
  it('explicit width prop is reasonable for a typical session (≤ 1000px)', () => {
    // This is the footprint a caller gets when flowing plan-DAG width
    // down. 6 stages × 140 COLUMN_WIDTH + 24 padding = 864px.
    render(
      <InterventionsTimeline
        rows={[row({ key: 'a', atMs: 5 * 60_000, source: 'user', kind: 'STEER' })]}
        startMs={0}
        endMs={10 * 60_000}
        width={864}
        _liveTickMs={0}
      />,
    );
    const wrapper = screen.getByTestId('interventions-timeline');
    // Footprint matches the DAG — not 1600px, not 100% of page.
    expect(wrapper.style.width).toBe('864px');
    expect(wrapper.style.maxWidth).toBe('864px');
    const widthPx = parseInt(wrapper.style.width, 10);
    expect(widthPx).toBeLessThanOrEqual(1000);
  });

  it('places markers correctly regardless of sparse content (one marker, wide strip)', () => {
    // Sparse-content regression: a single diamond 3 minutes into a
    // 3-minute session should land near the right edge, not at 0m.
    // The #125 time-accuracy invariant still holds after the width
    // cap is tightened.
    const width = 864;
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 'd', atMs: 3 * 60_000, source: 'user', kind: 'STEER' }),
        ]}
        startMs={0}
        endMs={3 * 60_000}
        width={width}
        _liveTickMs={0}
      />,
    );
    const marker = screen.getByTestId('intervention-marker-d');
    const cx = cxOf(marker);
    // tNorm = 1.0 (clamped): cx = STRIP_PAD_X + 1.0 * (864 - 28) = 850
    expect(cx).toBeGreaterThan(width * 0.9);
  });

  // --- accurate placement + dynamic axis scaling -------------------------
  //
  // Regression coverage for the user-reported bug: a drift/steer that
  // fired 7 minutes into a 10-minute session was rendering at the 0m
  // end of the strip because the caller passed a per-plan window where
  // row.atMs == startMs. The strip now receives a session-wide axis
  // (startMs=0, endMs=session_now) so markers land at their real
  // session-relative fraction across the strip.

  it('places a 10-minute-in marker at ~70% of a 10-minute strip', () => {
    // startMs=0, endMs=10m, drift at 7m → cx should be ~70% across the
    // usable strip (minus STRIP_PAD_X padding on each side).
    const width = 400;
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 'd', atMs: 7 * 60_000, source: 'drift', kind: 'LOOPING' }),
        ]}
        startMs={0}
        endMs={10 * 60_000}
        width={width}
        _liveTickMs={0}
      />,
    );
    const marker = screen.getByTestId('intervention-marker-d');
    const cx = cxOf(marker);
    // With STRIP_PAD_X=14 on each side: cx = 14 + 0.7 * (400 - 28) = 14 + 260.4 = 274.4
    // Allow ±2px slack for rounding / future minor spacing tweaks.
    expect(cx).toBeGreaterThan(272);
    expect(cx).toBeLessThan(277);
    // Crucially: NOT at the 0m end.
    expect(cx).toBeGreaterThan(width * 0.5);
  });

  it('places a 30-minute-in marker at ~50% of a 1-hour strip', () => {
    const width = 600;
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 's', atMs: 30 * 60_000, source: 'user', kind: 'STEER' }),
        ]}
        startMs={0}
        endMs={60 * 60_000}
        width={width}
        _liveTickMs={0}
      />,
    );
    const marker = screen.getByTestId('intervention-marker-s');
    const cx = cxOf(marker);
    // 14 + 0.5 * (600 - 28) = 14 + 286 = 300
    expect(cx).toBeGreaterThan(298);
    expect(cx).toBeLessThan(302);
  });

  // --- axis tick scaling -------------------------------------------------

  it('generates minute-step axis ticks across a 5-minute span', () => {
    render(
      <InterventionsTimeline
        rows={[]}
        startMs={0}
        endMs={5 * 60_000}
        width={500}
        _liveTickMs={0}
      />,
    );
    // pickTickStepMs picks 60s for spans ≤ 10m → 6 ticks at 0, 1, 2, 3, 4, 5 min.
    const labels = screen
      .getAllByTestId(/^axis-tick-\d+$/)
      .map((g) => g.textContent);
    expect(labels).toContain('0m');
    expect(labels).toContain('1m');
    expect(labels).toContain('2m');
    expect(labels).toContain('5m');
  });

  // --- live axis tracking ------------------------------------------------
  //
  // A live session has session.ended_at == null and the parent advances
  // endMs to track "now". With fake timers we can verify the snapshot
  // advances on the coarse live tick so markers re-place against the
  // growing span.

  describe('live axis (fake timers)', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it('advances the strip span when the live tick fires, re-placing markers', () => {
      const rows = [
        row({ key: 'u', atMs: 5 * 60_000, source: 'user', kind: 'STEER' }),
      ];
      const width = 400;
      // Start: session only 10 minutes in. Marker at 5m → 50%.
      const { rerender } = render(
        <InterventionsTimeline
          rows={rows}
          startMs={0}
          endMs={10 * 60_000}
          width={width}
          _liveTickMs={1000}
        />,
      );
      const beforeCx = cxOf(screen.getByTestId('intervention-marker-u'));
      // Expect ~ center.
      expect(beforeCx).toBeGreaterThan(width * 0.45);
      expect(beforeCx).toBeLessThan(width * 0.55);

      // Session grows to 20 minutes — parent re-renders with a wider
      // endMs. Before the live tick fires the snapshot is still 10m,
      // so marker x hasn't moved yet (stable-anchor invariant).
      rerender(
        <InterventionsTimeline
          rows={rows}
          startMs={0}
          endMs={20 * 60_000}
          width={width}
          _liveTickMs={1000}
        />,
      );
      expect(cxOf(screen.getByTestId('intervention-marker-u'))).toBeCloseTo(
        beforeCx,
        0,
      );

      // Now advance the timer past the 1s tick. The snapshot pulls in
      // the new endMs (20m), and the 5m marker slides to ~25%.
      act(() => {
        vi.advanceTimersByTime(1500);
      });
      const afterCx = cxOf(screen.getByTestId('intervention-marker-u'));
      expect(afterCx).toBeLessThan(beforeCx); // moved left as span grew
      expect(afterCx).toBeGreaterThan(width * 0.2);
      expect(afterCx).toBeLessThan(width * 0.3);
    });
  });
});
