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
    ...over,
  };
}

describe('<InterventionsTimeline />', () => {
  it('renders one marker per row with the right source attribute', () => {
    render(
      <InterventionsTimeline
        rows={[
          row({ key: 'u1', source: 'user', kind: 'STEER', annotationId: 'ann_1' }),
          row({ key: 'd1', source: 'drift', kind: 'LOOPING_REASONING' }),
          row({ key: 'g1', source: 'goldfive', kind: 'CASCADE_CANCEL' }),
        ]}
        startMs={0}
        endMs={1000}
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
            source: 'drift',
            kind: 'LOOPING_REASONING',
            bodyOrReason: 'agent re-read same doc',
            outcome: 'plan_revised:r3',
            planRevisionIndex: 3,
            severity: 'warning',
          }),
        ]}
        startMs={0}
        endMs={1000}
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
        endMs={1000}
        onJumpToRevision={onJump}
      />,
    );
    fireEvent.click(screen.getByTestId('intervention-marker-d1'));
    fireEvent.click(screen.getByTestId('intervention-card__jump'));
    expect(onJump).toHaveBeenCalledWith(5);
  });
});
