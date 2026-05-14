import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InterventionsList } from '../../components/Interventions/InterventionsList';
import type { InterventionRow } from '../../lib/interventions';

vi.mock('../../components/Interventions/InterventionsList.css', () => ({}));

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
    targetAgentId: '',
    driftId: '',
    attemptId: '',
    failureKind: '',
    ...over,
  };
}

describe('<InterventionsList />', () => {
  it('renders one line per row with kind + source colour', () => {
    render(
      <InterventionsList
        rows={[
          row({ key: 'u1', atMs: 2_000, source: 'user', kind: 'STEER', bodyOrReason: 'try again' }),
          row({ key: 'd1', atMs: 20_000, source: 'drift', kind: 'LOOPING_REASONING' }),
        ]}
      />,
    );
    expect(screen.getByTestId('interventions-list-row-u1')).toBeTruthy();
    expect(screen.getByTestId('interventions-list-row-d1')).toBeTruthy();
    expect(screen.getByTestId('interventions-list-row-u1').getAttribute('data-source')).toBe('user');
  });

  it('formats atMs as mm:ss', () => {
    render(
      <InterventionsList
        rows={[row({ key: 'a', atMs: 125_000, source: 'user', kind: 'STEER' })]}
      />,
    );
    expect(screen.getByText('2:05')).toBeTruthy();
  });

  it('formats plan_revised outcomes', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'a',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING',
            outcome: 'plan_revised:r3',
          }),
        ]}
      />,
    );
    expect(screen.getByText('→ rev 3')).toBeTruthy();
  });

  it('collapses to a single toggle when empty; expanding reveals the hint', () => {
    render(<InterventionsList rows={[]} />);
    expect(screen.queryByText(/No interventions recorded/i)).toBeNull();
    const toggle = screen.getByTestId('interventions-list-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toMatch(/Interventions \(0\)/);
    fireEvent.click(toggle);
    expect(screen.getByText(/No interventions recorded/i)).toBeTruthy();
    expect(
      screen.getByTestId('interventions-list-toggle').getAttribute('aria-expanded'),
    ).toBe('true');
  });

  it('never renders the toggle when there is at least one row', () => {
    render(
      <InterventionsList
        rows={[row({ key: 'a', atMs: 1000, source: 'user', kind: 'STEER' })]}
      />,
    );
    expect(screen.queryByTestId('interventions-list-toggle')).toBeNull();
  });

  it('fires onRowClick with the row when clicked', () => {
    const onRowClick = vi.fn();
    const r = row({ key: 'a', atMs: 1000, source: 'user', kind: 'STEER' });
    render(<InterventionsList rows={[r]} onRowClick={onRowClick} />);
    fireEvent.click(screen.getByTestId('interventions-list-row-a'));
    expect(onRowClick).toHaveBeenCalledWith(r);
  });

  it('renders rows as non-clickable divs when onRowClick is not provided', () => {
    const r = row({ key: 'a', atMs: 1000, source: 'user', kind: 'STEER' });
    render(<InterventionsList rows={[r]} />);
    const el = screen.getByTestId('interventions-list-row-a');
    expect(el.tagName.toLowerCase()).toBe('div');
  });

  it('cancel rows render the stop glyph + bare agent label', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'c1',
            atMs: 12_345,
            source: 'cancel',
            kind: 'CANCELLED',
            severity: 'critical',
            bodyOrReason: 'assistant veered off task',
            targetAgentId: 'presentation-orchestrated-abc:researcher_agent',
            driftId: 'drift-1',
            driftKind: 'off_topic',
            outcome: 'recorded',
          }),
        ]}
      />,
    );
    const rowEl = screen.getByTestId('interventions-list-row-c1');
    expect(rowEl.getAttribute('data-source')).toBe('cancel');
    // Stop glyph rendered inside the swatch so the row reads as terminal.
    expect(rowEl.textContent).toContain('⊘');
    // Bare agent label surfaces without the compound prefix.
    const agent = screen.getByTestId('interventions-list-row-c1-agent');
    expect(agent.textContent).toBe('researcher_agent');
    // Severity pill rendered for critical (info is suppressed; we
    // check the severity pill directly).
    expect(rowEl.textContent).toContain('critical');
    // Body surfaces the directive detail.
    expect(rowEl.textContent).toContain('assistant veered off task');
    // Outcome suppressed on cancel rows — the marker is the outcome.
    expect(rowEl.textContent).not.toContain('→');
  });

  it('cancel row is clickable and fires onRowClick with the row', () => {
    const onRowClick = vi.fn();
    const r = row({
      key: 'c1',
      atMs: 1000,
      source: 'cancel',
      kind: 'CANCELLED',
      bodyOrReason: 'cancelled',
      targetAgentId: 'agent_x',
      driftId: 'd1',
    });
    render(<InterventionsList rows={[r]} onRowClick={onRowClick} />);
    fireEvent.click(screen.getByTestId('interventions-list-row-c1'));
    expect(onRowClick).toHaveBeenCalledWith(r);
  });
});

// goldfive#318 frontend follow-up: condition grouping render. The
// deriver collapses observations sharing a condition_id; the renderer
// surfaces a count badge + a click-to-expand control that reveals each
// observation as a sub-row.
describe('<InterventionsList /> — drift condition grouping (goldfive#318)', () => {
  it('single-observation condition: shows a lifecycle chip but no expansion control', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift-cond:cond-A',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            bodyOrReason: 'loop detected',
            severity: 'warning',
            conditionId: 'cond-A',
            currentLifecycle: 'opened',
            observationCount: 1,
          }),
        ]}
      />,
    );
    const lc = screen.getByTestId(
      'interventions-list-row-drift-cond:cond-A-lifecycle',
    );
    expect(lc.textContent).toBe('OPENED');
    expect(
      screen.queryByTestId(
        'interventions-list-row-drift-cond:cond-A-expand',
      ),
    ).toBeNull();
  });

  it('multi-observation condition: count badge present; clicking expands observations', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift-cond:cond-X',
            atMs: 2000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            severity: 'critical',
            conditionId: 'cond-X',
            currentLifecycle: 'escalating',
            observationCount: 3,
            severityTransitions: [
              { fromSeverity: 'warning', toSeverity: 'critical', atMs: 1500 },
            ],
            observations: [
              {
                seq: 1,
                atMs: 1000,
                severity: 'warning',
                prevSeverity: '',
                lifecycle: 'opened',
                detail: 'first observation',
                driftId: 'd1',
              },
              {
                seq: 2,
                atMs: 1500,
                severity: 'critical',
                prevSeverity: 'warning',
                lifecycle: 'escalating',
                detail: 'severity bumped',
                driftId: 'd2',
              },
              {
                seq: 3,
                atMs: 2000,
                severity: 'critical',
                prevSeverity: '',
                lifecycle: 'escalating',
                detail: 'still escalating',
                driftId: 'd3',
              },
            ],
          }),
        ]}
      />,
    );
    const expand = screen.getByTestId(
      'interventions-list-row-drift-cond:cond-X-expand',
    );
    expect(expand.textContent).toContain('3 observations');
    expect(expand.getAttribute('aria-expanded')).toBe('false');
    // Observations panel is hidden until expand.
    expect(
      screen.queryByTestId(
        'interventions-list-row-drift-cond:cond-X-observations',
      ),
    ).toBeNull();
    fireEvent.click(expand);
    expect(expand.getAttribute('aria-expanded')).toBe('true');
    const list = screen.getByTestId(
      'interventions-list-row-drift-cond:cond-X-observations',
    );
    // All three observations rendered as sub-rows.
    expect(list.querySelectorAll('li')).toHaveLength(3);
    expect(screen.getByTestId('interventions-list-obs-2').textContent)
      .toContain('warning → critical');
  });

  it('severity transition surfaces on the row chrome (warning → critical)', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift-cond:cond-T',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            severity: 'critical',
            conditionId: 'cond-T',
            currentLifecycle: 'escalating',
            observationCount: 2,
            severityTransitions: [
              { fromSeverity: 'warning', toSeverity: 'critical', atMs: 1000 },
            ],
            observations: [],
          }),
        ]}
      />,
    );
    const t = screen.getByTestId(
      'interventions-list-row-drift-cond:cond-T-transition',
    );
    expect(t.textContent).toMatch(/warning\s*→\s*critical/);
  });

  it('lifecycle chip data attribute reflects current lifecycle', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift-cond:cond-R',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            severity: 'warning',
            conditionId: 'cond-R',
            currentLifecycle: 'resolved',
            observationCount: 2,
            observations: [
              {
                seq: 1,
                atMs: 100,
                severity: 'warning',
                prevSeverity: '',
                lifecycle: 'opened',
                detail: '',
                driftId: 'd1',
              },
              {
                seq: 2,
                atMs: 1000,
                severity: 'warning',
                prevSeverity: '',
                lifecycle: 'resolved',
                detail: '',
                driftId: 'd2',
              },
            ],
          }),
        ]}
      />,
    );
    const lc = screen.getByTestId(
      'interventions-list-row-drift-cond:cond-R-lifecycle',
    );
    expect(lc.getAttribute('data-lifecycle')).toBe('resolved');
    expect(lc.textContent).toBe('RESOLVED');
  });

  it('pre-#318 row (no conditionId) renders unchanged — no lifecycle chip, no expand control', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift:42',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            severity: 'warning',
            // conditionId / currentLifecycle / observationCount left
            // undefined to model the legacy emit path.
          }),
        ]}
      />,
    );
    expect(
      screen.queryByTestId('interventions-list-row-drift:42-lifecycle'),
    ).toBeNull();
    expect(
      screen.queryByTestId('interventions-list-row-drift:42-expand'),
    ).toBeNull();
    expect(
      screen.queryByTestId('interventions-list-row-drift:42-transition'),
    ).toBeNull();
  });

  it('clicking the expand control does not bubble to the row click handler', () => {
    const onRowClick = vi.fn();
    render(
      <InterventionsList
        rows={[
          row({
            key: 'drift-cond:cond-Z',
            atMs: 1000,
            source: 'drift',
            kind: 'LOOPING_REASONING',
            conditionId: 'cond-Z',
            observationCount: 2,
            observations: [
              {
                seq: 1,
                atMs: 500,
                severity: 'warning',
                prevSeverity: '',
                lifecycle: 'opened',
                detail: '',
                driftId: 'd1',
              },
              {
                seq: 2,
                atMs: 1000,
                severity: 'warning',
                prevSeverity: '',
                lifecycle: 'escalating',
                detail: '',
                driftId: 'd2',
              },
            ],
          }),
        ]}
        onRowClick={onRowClick}
      />,
    );
    fireEvent.click(
      screen.getByTestId(
        'interventions-list-row-drift-cond:cond-Z-expand',
      ),
    );
    expect(onRowClick).not.toHaveBeenCalled();
  });
});

// goldfive#423 PR 3 — observational discovery filter + dimming.
// Framework-synthesised NEW_WORK_DISCOVERED drifts fire at INFO
// severity (design doc §4.6). Sub-agent-authored discoveries remain at
// WARNING. The list:
//   1. Renders an INFO new_work_discovered row at lower contrast (the
//      ``data-observational`` attribute is set on the row).
//   2. Surfaces a checkbox to hide observational rows. The toggle is OFF
//      by default — users SHOULD see the rows by default.
//   3. WARNING new_work_discovered rows render normally (no
//      data-observational attribute), even when sourced from the same
//      drift kind.
describe('<InterventionsList /> — observational discovery filter (goldfive#423)', () => {
  it('flags INFO-severity new_work_discovered rows with data-observational', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'd-obs',
            atMs: 1000,
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'info',
          }),
        ]}
      />,
    );
    const r = screen.getByTestId('interventions-list-row-d-obs');
    expect(r.getAttribute('data-observational')).toBe('true');
  });

  it('does NOT flag WARNING-severity new_work_discovered (sub-agent-authored)', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'd-actionable',
            atMs: 1000,
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'warning',
          }),
        ]}
      />,
    );
    const r = screen.getByTestId('interventions-list-row-d-actionable');
    expect(r.getAttribute('data-observational')).toBeNull();
  });

  it('renders the filter toggle only when at least one observational row exists', () => {
    // No observational rows → toggle is suppressed.
    const { rerender } = render(
      <InterventionsList
        rows={[
          row({
            key: 'd-w',
            source: 'drift',
            kind: 'LOOPING_REASONING',
            driftKind: 'looping_reasoning',
            severity: 'warning',
          }),
        ]}
      />,
    );
    expect(
      screen.queryByTestId('interventions-list-observational-toggle'),
    ).toBeNull();

    // Add an observational row → toggle appears.
    rerender(
      <InterventionsList
        rows={[
          row({
            key: 'd-w',
            source: 'drift',
            kind: 'LOOPING_REASONING',
            driftKind: 'looping_reasoning',
            severity: 'warning',
          }),
          row({
            key: 'd-obs',
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'info',
          }),
        ]}
      />,
    );
    expect(
      screen.getByTestId('interventions-list-observational-toggle'),
    ).toBeTruthy();
  });

  it('toggle is off by default — observational rows visible', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'd-obs',
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'info',
          }),
        ]}
      />,
    );
    const toggle = screen.getByTestId(
      'interventions-list-observational-toggle',
    ) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    expect(screen.getByTestId('interventions-list-row-d-obs')).toBeTruthy();
  });

  it('flipping the toggle hides observational rows but keeps actionable rows', () => {
    render(
      <InterventionsList
        rows={[
          row({
            key: 'd-w',
            source: 'drift',
            kind: 'LOOPING_REASONING',
            driftKind: 'looping_reasoning',
            severity: 'warning',
          }),
          row({
            key: 'd-obs',
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'info',
          }),
          row({
            key: 'd-actionable',
            source: 'drift',
            kind: 'NEW_WORK_DISCOVERED',
            driftKind: 'new_work_discovered',
            severity: 'warning',
          }),
        ]}
      />,
    );
    const toggle = screen.getByTestId(
      'interventions-list-observational-toggle',
    );
    fireEvent.click(toggle);
    // Observational row vanishes…
    expect(
      screen.queryByTestId('interventions-list-row-d-obs'),
    ).toBeNull();
    // …but actionable rows stay.
    expect(screen.getByTestId('interventions-list-row-d-w')).toBeTruthy();
    expect(
      screen.getByTestId('interventions-list-row-d-actionable'),
    ).toBeTruthy();
  });
});
