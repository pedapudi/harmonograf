// RPC-snapshot loader for PlanHistoryRegistry.
//
// Sibling server agent (harmonograf-plan-rpc) is adding
// `GetSessionPlanHistory` — returns every persisted PlanRevision for a
// session. The frontend prefers this unary fetch over walking the
// event stream because (a) it gives an atomic snapshot, (b) it works
// before WatchSession finishes its initial burst, and (c) it exposes
// revisions from sessions that have long since completed and whose
// events are no longer being replayed.
//
// The loader is entirely best-effort: if the generated client doesn't
// yet have `getSessionPlanHistory` (i.e. the RPC hasn't landed in the
// submodule / codegen hasn't regenerated yet), we silently bail. The
// live event stream always populates `planHistory` via
// `applyGoldfiveEvent`, so a missing RPC degrades to "incremental only,
// no atomic seed" without breaking the renderers.
//
// Both paths are idempotent on (plan_id, revision_number) so a
// successful RPC followed by a live-stream replay of the same events
// produces the same final state.

import type { SessionStore } from '../gantt/index';
import { getHarmonografClient } from '../rpc/client';
import type { PlanRevisionRecord } from './planHistoryStore';
import { convertGoldfivePlan } from '../rpc/goldfiveEvent';
import {
  DriftKind as GoldfiveDriftKindEnum,
} from '../pb/goldfive/v1/types_pb.js';

// Structural type for the expected wire shape. We read through a shallow
// structural cast rather than importing a generated type so this file
// compiles cleanly against the current pb (where the RPC doesn't yet
// exist). Once the spec lands and codegen regenerates, the real
// generated type naturally satisfies this interface.
//
// Mirrors:
//   message PlanRevision {
//     Plan plan = 1;
//     int32 revision_number = 2;
//     string revision_reason = 3;
//     string revision_kind = 4;            // DriftKind name
//     string revision_trigger_event_id = 5;
//     google.protobuf.Timestamp emitted_at = 6;
//   }
interface WirePlanRevision {
  plan?: unknown;
  revisionNumber?: number;
  revisionReason?: string;
  revisionKind?: string;
  revisionTriggerEventId?: string;
  emittedAt?: { seconds: bigint; nanos: number };
}

interface WirePlanHistoryResponse {
  revisions?: WirePlanRevision[];
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ClientLike = Record<string, any>;

/**
 * Return value of `loadPlanHistory`. Tests assert on these to confirm
 * graceful degradation: `skipped` is true when the client lacks the
 * RPC method, regardless of whether a live event stream is feeding
 * the registry in parallel.
 */
export interface PlanHistoryLoadResult {
  /** True if the RPC method was missing at call time (graceful bail). */
  skipped: boolean;
  /** True if the RPC call was made and returned without throwing. */
  fetched: boolean;
  /** Count of revisions appended to the registry (post-dedup). */
  appended: number;
  /** Any error observed — exposed for logging/metrics, never thrown. */
  error: Error | null;
}

function tsToMsAbs(
  t: { seconds: bigint; nanos: number } | undefined,
): number {
  if (!t) return 0;
  return Number(t.seconds) * 1000 + Math.floor(t.nanos / 1_000_000);
}

// The on-wire DriftKind for a PlanRevision is the enum name as a string
// (per the spec comment). Normalize it to the lowercase form used
// throughout the harmonograf UI to match the event-stream path.
function normalizeRevisionKind(raw: string | undefined): string {
  if (!raw) return '';
  // Tolerate both "OFF_TOPIC" (enum name) and "off_topic" (already
  // normalized). UNSPECIFIED collapses to the empty string.
  const upper = raw.toUpperCase();
  if (upper === 'UNSPECIFIED' || upper === '') return '';
  // If the string is one of the known enum names, lowercase it. If
  // not, echo back lowercase — better to pass through an unknown
  // token than to drop a signal the server thought worth sending.
  const known = Object.keys(GoldfiveDriftKindEnum).some(
    (k) => k === upper,
  );
  return known ? upper.toLowerCase() : raw.toLowerCase();
}

/**
 * Fetch a session's persisted plan-revision history and seed
 * `store.planHistory`. Safe to call unconditionally from session-load
 * flows — it degrades to a no-op when the RPC method is absent.
 *
 * `clientOverride` exists for tests (inject a stub client instead of
 * the singleton). Production callers pass no args.
 */
export async function loadPlanHistory(
  sessionId: string,
  store: SessionStore,
  sessionStartMs: number,
  clientOverride?: ClientLike,
): Promise<PlanHistoryLoadResult> {
  const result: PlanHistoryLoadResult = {
    skipped: false,
    fetched: false,
    appended: 0,
    error: null,
  };
  if (!sessionId) {
    result.skipped = true;
    return result;
  }
  const client: ClientLike =
    clientOverride ?? (getHarmonografClient() as unknown as ClientLike);
  // Graceful bail when the generated client predates the RPC.
  if (typeof client.getSessionPlanHistory !== 'function') {
    result.skipped = true;
    return result;
  }
  let resp: WirePlanHistoryResponse;
  try {
    resp = (await client.getSessionPlanHistory({
      sessionId,
    })) as WirePlanHistoryResponse;
    result.fetched = true;
  } catch (err) {
    result.error = err instanceof Error ? err : new Error(String(err));
    return result;
  }
  const wireRevisions = resp?.revisions ?? [];
  for (const wire of wireRevisions) {
    const record = revisionFromWire(wire, sessionStartMs);
    if (!record) continue;
    const before = store.planHistory.historyFor(record.plan.id).length;
    store.planHistory.append(record);
    const after = store.planHistory.historyFor(record.plan.id).length;
    if (after > before) result.appended++;
  }
  return result;
}

/**
 * Convert a wire PlanRevision into a PlanRevisionRecord. Returns null
 * when the row is missing its Plan payload (defensive: the server
 * should never emit one, but the wire shape allows it). Exported so
 * tests can drive the converter without standing up a full client.
 */
export function revisionFromWire(
  wire: WirePlanRevision,
  sessionStartMs: number,
): PlanRevisionRecord | null {
  const rawPlan = wire.plan;
  if (!rawPlan || typeof rawPlan !== 'object') return null;
  // convertGoldfivePlan is structurally compatible with the goldfive
  // Plan type used by both the event stream and this RPC — it reads
  // the same fields off the same proto message. Cast through unknown
  // to satisfy the compiler without dragging the full generated type.
  const converted = convertGoldfivePlan(
    rawPlan as unknown as Parameters<typeof convertGoldfivePlan>[0],
    sessionStartMs,
  );
  return {
    revision: Number(wire.revisionNumber ?? 0),
    plan: converted,
    reason: wire.revisionReason || '',
    kind: normalizeRevisionKind(wire.revisionKind),
    triggerEventId: wire.revisionTriggerEventId || '',
    emittedAtMs: tsToMsAbs(wire.emittedAt),
  };
}
