// Helper for the Activity view's per-row kind label.
//
// CUSTOM-kind spans on the goldfive lane (drift detections, refine
// calls, judge invocations, etc.) used to render with the bare "CUSTOM"
// enum value, which conveyed nothing about what the row actually
// represents. We re-key those rows by inspecting the well-known
// goldfive / drift attributes the synthesizers stamp, and fall back to
// the span name when no opt-in attribute is present. Non-CUSTOM kinds
// are returned as their SpanKind enum value (INVOCATION, LLM_CALL, …)
// — those already read clearly and have UI affordances elsewhere keyed
// off them.
//
// Kept in its own module so ActivityView.tsx can satisfy the React
// fast-refresh "only export components" lint rule (the helper is
// shared with the focused unit test in __tests__/components).

import type { Span } from '../../../gantt/types';

export function activityKindLabel(span: Span): string {
  if (span.kind !== 'CUSTOM') return span.kind;
  // Drift-detected spans synthesised by goldfiveEvent.ts carry
  // ``drift.kind`` ("off_topic", "user_steer", …). Surface the
  // uppercase form so it reads alongside other UPPERCASE kind labels.
  const driftAttr = span.attributes?.['drift.kind'];
  if (driftAttr && driftAttr.kind === 'string' && driftAttr.value) {
    return driftAttr.value.toUpperCase();
  }
  // Refine-span synthesisers stamp ``refine.kind`` (the drift kind
  // that drove the refine). Surface that as "REFINE: <kind>" so it
  // reads distinct from the drift row.
  const refineKind = span.attributes?.['refine.kind'];
  if (refineKind && refineKind.kind === 'string' && refineKind.value) {
    return `REFINE: ${refineKind.value.toUpperCase()}`;
  }
  // Translated goldfive call_name (``refine_steer``, ``judge_reasoning``,
  // etc.) stamped by the harmonograf-side translator.
  const callName = span.attributes?.['goldfive.call_name'];
  if (callName && callName.kind === 'string' && callName.value) {
    return callName.value.toUpperCase();
  }
  // Last resort: the span's name often carries a useful tag (e.g. the
  // synthesiser sets ``name`` to the drift kind directly). Falling
  // back to the bare "CUSTOM" enum is uninformative; show the raw
  // name when it's set.
  if (span.name) return span.name.toUpperCase();
  return 'CUSTOM';
}
