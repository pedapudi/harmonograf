import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DelegationTooltip } from '../../components/DelegationTooltip/DelegationTooltip';
import type { DelegationRecord } from '../../gantt/index';

function mkRecord(overrides: Partial<DelegationRecord> = {}): DelegationRecord {
  return {
    seq: 0,
    fromAgentId: 'coordinator_agent',
    toAgentId: 'research_agent',
    taskId: 't-research-weather-bologna',
    invocationId: 'inv-abc12345-def67890-0000',
    observedAtMs: 185_000, // 3:05
    observedAtAbsoluteMs: 185_000,
    ...overrides,
  };
}

describe('DelegationTooltip', () => {
  it('renders header + all four lines with labelled fields', () => {
    render(
      <DelegationTooltip
        hover={{ record: mkRecord(), x: 100, y: 100 }}
        resolveAgentLabel={(id) => id}
      />,
    );
    const tip = screen.getByTestId('delegation-tooltip');
    expect(tip).toHaveTextContent('Delegation observed');
    expect(screen.getByTestId('delegation-tooltip-agents')).toHaveTextContent(
      'From: coordinator_agent → research_agent',
    );
    expect(screen.getByTestId('delegation-tooltip-task')).toHaveTextContent(
      'Task: t-research-weather-bologna',
    );
    expect(
      screen.getByTestId('delegation-tooltip-invocation'),
    ).toHaveTextContent('Invocation: abc12345…');
    expect(screen.getByTestId('delegation-tooltip-observed')).toHaveTextContent(
      'Observed: 3:05',
    );
  });

  it('applies resolveAgentLabel to synthetic actor ids', () => {
    const resolveAgentLabel = (id: string): string => {
      if (id === '__goldfive__') return 'goldfive';
      if (id === 'coord_X') return 'Coordinator X';
      return id;
    };
    render(
      <DelegationTooltip
        hover={{
          record: mkRecord({ fromAgentId: 'coord_X', toAgentId: '__goldfive__' }),
          x: 0,
          y: 0,
        }}
        resolveAgentLabel={resolveAgentLabel}
      />,
    );
    expect(screen.getByTestId('delegation-tooltip-agents')).toHaveTextContent(
      'From: Coordinator X → goldfive',
    );
  });

  it('omits the Task line when the record has no taskId', () => {
    render(
      <DelegationTooltip
        hover={{ record: mkRecord({ taskId: '' }), x: 0, y: 0 }}
        resolveAgentLabel={(id) => id}
      />,
    );
    expect(screen.queryByTestId('delegation-tooltip-task')).toBeNull();
  });

  it('renders the full invocation tail when it is already short', () => {
    render(
      <DelegationTooltip
        hover={{
          record: mkRecord({ invocationId: 'inv-xy42' }),
          x: 0,
          y: 0,
        }}
        resolveAgentLabel={(id) => id}
      />,
    );
    expect(
      screen.getByTestId('delegation-tooltip-invocation'),
    ).toHaveTextContent('Invocation: xy42');
  });

  it('anchors the tooltip near the pointer via inline left/top styles', () => {
    render(
      <DelegationTooltip
        hover={{ record: mkRecord(), x: 250, y: 400 }}
        resolveAgentLabel={(id) => id}
      />,
    );
    const tip = screen.getByTestId('delegation-tooltip');
    expect(tip.style.left).toBe('262px'); // 250 + 12 offset
    expect(Number(tip.style.top.replace('px', ''))).toBeGreaterThanOrEqual(0);
  });
});
