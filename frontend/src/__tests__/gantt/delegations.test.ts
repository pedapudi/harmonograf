import { describe, expect, it, vi } from 'vitest';
import { DelegationRegistry, SessionStore } from '../../gantt/index';

describe('DelegationRegistry', () => {
  it('starts empty', () => {
    const reg = new DelegationRegistry();
    expect(reg.list()).toHaveLength(0);
  });

  it('append assigns monotonic seq starting at 0', () => {
    const reg = new DelegationRegistry();
    reg.append({
      fromAgentId: 'coord',
      toAgentId: 'sub_a',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 1000,
    });
    reg.append({
      fromAgentId: 'coord',
      toAgentId: 'sub_b',
      taskId: 't-2',
      invocationId: 'inv-2',
      observedAtMs: 2000,
    });
    const list = reg.list();
    expect(list).toHaveLength(2);
    expect(list[0].seq).toBe(0);
    expect(list[1].seq).toBe(1);
    expect(list[0].toAgentId).toBe('sub_a');
    expect(list[1].toAgentId).toBe('sub_b');
  });

  it('append fires subscribers exactly once per entry', () => {
    const reg = new DelegationRegistry();
    const fn = vi.fn();
    reg.subscribe(fn);
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'b',
      taskId: '',
      invocationId: 'inv',
      observedAtMs: 0,
    });
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'c',
      taskId: '',
      invocationId: 'inv2',
      observedAtMs: 10,
    });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('unsubscribe stops further emissions', () => {
    const reg = new DelegationRegistry();
    const fn = vi.fn();
    const un = reg.subscribe(fn);
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'b',
      taskId: '',
      invocationId: 'inv',
      observedAtMs: 0,
    });
    un();
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'c',
      taskId: '',
      invocationId: 'inv2',
      observedAtMs: 10,
    });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('clear resets list, seq, and emits', () => {
    const reg = new DelegationRegistry();
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'b',
      taskId: '',
      invocationId: 'inv',
      observedAtMs: 0,
    });
    const fn = vi.fn();
    reg.subscribe(fn);
    reg.clear();
    expect(reg.list()).toHaveLength(0);
    expect(fn).toHaveBeenCalledTimes(1);

    // Seq restarts at 0 after clear.
    reg.append({
      fromAgentId: 'a',
      toAgentId: 'b',
      taskId: '',
      invocationId: 'inv2',
      observedAtMs: 5,
    });
    expect(reg.list()[0].seq).toBe(0);
  });

  it('clear on empty registry is a no-op (does not emit)', () => {
    const reg = new DelegationRegistry();
    const fn = vi.fn();
    reg.subscribe(fn);
    reg.clear();
    expect(fn).not.toHaveBeenCalled();
  });

  // Dedup suite — harmonograf#117's WatchSession double-delivers the same
  // DelegationObserved event during the initial-burst-vs-live-stream race,
  // which surfaced as two ↪↪ arrows per delegation in Agent Graph view.
  describe('dedup', () => {
    it('appending the same (from, to, invocationId) twice yields length 1', () => {
      const reg = new DelegationRegistry();
      const fn = vi.fn();
      reg.subscribe(fn);
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: 't-1',
        invocationId: 'inv-dup',
        observedAtMs: 1000,
      });
      // Second copy with the same invocation id — even if observedAtMs drifts
      // across the replay/live split, it must be treated as a duplicate.
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: 't-1',
        invocationId: 'inv-dup',
        observedAtMs: 1237,
      });
      expect(reg.list()).toHaveLength(1);
      expect(fn).toHaveBeenCalledTimes(1);
    });

    it('two distinct delegations are both kept', () => {
      const reg = new DelegationRegistry();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: 't-1',
        invocationId: 'inv-1',
        observedAtMs: 1000,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_b',
        taskId: 't-2',
        invocationId: 'inv-2',
        observedAtMs: 1100,
      });
      expect(reg.list()).toHaveLength(2);
    });

    it('same invocationId with different toAgentId is distinct', () => {
      // A coordinator can reuse an invocation id across two AgentTool fan-outs
      // in pathological setups; the to-agent disambiguates.
      const reg = new DelegationRegistry();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: 'inv-shared',
        observedAtMs: 1000,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_b',
        taskId: '',
        invocationId: 'inv-shared',
        observedAtMs: 1000,
      });
      expect(reg.list()).toHaveLength(2);
    });

    it('empty invocationId + same timestamp is treated as a duplicate', () => {
      // Fallback path for older goldfive versions that don't stamp
      // invocation_id on DelegationObserved.
      const reg = new DelegationRegistry();
      const fn = vi.fn();
      reg.subscribe(fn);
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: '',
        observedAtMs: 500,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: '',
        observedAtMs: 500,
      });
      expect(reg.list()).toHaveLength(1);
      expect(fn).toHaveBeenCalledTimes(1);
    });

    it('empty invocationId + different timestamps is distinct', () => {
      const reg = new DelegationRegistry();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: '',
        observedAtMs: 500,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: '',
        observedAtMs: 600,
      });
      expect(reg.list()).toHaveLength(2);
    });

    it('duplicate append does not advance seq', () => {
      const reg = new DelegationRegistry();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: 'inv-1',
        observedAtMs: 100,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: 'inv-1',
        observedAtMs: 100,
      });
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_b',
        taskId: '',
        invocationId: 'inv-2',
        observedAtMs: 200,
      });
      const list = reg.list();
      expect(list).toHaveLength(2);
      expect(list[0].seq).toBe(0);
      expect(list[1].seq).toBe(1);
    });

    it('clear resets dedup state so same invocationId can be re-appended', () => {
      const reg = new DelegationRegistry();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: 'inv-1',
        observedAtMs: 100,
      });
      reg.clear();
      reg.append({
        fromAgentId: 'coord',
        toAgentId: 'sub_a',
        taskId: '',
        invocationId: 'inv-1',
        observedAtMs: 100,
      });
      expect(reg.list()).toHaveLength(1);
    });
  });
});

describe('SessionStore.delegations integration', () => {
  it('SessionStore exposes an empty DelegationRegistry', () => {
    const store = new SessionStore();
    expect(store.delegations).toBeInstanceOf(DelegationRegistry);
    expect(store.delegations.list()).toHaveLength(0);
  });

  it('SessionStore.clear wipes delegations', () => {
    const store = new SessionStore();
    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 100,
    });
    expect(store.delegations.list()).toHaveLength(1);
    store.clear();
    expect(store.delegations.list()).toHaveLength(0);
  });
});
