// Client-side intervention history derivation.
//
// Mirrors the shape of the server's ``list_interventions`` aggregator
// (harmonograf_server/interventions.py) so the frontend can recompute the
// unified chronological history from the live WatchSession stream without
// issuing a fresh ListInterventions RPC on every delta. The server RPC is
// used once on session open (fetch history before the burst completes) and
// this deriver keeps it live from there.
//
// Sources:
//   1. annotationStore  — user STEERING / HUMAN_RESPONSE rows
//   2. DriftRegistry    — goldfive drift_detected events (user_steer /
//                          user_cancel kinds flag source=user)
//   3. TaskRegistry     — plan revisions (revisionIndex > 0)
//
// Dedup contract (harmonograf#99 / goldfive#199):
//
//   * Tier 1 — always on. Plan-revision rows merge onto their source
//     annotation or drift row when ``triggerEventId`` matches the
//     annotation id or drift id.
//   * Tier 2 — opt-in via the ``legacyPlanAttributionWindowMs`` option
//     on :func:`deriveInterventions` (default 0 / disabled). Time-window
//     fallback: a plan row whose ``triggerEventId`` matched nothing
//     strictly merges onto a preceding user-control row of the same
//     ``driftKind`` within the configured window. Console WARNING on
//     match so operators can diagnose mis-attribution.
//
// The derivation is intentionally tree-agnostic. No taxonomy knowledge is
// baked into the marker rendering; downstream components can show whatever
// kind/severity/outcome strings the server produced.

import type { Annotation } from '../state/annotationStore';
import type {
  DriftRecord,
  InvocationCancelRecord,
  RefineAttemptRecord,
  RefineFailureRecord,
  SessionStore,
  TaskTransitionRecord,
  UserMessageRecord,
} from '../gantt/index';
import type { TaskPlan } from '../gantt/types';

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
// to_status + meaningful source); see :func:`deriveInterventions` for
// the filter ladder. Distinct from ``cancel`` (which fires on
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

// Drift kinds emitted by goldfive when the user pulled the trigger. Mirrors
// _USER_DRIFT_KINDS on the server side.
const USER_DRIFT_KINDS = new Set(['user_steer', 'user_cancel']);

// TaskTransitioned filter ladder (goldfive#267).
//
// Only terminal ``to_status`` values surface as intervention rows.
// RUNNING transitions are too granular for the operator-facing list —
// the gantt + task panel already convey the running state. Statuses
// the renderer doesn't recognize fall through to "skip" so unknown
// future statuses don't crash the view.
const TASK_TRANSITION_TERMINAL_STATUSES = new Set([
  'COMPLETED',
  'FAILED',
  'CANCELLED',
]);

// Source-attribution values the operator cares about. Anything else
// (``handler_default``, ``other``, future unknowns) is suppressed —
// those are framework / adapter-driven transitions that flood the
// wire without representing an operator-meaningful intervention.
const TASK_TRANSITION_MEANINGFUL_SOURCES = new Set([
  'llm_report',
  'supersedes_reroute',
  'plan_revision',
  'cancellation',
]);

// Revision kinds minted by goldfive's own escalation ladder (not drift
// kinds). Mirrors _GOLDFIVE_REVISION_KINDS on the server.
const GOLDFIVE_REVISION_KINDS = new Set([
  'cascade_cancel',
  'refine_retry',
  'human_intervention_required',
]);

// Pretty labels for drift kinds emitted from the user. Uppercase so the
// renderer can splash them next to other kinds without special-casing.
function normalizeDriftKind(raw: string): string {
  const k = (raw || '').toLowerCase();
  if (k === 'user_steer') return 'STEER';
  if (k === 'user_cancel') return 'CANCEL';
  return k.toUpperCase();
}

function annotationKindLabel(kind: Annotation['kind']): string | null {
  if (kind === 'STEERING') return 'STEER';
  if (kind === 'HUMAN_RESPONSE') return 'HUMAN_RESPONSE';
  return null;
}

// Inputs the deriver needs. Passed as an object so tests can stub the
// subset they care about.
export interface DeriveInput {
  annotations: readonly Annotation[];
  drifts: readonly DriftRecord[];
  plans: readonly TaskPlan[]; // every plan rev ever seen, chronological
  // goldfive#251 Stream C: invocation-cancellation markers. Distinct
  // from drifts (a cancel is the *consequence* of a drift, not a drift
  // itself). Optional so callers that don't have a SessionStore handy
  // can still derive over the existing three sources.
  cancels?: readonly InvocationCancelRecord[];
  // goldfive#264: refine-attempt lifecycle records. ``refineAttempts``
  // is the start side; ``refineFailures`` carries the failed-terminal
  // counterparts (correlated via ``attemptId``). Successful terminals
  // are inferred from ``plans`` matching by ``drift_id``. Both arrays
  // are optional so callers without a SessionStore can still derive
  // the historical four sources.
  refineAttempts?: readonly RefineAttemptRecord[];
  refineFailures?: readonly RefineFailureRecord[];
  // goldfive#267 / #251 R4: every plan-state transition. The deriver
  // filters by ``to_status`` and ``source`` (see
  // ``TASK_TRANSITION_TERMINAL_STATUSES`` and
  // ``TASK_TRANSITION_MEANINGFUL_SOURCES``) before surfacing as a
  // row. Optional so callers without a SessionStore stay backward-
  // compatible.
  transitions?: readonly TaskTransitionRecord[];
  // harmonograf user-message UX gap: verbatim user-authored messages
  // observed via ADK's ``on_user_message_callback``. Distinct from
  // ``annotations`` (frontend-authored side-channel notes) and from
  // ``drifts`` of kind=user_steer (goldfive's interpretation of the
  // operator signal). Surfaced as ``source: 'user'`` rows so they
  // sit alongside annotation steers in the unified intervention
  // list. Optional so callers without a SessionStore stay backward-
  // compatible.
  userMessages?: readonly UserMessageRecord[];
  // Opt-in Tier-2 legacy time-window fallback for plan-revision
  // attribution (pre-#99 behaviour). Default 0 / undefined disables.
  // Provided by the caller (app runtime context) — never read from
  // ``import.meta.env`` — so the aggregator stays testable and doesn't
  // carry build-time config knowledge.
  legacyPlanAttributionWindowMs?: number;
}

export function deriveInterventions(input: DeriveInput): InterventionRow[] {
  const rows: InterventionRow[] = [];

  for (const ann of input.annotations) {
    const label = annotationKindLabel(ann.kind);
    if (!label) continue;
    rows.push({
      key: `ann:${ann.id}`,
      atMs: ann.createdAtMs,
      source: 'user',
      kind: label,
      bodyOrReason: ann.body,
      author: ann.author || 'user',
      outcome: '',
      planRevisionIndex: 0,
      severity: '',
      annotationId: ann.id,
      driftKind: '',
      // harmonograf#99: the annotation's id IS the strict join key for
      // a downstream PlanRevised refine.
      triggerEventId: ann.id,
      targetAgentId: '',
      driftId: '',
      attemptId: '',
      failureKind: '',
    });
  }

  for (const dr of input.drifts) {
    const driftKind = (dr.kind || '').toLowerCase();
    if (!driftKind) continue;
    const isUser = USER_DRIFT_KINDS.has(driftKind);
    // harmonograf#99: for user-control drifts use annotationId (so we
    // merge onto the annotation); for autonomous drifts use driftId.
    const trig = (isUser && dr.annotationId) ? dr.annotationId : (dr.driftId || '');
    rows.push({
      key: `drift:${dr.seq}`,
      atMs: dr.recordedAtMs,
      source: isUser ? 'user' : 'drift',
      kind: normalizeDriftKind(driftKind),
      bodyOrReason: dr.detail || '',
      author: '',
      outcome: '',
      planRevisionIndex: 0,
      severity: dr.severity || '',
      annotationId: dr.annotationId || '',
      driftKind,
      triggerEventId: trig,
      targetAgentId: dr.agentId || '',
      driftId: dr.driftId || '',
      attemptId: '',
      failureKind: '',
    });
  }

  for (const plan of input.plans) {
    const revKind = (plan.revisionKind || '').toLowerCase();
    const revIdx = plan.revisionIndex ?? 0;
    if (!revKind || revIdx <= 0) continue;
    let source: InterventionSource;
    if (GOLDFIVE_REVISION_KINDS.has(revKind)) source = 'goldfive';
    else if (USER_DRIFT_KINDS.has(revKind)) source = 'user';
    else source = 'drift';
    const kind =
      source === 'user' ? normalizeDriftKind(revKind) : revKind.toUpperCase();
    rows.push({
      key: `plan:${plan.id}:r${revIdx}`,
      atMs: plan.createdAtMs,
      source,
      kind,
      bodyOrReason: plan.revisionReason || '',
      author: '',
      outcome: `plan_revised:r${revIdx}`,
      planRevisionIndex: revIdx,
      severity: plan.revisionSeverity || '',
      annotationId: '',
      driftKind: source === 'goldfive' ? '' : revKind,
      triggerEventId: plan.triggerEventId || '',
      targetAgentId: '',
      driftId: '',
      attemptId: '',
      failureKind: '',
      // Plan-rev rows are intrinsically scoped to their own plan.
      targetPlanId: plan.id,
    });
  }

  // InvocationCancelled — operator-only record. Sourced separately from
  // drifts because a cancel is the consequence of a drift, not a drift
  // itself; both rows coexist (the drift explains WHY and the cancel
  // records WHAT happened). Body format is prompt-injection-safe
  // directive copy: "{agent_name} cancelled ({reason})" rather than
  // "{agent_name} failed to …".
  for (const cr of input.cancels ?? []) {
    const reason = (cr.reason || '').toLowerCase();
    const driftKind = (cr.driftKind || '').toLowerCase();
    // Default body is the human-readable ``detail`` the goldfive side
    // stamped; fall back to a directive one-liner when the emitter didn't
    // populate detail (shouldn't happen in practice but the UI must
    // render something).
    const body =
      cr.detail ||
      (reason && driftKind
        ? `cancelled (${reason} → ${driftKind})`
        : reason
          ? `cancelled (${reason})`
          : 'cancelled');
    rows.push({
      key: `cancel:${cr.seq}`,
      atMs: cr.recordedAtMs,
      source: 'cancel',
      kind: 'CANCELLED',
      bodyOrReason: body,
      author: '',
      outcome: 'recorded',
      planRevisionIndex: 0,
      severity: cr.severity || '',
      annotationId: '',
      driftKind,
      // triggerEventId is deliberately empty on cancel rows — the
      // cancel marker is meant to render *alongside* its triggering
      // drift, not merge into it. ``driftId`` still carries the
      // backlink so the renderer can hover/click through to the
      // drift's detail drawer.
      triggerEventId: '',
      targetAgentId: cr.agentId || '',
      driftId: cr.driftId || '',
      attemptId: '',
      failureKind: '',
    });
  }

  // RefineAttempted + (PlanRevised | RefineFailed) — goldfive#264.
  // Merged into a single row per attempt: a successful attempt renders
  // as ``REFINE`` with ``outcome=plan_revised:rN``; a failed attempt
  // renders as ``REFINE_FAILED`` with the failure_kind in ``kind`` and
  // the warning severity. A pending attempt (attempted observed but no
  // terminal yet — rare since refine is sync or near-sync per
  // goldfive's _handle_drift) renders as ``REFINE`` with
  // ``outcome=pending``.
  //
  // Correlation strategy:
  //   * Strict: attempted.attemptId === failed.attemptId for failures.
  //   * For successes, plan_revised carries triggerEventId == drift_id;
  //     the attempted record carries the same drift_id, so we look up
  //     the success outcome via plan rows whose triggerEventId matches
  //     the attempted's driftId. Goldfive emits at most one terminal
  //     per drift's _handle_drift cycle, so this 1:1 match is safe.
  //
  // Side effect on the original plan / drift rows: when a refine
  // succeeds, the existing per-drift + per-plan rows STILL render —
  // they capture the WHY (drift) and the WHAT-CHANGED (plan revision).
  // The new merged refine row sits alongside them, capturing the
  // ATTEMPT itself with its outcome rolled in. Operators see all three
  // (drift → refine → plan-rev) but the refine row is the one that
  // says "goldfive tried to fix this and {succeeded|failed}".
  if ((input.refineAttempts && input.refineAttempts.length) || false) {
    const attempts = input.refineAttempts ?? [];
    const failures = input.refineFailures ?? [];
    const failuresByAttempt = new Map<string, RefineFailureRecord>();
    for (const f of failures) {
      if (!f.attemptId) continue;
      // First-write wins; goldfive only fires one failure per attempt.
      if (!failuresByAttempt.has(f.attemptId)) {
        failuresByAttempt.set(f.attemptId, f);
      }
    }
    const planByDrift = new Map<string, TaskPlan>();
    for (const p of input.plans) {
      if ((p.revisionIndex ?? 0) <= 0) continue;
      if (!p.triggerEventId) continue;
      // First-write wins on duplicate triggerEventId (would only happen
      // on data corruption — a single drift driving two refines).
      if (!planByDrift.has(p.triggerEventId)) {
        planByDrift.set(p.triggerEventId, p);
      }
    }
    for (const att of attempts) {
      const triggerKind = (att.triggerKind || '').toLowerCase();
      const triggerSeverity = (att.triggerSeverity || '').toLowerCase();
      const failed = att.attemptId
        ? failuresByAttempt.get(att.attemptId)
        : undefined;
      const succeeded = att.driftId
        ? planByDrift.get(att.driftId)
        : undefined;
      let kind: string;
      let outcome: string;
      let severity: string;
      let body: string;
      let planRev = 0;
      let failureKind = '';
      if (failed) {
        const fkRaw = (failed.failureKind || '').toLowerCase();
        kind = `REFINE_FAILED${fkRaw ? `:${fkRaw.toUpperCase()}` : ''}`;
        outcome = `refine_failed${fkRaw ? `:${fkRaw}` : ''}`;
        // Failures escalate to warning by default — operator attention
        // needed. Honor the explicit trigger severity when it's higher
        // than warning (e.g. critical drift that produced a parse
        // error stays critical).
        severity =
          triggerSeverity === 'critical' ? 'critical' : 'warning';
        body =
          failed.detail ||
          failed.reason ||
          (fkRaw
            ? `refine failed (${fkRaw})`
            : 'refine failed');
        failureKind = fkRaw;
      } else if (succeeded) {
        const idx = succeeded.revisionIndex ?? 0;
        kind = `REFINE${triggerKind ? `:${triggerKind.toUpperCase()}` : ''}`;
        outcome = `plan_revised:r${idx}`;
        // Success is informational — no operator action needed. Inherit
        // the trigger severity for chrome (a critical drift that was
        // successfully refined still shows the critical chevron).
        severity = triggerSeverity || 'info';
        body = succeeded.revisionReason ||
          (triggerKind
            ? `refine succeeded (${triggerKind})`
            : 'refine succeeded');
        planRev = idx;
      } else {
        // Pending — attempted observed but no terminal yet. Render with
        // a 'pending' outcome marker so the renderer can show a spinner
        // / pending-state indicator. See goldfive#264 for the (rare)
        // ordering this triggers.
        kind = `REFINE${triggerKind ? `:${triggerKind.toUpperCase()}` : ''}`;
        outcome = 'pending';
        severity = triggerSeverity || 'info';
        body = triggerKind
          ? `refine pending (${triggerKind})`
          : 'refine pending';
      }
      rows.push({
        key: `refine:${att.attemptId || `seq${att.seq}`}`,
        atMs: att.recordedAtMs,
        source: 'refine',
        kind,
        bodyOrReason: body,
        author: '',
        outcome,
        planRevisionIndex: planRev,
        severity,
        annotationId: '',
        driftKind: triggerKind,
        // Refine rows carry the source drift_id as triggerEventId so
        // they can backlink in the click-through detail panel; we
        // intentionally do NOT participate in the strict-id merge
        // groups (the merged refine row is itself the consolidation —
        // collapsing further would lose information). The merge
        // function below special-cases ``source === 'refine'`` to skip
        // the merge group.
        triggerEventId: att.driftId || '',
        targetAgentId: att.agentId || '',
        driftId: att.driftId || '',
        attemptId: att.attemptId || '',
        failureKind,
      });
    }
  }

  // TaskTransitioned — goldfive#267 / #251 R4. Every plan-state
  // transition emits one with source attribution; the deriver below
  // filters to the user-meaningful subset.
  //
  //   * Skip when ``to_status`` is not terminal (RUNNING transitions
  //     are too granular for the intervention list — Gantt + task
  //     panel already surface the running state).
  //   * Skip when ``source`` is ``handler_default`` /
  //     ``executor_dispatch`` / ``other`` / unknown — these are the
  //     framework / adapter-driven transitions that flood the wire
  //     and don't represent an operator-meaningful intervention.
  //   * Surface only ``llm_report`` / ``supersedes_reroute`` /
  //     ``plan_revision`` / ``cancellation`` sources as rows.
  //
  // Severity ladder mirrors the cancel / refine pattern:
  //   * COMPLETED     ⇒ info     (success outcome, informational)
  //   * FAILED        ⇒ warning  (operator attention)
  //   * CANCELLED     ⇒ warning  (operator attention)
  //
  // The intervention row carries the source attribution as a separate
  // field (``transitionSource``) so the renderer / detail panel can
  // surface the "why" alongside the "what" without re-parsing kind.
  // User messages — verbatim operator turns observed via ADK's
  // ``on_user_message_callback`` (harmonograf user-message UX gap).
  // Source-tagged ``user`` so they sit alongside annotation steers in
  // the unified intervention list. Distinct from a drift(user_steer)
  // row: this carries the RAW input, before goldfive's drift
  // interpretation. Both rows can coexist for the same turn — the
  // user_message row says WHAT the operator typed; the
  // drift(user_steer) row says how goldfive responded.
  for (const um of input.userMessages ?? []) {
    rows.push({
      key: `usermsg:${um.seq}`,
      atMs: um.recordedAtMs,
      source: 'user',
      kind: um.midTurn ? 'USER_MESSAGE_INTERJECTION' : 'USER_MESSAGE',
      bodyOrReason: um.content,
      author: um.author || 'user',
      outcome: '',
      planRevisionIndex: 0,
      severity: '',
      annotationId: '',
      driftKind: '',
      triggerEventId: '',
      targetAgentId: '',
      driftId: '',
      attemptId: '',
      failureKind: '',
    });
  }

  for (const tr of input.transitions ?? []) {
    const to = (tr.toStatus || '').toUpperCase();
    const src = (tr.source || '').toLowerCase();
    if (!TASK_TRANSITION_TERMINAL_STATUSES.has(to)) continue;
    if (!TASK_TRANSITION_MEANINGFUL_SOURCES.has(src)) continue;
    let severity: string;
    if (to === 'COMPLETED') severity = 'info';
    else severity = 'warning'; // FAILED / CANCELLED
    const taskLabel = tr.taskId || '?';
    const kind = `TASK_${to}`;
    const body = `Task ${taskLabel} ${to} via ${src}`;
    rows.push({
      key: `transition:${tr.seq}`,
      atMs: tr.recordedAtMs,
      source: 'transition',
      kind,
      bodyOrReason: body,
      author: '',
      // Carry the from→to + source as the outcome string so existing
      // chrome that reads ``outcome`` (the compact list line, the
      // outcome formatter) renders something meaningful without a
      // schema bump. Detail panel reads the dedicated transition*
      // fields directly.
      outcome: `transition:${(tr.fromStatus || '?').toLowerCase()}->${to.toLowerCase()}`,
      planRevisionIndex: tr.revisionStamp || 0,
      severity,
      annotationId: '',
      driftKind: '',
      // Transitions don't participate in the trigger_event_id strict-id
      // merge groups (they're a parallel observability stream — see
      // ``mergeByTriggerEventId``).
      triggerEventId: '',
      targetAgentId: tr.agentName || '',
      driftId: '',
      attemptId: '',
      failureKind: '',
      transitionToStatus: to,
      transitionSource: src,
      transitionTaskId: tr.taskId || '',
    });
  }

  rows.sort((a, b) => a.atMs - b.atMs);

  // Outcome attribution (pre-merge). Strict-id only by default; the
  // caller may opt in to the legacy time-window by passing
  // ``legacyPlanAttributionWindowMs``.
  const rawWindow = input.legacyPlanAttributionWindowMs ?? 0;
  const windowMs = Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : 0;
  attributeOutcomes(rows, input.plans, { windowMs });

  // Tier 1 merge — strict trigger_event_id.
  let merged = mergeByTriggerEventId(rows);

  // Tier 2 merge — opt-in legacy time-window, only for plan rows with
  // no triggerEventId (pre-#99 data or bridge misconfigured).
  if (windowMs > 0) {
    merged = legacyTimeWindowMerge(merged, windowMs);
  }

  return merged;
}

function attributeOutcomes(
  rows: InterventionRow[],
  plans: readonly TaskPlan[],
  opts: { windowMs: number },
): void {
  const planByTrigger = new Map<string, TaskPlan>();
  for (const p of plans) {
    if ((p.revisionIndex ?? 0) <= 0) continue;
    if (!p.triggerEventId) continue;
    if (!planByTrigger.has(p.triggerEventId)) {
      planByTrigger.set(p.triggerEventId, p);
    }
  }

  for (const row of rows) {
    if (row.source === 'goldfive') continue;
    if (row.outcome) continue;

    // Tier 1 — strict-id.
    if (row.triggerEventId) {
      const matched = planByTrigger.get(row.triggerEventId);
      if (matched) {
        const idx = matched.revisionIndex ?? 0;
        row.outcome = `plan_revised:r${idx}`;
        row.planRevisionIndex = idx;
        // Carry the matched plan's id so the per-plan filter in
        // GanttView doesn't fall through to "every plan with same
        // revisionIndex" — fixes the duplicate-rendering bug under
        // multi-plan sessions (Item 5 / PR #184 follow-up).
        if (!row.targetPlanId) row.targetPlanId = matched.id;
        continue;
      }
    }

    // Tier 2 — opt-in legacy time-window.
    if (opts.windowMs > 0) {
      const fallback = legacyFindRevision(row, plans, opts.windowMs);
      if (fallback) {
        console.warn(
          `[interventions] legacy time-window fallback matched ` +
            `driftKind=${row.driftKind} triggerEventId=${JSON.stringify(
              row.triggerEventId,
            )} -> plan rev=${fallback.revisionIndex} ` +
            `(triggerEventId=${JSON.stringify(fallback.triggerEventId ?? '')}). ` +
            `legacyPlanAttributionWindowMs=${opts.windowMs}. ` +
            `Investigate why strict-id did not match (pre-#99 data? goldfive < #199?).`,
        );
        const idx = fallback.revisionIndex ?? 0;
        row.outcome = `plan_revised:r${idx}`;
        row.planRevisionIndex = idx;
        if (!row.targetPlanId) row.targetPlanId = fallback.id;
        continue;
      }
    }

    // Fallback: cascade_cancel if a later plan has cancelled tasks.
    const latest = latestPlanAfter(row.atMs, plans);
    if (latest) {
      const cancelled = latest.tasks.filter((t) => t.status === 'CANCELLED').length;
      if (cancelled > 0) {
        row.outcome = `cascade_cancel:${cancelled}_tasks`;
        continue;
      }
    }
    row.outcome = 'recorded';
  }

  // Final pass: ensure every row has a ``targetPlanId`` so the per-plan
  // filter in GanttView can attach it exactly once. Rows that didn't
  // match a plan revision via strict-id or legacy fallback still need a
  // home; we attach them to the most recent plan whose ``createdAtMs``
  // is at-or-before the row's ``atMs`` (i.e. the plan that was active
  // when the intervention fired). No active plan ⇒ first plan in the
  // session (the row was an early annotation / drift before any plan
  // landed). No plans at all ⇒ leave empty; the GanttView filter has
  // nothing to attach to anyway.
  if (plans.length > 0) {
    const sortedPlans = [...plans].sort((a, b) => a.createdAtMs - b.createdAtMs);
    for (const row of rows) {
      if (row.targetPlanId) continue;
      let active: TaskPlan | null = null;
      for (const p of sortedPlans) {
        if (p.createdAtMs <= row.atMs) active = p;
        else break;
      }
      row.targetPlanId = (active ?? sortedPlans[0]).id;
    }
  }
}

function legacyFindRevision(
  row: InterventionRow,
  plans: readonly TaskPlan[],
  windowMs: number,
): TaskPlan | null {
  let best: TaskPlan | null = null;
  let bestDelta = windowMs + 1;
  for (const plan of plans) {
    const delta = plan.createdAtMs - row.atMs;
    if (delta < 0 || delta > windowMs) continue;
    const revKind = (plan.revisionKind || '').toLowerCase();
    if (!revKind || (plan.revisionIndex ?? 0) <= 0) continue;
    if (row.driftKind && revKind === row.driftKind) {
      if (delta < bestDelta) {
        bestDelta = delta;
        best = plan;
      }
    } else if (!row.driftKind && best === null) {
      bestDelta = delta;
      best = plan;
    }
  }
  return best;
}

function latestPlanAfter(
  atMs: number,
  plans: readonly TaskPlan[],
): TaskPlan | null {
  let latest: TaskPlan | null = null;
  for (const p of plans) {
    if (p.createdAtMs <= atMs) continue;
    if (latest === null || p.createdAtMs > latest.createdAtMs) latest = p;
  }
  return latest;
}

// harmonograf#99 strict-id merge: collapse rows sharing a triggerEventId.
// Survivor priority: annotation row → drift row → earliest. Rows with
// empty triggerEventId pass through unchanged.
function mergeByTriggerEventId(rows: InterventionRow[]): InterventionRow[] {
  const groups = new Map<string, InterventionRow[]>();
  const passthrough: InterventionRow[] = [];
  for (const row of rows) {
    // Refine rows are pre-merged by the deriver (one row per attempt
    // with the outcome rolled in via attemptId correlation). Skipping
    // them here keeps the merged row from collapsing into its source
    // drift / plan revision, which would discard the attempt-specific
    // failure_kind + bodyOrReason styling. See goldfive#264 / Option A.
    //
    // Transition rows are similarly self-contained — they carry no
    // ``triggerEventId`` (the merger short-circuits empty-string
    // entries below anyway, but we route them through ``passthrough``
    // explicitly so the intent is recorded near the refine guard).
    if (row.source === 'refine' || row.source === 'transition') {
      passthrough.push(row);
      continue;
    }
    if (row.triggerEventId) {
      const g = groups.get(row.triggerEventId);
      if (g) g.push(row);
      else groups.set(row.triggerEventId, [row]);
    } else {
      passthrough.push(row);
    }
  }
  if (groups.size === 0) return rows;

  const merged: InterventionRow[] = [...passthrough];
  for (const group of groups.values()) {
    group.sort((a, b) => a.atMs - b.atMs);
    // Prefer annotation row (source=user, no driftKind).
    let survivor: InterventionRow | undefined = group.find(
      (r) => r.source === 'user' && !r.driftKind,
    );
    if (!survivor) {
      // Otherwise prefer drift row (has driftKind set, no planRevisionIndex).
      survivor = group.find((r) => r.driftKind && !r.planRevisionIndex);
    }
    if (!survivor) {
      survivor = group[0];
    }
    for (const other of group) {
      if (other === survivor) continue;
      if (other.outcome && other.outcome !== 'recorded') {
        if (!survivor.outcome || survivor.outcome === 'recorded') {
          survivor.outcome = other.outcome;
        }
      } else if (other.outcome && !survivor.outcome) {
        survivor.outcome = other.outcome;
      }
      if (other.planRevisionIndex && !survivor.planRevisionIndex) {
        survivor.planRevisionIndex = other.planRevisionIndex;
      }
      if (other.severity && !survivor.severity) survivor.severity = other.severity;
      if (other.driftKind && !survivor.driftKind) survivor.driftKind = other.driftKind;
      if (other.annotationId && !survivor.annotationId) {
        survivor.annotationId = other.annotationId;
      }
    }
    merged.push(survivor);
  }

  merged.sort((a, b) => a.atMs - b.atMs);
  return merged;
}

// harmonograf#99 opt-in fallback: merge orphan plan rows (no
// triggerEventId) onto preceding user/drift rows by time window.
function legacyTimeWindowMerge(
  rows: InterventionRow[],
  windowMs: number,
): InterventionRow[] {
  const survivors: InterventionRow[] = [];
  for (const row of rows) {
    const orphanPlan =
      !row.triggerEventId &&
      row.driftKind &&
      row.outcome.startsWith('plan_revised:');
    if (!orphanPlan) {
      survivors.push(row);
      continue;
    }
    // Find most recent preceding user/drift row of matching kind.
    let target: InterventionRow | null = null;
    for (let i = survivors.length - 1; i >= 0; i--) {
      const prior = survivors[i];
      if (prior.source !== 'user' && prior.source !== 'drift') continue;
      if (prior.driftKind !== row.driftKind) continue;
      const delta = row.atMs - prior.atMs;
      if (delta >= 0 && delta <= windowMs) {
        target = prior;
        break;
      }
      if (delta > windowMs) break;
    }
    if (target) {
      console.warn(
        `[interventions] legacy time-window fallback merged plan rev=` +
          `${row.planRevisionIndex} (driftKind=${row.driftKind}, no ` +
          `triggerEventId) onto ${target.source} row. ` +
          `legacyPlanAttributionWindowMs=${windowMs}. ` +
          `Investigate why strict-id did not match (pre-#99 data?).`,
      );
      if (!target.outcome || target.outcome === 'recorded') {
        target.outcome = row.outcome;
      }
      if (!target.planRevisionIndex) target.planRevisionIndex = row.planRevisionIndex;
      if (!target.severity && row.severity) target.severity = row.severity;
      continue;
    }
    survivors.push(row);
  }
  return survivors;
}

// Convenience adapter: pull the three inputs from a live SessionStore +
// annotation store snapshot and run the deriver. Used by the React
// components so they don't duplicate the shape plumbing.
//
// ``opts.legacyPlanAttributionWindowMs`` is forwarded to the underlying
// :func:`deriveInterventions` call when provided. Callers that want the
// Tier-2 fallback wire it in from their runtime config context (there
// is no build-time env var — that was the previous design).
export function deriveInterventionsFromStore(
  store: SessionStore,
  annotations: readonly Annotation[],
  opts: { legacyPlanAttributionWindowMs?: number } = {},
): InterventionRow[] {
  const plans = store.tasks.listPlans();
  const allRevs: TaskPlan[] = [];
  const seen = new Set<TaskPlan>();
  for (const live of plans) {
    for (const snap of store.tasks.allRevsForPlan(live.id)) {
      if (seen.has(snap)) continue;
      seen.add(snap);
      allRevs.push(snap);
    }
  }
  allRevs.sort((a, b) => a.createdAtMs - b.createdAtMs);
  return deriveInterventions({
    annotations,
    drifts: store.drifts.list(),
    plans: allRevs,
    cancels: store.invocationCancels.list(),
    refineAttempts: store.refineAttempts.list(),
    refineFailures: store.refineFailures.list(),
    transitions: store.taskTransitions.list(),
    userMessages: store.userMessages.list(),
    legacyPlanAttributionWindowMs: opts.legacyPlanAttributionWindowMs,
  });
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
