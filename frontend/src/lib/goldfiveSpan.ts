// Goldfive span helpers — identify a goldfive-translated span, read its
// new-wire attributes, and classify it into a "call type" the three
// rendering surfaces (Gantt bar, popover, drawer) share.
//
// Incoming attributes (stamped by the harmonograf client sink when it
// translates goldfive_llm_call_{start,end} / reasoning_judge_invoked
// events into span frames):
//
//   * goldfive.call_name       — e.g. "refine_steer", "judge_reasoning",
//                                "goal_derive", "judge_goal_drift",
//                                "reflective_check", "plan_generate"
//   * goldfive.input_preview   — up to 4 KiB of the call's input
//   * goldfive.output_preview  — up to 4 KiB of the call's output / verdict
//   * goldfive.target_agent_id — compound "<client>:agent_name" or empty
//   * goldfive.target_task_id  — task context or empty
//   * goldfive.decision_summary — one-line active-voice summary (≤512 chars)
//
// Plus the older `judge.*` attributes the JudgeInvocationDetail already
// consumes when the call is a judge invocation.
//
// All attributes are optional. Pre-merge sessions carry none of them and
// the helpers degrade: a "goldfive" span is still detected via the
// legacy `agentId.endsWith(":goldfive")` or `__goldfive__` heuristic, and
// consumers render the basic popover / drawer.

import type { Span } from '../gantt/types';

export type GoldfiveCallCategory =
  | 'judge'       // judge_reasoning, judge_goal_drift, etc.
  | 'refine'      // refine_steer, refine_retry, etc.
  | 'plan'        // goal_derive, plan_generate
  | 'reflective'  // reflective_check
  | 'unknown';

export type GoldfiveVerdict = 'on_task' | 'off_task_warning' | 'off_task_critical' | 'no_verdict';

export interface GoldfiveSpanInfo {
  /** call_name attribute value (e.g. "refine_steer"). Falls back to the span name. */
  callName: string;
  /** coarse category used by the color bucket + drawer branching. */
  category: GoldfiveCallCategory;
  /** human-readable one-liner; falls back to `goldfive: <callName>` then the span name. */
  decisionSummary: string;
  /** bare agent name (after the ``<client>:`` prefix) when ``target_agent_id`` is set. */
  targetAgentId: string;
  /** as-stamped compound target id. */
  targetAgentIdRaw: string;
  /** ``target_task_id`` when set. */
  targetTaskId: string;
  /** up to ~4 KiB of the input preview. */
  inputPreview: string;
  /** up to ~4 KiB of the output preview. */
  outputPreview: string;
  /** verdict classification for judge calls; ``no_verdict`` for non-judge / unknown. */
  verdict: GoldfiveVerdict;
  /** true when the `judge.on_task` attribute resolved to true. */
  onTask: boolean;
  /** lowercased `judge.severity` (info / warning / critical / ""). */
  severity: string;
}

/** Read a string attribute gracefully — empty string when absent or wrong kind. */
function readStr(span: Span, key: string): string {
  const v = span.attributes?.[key];
  if (!v) return '';
  if (v.kind !== 'string') return '';
  return v.value;
}

function readBool(span: Span, key: string): boolean | undefined {
  const v = span.attributes?.[key];
  if (!v) return undefined;
  if (v.kind !== 'bool') return undefined;
  return v.value;
}

const JUDGE_PREFIXES = ['judge_'];
const REFINE_PREFIXES = ['refine_'];
const PLAN_NAMES = new Set(['goal_derive', 'plan_generate']);
const REFLECTIVE_NAMES = new Set(['reflective_check']);

function classifyCallName(name: string): GoldfiveCallCategory {
  if (!name) return 'unknown';
  for (const p of JUDGE_PREFIXES) if (name.startsWith(p)) return 'judge';
  for (const p of REFINE_PREFIXES) if (name.startsWith(p)) return 'refine';
  if (PLAN_NAMES.has(name)) return 'plan';
  if (REFLECTIVE_NAMES.has(name)) return 'reflective';
  // Back-compat: legacy frontend-synthesized refine spans were named
  // `refine: <kind>` on the __goldfive__ row. Classify those as refine too
  // so color + drawer routing match post-Option-X.
  if (name.startsWith('refine:')) return 'refine';
  // Legacy frontend-synthesized judge spans were `judge: <...>` — tests
  // still exercise that shape against isJudgeSpan.
  if (name.startsWith('judge:')) return 'judge';
  return 'unknown';
}

/** Strip ``<client>:`` prefix from a compound agent id; pass-through otherwise. */
export function bareGoldfiveAgentName(compound: string): string {
  if (!compound) return '';
  const colon = compound.indexOf(':');
  if (colon < 0) return compound;
  return compound.slice(colon + 1);
}

/**
 * True when the span represents a goldfive call — either translated via the
 * sink (agentId ends with `:goldfive` OR a `goldfive.call_name` is set) or
 * legacy-synthesized on the `__goldfive__` actor row.
 */
export function isGoldfiveSpan(span: Span | null | undefined): boolean {
  if (!span) return false;
  if (readStr(span, 'goldfive.call_name')) return true;
  if (span.agentId === '__goldfive__') return true;
  // Compound id form stamped by the sink (any `:goldfive` suffix).
  if (span.agentId.endsWith(':goldfive')) return true;
  return false;
}

export function resolveGoldfiveSpanInfo(span: Span): GoldfiveSpanInfo {
  const callName = readStr(span, 'goldfive.call_name') || span.name;
  const category = classifyCallName(callName);
  const targetAgentIdRaw = readStr(span, 'goldfive.target_agent_id');
  const targetAgentId = bareGoldfiveAgentName(targetAgentIdRaw);
  const targetTaskId = readStr(span, 'goldfive.target_task_id');
  const inputPreview = readStr(span, 'goldfive.input_preview');
  const outputPreview = readStr(span, 'goldfive.output_preview');

  const verdictStr = readStr(span, 'judge.verdict');
  const onTaskAttr = readBool(span, 'judge.on_task');
  const onTask = onTaskAttr === true || (onTaskAttr === undefined && verdictStr === 'on_task');
  const severity = readStr(span, 'judge.severity').toLowerCase();
  let verdict: GoldfiveVerdict = 'no_verdict';
  if (category === 'judge') {
    if (onTask) verdict = 'on_task';
    else if (severity === 'critical') verdict = 'off_task_critical';
    else if (severity) verdict = 'off_task_warning';
    else if (verdictStr) verdict = verdictStr === 'on_task' ? 'on_task' : 'off_task_warning';
    else verdict = 'no_verdict';
  }

  let decisionSummary = readStr(span, 'goldfive.decision_summary');
  if (!decisionSummary) decisionSummary = `goldfive: ${callName}`;

  return {
    callName,
    category,
    decisionSummary,
    targetAgentId,
    targetAgentIdRaw,
    targetTaskId,
    inputPreview,
    outputPreview,
    verdict,
    onTask,
    severity,
  };
}

/**
 * Resolve the per-category base color used by the Gantt bar + popover
 * accents. Reads CSS custom properties so theme switches recolor on the
 * next frame. Callers provide the ``cssVar`` reader (the renderer's
 * theme-cache-aware one) so we don't re-read ``getComputedStyle`` per
 * span.
 */
export function goldfiveCallFill(
  info: GoldfiveSpanInfo,
  cssVar: (name: string) => string,
  fallback: string,
): string {
  switch (info.category) {
    case 'judge':
      if (info.verdict === 'on_task') {
        return cssVar('--hg-goldfive-judge-on-task') || '#3bb273';
      }
      if (info.verdict === 'off_task_critical') {
        return cssVar('--hg-goldfive-judge-critical') || '#e06070';
      }
      if (info.verdict === 'off_task_warning') {
        return cssVar('--hg-goldfive-judge-warning') || '#f59e0b';
      }
      return cssVar('--hg-goldfive-judge-neutral') || '#8d9199';
    case 'refine':
      return cssVar('--hg-goldfive-refine') || '#a78bfa';
    case 'plan':
      return cssVar('--hg-goldfive-plan') || '#4fd1c5';
    case 'reflective':
      return cssVar('--hg-goldfive-reflective') || '#8d9199';
    case 'unknown':
    default:
      return fallback;
  }
}

/** Clip a preview to the popover's shorter budget. */
export function truncatePreview(text: string, limit: number): string {
  if (!text) return '';
  if (text.length <= limit) return text;
  return text.slice(0, limit) + '…';
}

/**
 * Per-category glyph used to disambiguate goldfive lane spans visually.
 * The goldfive lane otherwise renders every category as a "CUSTOM"-kind
 * span (• dot), so refine / judge / plan / reflective calls were
 * indistinguishable beyond their fill colour. Distinct glyphs let
 * operators triage at a glance: refine spans (↻ self-correction) read
 * different from judge spans (⚖ verdict) read different from plan
 * spans (📐 planning) read different from reflective spans (✱ progress
 * check). See Item 6 of the UX cleanup batch.
 *
 * Returns null for ``unknown`` so the renderer keeps the default
 * SpanKind icon (or no icon for plain CUSTOM spans).
 */
export function goldfiveCallGlyph(category: GoldfiveCallCategory): string | null {
  switch (category) {
    case 'refine':
      return '↻';
    case 'judge':
      return '⚖';
    case 'plan':
      return '📐';
    case 'reflective':
      return '✱';
    case 'unknown':
    default:
      return null;
  }
}
