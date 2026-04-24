// TrajectoryTimelineRibbon — single compact horizontal band (~70-80px tall)
// that subsumes the four stacked sections the Trajectory view used to show:
//   1. the Trajectory header rev-chip row,
//   2. the REVISIONS strip with notches + "Latest",
//   3. the per-rev descriptive cards,
//   4. the INTERVENTIONS list.
// All four communicate the same dimension — "what revisions happened and
// what triggered them" — so the ribbon collapses them into one scrubber.
//
// The component is a primitive: no store lookups, no session watching. The
// caller owns `selectedRevision` / `onSelectRevision` / `onInterventionClick`
// and passes the already-derived `revisions` + `interventions` arrays.
// Interface is FROZEN — a sibling agent building TrajectoryView layout is
// adopting it verbatim.

import { useCallback, useId, useMemo, useRef, useState } from 'react';
import type React from 'react';
import type { PlanRevisionRecord } from '../../../state/planHistoryStore';
import type { InterventionRow } from '../../../lib/interventions';
import './TrajectoryTimelineRibbon.css';

export interface TrajectoryTimelineRibbonProps {
  revisions: readonly PlanRevisionRecord[];
  interventions: readonly InterventionRow[];
  /** Controlled: revision number pinned, or the sentinel "latest". */
  selectedRevision: number | 'latest';
  onSelectRevision: (rev: number | 'latest') => void;
  onInterventionClick: (intervention: InterventionRow) => void;
  /** Escape-hatch: when true the caller is free to render the old stacked
   *  view. The ribbon itself does not change shape; it only toggles its
   *  expand-icon pressed-state so the affordance reads correctly. */
  expanded?: boolean;
  onToggleExpanded?: () => void;
}

// Severity → colour for intervention glyphs. Kept local so the ribbon has
// no palette dependency on lib/interventions beyond the row shape.
const SEVERITY_COLOR: Record<string, string> = {
  critical: '#e06070',
  warning: '#f59e0b',
  info: '#8d9199',
  '': '#8d9199',
};

function interventionColor(row: InterventionRow): string {
  const sev = (row.severity || '').toLowerCase();
  return SEVERITY_COLOR[sev] ?? SEVERITY_COLOR.info;
}

// Compute marker x-positions in [0, 1]. Revisions anchor the scale
// (oldest → 0, newest → 1). If timestamps cluster (delta < 1ms across
// the whole range) we fall back to even spacing so notches don't stack.
interface ScaleDomain {
  minMs: number;
  maxMs: number;
  even: boolean;
}

function computeScale(revisions: readonly PlanRevisionRecord[]): ScaleDomain {
  if (revisions.length === 0) return { minMs: 0, maxMs: 0, even: true };
  let min = Infinity;
  let max = -Infinity;
  for (const r of revisions) {
    if (r.emittedAtMs < min) min = r.emittedAtMs;
    if (r.emittedAtMs > max) max = r.emittedAtMs;
  }
  const even = !(Number.isFinite(min) && Number.isFinite(max)) || max - min < 1;
  return { minMs: even ? 0 : min, maxMs: even ? 1 : max, even };
}

function revFrac(
  rec: PlanRevisionRecord,
  idx: number,
  total: number,
  scale: ScaleDomain,
): number {
  if (total <= 1) return 0;
  if (scale.even) return idx / (total - 1);
  return (rec.emittedAtMs - scale.minMs) / (scale.maxMs - scale.minMs);
}

function interventionFrac(
  row: InterventionRow,
  revisions: readonly PlanRevisionRecord[],
  scale: ScaleDomain,
): number {
  if (revisions.length === 0) return 0;
  if (scale.even) {
    // Place each intervention between its flanking revisions by time order.
    // Scan revisions; interventions emitted before r[i].emittedAtMs land in
    // the gap between i-1 and i. Falls back to 0 / 1 for out-of-range.
    for (let i = 0; i < revisions.length; i++) {
      if (row.atMs <= revisions[i].emittedAtMs) {
        if (i === 0) return 0;
        return (i - 0.5) / Math.max(1, revisions.length - 1);
      }
    }
    return 1;
  }
  if (scale.maxMs <= scale.minMs) return 0;
  const f = (row.atMs - scale.minMs) / (scale.maxMs - scale.minMs);
  return Math.max(0, Math.min(1, f));
}

function revisionSummary(rec: PlanRevisionRecord): string {
  if (rec.revision === 0) return 'R0: initial plan';
  const kind = rec.kind ? ` · ${rec.kind.toUpperCase()}` : '';
  const reason = rec.reason ? `: ${rec.reason}` : '';
  return `R${rec.revision}${kind}${reason}`;
}

function interventionSummary(row: InterventionRow): string {
  const sev = row.severity ? ` · ${row.severity.toUpperCase()}` : '';
  const detail = row.bodyOrReason
    ? `: ${row.bodyOrReason.slice(0, 120)}${row.bodyOrReason.length > 120 ? '…' : ''}`
    : '';
  return `${row.kind}${sev}${detail}`;
}

function fmtAt(atMs: number): string {
  if (!Number.isFinite(atMs) || atMs < 0) return '';
  const total = Math.max(0, Math.floor(atMs / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function TrajectoryTimelineRibbon(
  props: TrajectoryTimelineRibbonProps,
): React.ReactElement {
  const {
    revisions,
    interventions,
    selectedRevision,
    onSelectRevision,
    onInterventionClick,
    expanded,
    onToggleExpanded,
  } = props;

  const popoverIdBase = useId();
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const markersRef = useRef<Map<string, HTMLButtonElement>>(new Map());

  const scale = useMemo(() => computeScale(revisions), [revisions]);
  const sortedRevs = useMemo(
    () => [...revisions].sort((a, b) => a.revision - b.revision),
    [revisions],
  );
  const sortedIntvs = useMemo(
    () => [...interventions].sort((a, b) => a.atMs - b.atMs),
    [interventions],
  );

  // Ordered focus ring: revision notches in revision order, then "Latest".
  // Intervention glyphs are tab-stoppable too, interleaved by time after
  // the revision they follow.
  const markerOrder = useMemo(() => {
    const order: string[] = [];
    for (let i = 0; i < sortedRevs.length; i++) {
      order.push(`rev:${sortedRevs[i].revision}`);
      const upto =
        i + 1 < sortedRevs.length ? sortedRevs[i + 1].emittedAtMs : Infinity;
      for (const iv of sortedIntvs) {
        if (iv.atMs >= sortedRevs[i].emittedAtMs && iv.atMs < upto) {
          order.push(`intv:${iv.key}`);
        }
      }
    }
    order.push('rev:latest');
    return order;
  }, [sortedRevs, sortedIntvs]);

  const focusByKey = useCallback((key: string) => {
    const el = markersRef.current.get(key);
    if (el) el.focus();
  }, []);

  const moveFocus = useCallback(
    (currentKey: string, delta: 1 | -1) => {
      const idx = markerOrder.indexOf(currentKey);
      if (idx < 0) return;
      const next = idx + delta;
      if (next < 0 || next >= markerOrder.length) return;
      focusByKey(markerOrder[next]);
    },
    [markerOrder, focusByKey],
  );

  const onRevKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>, key: string) => {
      if (e.key === 'ArrowRight') {
        e.preventDefault();
        moveFocus(key, 1);
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        moveFocus(key, -1);
      }
    },
    [moveFocus],
  );

  const setMarkerRef = (key: string) => (el: HTMLButtonElement | null) => {
    if (el) markersRef.current.set(key, el);
    else markersRef.current.delete(key);
  };

  const hasLatestSelected = selectedRevision === 'latest';
  const totalRevs = sortedRevs.length;
  const headerCount =
    `${revisions.length} revision${revisions.length === 1 ? '' : 's'} · ` +
    `${interventions.length} intervention${interventions.length === 1 ? '' : 's'}`;

  return (
    <div
      className="hg-traj-ribbon"
      data-testid="trajectory-timeline-ribbon"
      data-expanded={expanded ? 'true' : 'false'}
    >
      <div className="hg-traj-ribbon__lead">
        <span className="hg-traj-ribbon__title">Trajectory</span>
        <span className="hg-traj-ribbon__count">{headerCount}</span>
      </div>

      <div
        className="hg-traj-ribbon__track"
        role="tablist"
        aria-label="Plan revisions and interventions"
      >
        <div className="hg-traj-ribbon__baseline" aria-hidden="true" />

        {sortedRevs.map((rec, idx) => {
          const frac = revFrac(rec, idx, totalRevs, scale);
          const selected =
            typeof selectedRevision === 'number' &&
            selectedRevision === rec.revision;
          const key = `rev:${rec.revision}`;
          const popId = `${popoverIdBase}-${key}`;
          return (
            <button
              key={key}
              ref={setMarkerRef(key)}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-describedby={hoverKey === key ? popId : undefined}
              className="hg-traj-ribbon__rev"
              data-testid={`ribbon-rev-${rec.revision}`}
              data-selected={selected ? 'true' : 'false'}
              style={{ left: `${frac * 100}%` }}
              onClick={() => onSelectRevision(rec.revision)}
              onMouseEnter={() => setHoverKey(key)}
              onMouseLeave={() =>
                setHoverKey((k) => (k === key ? null : k))
              }
              onFocus={() => setHoverKey(key)}
              onBlur={() =>
                setHoverKey((k) => (k === key ? null : k))
              }
              onKeyDown={(e) => onRevKeyDown(e, key)}
            >
              <span className="hg-traj-ribbon__rev-dot" aria-hidden="true" />
              <span className="hg-traj-ribbon__rev-label">
                R{rec.revision}
              </span>
              {hoverKey === key && (
                <span
                  id={popId}
                  role="tooltip"
                  className="hg-traj-ribbon__popover"
                  data-testid={`ribbon-popover-rev-${rec.revision}`}
                >
                  {revisionSummary(rec)}
                </span>
              )}
            </button>
          );
        })}

        {sortedIntvs.map((row) => {
          const frac = interventionFrac(row, sortedRevs, scale);
          const key = `intv:${row.key}`;
          const popId = `${popoverIdBase}-${key}`;
          const color = interventionColor(row);
          const sev = (row.severity || 'info').toLowerCase();
          return (
            <button
              key={key}
              ref={setMarkerRef(key)}
              type="button"
              role="button"
              aria-label={`Intervention ${row.kind}${
                row.severity ? ` severity ${row.severity}` : ''
              } at ${fmtAt(row.atMs)}`}
              aria-describedby={hoverKey === key ? popId : undefined}
              className="hg-traj-ribbon__intv"
              data-testid={`ribbon-intv-${row.key}`}
              data-severity={sev}
              style={{ left: `${frac * 100}%`, color }}
              onClick={() => onInterventionClick(row)}
              onMouseEnter={() => setHoverKey(key)}
              onMouseLeave={() =>
                setHoverKey((k) => (k === key ? null : k))
              }
              onFocus={() => setHoverKey(key)}
              onBlur={() =>
                setHoverKey((k) => (k === key ? null : k))
              }
              onKeyDown={(e) => onRevKeyDown(e, key)}
            >
              <span className="hg-traj-ribbon__intv-glyph" aria-hidden="true">
                ▲
              </span>
              {hoverKey === key && (
                <span
                  id={popId}
                  role="tooltip"
                  className="hg-traj-ribbon__popover"
                  data-testid={`ribbon-popover-intv-${row.key}`}
                >
                  {interventionSummary(row)}
                  <span className="hg-traj-ribbon__popover-at">
                    {fmtAt(row.atMs)}
                  </span>
                </span>
              )}
            </button>
          );
        })}

        {/* Latest pseudo-notch anchored at right edge. Selected when the
            caller's selectedRevision === 'latest'. */}
        <button
          ref={setMarkerRef('rev:latest')}
          type="button"
          role="tab"
          aria-selected={hasLatestSelected}
          className="hg-traj-ribbon__latest"
          data-testid="ribbon-rev-latest"
          data-selected={hasLatestSelected ? 'true' : 'false'}
          onClick={() => onSelectRevision('latest')}
          onKeyDown={(e) => onRevKeyDown(e, 'rev:latest')}
        >
          <span className="hg-traj-ribbon__rev-dot" aria-hidden="true" />
          <span className="hg-traj-ribbon__rev-label">Latest</span>
        </button>
      </div>

      <div className="hg-traj-ribbon__tail">
        {!hasLatestSelected && (
          <button
            type="button"
            className="hg-traj-ribbon__latest-btn"
            data-testid="ribbon-latest-btn"
            onClick={() => onSelectRevision('latest')}
          >
            Latest
          </button>
        )}
        {onToggleExpanded && (
          <button
            type="button"
            className="hg-traj-ribbon__expand"
            data-testid="ribbon-expand-btn"
            aria-label={expanded ? 'Collapse ribbon' : 'Expand to stacked view'}
            aria-pressed={expanded ? 'true' : 'false'}
            onClick={onToggleExpanded}
            title={expanded ? 'Collapse' : 'Expand to stacked view'}
          >
            ⤢
          </button>
        )}
      </div>
    </div>
  );
}
