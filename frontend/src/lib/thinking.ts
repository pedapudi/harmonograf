// Thinking/reasoning extraction helpers. The client library records model
// reasoning tokens on LLM_CALL spans via a small zoo of attributes, depending
// on which model backend is in play and how recent the capture path is:
//
//   - ``llm.thought``       — full aggregate from HarmonografAgent's
//                             _run_async_impl (handles ADK-style thought parts
//                             and OpenAI-style reasoning_content blocks).
//   - ``thinking_text``     — full text captured by the plugin on span end.
//   - ``thinking_preview``  — first 300 chars (set as a fallback).
//   - ``has_thinking``      — bool flag set as soon as any reasoning lands.
//   - ``llm.reasoning``     — per-response reasoning_content / thinking-block
//                             capture emitted by
//                             ``HarmonografTelemetryPlugin.after_model_callback``.
//                             Small reasoning rides as a string attribute;
//                             large reasoning lives in a payload_ref with
//                             ``role="reasoning"`` (rendered by the Drawer's
//                             ReasoningSection, not this helper).
//   - ``has_reasoning``     — bool flag for the Drawer's Reasoning toggle.
//
// Every reader in the app (SpanPopover, Drawer, renderer, timeline, etc.)
// routes through the functions here so that fallbacks are consistent and we
// have one place to update when a new carrier appears.
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

// Return the full thinking text attached to a span, or null if no reasoning
// carrier is present. Priority order: llm.thought > thinking_text >
// thinking_preview. The preview fallback is intentionally last so that when
// the full text exists we never truncate it prematurely.
export function extractThinkingText(span: Span): string | null {
  const attrs = span.attributes;
  const full =
    attrString(attrs['llm.thought']) ||
    attrString(attrs['thinking_text']) ||
    attrString(attrs['thinking_preview']);
  return full && full.length > 0 ? full : null;
}

// True when the span carries reasoning — either an explicit has_thinking=true
// flag (set the moment the first thought part arrives, even before text
// accumulates) or any thinking-text attribute.
export function hasThinking(span: Span): boolean {
  if (attrBool(span.attributes['has_thinking'])) return true;
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
