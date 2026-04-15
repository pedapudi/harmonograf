import { describe, expect, it } from 'vitest';
import { getSessionStore } from '../../rpc/hooks';

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
