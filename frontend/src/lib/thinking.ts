// Reasoning/thinking extraction helpers. The client library records model
// chain-of-thought on two span kinds via a small set of attributes emitted
// by ``HarmonografTelemetryPlugin``:
//
//   - ``llm.reasoning``       — per-LLM_CALL reasoning captured on span end
//                               by ``after_model_callback``. Small reasoning
//                               rides as a string attribute; large reasoning
//                               lives in a ``payload_ref`` with
//                               ``role="reasoning"`` (rendered by the
//                               Drawer's ReasoningSection, not this helper).
//   - ``llm.reasoning_trail`` — per-INVOCATION aggregate stamped by the
//                               plugin's ``after_run_callback``. Concatenates
//                               every child LLM_CALL's reasoning with
//                               ``[LLM call N]`` headers so clicking an
//                               agent row in the Gantt surfaces the full
//                               agent-level chain-of-thought without the
//                               user having to dig into each child span.
//                               See harmonograf#108.
//   - ``has_reasoning``       — bool flag set alongside either attribute so
//                               consumers can render a disclosure / badge
//                               before the full text is decoded.
//   - ``reasoning_call_count``— integer attached to the trail telling the
//                               drawer how many LLM turns contributed.
//
// Every reader in the app (SpanPopover, Drawer, timeline, LiveActivity)
// routes through the functions here so that fallbacks are consistent and
// we have one place to update when a new carrier appears.
import type { AttributeValue, Span, SpanKind } from '../gantt/types';

function attrString(attr: AttributeValue | undefined): string | null {
  if (!attr) return null;
  if (attr.kind === 'string') return attr.value;
  return null;
}

function attrBool(attr: AttributeValue | undefined): boolean {
  if (!attr) return false;
  if (attr.kind === 'bool') return attr.value;
  return false;
}

// Return the reasoning text attached to a span, or null when no reasoning
// carrier is present. Priority order: llm.reasoning_trail (INVOCATION
// aggregate) > llm.reasoning (single LLM_CALL). The aggregate is preferred
// so that selecting an agent's INVOCATION span surfaces the full trail
// instead of just the first call's text.
export function extractThinkingText(span: Span): string | null {
  const attrs = span.attributes;
  const full =
    attrString(attrs['llm.reasoning_trail']) ||
    attrString(attrs['llm.reasoning']);
  return full && full.length > 0 ? full : null;
}

// True when the span carries reasoning — either the has_reasoning flag
// (set the moment any reasoning is captured) or any reasoning-text
// attribute. Used by the Drawer / popover / live panel to decide whether
// to render a reasoning section / badge.
export function hasThinking(span: Span): boolean {
  if (attrBool(span.attributes['has_reasoning'])) return true;
  return extractThinkingText(span) !== null;
}

// Truncate thinking text to a preview length with an ellipsis. Trims leading
// whitespace so the caller can drop the result directly into a <blockquote>
// without worrying about indentation from multiline reasoning.
export function formatThinkingPreview(
  text: string | null,
  maxChars = 200,
): string {
  if (!text) return '';
  const trimmed = text.replace(/^\s+/, '').replace(/\s+$/, ' ');
  if (trimmed.length <= maxChars) return trimmed;
  return trimmed.slice(0, maxChars - 1).trimEnd() + '…';
}

// Collapse internal whitespace and trim for a one-line preview suitable for
// inline rendering inside a timeline row.
export function formatThinkingInline(
  text: string | null,
  maxChars = 200,
): string {
  if (!text) return '';
  const squashed = text.replace(/\s+/g, ' ').trim();
  if (squashed.length <= maxChars) return squashed;
  return squashed.slice(0, maxChars - 1).trimEnd() + '…';
}

export interface ThinkingEntry {
  spanId: string;
  agentId: string;
  spanName: string;
  spanKind: SpanKind;
  startMs: number;
  endMs: number | null;
  text: string;
  isLive: boolean;
}

// Produce a chronological list of thinking entries from an iterable of spans,
// filtered to those that actually carry reasoning text. Ordered by startMs
// ascending so callers can render as a scrollable feed.
export function collectThinkingEntries(spans: Iterable<Span>): ThinkingEntry[] {
  const out: ThinkingEntry[] = [];
  for (const s of spans) {
    const text = extractThinkingText(s);
    if (!text) continue;
    out.push({
      spanId: s.id,
      agentId: s.agentId,
      spanName: s.name,
      spanKind: s.kind,
      startMs: s.startMs,
      endMs: s.endMs,
      text,
      isLive: s.endMs == null,
    });
  }
  out.sort((a, b) => a.startMs - b.startMs || a.spanId.localeCompare(b.spanId));
  return out;
}

// Filter thinking entries to a given task id by matching the hgraf.task_id
// attribute on the source span. Accepts the same spans iterable so callers
// don't have to pre-filter (and so test fixtures stay small).
export function collectThinkingForTask(
  spans: Iterable<Span>,
  taskId: string,
): ThinkingEntry[] {
  const matched: Span[] = [];
  for (const s of spans) {
    const attr = s.attributes['hgraf.task_id'];
    if (attr && attr.kind === 'string' && attr.value === taskId) {
      matched.push(s);
    }
  }
  return collectThinkingEntries(matched);
}
