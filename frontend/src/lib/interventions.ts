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
//   3. TaskRegistry     — plan revisions with a non-empty revisionKind
//                          that are NOT already covered by a matching
//                          drift in the attribution window (autonomous
//                          goldfive escalations like cascade_cancel)
//
// The derivation is intentionally tree-agnostic. No taxonomy knowledge is
// baked into the marker rendering; downstream components can show whatever
// kind/severity/outcome strings the server produced.

import type { Annotation } from '../state/annotationStore';
import type { DriftRecord, SessionStore } from '../gantt/index';
import type { TaskPlan } from '../gantt/types';

// Stable source taxonomy used by the UI. Anything else renders as "goldfive"
// grey so new kinds emitted by the server don't crash the view.
export type InterventionSource = 'user' | 'drift' | 'goldfive';

export interface InterventionRow {
  // Stable key for React lists — composed from source + (annotation id /
  // drift seq / plan id + rev index).
  key: string;
  // Session-relative ms, mirroring Span.startMs. Callers that only have
  // wall-clock should align against the session createdAt in the outer
  // component since we don't have a way to reference it from here.
  atMs: number;
  source: InterventionSource;
  // Human-readable label ("STEER" / "LOOPING_REASONING" / "CASCADE_CANCEL").
  kind: string;
  bodyOrReason: string;
  author: string;
  outcome: string; // "plan_revised:r3" / "cascade_cancel:2_tasks" / "recorded"
  planRevisionIndex: number; // 0 when outcome is not plan_revised
  severity: string; // "info" | "warning" | "critical" | ""
  annotationId: string; // present for user-sourced rows
  driftKind: string;   // raw lowercase drift kind for drift-sourced rows
}

// Drift kinds emitted by goldfive when the user pulled the trigger. Mirrors
// _USER_DRIFT_KINDS on the server side.
const USER_DRIFT_KINDS = new Set(['user_steer', 'user_cancel']);

// Revision kinds minted by goldfive's own escalation ladder (not drift
// kinds). Mirrors _GOLDFIVE_REVISION_KINDS on the server.
const GOLDFIVE_REVISION_KINDS = new Set([
  'cascade_cancel',
  'refine_retry',
  'human_intervention_required',
]);

// How far forward we look to attribute an outcome to a drift / user row.
// Matches the server's _OUTCOME_WINDOW_S (5 seconds). Expressed in ms
// because every timeline in the UI is already session-relative ms.
const OUTCOME_WINDOW_MS = 5000;

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
    });
  }

  for (const dr of input.drifts) {
    const driftKind = (dr.kind || '').toLowerCase();
    if (!driftKind) continue;
    const isUser = USER_DRIFT_KINDS.has(driftKind);
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
      annotationId: '',
      driftKind,
    });
  }

  // Plans projected as interventions only when they are (a) a revision
  // (revisionIndex > 0 and a revisionKind is set) AND (b) not already
  // covered by a drift in the window. The second condition prevents the
  // common path (drift → plan_revised within 5s) from emitting two rows
  // for one logical intervention.
  const driftList = input.drifts;
  for (const plan of input.plans) {
    const revKind = (plan.revisionKind || '').toLowerCase();
    const revIdx = plan.revisionIndex ?? 0;
    if (!revKind || revIdx <= 0) continue;
    const hasPrecedingDrift = driftList.some(
      (dr) =>
        (dr.kind || '').toLowerCase() === revKind &&
        plan.createdAtMs - dr.recordedAtMs >= 0 &&
        plan.createdAtMs - dr.recordedAtMs <= OUTCOME_WINDOW_MS,
    );
    if (hasPrecedingDrift) continue;
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
    });
  }

  rows.sort((a, b) => a.atMs - b.atMs);

  // Outcome attribution pass — fill in the outcome for drift and user
  // rows that did not already carry one.
  const revisionPlans = input.plans.filter(
    (p) => (p.revisionIndex ?? 0) > 0 && (p.revisionKind || '').length > 0,
  );
  const latestPlanAfter = (atMs: number): TaskPlan | null => {
    let latest: TaskPlan | null = null;
    for (const p of input.plans) {
      if (p.createdAtMs <= atMs) continue;
      if (latest === null || p.createdAtMs > latest.createdAtMs) latest = p;
    }
    return latest;
  };

  for (const row of rows) {
    if (row.outcome) continue;
    if (row.source === 'goldfive') continue;

    // Prefer a revision that matches this row's driftKind within the window.
    let bestPlan: TaskPlan | null = null;
    let bestDelta = Infinity;
    for (const plan of revisionPlans) {
      const delta = plan.createdAtMs - row.atMs;
      if (delta < 0 || delta > OUTCOME_WINDOW_MS) continue;
      const revKind = (plan.revisionKind || '').toLowerCase();
      if (row.driftKind && revKind === row.driftKind) {
        if (delta < bestDelta) {
          bestDelta = delta;
          bestPlan = plan;
        }
      } else if (!row.driftKind && bestPlan === null) {
        bestDelta = delta;
        bestPlan = plan;
      }
    }
    if (bestPlan !== null) {
      row.outcome = `plan_revised:r${bestPlan.revisionIndex ?? 0}`;
      row.planRevisionIndex = bestPlan.revisionIndex ?? 0;
      continue;
    }

    // No revision in the window — fall back to cascade_cancel if the
    // latest subsequent plan has cancelled tasks.
    const latestPlan = latestPlanAfter(row.atMs);
    if (latestPlan) {
      const cancelled = latestPlan.tasks.filter(
        (t) => t.status === 'CANCELLED',
      ).length;
      if (cancelled > 0) {
        row.outcome = `cascade_cancel:${cancelled}_tasks`;
        continue;
      }
    }
    row.outcome = 'recorded';
  }

  return rows;
}

// Convenience adapter: pull the three inputs from a live SessionStore +
// annotation store snapshot and run the deriver. Used by the React
// components so they don't duplicate the shape plumbing.
export function deriveInterventionsFromStore(
  store: SessionStore,
  annotations: readonly Annotation[],
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
  // Non-drift rows render at the "info" weight; only drift severities can
  // grow the marker. This keeps the strip calm when only user STEERs fire.
  if (row.source !== 'drift') return SEVERITY_WEIGHT.info;
  return SEVERITY_WEIGHT[row.severity] ?? SEVERITY_WEIGHT.info;
}

// Palette keyed by source — aligns with the palette note in issue #69:
//   user-blue, drift-amber, goldfive-grey.
// Centralized so the planning and trajectory views render uniformly.
export const SOURCE_COLOR: Record<InterventionSource, string> = {
  user: '#5b8def',
  drift: '#f59e0b',
  goldfive: '#8d9199',
};
