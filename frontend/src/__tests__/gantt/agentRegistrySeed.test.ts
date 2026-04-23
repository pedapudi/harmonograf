import { describe, expect, it, vi } from 'vitest';
import {
  AgentRegistry,
  PLACEHOLDER_AGENT_FLAG,
  bareAgentName,
} from '../../gantt/index';
import type { Agent } from '../../gantt/types';

// Coverage for the plan-seeded agent rows added in harmonograf#133 (closes
// the display-time resolver falling back to the raw compound id when an
// agent is listed in the plan but hasn't yet emitted a span).

function realAgent(id: string, overrides: Partial<Agent> = {}): Agent {
  return {
    id,
    name: id,
    framework: 'ADK',
    capabilities: [],
    status: 'CONNECTED',
    connectedAtMs: 1000,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
    ...overrides,
  };
}

describe('bareAgentName', () => {
  it('strips everything up to and including the last colon', () => {
    expect(bareAgentName('client-abc:reviewer_agent')).toBe('reviewer_agent');
  });

  it('only strips the last colon (nested prefixes collapse)', () => {
    expect(bareAgentName('a:b:research_agent')).toBe('research_agent');
  });

  it('returns bare ids (no colon) unchanged', () => {
    expect(bareAgentName('research_agent')).toBe('research_agent');
  });

  it('is a no-op on the empty string', () => {
    expect(bareAgentName('')).toBe('');
  });
});

describe('AgentRegistry.ensureAgent', () => {
  it('inserts a placeholder row for an unknown agent id', () => {
    const reg = new AgentRegistry();
    const inserted = reg.ensureAgent(
      'client:reviewer_agent',
      'reviewer_agent',
    );
    expect(inserted).toBe(true);
    const row = reg.get('client:reviewer_agent');
    expect(row).toBeDefined();
    expect(row!.name).toBe('reviewer_agent');
    expect(row!.status).toBe('DISCONNECTED');
    expect(row!.framework).toBe('UNKNOWN');
    expect(row!.metadata[PLACEHOLDER_AGENT_FLAG]).toBe('1');
  });

  it('is idempotent — a second ensureAgent call is a no-op', () => {
    const reg = new AgentRegistry();
    reg.ensureAgent('client:reviewer_agent', 'reviewer_agent');
    const after1 = reg.get('client:reviewer_agent');
    const inserted2 = reg.ensureAgent(
      'client:reviewer_agent',
      'reviewer_agent',
    );
    expect(inserted2).toBe(false);
    expect(reg.get('client:reviewer_agent')).toBe(after1);
    expect(reg.size).toBe(1);
  });

  it('does not clobber a pre-existing real agent row', () => {
    const reg = new AgentRegistry();
    const real = realAgent('client:research_agent', {
      name: 'research_agent',
      framework: 'ADK',
      status: 'CONNECTED',
      taskReport: 'live summary',
      metadata: { 'harmonograf.execution_mode': 'sequential' },
    });
    reg.upsert(real);
    const inserted = reg.ensureAgent(
      'client:research_agent',
      'research_agent',
    );
    expect(inserted).toBe(false);
    const row = reg.get('client:research_agent')!;
    expect(row.status).toBe('CONNECTED');
    expect(row.framework).toBe('ADK');
    expect(row.taskReport).toBe('live summary');
    expect(row.metadata[PLACEHOLDER_AGENT_FLAG]).toBeUndefined();
    expect(row.metadata['harmonograf.execution_mode']).toBe('sequential');
  });

  it('ignores the empty agent id', () => {
    const reg = new AgentRegistry();
    expect(reg.ensureAgent('', '')).toBe(false);
    expect(reg.size).toBe(0);
  });

  it('fires the subscribe callback on first insertion only', () => {
    const reg = new AgentRegistry();
    const fn = vi.fn();
    reg.subscribe(fn);
    reg.ensureAgent('client:a', 'a');
    reg.ensureAgent('client:a', 'a');
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('AgentRegistry.upsert clears the placeholder marker', () => {
  it('drops the placeholder flag when a real row lands', () => {
    const reg = new AgentRegistry();
    reg.ensureAgent('client:reviewer_agent', 'reviewer_agent');
    expect(reg.get('client:reviewer_agent')!.metadata[PLACEHOLDER_AGENT_FLAG]).toBe(
      '1',
    );
    reg.upsert(
      realAgent('client:reviewer_agent', { name: 'reviewer_agent' }),
    );
    const row = reg.get('client:reviewer_agent')!;
    expect(row.status).toBe('CONNECTED');
    expect(row.framework).toBe('ADK');
    expect(row.metadata[PLACEHOLDER_AGENT_FLAG]).toBeUndefined();
  });

  it('preserves unrelated metadata keys across the upsert merge', () => {
    const reg = new AgentRegistry();
    reg.ensureAgent('client:a', 'a');
    // Simulate some later code stashing a key on the placeholder row
    // (today nothing does, but the merge should be additive either way).
    reg.get('client:a')!.metadata['other.custom_key'] = 'keep-me';
    reg.upsert(
      realAgent('client:a', {
        name: 'a',
        metadata: { 'harmonograf.execution_mode': 'parallel' },
      }),
    );
    const row = reg.get('client:a')!;
    expect(row.metadata['other.custom_key']).toBe('keep-me');
    expect(row.metadata['harmonograf.execution_mode']).toBe('parallel');
    expect(row.metadata[PLACEHOLDER_AGENT_FLAG]).toBeUndefined();
  });
});
