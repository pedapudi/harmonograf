import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InterventionsTimeline } from '../../components/Interventions/InterventionsTimeline';
import type { InterventionRow } from '../../lib/interventions';

vi.mock('../../components/Interventions/InterventionsTimeline.css', () => ({}));

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
});
