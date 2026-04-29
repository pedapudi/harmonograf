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
import { SOURCE_COLOR, SOURCE_GLYPH } from '../../lib/interventions';
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
  // goldfive#264 refine outcomes:
  //   - ``refine_failed:<failure_kind>`` — failure terminal.
  //   - ``pending`` — attempted but no terminal observed yet.
  if (outcome.startsWith('refine_failed:')) {
    const fk = outcome.slice('refine_failed:'.length).replace('_', ' ');
    return `× ${fk}`;
  }
  if (outcome === 'pending') {
    return '… pending';
  }
  return `→ ${outcome}`;
}

// goldfive#318 (frontend follow-up): render lifecycle as a short uppercase
// label so the chip reads at a glance ("OPENED", "ESCALATING", "RESOLVED",
// "HUMAN INTV"). Empty string when goldfive emitted UNSPECIFIED — the chip
// is suppressed in that case (see render guards below).
function fmtLifecycle(lifecycle: string): string {
  const lc = (lifecycle || '').toLowerCase();
  if (!lc) return '';
  if (lc === 'human_intervention_required') return 'HUMAN INTV';
  return lc.toUpperCase();
}

export function InterventionsList({ rows, onRowClick }: InterventionsListProps) {
  const [expanded, setExpanded] = useState(false);
  // Per-row expansion state for grouped drift conditions
  // (goldfive#318). Keyed by row.key so re-renders don't churn the
  // open set as long as the conditionId stays stable. A Set kept in
  // local component state is enough — no global store concept needed
  // because the expansion is purely cosmetic.
  const [expandedConditions, setExpandedConditions] = useState<Set<string>>(
    () => new Set(),
  );
  const isEmpty = rows.length === 0;
  // Empty → collapsed by default, with a toggle to un-collapse so the
  // user can still see the "no interventions recorded" hint if they want.
  // Non-empty → always shown (no toggle chrome).
  const show = !isEmpty || expanded;

  function toggleCondition(key: string) {
    setExpandedConditions((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

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
            const glyph =
              SOURCE_GLYPH[row.source] ?? SOURCE_GLYPH.goldfive;
            const clickable = !!onRowClick;
            const Tag = clickable ? 'button' : 'div';
            // Cancel rows carry a short agent label on the main line so
            // operators can identify which invocation was cancelled
            // without drilling into the detail pane. Other rows leave
            // the agent label blank (the drift/annotation row already
            // surfaces its attribution via the drift detail drawer).
            const agentLabel =
              row.source === 'cancel' && row.targetAgentId
                ? row.targetAgentId.includes(':')
                  ? row.targetAgentId.split(':').pop() ?? ''
                  : row.targetAgentId
                : '';
            // goldfive#318: collapsed-condition chrome. A row with
            // ``observationCount > 1`` renders a count badge + an
            // expansion caret; clicking the caret reveals each
            // observation as a sub-row. Lifecycle chip renders for any
            // row that has a non-empty currentLifecycle (single-shot or
            // grouped).
            const obsCount = row.observationCount ?? 0;
            const isGrouped = obsCount > 1;
            const isExpanded = expandedConditions.has(row.key);
            const lifecycleLabel = fmtLifecycle(row.currentLifecycle ?? '');
            const transitions = row.severityTransitions ?? [];
            const observations = row.observations ?? [];
            return (
              <li key={row.key} className="hg-interventions-list__row-item">
                <Tag
                  type={clickable ? 'button' : undefined}
                  className="hg-interventions-list__row"
                  data-testid={`interventions-list-row-${row.key}`}
                  data-source={row.source}
                  data-grouped={isGrouped ? 'true' : undefined}
                  onClick={clickable ? () => onRowClick?.(row) : undefined}
                >
                  <span
                    className="hg-interventions-list__swatch"
                    style={{ background: color }}
                    aria-hidden="true"
                  >
                    {row.source === 'cancel' || row.source === 'refine' ? (
                      <span className="hg-interventions-list__glyph" aria-hidden="true">
                        {glyph}
                      </span>
                    ) : null}
                  </span>
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
                  {/* goldfive#318: lifecycle chip + severity-transition
                      marker. The chip renders as a subtle uppercase pill
                      next to the kind so operators can read the current
                      condition state at a glance. The transition marker
                      shows the most recent severity bump (e.g.
                      "warning → critical") — when multiple transitions
                      exist we summarise as the first→last severity so
                      the row stays one line. */}
                  {lifecycleLabel && (
                    <span
                      className="hg-interventions-list__lifecycle"
                      data-lifecycle={(row.currentLifecycle ?? '').toLowerCase()}
                      data-testid={`interventions-list-row-${row.key}-lifecycle`}
                    >
                      {lifecycleLabel}
                    </span>
                  )}
                  {transitions.length > 0 && (
                    <span
                      className="hg-interventions-list__transition"
                      data-testid={`interventions-list-row-${row.key}-transition`}
                      title={transitions
                        .map((t) => `${t.fromSeverity} → ${t.toSeverity}`)
                        .join(', ')}
                    >
                      {transitions[0].fromSeverity} →{' '}
                      {transitions[transitions.length - 1].toSeverity}
                    </span>
                  )}
                  {agentLabel && (
                    <span
                      className="hg-interventions-list__agent"
                      data-testid={`interventions-list-row-${row.key}-agent`}
                    >
                      {agentLabel}
                    </span>
                  )}
                  {row.bodyOrReason && (
                    <span className="hg-interventions-list__body" title={row.bodyOrReason}>
                      {row.bodyOrReason}
                    </span>
                  )}
                  {row.outcome && row.source !== 'cancel' && (
                    <span className="hg-interventions-list__outcome">
                      {fmtOutcome(row.outcome)}
                    </span>
                  )}
                </Tag>
                {/* goldfive#318: count badge + expansion control rendered
                    as a SECOND control under the row to keep the click
                    targets distinct (clicking the row pans the Gantt;
                    clicking the caret expands the observations). */}
                {isGrouped && (
                  <div className="hg-interventions-list__condition-control">
                    <button
                      type="button"
                      className="hg-interventions-list__expand"
                      data-testid={`interventions-list-row-${row.key}-expand`}
                      aria-expanded={isExpanded}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleCondition(row.key);
                      }}
                    >
                      <span
                        className="hg-interventions-list__caret"
                        aria-hidden="true"
                      >
                        {isExpanded ? '▾' : '▸'}
                      </span>
                      {obsCount} observations
                    </button>
                  </div>
                )}
                {isGrouped && isExpanded && (
                  <ul
                    className="hg-interventions-list__observations"
                    data-testid={`interventions-list-row-${row.key}-observations`}
                  >
                    {observations.map((obs) => {
                      const obsLabel = fmtLifecycle(obs.lifecycle);
                      const showTransition =
                        obs.prevSeverity &&
                        obs.prevSeverity !== obs.severity;
                      return (
                        <li
                          key={`obs-${obs.seq}`}
                          className="hg-interventions-list__observation"
                          data-testid={`interventions-list-obs-${obs.seq}`}
                        >
                          <span className="hg-interventions-list__at">
                            {fmtAt(obs.atMs)}
                          </span>
                          {obsLabel && (
                            <span
                              className="hg-interventions-list__lifecycle"
                              data-lifecycle={obs.lifecycle.toLowerCase()}
                            >
                              {obsLabel}
                            </span>
                          )}
                          {obs.severity && obs.severity !== 'info' && (
                            <span
                              className="hg-interventions-list__severity"
                              data-severity={obs.severity}
                            >
                              {obs.severity}
                            </span>
                          )}
                          {showTransition && (
                            <span className="hg-interventions-list__transition">
                              {obs.prevSeverity} → {obs.severity}
                            </span>
                          )}
                          {obs.detail && (
                            <span
                              className="hg-interventions-list__body"
                              title={obs.detail}
                            >
                              {obs.detail}
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
