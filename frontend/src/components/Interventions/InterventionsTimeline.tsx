// Unified intervention timeline strip (issue #69).
//
// Renders a horizontal strip of markers — one per Intervention — that sits
// above (or below) the stage DAG in the planning view. Markers are colour-
// coded by source (user / drift / goldfive) and sized by severity for drift
// rows. Hover surfaces a tooltip with kind + outcome; click expands a card
// with body/author/outcome and a "jump to plan revision" affordance when
// the intervention produced a new revision.
//
// Rendering is tree-agnostic: the component never inspects kind taxonomies
// or makes domain-specific decisions. Any kind string the server emits
// renders uniformly — new drift kinds added to goldfive tomorrow work
// without a frontend change.

import { useMemo, useState } from 'react';
import type { TaskPlan } from '../../gantt/types';
import {
  markerRadiusFor,
  SOURCE_COLOR,
  type InterventionRow,
} from '../../lib/interventions';
import './InterventionsTimeline.css';

interface InterventionsTimelineProps {
  rows: readonly InterventionRow[];
  // Session-relative ms window the strip should span. When the planning
  // view draws multiple plans stacked, each plan passes its own window so
  // the markers align to the DAG above.
  startMs: number;
  endMs: number;
  // Optional: all plan revs so the "jump to rev" button can find the target.
  revs?: readonly TaskPlan[];
  // Callback fired when the user presses the jump-to-rev button. The
  // component does not navigate on its own — this keeps the timeline
  // reusable from both planning and trajectory views.
  onJumpToRevision?: (revisionIndex: number) => void;
  // Width hint from the parent container. If omitted the strip expands to
  // fill its container.
  width?: number;
}

const STRIP_HEIGHT = 36;
const STRIP_PAD_X = 12;

function labelForOutcome(outcome: string): string {
  if (!outcome) return 'pending';
  // "plan_revised:r3" → "→ rev 3"; "cascade_cancel:2_tasks" → "→ cancel 2 tasks".
  if (outcome.startsWith('plan_revised:r')) {
    return `→ rev ${outcome.slice('plan_revised:r'.length)}`;
  }
  if (outcome.startsWith('cascade_cancel:')) {
    const rest = outcome.slice('cascade_cancel:'.length).replace('_', ' ');
    return `→ cancel ${rest}`;
  }
  return `→ ${outcome}`;
}

function fmtAt(atMs: number): string {
  if (!Number.isFinite(atMs) || atMs < 0) return '';
  const total = Math.max(0, Math.floor(atMs / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function InterventionsTimeline({
  rows,
  startMs,
  endMs,
  revs,
  onJumpToRevision,
  width,
}: InterventionsTimelineProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  const span = Math.max(1, endMs - startMs);
  const actualWidth = width ?? 480;

  // Map each row to its x coordinate on the strip.
  const plotted = useMemo(() => {
    return rows.map((row) => {
      const tNorm = Math.min(1, Math.max(0, (row.atMs - startMs) / span));
      const cx =
        STRIP_PAD_X + tNorm * (actualWidth - STRIP_PAD_X * 2);
      return { row, cx };
    });
  }, [rows, startMs, span, actualWidth]);

  const expandedRow = expandedKey
    ? rows.find((r) => r.key === expandedKey) ?? null
    : null;

  return (
    <div
      className="hg-interventions-strip"
      data-testid="interventions-timeline"
      style={{ width: width ? `${width}px` : '100%' }}
    >
      <svg
        className="hg-interventions-strip__svg"
        width="100%"
        height={STRIP_HEIGHT}
        viewBox={`0 0 ${actualWidth} ${STRIP_HEIGHT}`}
        preserveAspectRatio="none"
      >
        <line
          className="hg-interventions-strip__rule"
          x1={STRIP_PAD_X}
          y1={STRIP_HEIGHT / 2}
          x2={actualWidth - STRIP_PAD_X}
          y2={STRIP_HEIGHT / 2}
        />
        {plotted.map(({ row, cx }) => {
          const r = markerRadiusFor(row) / 2;
          const color = SOURCE_COLOR[row.source] ?? SOURCE_COLOR.goldfive;
          const selected = row.key === expandedKey;
          const glyph = row.source === 'user' ? 'diamond' : 'circle';
          const title = `${row.kind} (${row.source})${
            row.outcome ? ' · ' + labelForOutcome(row.outcome) : ''
          }`;
          return (
            <g
              key={row.key}
              className={
                selected
                  ? 'hg-interventions-strip__marker hg-interventions-strip__marker--selected'
                  : 'hg-interventions-strip__marker'
              }
              transform={`translate(${cx}, ${STRIP_HEIGHT / 2})`}
              data-testid={`intervention-marker-${row.key}`}
              data-source={row.source}
              data-kind={row.kind}
              onClick={() =>
                setExpandedKey((k) => (k === row.key ? null : row.key))
              }
            >
              <title>{title}</title>
              {glyph === 'diamond' ? (
                // User markers are diamonds so they're distinguishable in
                // print / colour-blind view beyond just the blue hue.
                <rect
                  className="hg-interventions-strip__marker-shape"
                  x={-r}
                  y={-r}
                  width={r * 2}
                  height={r * 2}
                  fill={color}
                  transform="rotate(45)"
                />
              ) : (
                <circle
                  className="hg-interventions-strip__marker-shape"
                  r={r}
                  fill={color}
                />
              )}
            </g>
          );
        })}
      </svg>
      {expandedRow && (
        <InterventionCard
          row={expandedRow}
          revs={revs}
          onClose={() => setExpandedKey(null)}
          onJumpToRevision={onJumpToRevision}
        />
      )}
      {rows.length === 0 && (
        <div className="hg-interventions-strip__empty">
          No interventions recorded.
        </div>
      )}
    </div>
  );
}

interface InterventionCardProps {
  row: InterventionRow;
  revs?: readonly TaskPlan[];
  onClose(): void;
  onJumpToRevision?: (revisionIndex: number) => void;
}

function InterventionCard({
  row,
  revs,
  onClose,
  onJumpToRevision,
}: InterventionCardProps) {
  const targetRev =
    row.planRevisionIndex > 0
      ? revs?.find((p) => (p.revisionIndex ?? 0) === row.planRevisionIndex) ??
        null
      : null;

  return (
    <div
      className="hg-interventions-card"
      data-testid="intervention-card"
      data-source={row.source}
    >
      <header className="hg-interventions-card__head">
        <span
          className="hg-interventions-card__chip"
          data-source={row.source}
          title={`source: ${row.source}`}
        >
          {row.source}
        </span>
        <span className="hg-interventions-card__kind">{row.kind}</span>
        <span className="hg-interventions-card__at">{fmtAt(row.atMs)}</span>
        {row.severity && (
          <span
            className="hg-interventions-card__severity"
            data-severity={row.severity}
          >
            {row.severity}
          </span>
        )}
        <button
          type="button"
          className="hg-interventions-card__close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </header>
      {row.author && (
        <div className="hg-interventions-card__author">
          by <strong>{row.author}</strong>
        </div>
      )}
      {row.bodyOrReason && (
        <div className="hg-interventions-card__body">{row.bodyOrReason}</div>
      )}
      <footer className="hg-interventions-card__foot">
        <span className="hg-interventions-card__outcome">
          {labelForOutcome(row.outcome)}
        </span>
        {row.planRevisionIndex > 0 && targetRev && onJumpToRevision && (
          <button
            type="button"
            className="hg-interventions-card__jump"
            onClick={() => onJumpToRevision(row.planRevisionIndex)}
            data-testid="intervention-card__jump"
          >
            Jump to rev {row.planRevisionIndex}
          </button>
        )}
      </footer>
    </div>
  );
}
