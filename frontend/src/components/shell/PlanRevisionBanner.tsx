import { useEffect, useRef, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { getSessionStore } from '../../rpc/hooks';
import type { PlanDiff } from '../../gantt';
import { parseRevisionReason } from '../../gantt/driftKinds';

interface BannerEntry {
  key: number;
  planId: string;
  reason: string;
  diff?: PlanDiff;
}

const MAX_VISIBLE = 3;
const DISMISS_MS = 4000;

// Horizontal strip below CurrentTaskStrip that shows a pill whenever a plan's
// revisionReason changes. Pills auto-dismiss after 4s, and up to 3 stack FIFO
// when revisions arrive rapidly.
export function PlanRevisionBanner() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const store = getSessionStore(sessionId);
  const [entries, setEntries] = useState<BannerEntry[]>([]);
  const keyRef = useRef(0);

  useEffect(() => {
    if (!store) return;
    // Per-subscription map so sessions don't pollute each other's dedup.
    const seen = new Map<string, string>();
    // Seed with current state so pre-existing revisions don't flash on mount.
    for (const plan of store.tasks.listPlans()) {
      seen.set(plan.id, plan.revisionReason || '');
    }
    const scan = () => {
      const next: BannerEntry[] = [];
      for (const plan of store.tasks.listPlans()) {
        const reason = plan.revisionReason || '';
        const prev = seen.get(plan.id) || '';
        if (reason && reason !== prev) {
          seen.set(plan.id, reason);
          // Pull the diff recorded by TaskRegistry.upsertPlan for this
          // revision — it's the most recent entry in revisionsForPlan since
          // we only observe transitions where reason changed.
          const revisions = store.tasks.revisionsForPlan(plan.id);
          const diff = revisions[revisions.length - 1]?.diff;
          next.push({
            key: ++keyRef.current,
            planId: plan.id,
            reason,
            diff,
          });
        } else if (!seen.has(plan.id)) {
          seen.set(plan.id, reason);
        }
      }
      if (next.length > 0) {
        setEntries((cur) => {
          const merged = [...cur, ...next];
          return merged.slice(Math.max(0, merged.length - MAX_VISIBLE));
        });
      }
    };
    const unsubscribe = store.tasks.subscribe(scan);
    return () => {
      unsubscribe();
      setEntries([]);
    };
  }, [store]);

  useEffect(() => {
    if (entries.length === 0) return;
    const timers = entries.map((e) =>
      window.setTimeout(() => {
        setEntries((cur) => cur.filter((x) => x.key !== e.key));
      }, DISMISS_MS),
    );
    return () => {
      for (const t of timers) window.clearTimeout(t);
    };
  }, [entries]);

  if (entries.length === 0) return null;

  return (
    <div className="hg-revision-banner" data-testid="plan-revision-banner">
      {entries.map((e) => {
        const added = e.diff?.added.length ?? 0;
        const removed = e.diff?.removed.length ?? 0;
        const modified = e.diff?.modified.length ?? 0;
        const hasCounts = e.diff !== undefined;
        const parsed = parseRevisionReason(e.reason);
        const display = parsed.detail || parsed.meta.label;
        return (
          <div
            key={e.key}
            className="hg-revision-banner__pill"
            data-testid="plan-revision-pill"
            data-drift-kind={parsed.kind ?? 'unknown'}
            data-drift-category={parsed.meta.category}
            style={{ borderLeftColor: parsed.meta.color }}
          >
            <span
              className="hg-revision-banner__icon"
              data-testid="plan-revision-pill-icon"
              style={{ color: parsed.meta.color }}
              aria-hidden="true"
            >
              {parsed.meta.icon}
            </span>
            <span className="hg-revision-banner__label">
              {parsed.meta.label}
            </span>
            <span className="hg-revision-banner__reason" title={e.reason}>
              {display}
            </span>
            {hasCounts && (
              <span
                className="hg-revision-banner__counts"
                data-testid="plan-revision-pill-counts"
                aria-label={`${added} added, ${removed} removed, ${modified} modified`}
              >
                +{added} -{removed} ~{modified}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
