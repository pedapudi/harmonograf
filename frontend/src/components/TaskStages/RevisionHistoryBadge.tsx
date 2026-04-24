import {
  useCallback,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactElement,
} from 'react';
import type { Task } from '../../gantt/types';
// TODO(INT): LAY is landing the real `./collapsedLayout` module on a parallel
// branch. Until it merges into this worktree we declare a minimal local shape
// compatible with the one LAY is shipping. At integration time INT replaces
// this block with `import type { TaskRevisionChain } from './collapsedLayout';`
// and deletes the local interface.
export interface TaskRevisionChain {
  /** The representative task for the equivalence class — the one the DAG
   *  actually renders as a node. */
  canonical: Task;
  /** All tasks in the equivalence class, ordered oldest → newest. The
   *  last member MUST equal `canonical`. */
  members: Task[];
  /** Revision indices at which each `members[i]` was introduced. Same length
   *  as `members`. */
  revisions: number[];
}
import './RevisionHistoryBadge.css';

export interface RevisionHistoryBadgeProps {
  chain: TaskRevisionChain;
  /** Pinned revision from the scrubber. null = "latest". Determines which
   *  chain member appears "foregrounded" in the expanded view and how the
   *  badge labels itself (e.g. "R0→R1→R2 (current R2)" vs
   *  "R0→R1 (pinned R1, superseded by R2)"). */
  currentRevision: number | null;
  /** Controlled-expansion mode. If omitted, the component manages its own
   *  open/close state internally. When provided, parent owns state. */
  expanded?: boolean;
  onToggleExpanded?: () => void;
  /** Click handler for a specific chain member in the expanded trail.
   *  Typical use: open a task-detail drawer for the predecessor. */
  onClickMember?: (task: Task) => void;
}

const MAX_TITLE_LEN = 40;

function truncate(s: string, n: number = MAX_TITLE_LEN): string {
  if (s.length <= n) return s;
  return `${s.slice(0, Math.max(0, n - 1))}…`;
}

function formatChainLabel(revisions: number[]): string {
  return revisions.map((r) => `R${r}`).join('→');
}

/**
 * Classify `currentRevision` against the chain. Returned `pinnedIndex` is the
 * index in `members` / `revisions` that the scrubber is pointing at, or null
 * when the pin is "latest" (>= canonical rev) or "not yet introduced"
 * (< earliest rev). Exposed for test insight via the rendered DOM.
 */
function classifyPin(
  revisions: number[],
  currentRevision: number | null,
): { mode: 'latest' | 'pinned' | 'not-yet'; pinnedIndex: number | null } {
  if (currentRevision === null || revisions.length === 0) {
    return { mode: 'latest', pinnedIndex: null };
  }
  const canonicalRev = revisions[revisions.length - 1];
  const earliestRev = revisions[0];
  if (currentRevision >= canonicalRev) {
    return { mode: 'latest', pinnedIndex: null };
  }
  if (currentRevision < earliestRev) {
    return { mode: 'not-yet', pinnedIndex: null };
  }
  // Find the latest member whose introduction rev is <= currentRevision.
  // Scrubbing to an in-between rev (say R1 when the chain is R0→R2→R4) pins
  // the most recent predecessor that was already live at that rev, not the
  // one introduced strictly at that rev — matches how the scrubber filters
  // tasks elsewhere.
  let pinnedIndex = 0;
  for (let i = 0; i < revisions.length; i++) {
    if (revisions[i] <= currentRevision) pinnedIndex = i;
  }
  return { mode: 'pinned', pinnedIndex };
}

export function RevisionHistoryBadge(
  props: RevisionHistoryBadgeProps,
): ReactElement {
  const { chain, currentRevision, expanded, onToggleExpanded, onClickMember } =
    props;

  const isControlled = expanded !== undefined;
  const [internalExpanded, setInternalExpanded] = useState(false);
  const isExpanded = isControlled ? Boolean(expanded) : internalExpanded;

  const multiMember = chain.members.length >= 2;
  const pin = classifyPin(chain.revisions, currentRevision);
  const canonicalRev =
    chain.revisions.length > 0
      ? chain.revisions[chain.revisions.length - 1]
      : 0;

  const handleToggle = useCallback(() => {
    if (!multiMember) return;
    if (isControlled) {
      onToggleExpanded?.();
    } else {
      setInternalExpanded((v) => !v);
      onToggleExpanded?.();
    }
  }, [multiMember, isControlled, onToggleExpanded]);

  const handleKey = useCallback(
    (e: KeyboardEvent<HTMLButtonElement>) => {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        handleToggle();
      }
    },
    [handleToggle],
  );

  // Singleton chain: inert badge, no toggle. We still render it so DAG nodes
  // get a consistent "R<n>" tag. Muted styling signals "no history to see".
  if (!multiMember) {
    const only = chain.revisions[0] ?? 0;
    return (
      <span
        className="hg-rev-badge hg-rev-badge--singleton"
        data-testid="hg-rev-badge"
        aria-label={`Revision ${only}`}
      >
        <span className="hg-rev-badge__chip hg-rev-badge__chip--neutral">
          R{only}
        </span>
      </span>
    );
  }

  // Multi-member chain.
  const chainLabel = formatChainLabel(chain.revisions);
  const supersededRevs = chain.revisions
    .slice(0, -1)
    .map((r) => `R${r}`)
    .join(', ');
  const introducedRev = chain.revisions[0];
  const ariaLabel =
    `Revision history: introduced R${introducedRev}, superseded in ` +
    `${supersededRevs.replace(/^R\d+, /, '') || `R${canonicalRev}`}`;

  // Suffix summary attached to the pill text for quick glance. Kept brief so
  // the pill doesn't blow up the node bounding box.
  let summarySuffix = '';
  if (pin.mode === 'pinned' && pin.pinnedIndex !== null) {
    const pinnedRev = chain.revisions[pin.pinnedIndex];
    summarySuffix = ` (pinned R${pinnedRev}, superseded by R${canonicalRev})`;
  } else if (pin.mode === 'not-yet') {
    summarySuffix = ' (not yet introduced)';
  } else {
    summarySuffix = ` (current R${canonicalRev})`;
  }

  // Predecessors, newest-first. Canonical is excluded — it's the node itself.
  // `members[0 .. len-2]` are predecessors oldest→newest; reverse for display.
  const predecessors = chain.members
    .slice(0, -1)
    .map((task, idx) => ({ task, rev: chain.revisions[idx], originalIndex: idx }))
    .reverse();

  const handleMemberClick = (task: Task) => (e: MouseEvent) => {
    if (!onClickMember) return;
    e.stopPropagation();
    onClickMember(task);
  };

  return (
    <span
      className={`hg-rev-badge hg-rev-badge--multi${
        isExpanded ? ' hg-rev-badge--expanded' : ''
      }`}
      data-testid="hg-rev-badge"
      data-pin-mode={pin.mode}
    >
      <button
        type="button"
        className="hg-rev-badge__toggle"
        aria-expanded={isExpanded}
        aria-label={ariaLabel}
        onClick={handleToggle}
        onKeyDown={handleKey}
      >
        <span className="hg-rev-badge__chip hg-rev-badge__chip--chain">
          {chainLabel}
        </span>
        <span
          className="hg-rev-badge__chevron"
          aria-hidden="true"
          data-open={isExpanded}
        >
          {isExpanded ? '▾' : '▸'}
        </span>
        <span className="hg-rev-badge__summary">{summarySuffix.trim()}</span>
      </button>
      {isExpanded && (
        <ul className="hg-rev-badge__history" role="list">
          {predecessors.map(({ task, rev, originalIndex }) => {
            const isPinned =
              pin.mode === 'pinned' && pin.pinnedIndex === originalIndex;
            const clickable = Boolean(onClickMember);
            const rowClasses = [
              'hg-rev-badge__row',
              isPinned ? 'hg-rev-badge__row--pinned' : '',
              clickable ? 'hg-rev-badge__row--clickable' : '',
            ]
              .filter(Boolean)
              .join(' ');
            const commonProps = {
              className: rowClasses,
              'data-task-id': task.id,
              'data-rev': rev,
              role: 'listitem',
            } as const;
            const inner = (
              <>
                <span
                  className="hg-rev-badge__chip hg-rev-badge__chip--history"
                  data-rev={rev}
                >
                  R{rev}
                </span>
                <span className="hg-rev-badge__title" title={task.title}>
                  {truncate(task.title)}
                </span>
                {isPinned && (
                  <span className="hg-rev-badge__pinned-tag">(pinned)</span>
                )}
              </>
            );
            if (clickable) {
              return (
                <li key={task.id} {...commonProps}>
                  <button
                    type="button"
                    className="hg-rev-badge__row-btn"
                    onClick={handleMemberClick(task)}
                    aria-label={`Open predecessor R${rev}: ${task.title}`}
                  >
                    {inner}
                  </button>
                </li>
              );
            }
            return (
              <li key={task.id} {...commonProps}>
                {inner}
              </li>
            );
          })}
        </ul>
      )}
    </span>
  );
}
