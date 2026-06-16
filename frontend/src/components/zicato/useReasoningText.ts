// useReasoningText.ts — resolve a span's reasoning/chain-of-thought text,
// transparently following a payload reference when the trace is too large to
// ride inline. The client library records small reasoning as a string
// attribute (see lib/thinking.ts) but spills large traces to a payload blob
// referenced by a PayloadRef with `role === 'reasoning'`. The zicato hovercard
// and inspector both need the *actual* text, not the "captured (full text in a
// payload reference)" placeholder — this hook gives them one source of truth.
//
// It ALWAYS calls usePayload (with a null digest when there is nothing to
// fetch) so the rules of hooks are never violated regardless of which carrier
// the span uses.

import { usePayload } from '../../rpc/hooks';
import { extractThinkingText } from '../../lib/thinking';
import type { Span } from '../../gantt/types';

export interface ReasoningTextState {
  /** The resolved reasoning text, or null when none is available. */
  text: string | null;
  /** True while a payload-backed trace is being fetched. */
  loading: boolean;
}

/**
 * Resolve the reasoning text for a span. Inline reasoning (a string attribute)
 * is returned immediately with no fetch. When there is no inline text but the
 * span carries a `role: 'reasoning'` payload reference, the referenced blob is
 * fetched and decoded as UTF-8. Returns `{ text: null, loading: false }` when
 * the span carries no reasoning at all (or is null).
 */
export function useReasoningText(span: Span | null): ReasoningTextState {
  const inline = span ? extractThinkingText(span) : null;
  // Only chase a payload ref when there is no inline text to show.
  const ref =
    !inline && span
      ? (span.payloadRefs ?? []).find((r) => r.role === 'reasoning')
      : undefined;
  // ALWAYS call the hook (rules of hooks); a null digest means no fetch.
  const payload = usePayload(ref?.digest ?? null);

  if (inline) {
    return { text: inline, loading: false };
  }
  if (ref) {
    if (payload.loading) {
      return { text: null, loading: true };
    }
    if (payload.bytes) {
      return { text: new TextDecoder().decode(payload.bytes), loading: false };
    }
    return { text: null, loading: false };
  }
  return { text: null, loading: false };
}
