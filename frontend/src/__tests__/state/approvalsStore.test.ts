import { beforeEach, describe, expect, it } from 'vitest';
import {
  useApprovalsStore,
  type PendingApproval,
} from '../../state/approvalsStore';

function entry(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    sessionId: 'sess-1',
    targetId: 't-1',
    kind: 'task',
    prompt: 'ok to spend $500?',
    taskId: 't-1',
    metadata: {},
    requestedAtMs: 0,
    agentId: 'agent-a',
    spanId: '',
    ...overrides,
  };
}

describe('approvalsStore', () => {
  beforeEach(() => {
    useApprovalsStore.setState({ bySession: new Map() });
  });

  it('request() pushes a new entry and list() returns it', () => {
    useApprovalsStore.getState().request(entry());
    const list = useApprovalsStore.getState().list('sess-1');
    expect(list).toHaveLength(1);
    expect(list[0].targetId).toBe('t-1');
    expect(list[0].prompt).toBe('ok to spend $500?');
  });

  it('request() replaces an entry with the same targetId rather than duplicating', () => {
    const store = useApprovalsStore.getState();
    store.request(entry({ prompt: 'first' }));
    store.request(entry({ prompt: 'second' }));
    const list = useApprovalsStore.getState().list('sess-1');
    expect(list).toHaveLength(1);
    expect(list[0].prompt).toBe('second');
  });

  it('entries sort by requestedAtMs', () => {
    const store = useApprovalsStore.getState();
    store.request(entry({ targetId: 'b', requestedAtMs: 2000 }));
    store.request(entry({ targetId: 'a', requestedAtMs: 1000 }));
    store.request(entry({ targetId: 'c', requestedAtMs: 3000 }));
    const ids = useApprovalsStore
      .getState()
      .list('sess-1')
      .map((e) => e.targetId);
    expect(ids).toEqual(['a', 'b', 'c']);
  });

  it('resolve() removes matching targetId; mismatched resolve is a no-op', () => {
    const store = useApprovalsStore.getState();
    store.request(entry({ targetId: 't-1' }));
    store.request(entry({ targetId: 't-2' }));
    store.resolve('sess-1', 't-1');
    const remaining = useApprovalsStore.getState().list('sess-1');
    expect(remaining.map((e) => e.targetId)).toEqual(['t-2']);
    // Resolve with unknown id: no change.
    store.resolve('sess-1', 'never-existed');
    expect(useApprovalsStore.getState().list('sess-1')).toHaveLength(1);
  });

  it('clear() drops all entries for a session', () => {
    const store = useApprovalsStore.getState();
    store.request(entry({ targetId: 't-1' }));
    store.request(entry({ targetId: 't-2', sessionId: 'sess-2' }));
    store.clear('sess-1');
    expect(useApprovalsStore.getState().list('sess-1')).toEqual([]);
    expect(useApprovalsStore.getState().list('sess-2')).toHaveLength(1);
  });

  it('list(null) returns empty array', () => {
    expect(useApprovalsStore.getState().list(null)).toEqual([]);
  });
});
