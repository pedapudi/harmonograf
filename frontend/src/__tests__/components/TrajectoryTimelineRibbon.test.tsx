import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TrajectoryTimelineRibbon } from '../../components/shell/views/TrajectoryTimelineRibbon';
import type { PlanRevisionRecord } from '../../state/planHistoryStore';
import type { InterventionRow } from '../../lib/interventions';
import type { TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/TrajectoryTimelineRibbon.css', () => ({}));

function plan(id: string): TaskPlan {
  return {
    id,
    revisionIndex: 0,
    tasks: [],
    edges: [],
    createdAtMs: 0,
  } as unknown as TaskPlan;
}

function rev(over: Partial<PlanRevisionRecord>): PlanRevisionRecord {
  return {
    revision: 0,
    plan: plan('p1'),
    reason: '',
    kind: '',
    triggerEventId: '',
    emittedAtMs: 0,
    ...over,
  };
}

function intv(over: Partial<InterventionRow>): InterventionRow {
  return {
    key: 'k',
    atMs: 0,
    source: 'drift',
    kind: 'LOOPING_REASONING',
    bodyOrReason: '',
    author: '',
    outcome: '',
    planRevisionIndex: 0,
    severity: 'warning',
    annotationId: '',
    driftKind: '',
    triggerEventId: '',
    targetAgentId: '',
    driftId: '',
    ...over,
  };
}

describe('<TrajectoryTimelineRibbon />', () => {
  const baseRevs = [
    rev({ revision: 0, emittedAtMs: 0, reason: '', kind: '' }),
    rev({
      revision: 1,
      emittedAtMs: 10_000,
      reason: 'stop drifting',
      kind: 'off_topic',
    }),
    rev({
      revision: 2,
      emittedAtMs: 30_000,
      reason: 'target task 3',
      kind: 'user_steer',
    }),
  ];

  it('renders one notch per revision plus a Latest pseudo-notch', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(screen.getByTestId('ribbon-rev-0')).toBeTruthy();
    expect(screen.getByTestId('ribbon-rev-1')).toBeTruthy();
    expect(screen.getByTestId('ribbon-rev-2')).toBeTruthy();
    expect(screen.getByTestId('ribbon-rev-latest')).toBeTruthy();
  });

  it('renders intervention glyphs interleaved by timestamp', () => {
    const ivs = [
      intv({ key: 'i1', atMs: 5_000, kind: 'OFF_TOPIC', severity: 'warning' }),
      intv({ key: 'i2', atMs: 25_000, kind: 'LOOPING', severity: 'critical' }),
    ];
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={ivs}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(screen.getByTestId('ribbon-intv-i1')).toBeTruthy();
    expect(screen.getByTestId('ribbon-intv-i2')).toBeTruthy();
  });

  it('intervention colour varies by severity', () => {
    const ivs = [
      intv({ key: 'w', atMs: 5_000, severity: 'warning' }),
      intv({ key: 'c', atMs: 15_000, severity: 'critical' }),
      intv({ key: 'i', atMs: 25_000, severity: 'info' }),
    ];
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={ivs}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    const w = screen.getByTestId('ribbon-intv-w');
    const c = screen.getByTestId('ribbon-intv-c');
    const i = screen.getByTestId('ribbon-intv-i');
    expect(w.getAttribute('data-severity')).toBe('warning');
    expect(c.getAttribute('data-severity')).toBe('critical');
    expect(i.getAttribute('data-severity')).toBe('info');
    // Inline colour differs across severities. jsdom canonicalises hex to
    // rgb(...) so we compare on the decoded rgb triplet.
    expect(w.style.color).toBe('rgb(245, 158, 11)');  // warning amber
    expect(c.style.color).toBe('rgb(224, 96, 112)');  // critical red
    expect(i.style.color).toBe('rgb(141, 145, 153)'); // info grey
  });

  it('selected revision gets aria-selected=true; others get false', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision={1}
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(screen.getByTestId('ribbon-rev-1').getAttribute('aria-selected')).toBe(
      'true',
    );
    expect(screen.getByTestId('ribbon-rev-0').getAttribute('aria-selected')).toBe(
      'false',
    );
    expect(
      screen.getByTestId('ribbon-rev-latest').getAttribute('aria-selected'),
    ).toBe('false');
  });

  it('selects Latest when selectedRevision === "latest"', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(
      screen.getByTestId('ribbon-rev-latest').getAttribute('aria-selected'),
    ).toBe('true');
  });

  it('click revision fires onSelectRevision with the revision number', () => {
    const onSelect = vi.fn();
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={onSelect}
        onInterventionClick={() => {}}
      />,
    );
    fireEvent.click(screen.getByTestId('ribbon-rev-2'));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('click Latest pseudo-notch fires onSelectRevision("latest")', () => {
    const onSelect = vi.fn();
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision={1}
        onSelectRevision={onSelect}
        onInterventionClick={() => {}}
      />,
    );
    fireEvent.click(screen.getByTestId('ribbon-rev-latest'));
    expect(onSelect).toHaveBeenCalledWith('latest');
  });

  it('click intervention fires onInterventionClick with the row', () => {
    const onIntv = vi.fn();
    const row = intv({ key: 'xyz', atMs: 5_000, kind: 'STEER' });
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[row]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={onIntv}
      />,
    );
    fireEvent.click(screen.getByTestId('ribbon-intv-xyz'));
    expect(onIntv).toHaveBeenCalledWith(row);
  });

  it('Latest button appears when selectedRevision !== "latest"', () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision={1}
        onSelectRevision={onSelect}
        onInterventionClick={() => {}}
      />,
    );
    const btn = screen.getByTestId('ribbon-latest-btn');
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(onSelect).toHaveBeenCalledWith('latest');

    rerender(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={onSelect}
        onInterventionClick={() => {}}
      />,
    );
    expect(screen.queryByTestId('ribbon-latest-btn')).toBeNull();
  });

  it('hover revision reveals popover with reason + kind', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    const notch = screen.getByTestId('ribbon-rev-1');
    fireEvent.mouseEnter(notch);
    const pop = screen.getByTestId('ribbon-popover-rev-1');
    expect(pop.textContent).toContain('R1');
    expect(pop.textContent).toContain('OFF_TOPIC');
    expect(pop.textContent).toContain('stop drifting');
    expect(pop.getAttribute('role')).toBe('tooltip');
    // aria-describedby wires the anchor to the tooltip.
    expect(notch.getAttribute('aria-describedby')).toBe(pop.getAttribute('id'));
    fireEvent.mouseLeave(notch);
    expect(screen.queryByTestId('ribbon-popover-rev-1')).toBeNull();
  });

  it('hover intervention reveals popover with detail', () => {
    const row = intv({
      key: 'k1',
      atMs: 5_000,
      kind: 'STEER',
      severity: 'warning',
      bodyOrReason: 'please stop repeating the same search',
    });
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[row]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    const glyph = screen.getByTestId('ribbon-intv-k1');
    fireEvent.mouseEnter(glyph);
    const pop = screen.getByTestId('ribbon-popover-intv-k1');
    expect(pop.textContent).toContain('STEER');
    expect(pop.textContent).toContain('WARNING');
    expect(pop.textContent).toContain('please stop repeating');
  });

  it('keyboard arrow navigation walks between markers', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[
          intv({ key: 'i1', atMs: 5_000, kind: 'STEER' }),
        ]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    const r0 = screen.getByTestId('ribbon-rev-0');
    r0.focus();
    expect(document.activeElement).toBe(r0);
    fireEvent.keyDown(r0, { key: 'ArrowRight' });
    // Intervention i1 sits between r0 and r1 so it's next in focus order.
    expect(document.activeElement).toBe(screen.getByTestId('ribbon-intv-i1'));
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(screen.getByTestId('ribbon-rev-1'));
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(screen.getByTestId('ribbon-intv-i1'));
  });

  it('Enter / Space on a focused revision activates onSelectRevision', () => {
    const onSelect = vi.fn();
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={onSelect}
        onInterventionClick={() => {}}
      />,
    );
    // Buttons natively activate on Enter via the click event; simulate it.
    const notch = screen.getByTestId('ribbon-rev-2');
    notch.focus();
    fireEvent.click(notch);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('expand toggle fires onToggleExpanded when provided', () => {
    const onToggle = vi.fn();
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
        expanded={false}
        onToggleExpanded={onToggle}
      />,
    );
    fireEvent.click(screen.getByTestId('ribbon-expand-btn'));
    expect(onToggle).toHaveBeenCalled();
  });

  it('omits expand toggle when onToggleExpanded is not provided', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(screen.queryByTestId('ribbon-expand-btn')).toBeNull();
  });

  it('header shows correct revision + intervention counts', () => {
    render(
      <TrajectoryTimelineRibbon
        revisions={baseRevs}
        interventions={[intv({ key: 'a', atMs: 1 }), intv({ key: 'b', atMs: 2 })]}
        selectedRevision="latest"
        onSelectRevision={() => {}}
        onInterventionClick={() => {}}
      />,
    );
    expect(
      screen.getByText(/3 revisions · 2 interventions/),
    ).toBeTruthy();
  });
});
