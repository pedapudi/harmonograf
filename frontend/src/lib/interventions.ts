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
  SessionStore,
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
export type InterventionSource = 'user' | 'drift' | 'goldfive' | 'cancel';

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
  // Non-drift / non-cancel rows render at the "info" weight; drift and
  // cancel severities can grow the marker so high-severity cancels read
  // as heavier on the strip (they're the most consequential marker).
  if (row.source !== 'drift' && row.source !== 'cancel')
    return SEVERITY_WEIGHT.info;
  return SEVERITY_WEIGHT[row.severity] ?? SEVERITY_WEIGHT.info;
}

// Palette keyed by source — aligns with the palette note in issue #69:
//   user-blue, drift-amber, goldfive-grey, cancel-red.
// Centralized so the planning and trajectory views render uniformly.
// Cancel rows use a distinct red so the stop-glyph reads at a glance as
// a terminal, operator-only marker (critical cancels intensify in the
// renderer via the severity weight, not via the palette swatch).
export const SOURCE_COLOR: Record<InterventionSource, string> = {
  user: '#5b8def',
  drift: '#f59e0b',
  goldfive: '#8d9199',
  cancel: '#e05e4a',
};

// Glyph character keyed by source — so the compact list renders a
// source-discriminating leading symbol even before the row's text kicks
// in. Cancel is the stop / cancel symbol (U+2298 CIRCLED DIVISION SLASH),
// mirroring the lane markers on the Gantt and Graph views. Other sources
// default to a small middle dot so the column aligns across rows.
export const SOURCE_GLYPH: Record<InterventionSource, string> = {
  user: '·',
  drift: '·',
  goldfive: '·',
  cancel: '⊘',
};
