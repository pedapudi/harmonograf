import { describe, expect, it } from 'vitest';
import {
  getSessionStore,
  sessionIsInactive,
  INACTIVITY_COMPLETED_MS,
} from '../../rpc/hooks';

// Note: convertTaskPlan / convertTask / taskStatusFromInt are module-private
// inside src/rpc/hooks.ts. Testing them directly would require a production
// code change (adding exports), which this task disallows. Their behavior is
// covered indirectly by the gantt/TaskRegistry tests (which exercise the
// upsertPlan pipeline they feed into) and by the e2e/integration suite in
// tests/e2e. See the test-bootstrap report for details.

describe('getSessionStore', () => {
  it('returns undefined for a null sessionId', () => {
    expect(getSessionStore(null)).toBeUndefined();
  });

  it('returns undefined for a sessionId that has never been watched', () => {
    expect(getSessionStore('never-seen')).toBeUndefined();
  });
});

describe('sessionIsInactive (harmonograf#89)', () => {
  const T0 = 1_000_000_000_000;
  const base = {
    sessionStatus: 'UNKNOWN' as const,
    lastEventAtMs: T0,
    initialBurstComplete: true,
    connected: true,
  };

  it('returns false before the initial burst completes', () => {
    expect(
      sessionIsInactive(
        { ...base, sessionStatus: 'COMPLETED', initialBurstComplete: false },
        T0,
      ),
    ).toBe(false);
  });

  it('returns true for an explicit COMPLETED status', () => {
    expect(
      sessionIsInactive({ ...base, sessionStatus: 'COMPLETED' }, T0),
    ).toBe(true);
  });

  it('returns true for an explicit ABORTED status', () => {
    expect(
      sessionIsInactive({ ...base, sessionStatus: 'ABORTED' }, T0),
    ).toBe(true);
  });

  it('returns false for a LIVE session even if the stream is momentarily quiet', () => {
    expect(
      sessionIsInactive(
        { ...base, sessionStatus: 'LIVE', connected: true },
        T0 + 5 * 60_000,
      ),
    ).toBe(false);
  });

  it('returns true when UNKNOWN, disconnected, and inactive past the threshold', () => {
    expect(
      sessionIsInactive(
        { ...base, connected: false, lastEventAtMs: T0 },
        T0 + INACTIVITY_COMPLETED_MS + 1,
      ),
    ).toBe(true);
  });

  it('stays false while still inside the inactivity grace window', () => {
    expect(
      sessionIsInactive(
        { ...base, connected: false, lastEventAtMs: T0 },
        T0 + INACTIVITY_COMPLETED_MS - 1,
      ),
    ).toBe(false);
  });

  it('stays false while the stream is still connected, even if quiet', () => {
    // Heartbeats on the server side keep a live session from flipping to
    // "inactive" purely on a long quiet stretch — only a disconnect +
    // staleness should trigger the fallback heuristic.
    expect(
      sessionIsInactive(
        { ...base, connected: true, lastEventAtMs: T0 },
        T0 + 10 * 60_000,
      ),
    ).toBe(false);
  });
});
