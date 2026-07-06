// Intervention row model + wire conversion + presentation helpers.
//
// The unified intervention history (merge / outcome attribution / drift
// condition collapse across all seven source families) is derived
// SERVER-SIDE by the ListInterventions RPC — see
// harmonograf_server/interventions.py, the single source of truth. This
// file no longer computes the history; it defines the ``InterventionRow``
// shape the renderers consume, converts each proto ``Intervention`` onto
// that shape (:func:`interventionRowFromPb`), and holds the presentation
// tables (colors, glyphs, marker sizing). ``state/interventionsStore.ts``
// fetches + caches the rows; the views read them from there.
//
// The renderers are intentionally tree-agnostic: no taxonomy knowledge is
// baked into the markers; they show whatever kind/severity/outcome strings
// the server produced.

import type { Intervention as PbIntervention } from '../pb/harmonograf/v1/types_pb';

// Stable source taxonomy used by the UI. Anything else renders as "goldfive"
// grey so new kinds emitted by the server don't crash the view.
//
// ``cancel`` is the source tag for InvocationCancelled markers — an
// operator-observability record that goldfive cooperatively cancelled an
// agent invocation (goldfive#251 Stream C / #259). Distinct from `drift`
// because a cancel is a consequence of a drift, not a drift itself; the
// two rows coexist in the timeline (the drift explains WHY and the
// cancel records WHAT happened to the invocation).
//
// ``refine`` is the source tag for the merged refine-attempt rows
// (goldfive#264). One row per ``RefineAttempted`` event, carrying the
// outcome of the paired terminal (a successful ``PlanRevised`` or a
// ``RefineFailed``) inline in ``outcome`` and ``severity``. Distinct
// from ``goldfive`` (which is reserved for autonomous orchestrator
// kinds like cascade_cancel) so the UI can pick a refine-specific
// glyph + palette swatch.
// ``transition`` is the source tag for TaskTransitioned rows
// (goldfive#267 / #251 R4). One row per filtered transition (terminal
// to_status + meaningful source; the server applies the filter ladder).
// Distinct from ``cancel`` (which fires on
// invocation cancellation, not task status flip) and from ``goldfive``
// (which is reserved for autonomous orchestrator kinds at the plan
// revision level) so the UI can pick a transition-specific glyph and
// palette swatch.
export type InterventionSource =
  | 'user'
  | 'drift'
  | 'goldfive'
  | 'cancel'
  | 'refine'
  | 'transition';

const KNOWN_SOURCES = new Set<InterventionSource>([
  'user',
  'drift',
  'goldfive',
  'cancel',
  'refine',
  'transition',
]);

export interface InterventionRow {
  // Stable key for React lists — composed from source + (annotation id /
  // drift seq / plan id + rev index / cancel seq).
  key: string;
  // Session-relative ms, mirroring Span.startMs. Callers that only have
  // wall-clock should align against the session createdAt in the outer
  // component since we don't have a way to reference it from here.
  atMs: number;
  source: InterventionSource;
  // Human-readable label ("STEER" / "LOOPING_REASONING" / "CASCADE_CANCEL"
  // / "CANCELLED").
  kind: string;
  bodyOrReason: string;
  author: string;
  outcome: string; // "plan_revised:r3" / "cascade_cancel:2_tasks" / "recorded"
  planRevisionIndex: number; // 0 when outcome is not plan_revised
  severity: string; // "info" | "warning" | "critical" | ""
  annotationId: string; // present for user-sourced rows
  driftKind: string;   // raw lowercase drift kind for drift-sourced rows
  // harmonograf#99 / goldfive#199: opaque id of the event that triggered
  // a plan revision (or that the row _is_, for drift/annotation rows).
  // Strict dedup key.
  triggerEventId: string;
  // Agent the marker attributes to. Populated for cancel rows (the agent
  // whose invocation was cancelled); empty on annotation / drift / plan
  // rows where the attribution lives on the source record's own agentId
  // field (those consumers read through DriftRecord directly). Exposed
  // on the intervention row so the renderer can surface the agent name
  // in the compact list line without crawling back to the source store.
  targetAgentId: string;
  // For cancel rows: the id of the drift that triggered the cancel.
  // Empty when no drift backed it (user-cancel path, plan-revised path)
  // or when this row isn't a cancel.
  driftId: string;
  // For refine rows: the goldfive-minted UUID4 correlating
  // ``RefineAttempted`` with its terminal counterpart. Empty on every
  // other source. Surfaced on the row so the click-through detail
  // panel can address the underlying RefineAttemptRecord directly.
  attemptId: string;
  // For refine rows whose terminal was a failure: one of
  // 'parse_error' / 'validator_rejected' / 'llm_error' / 'other'.
  // Empty for successful and pending refines, and on every other
  // source. Together with ``severity`` lets the renderer pick the
  // failed-refine glyph variant (warning chevron) without re-deriving
  // the outcome from ``outcome``.
  failureKind: string;
  // For ``transition`` rows: the bare uppercase ``to_status`` of the
  // TaskTransitioned event (e.g. ``COMPLETED``, ``FAILED``,
  // ``CANCELLED``). Absent on every other source. Surfaced on the row
  // so the click-through detail panel and renderer can branch on the
  // transition outcome without re-parsing ``outcome``. Optional (rather
  // than required + ``''`` default) so existing test fixtures and any
  // future synthetic-row builders don't have to know about the field.
  transitionToStatus?: string;
  // For ``transition`` rows: the source attribution string that goldfive
  // stamped on the event (``llm_report`` / ``supersedes_reroute`` /
  // ``plan_revision`` / ``cancellation`` / ``other``). Absent on every
  // other source. Operators read it directly in the detail pane.
  transitionSource?: string;
  // For ``transition`` rows: the goldfive task id that transitioned
  // (after supersedes-reroute this is the SUCCESSOR id). Absent on
  // every other source.
  transitionTaskId?: string;
  // goldfive#318 (frontend follow-up to PR #318): drift condition
  // grouping. When multiple ``DriftDetected`` events share a
  // ``condition_id`` (same logical drift evolving through OPENED →
  // ESCALATING → RESOLVED), the deriver collapses them into a single
  // ``InterventionRow`` carrying these aggregate fields. The
  // ``InterventionsList`` renderer surfaces a count badge + a
  // click-to-expand affordance that reveals the individual observations.
  //
  // Empty / undefined on rows that do NOT participate in condition
  // grouping (every non-drift source, plus drift events whose
  // ``conditionId`` is empty — pre-#318 sessions, see backward-compat
  // note in ``groupDriftConditions``).
  conditionId?: string;
  // Lifecycle phase of the most recent observation in the condition
  // (lowercase: 'opened' | 'escalating' | 'resolved' |
  // 'human_intervention_required'). Empty when the condition has only
  // legacy (UNSPECIFIED) lifecycle observations. Renderer surfaces it
  // as a small chip alongside the row.
  currentLifecycle?: string;
  // Number of observations rolled into this row. ``1`` (or undefined)
  // means a single emit — no count badge, no expansion. >1 means the
  // row is collapsed and the renderer should show "(N observations)"
  // and offer a click-to-expand affordance.
  observationCount?: number;
  // Severity transitions collected across the condition's observations
  // (each ``prev_severity → severity`` step where the two differ).
  // Empty / undefined on un-grouped rows or on conditions where every
  // observation kept the same severity. Each entry's ``atMs`` is
  // session-relative, mirroring ``InterventionRow.atMs``.
  severityTransitions?: ReadonlyArray<{
    fromSeverity: string;
    toSeverity: string;
    atMs: number;
  }>;
  // Per-observation breakdown for the collapsed row. Sorted by atMs
  // ascending. Surfaced as sub-rows in the expanded view; absent /
  // undefined on un-grouped rows.
  observations?: ReadonlyArray<DriftObservation>;
  // Plan id this intervention is scoped to (Item 5 of UX cleanup batch /
  // PR #184 follow-up). When a session contains multiple plans (different
  // plan_ids), the per-plan ``InterventionsList`` filter scopes to this
  // field so a row produced under one plan doesn't render under every
  // other plan with the same revisionIndex. Empty when the deriver could
  // not pin the intervention to a specific plan (e.g. a drift that fired
  // before any plan landed) — the renderer falls back to attaching it to
  // the earliest plan in that case so the row still renders exactly
  // once.
  targetPlanId?: string;
}

// One row's worth of detail per ``DriftDetected`` event inside a grouped
// drift condition. Decoupled from ``DriftRecord`` so the renderer
// doesn't have to know about the gantt store shape; the deriver
// projects the fields it needs.
export interface DriftObservation {
  seq: number;
  atMs: number;
  severity: string;
  prevSeverity: string;
  lifecycle: string;
  detail: string;
  driftId: string;
}

// Severity→marker-size mapping used by the timeline strip. Expressed here
// so both the planning view timeline and the trajectory view chip code
// agree on the visual weight of each severity.
export const SEVERITY_WEIGHT: Record<string, number> = {
  critical: 14,
  warning: 11,
  info: 9,
  '': 9,
};

export function markerRadiusFor(row: InterventionRow): number {
  // drift / cancel / refine / transition rows scale by severity so
  // high-severity markers read as heavier on the strip (the most
  // consequential markers). Annotation + plan-only rows render at the
  // "info" weight (they don't carry a meaningful severity for the
  // sizing axis).
  if (
    row.source !== 'drift' &&
    row.source !== 'cancel' &&
    row.source !== 'refine' &&
    row.source !== 'transition'
  )
    return SEVERITY_WEIGHT.info;
  return SEVERITY_WEIGHT[row.severity] ?? SEVERITY_WEIGHT.info;
}

// Palette keyed by source — aligns with the palette note in issue #69:
//   user-blue, drift-amber, goldfive-grey, cancel-red, refine-teal,
//   transition-violet.
// Centralized so the planning and trajectory views render uniformly.
// Cancel rows use a distinct red so the stop-glyph reads at a glance as
// a terminal, operator-only marker (critical cancels intensify in the
// renderer via the severity weight, not via the palette swatch). Refine
// rows use a teal so they read as "orchestrator self-correction" without
// pulling visual weight from cancels (red) or drifts (amber). Transition
// rows use a violet so terminal task-status events read as a distinct
// "task moved" surface alongside the drift/refine lanes without
// competing with cancel red.
export const SOURCE_COLOR: Record<InterventionSource, string> = {
  user: '#5b8def',
  drift: '#f59e0b',
  goldfive: '#8d9199',
  cancel: '#e05e4a',
  refine: '#3a9b8a',
  transition: '#9b6dd6',
};

// Glyph character keyed by source — so the compact list renders a
// source-discriminating leading symbol even before the row's text kicks
// in. Cancel is the stop / cancel symbol (U+2298 CIRCLED DIVISION SLASH),
// mirroring the lane markers on the Gantt and Graph views. Refine uses
// the cycle / refresh symbol (U+21BB CLOCKWISE OPEN CIRCLE ARROW).
// Transition uses the rightwards-arrow (U+2192 RIGHTWARDS ARROW) so the
// "from→to" intent is legible at a glance without inspecting body
// text. Other sources default to a small middle dot so the column
// aligns across rows.
export const SOURCE_GLYPH: Record<InterventionSource, string> = {
  user: '·',
  drift: '·',
  goldfive: '·',
  cancel: '⊘',
  refine: '↻',
  transition: '→',
};

// ---------------------------------------------------------------------------
// Wire → row conversion.
//
// The unified intervention history is derived server-side (the
// ``ListInterventions`` RPC — see harmonograf_server/interventions.py) so
// the merge/attribution/collapse logic lives in exactly one place. This
// converter maps each proto ``Intervention`` onto the ``InterventionRow``
// the renderers consume. ``sessionStartMs`` rebases the server's absolute
// timestamps onto the session-relative ms axis the rest of the store uses
// (mirrors ``SessionStore.wallClockStartMs``).
// ---------------------------------------------------------------------------

function pbTsToMs(t: { seconds: bigint; nanos: number } | undefined): number {
  if (!t) return 0;
  return Number(t.seconds) * 1000 + Math.floor(t.nanos / 1_000_000);
}

export function interventionRowFromPb(
  pb: PbIntervention,
  sessionStartMs: number,
): InterventionRow {
  const atAbs = pbTsToMs(pb.at);
  const source = (
    KNOWN_SOURCES.has(pb.source as InterventionSource)
      ? pb.source
      : 'goldfive'
  ) as InterventionSource;
  const severityTransitions =
    pb.severityTransitions.length > 0
      ? pb.severityTransitions.map((st) => ({
          fromSeverity: st.from,
          toSeverity: st.to,
          atMs: (() => {
            const a = pbTsToMs(st.at);
            return a ? a - sessionStartMs : 0;
          })(),
        }))
      : undefined;
  const observations =
    pb.observations.length > 0
      ? pb.observations.map((ob, i) => ({
          seq: i,
          atMs: (() => {
            const a = pbTsToMs(ob.at);
            return a ? a - sessionStartMs : 0;
          })(),
          severity: ob.severity,
          prevSeverity: ob.prevSeverity,
          lifecycle: ob.lifecycle,
          detail: ob.detail,
          driftId: ob.driftId,
        }))
      : undefined;
  return {
    key: pb.key,
    atMs: atAbs ? atAbs - sessionStartMs : 0,
    source,
    kind: pb.kind,
    bodyOrReason: pb.bodyOrReason,
    author: pb.author,
    outcome: pb.outcome,
    planRevisionIndex: pb.planRevisionIndex,
    severity: pb.severity,
    annotationId: pb.annotationId,
    driftKind: pb.driftKind,
    // Server-internal merge key; the row-level merge lives on the server
    // now, so the UI never reads this. Kept for shape compatibility.
    triggerEventId: '',
    targetAgentId: pb.targetAgentId,
    driftId: pb.driftId,
    attemptId: pb.attemptId,
    failureKind: pb.failureKind,
    transitionToStatus: pb.transitionToStatus || undefined,
    transitionSource: pb.transitionSource || undefined,
    transitionTaskId: pb.transitionTaskId || undefined,
    conditionId: pb.conditionId || undefined,
    currentLifecycle: pb.lifecycle || undefined,
    observationCount: pb.count > 1 ? pb.count : undefined,
    severityTransitions,
    observations,
    targetPlanId: pb.targetPlanId || undefined,
  };
}
