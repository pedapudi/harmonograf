// RevisionScrubber — horizontal timeline of plan revisions. Rendered at
// the top of the Trajectory view (and intended for reuse by the Task/Plan
// panel sibling). One notch per PlanRevisionRecord; clicking a notch pins
// the view to that revision so tasks introduced later are hidden. The
// "Latest" sentinel (cumulative, all tasks including superseded) is the
// default; selecting any earlier notch sets `pinned = revision number`.
//
// Keyboard: left/right arrows move between notches; Home jumps to the
// initial revision, End returns to "Latest". The buttons are tab-stopped
// so the full scrubber is operable without a mouse.
//
// This is a primitive: no store lookups, no session watching. Callers
// own the `pinnedRevision` state and pass `history` derived from
// `usePlanHistory(sessionId, planId)`.

import type React from 'react';
import { useCallback } from 'react';
import type { PlanRevisionRecord } from '../../../state/planHistoryStore';

export interface RevisionScrubberProps {
  /** Oldest → newest. Empty array renders nothing. */
  history: readonly PlanRevisionRecord[];
  /** Revision number the caller has pinned; null means "Latest" (cumulative). */
  pinnedRevision: number | null;
  /** Invoked with the revision number clicked, or null for "Latest". */
  onPinRevision: (revision: number | null) => void;
}

// Short human-readable label for a revision's trigger. Falls back to the
// bare kind and finally to "initial" on rev 0.
function triggerLabel(rec: PlanRevisionRecord): string {
  if (rec.revision === 0) return 'initial';
  if (rec.kind) return rec.kind;
  if (rec.reason) return rec.reason.slice(0, 32);
  return `rev ${rec.revision}`;
}

// Severity inferred from the drift kind — user steers bucket as "user",
// goldfive-authored as "goldfive", initial plan has no authorship.
function authorshipClass(rec: PlanRevisionRecord): string {
  if (rec.revision === 0) return 'initial';
  if (rec.kind.startsWith('user_')) return 'user';
  return 'goldfive';
}

export function RevisionScrubber(props: RevisionScrubberProps): React.ReactElement | null {
  const { history, pinnedRevision, onPinRevision } = props;

  const onKey = useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      if (history.length === 0) return;
      const currentIdx =
        pinnedRevision == null
          ? history.length
          : history.findIndex((r) => r.revision === pinnedRevision);
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        const nextIdx = Math.max(0, currentIdx - 1);
        const next = history[nextIdx];
        onPinRevision(next ? next.revision : null);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        if (currentIdx >= history.length - 1) {
          onPinRevision(null);
        } else {
          const next = history[currentIdx + 1];
          onPinRevision(next ? next.revision : null);
        }
      } else if (e.key === 'Home') {
        e.preventDefault();
        onPinRevision(history[0].revision);
      } else if (e.key === 'End') {
        e.preventDefault();
        onPinRevision(null);
      }
    },
    [history, pinnedRevision, onPinRevision],
  );

  if (history.length === 0) return null;

  return (
    <div
      className="hg-traj__scrubber"
      data-testid="revision-scrubber"
      role="tablist"
      aria-label="Plan revisions"
      onKeyDown={onKey}
    >
      <span className="hg-traj__scrubber-label" aria-hidden="true">
        Revisions
      </span>
      {history.map((rec) => {
        const selected = pinnedRevision === rec.revision;
        const cls = authorshipClass(rec);
        return (
          <button
            key={`scrub-${rec.plan.id}-${rec.revision}`}
            type="button"
            role="tab"
            className="hg-traj__scrubber-notch"
            aria-selected={selected}
            data-authorship={cls}
            data-testid={`scrubber-notch-${rec.revision}`}
            onClick={() => onPinRevision(rec.revision)}
            title={
              rec.reason
                ? `rev ${rec.revision} · ${rec.kind || 'initial'}: ${rec.reason}`
                : `rev ${rec.revision} · ${rec.kind || 'initial'}`
            }
          >
            <span className="hg-traj__scrubber-tick" />
            <span className="hg-traj__scrubber-num">REV {rec.revision}</span>
            <span className="hg-traj__scrubber-kind">{triggerLabel(rec)}</span>
          </button>
        );
      })}
      <button
        type="button"
        role="tab"
        className="hg-traj__scrubber-latest"
        aria-selected={pinnedRevision === null}
        data-testid="scrubber-notch-latest"
        onClick={() => onPinRevision(null)}
        title="Show the cumulative view across every revision"
      >
        Latest
      </button>
    </div>
  );
}
