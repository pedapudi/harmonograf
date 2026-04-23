// Compact textual list of interventions, rendered under the plan DAG in the
// Gantt task panel. Replaces InterventionsTimeline: the *when* visualisation
// now lives on the Gantt canvas as translucent bands (renderer.ts
// `drawInterventionBands`), so this view only needs to enumerate the rows
// with enough detail that the user can pick one out and jump to its moment.
//
// Intentionally boring: no SVG, no time axis, no clustering. Every row is
// one line. When the user clicks a row we pin the Gantt cursor to that atMs
// via the shared UI store so the band on the canvas visually pulses.

import { useState } from 'react';
import type { InterventionRow } from '../../lib/interventions';
import { SOURCE_COLOR } from '../../lib/interventions';
import './InterventionsList.css';

interface InterventionsListProps {
  rows: readonly InterventionRow[];
  // Fired when the user clicks a row. Parents should pan the Gantt to
  // `atMs` and pulse the band. Omitting the callback makes rows
  // non-clickable — useful when the list is rendered outside the Gantt.
  onRowClick?: (row: InterventionRow) => void;
}

function fmtAt(atMs: number): string {
  if (!Number.isFinite(atMs) || atMs < 0) return '';
  const total = Math.max(0, Math.floor(atMs / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number): string => n.toString().padStart(2, '0');
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}

function fmtOutcome(outcome: string): string {
  if (!outcome) return '';
  if (outcome.startsWith('plan_revised:r')) {
    return `→ rev ${outcome.slice('plan_revised:r'.length)}`;
  }
  if (outcome.startsWith('cascade_cancel:')) {
    const rest = outcome.slice('cascade_cancel:'.length).replace('_', ' ');
    return `→ cancel ${rest}`;
  }
  return `→ ${outcome}`;
}

export function InterventionsList({ rows, onRowClick }: InterventionsListProps) {
  const [expanded, setExpanded] = useState(false);
  const isEmpty = rows.length === 0;
  // Empty → collapsed by default, with a toggle to un-collapse so the
  // user can still see the "no interventions recorded" hint if they want.
  // Non-empty → always shown (no toggle chrome).
  const show = !isEmpty || expanded;

  return (
    <div
      className="hg-interventions-list"
      data-testid="interventions-list"
      data-empty={isEmpty ? 'true' : 'false'}
    >
      <div className="hg-interventions-list__header">
        {isEmpty ? (
          <button
            type="button"
            className="hg-interventions-list__toggle"
            data-testid="interventions-list-toggle"
            aria-expanded={show}
            onClick={() => setExpanded((v) => !v)}
          >
            <span className="hg-interventions-list__caret" aria-hidden="true">
              {show ? '▾' : '▸'}
            </span>
            Interventions (0)
          </button>
        ) : (
          <span className="hg-interventions-list__label">
            Interventions ({rows.length})
          </span>
        )}
      </div>
      {show && isEmpty && (
        <div className="hg-interventions-list__empty">No interventions recorded.</div>
      )}
      {show && !isEmpty && (
        <ul className="hg-interventions-list__rows">
          {rows.map((row) => {
            const color = SOURCE_COLOR[row.source] ?? SOURCE_COLOR.goldfive;
            const clickable = !!onRowClick;
            const Tag = clickable ? 'button' : 'div';
            return (
              <li key={row.key} className="hg-interventions-list__row-item">
                <Tag
                  type={clickable ? 'button' : undefined}
                  className="hg-interventions-list__row"
                  data-testid={`interventions-list-row-${row.key}`}
                  data-source={row.source}
                  onClick={clickable ? () => onRowClick?.(row) : undefined}
                >
                  <span
                    className="hg-interventions-list__swatch"
                    style={{ background: color }}
                    aria-hidden="true"
                  />
                  <span className="hg-interventions-list__at">{fmtAt(row.atMs)}</span>
                  <span className="hg-interventions-list__kind">{row.kind}</span>
                  {row.severity && row.severity !== 'info' && (
                    <span
                      className="hg-interventions-list__severity"
                      data-severity={row.severity}
                    >
                      {row.severity}
                    </span>
                  )}
                  {row.bodyOrReason && (
                    <span className="hg-interventions-list__body" title={row.bodyOrReason}>
                      {row.bodyOrReason}
                    </span>
                  )}
                  {row.outcome && (
                    <span className="hg-interventions-list__outcome">
                      {fmtOutcome(row.outcome)}
                    </span>
                  )}
                </Tag>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
