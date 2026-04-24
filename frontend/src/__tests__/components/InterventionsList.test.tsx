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
