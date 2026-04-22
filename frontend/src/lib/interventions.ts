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

// Extended window for user-control drifts (user_steer / user_cancel).
// Mirrors the server's _USER_OUTCOME_WINDOW_S. A user STEER routes
// through the planner's LLM which can take tens of seconds on a long
// prompt (issue #86 saw a 70s drift→plan gap on a local Qwen3.5-35B).
// The 5s default stranded the drift row and leaked a second card;
// the extended window is still bounded so two separate user STEERs
// in a session aren't claim-stolen by each other's plan revisions.
//
// harmonograf#95 bumped this from 300s → 900s after observing a
// 13m51s drift→plan gap on kikuchi/Qwen3.5-35B. The primary fix is
// the strict-id dedup via the PlanRevised annotation_id stamp
// (goldfive#196); the wider window is a belt-and-suspenders fallback
// for pre-#196 producers and edge cases where the stamp fails to
// propagate.
const USER_OUTCOME_WINDOW_MS = 900_000;

function outcomeWindowFor(driftKind: string): number {
  return USER_DRIFT_KINDS.has((driftKind || '').toLowerCase())
    ? USER_OUTCOME_WINDOW_MS
    : OUTCOME_WINDOW_MS;
}

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
      // Thread annotation_id through (goldfive#176). Present on
      // user-control drifts; empty on autonomous ones. The post-sort
      // dedup pass uses this to fold the drift into the annotation row.
      annotationId: dr.annotationId || '',
      driftKind,
    });
  }

  // Plans projected as interventions only when they are (a) a revision
  // (revisionIndex > 0 and a revisionKind is set) AND (b) not already
  // covered by a drift in the window. The second condition prevents the
  // common path (drift → plan_revised) from emitting two rows for one
  // logical intervention. The window is kind-dependent: user-control
  // kinds use the extended USER_OUTCOME_WINDOW_MS since the refine LLM
  // may take tens of seconds (issue #86), while autonomous kinds keep
  // the tight default so unrelated revisions aren't claim-stolen.
  const driftList = input.drifts;
  for (const plan of input.plans) {
    const revKind = (plan.revisionKind || '').toLowerCase();
    const revIdx = plan.revisionIndex ?? 0;
    if (!revKind || revIdx <= 0) continue;
    const window = outcomeWindowFor(revKind);
    const hasPrecedingDrift = driftList.some(
      (dr) =>
        (dr.kind || '').toLowerCase() === revKind &&
        plan.createdAtMs - dr.recordedAtMs >= 0 &&
        plan.createdAtMs - dr.recordedAtMs <= window,
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
      // goldfive#196 / harmonograf#95: carry the source annotation id
      // stamped on the plan so the final dedup pass can strict-join
      // this row against the source annotation — no more time-window
      // fallback for slow refines.
      annotationId: plan.revisionAnnotationId || '',
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

    // Prefer a revision that matches this row's driftKind within the
    // kind-dependent window. User-control drifts (issue #86) use the
    // extended USER_OUTCOME_WINDOW_MS to tolerate long refine latencies.
    const window = outcomeWindowFor(row.driftKind);
    let bestPlan: TaskPlan | null = null;
    let bestDelta = Infinity;
    for (const plan of revisionPlans) {
      const delta = plan.createdAtMs - row.atMs;
      if (delta < 0 || delta > window) continue;
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

  return mergeByAnnotationId(rows);
}

// harmonograf#75: collapse rows that share an annotation_id into a single
// card. Rule: annotation row wins as the survivor; drift + plan rows
// with the same annotation_id merge their outcome / severity /
// planRevisionIndex / driftKind onto it. Rows with no annotation_id
// (autonomous drifts, goldfive-autonomous plan revisions) pass through
// unchanged — they keep their own cards per the user directive
// ("steering due to drift IS a steering").
//
// A final post-pass folds plan-sourced rows whose driftKind matches a
// merged annotation's driftKind AND whose atMs lands inside the 5s
// attribution window — catches the case where _project_plans had to
// emit a plan row because its matching drift was suppressed by the
// dedup step but the plan itself has no annotation_id to join on.
function mergeByAnnotationId(rows: InterventionRow[]): InterventionRow[] {
  const groups = new Map<string, InterventionRow[]>();
  const passthrough: InterventionRow[] = [];
  for (const row of rows) {
    if (row.annotationId) {
      const g = groups.get(row.annotationId);
      if (g) g.push(row);
      else groups.set(row.annotationId, [row]);
    } else {
      passthrough.push(row);
    }
  }
  if (groups.size === 0) return rows;

  const merged: InterventionRow[] = [...passthrough];
  for (const group of groups.values()) {
    // Prefer the annotation row as the survivor; it carries user text +
    // author + wall-clock-correct timestamp. Annotation rows are the only
    // ones with source="user" AND no driftKind (drift rows set driftKind).
    let survivor: InterventionRow | undefined;
    const others: InterventionRow[] = [];
    for (const row of group) {
      if (!row.driftKind && row.source === 'user') survivor = row;
      else others.push(row);
    }
    if (!survivor) {
      group.sort((a, b) => a.atMs - b.atMs);
      survivor = group[0];
      others.splice(0, others.length, ...group.slice(1));
    }
    for (const other of others) {
      // Prefer the other row's outcome when the survivor either has no
      // outcome yet OR has only the fallback 'recorded' label — the
      // drift path is usually the one that attributed a real
      // ``plan_revised:rN`` (especially for slow refines beyond the
      // default attribution window; issue #86). Never downgrade a real
      // outcome back to 'recorded'.
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
    }
    merged.push(survivor);
  }

  // Fold in plan-sourced rows (no annotation_id, driftKind = user_*,
  // outcome starts with plan_revised:) whose driftKind matches a merged
  // annotation row inside the kind-dependent attribution window. The
  // extended user window catches slow refines (issue #86).
  const findMergeTarget = (planRow: InterventionRow): InterventionRow | null => {
    const window = outcomeWindowFor(planRow.driftKind);
    for (const row of merged) {
      if (!row.annotationId || !row.driftKind) continue;
      if (row.driftKind !== planRow.driftKind) continue;
      const delta = planRow.atMs - row.atMs;
      if (delta >= 0 && delta <= window) return row;
    }
    return null;
  };
  const survivors: InterventionRow[] = [];
  for (const row of merged) {
    if (
      !row.annotationId &&
      USER_DRIFT_KINDS.has(row.driftKind) &&
      row.outcome.startsWith('plan_revised:')
    ) {
      const target = findMergeTarget(row);
      if (target) {
        // Prefer the plan's real outcome (plan_revised:rN) over a
        // fallback 'recorded' that the annotation row may have picked
        // up during attribution when the drift was stranded outside
        // the default window (issue #86).
        if (!target.outcome || target.outcome === 'recorded') {
          target.outcome = row.outcome;
        }
        if (!target.planRevisionIndex) target.planRevisionIndex = row.planRevisionIndex;
        continue;
      }
    }
    survivors.push(row);
  }
  survivors.sort((a, b) => a.atMs - b.atMs);
  return survivors;
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
