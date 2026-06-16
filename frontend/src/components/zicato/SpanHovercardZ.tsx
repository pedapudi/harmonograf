// SpanHovercardZ.tsx — the zicato quick-look hovercard. A compact line-art card
// that appears when the user HOVERS a span in the Gantt and is dismissed on
// mouse-out (with a small grace delay so the card stays readable while the
// pointer travels off the thin bar). It is the zicato analogue of the MD3
// SpanPopover's quick-look surface — same content model (title, kind/status,
// agent, duration, a 🧠 thinking preview, a one-line goldfive/judge verdict),
// restyled in the zicato language (token-only color, --mono, single accent).
//
// It renders as an absolutely-positioned overlay inside .zk-app-body (which is
// position:relative) and never intercepts pointer events (pointer-events:none),
// so it can't interfere with span CLICK (→ drawer) or wheel-zoom / drag-pan.
//
// Wiring: GanttZ reports the hovered span id + its on-screen rect via
// SpanHoverContext (hoverContext.ts). The console lifts that into local state
// and renders THIS component anchored near the hovered bar. All span detail is
// read from the live SessionStore (getSessionStore), mirroring SpanPopover's
// extraction, so the card shows real data — not the adapter's pre-derived ZSpan
// (kept in sync via a subscription bump in the console).

import { useEffect, useLayoutEffect, useRef, useState, type ReactElement } from 'react';
import type { Span } from '../../gantt/types';
import { bareAgentName } from '../../gantt/index';
import { actorDisplayLabel } from '../../theme/agentColors';
import { formatThinkingPreview, hasThinking } from '../../lib/thinking';
import { isGoldfiveSpan, resolveGoldfiveSpanInfo } from '../../lib/goldfiveSpan';
import { isJudgeSpan, resolveJudgeDetail } from '../../lib/interventionDetail';
import { toStatusToken } from './adapter';
import { useReasoningText } from './useReasoningText';

/** A short verdict label for off-/on-task judge spans. */
function judgeVerdictLabel(bucket: string, severity: string): string {
  if (bucket === 'on_task') return 'on task';
  if (bucket === 'no_verdict') return 'no verdict';
  return severity ? `off task (${severity})` : 'off task';
}

/** Map a status token to the .dn-pill modifier the rest of the console uses. */
function statusPillClass(status: string): string {
  if (status === 'failed') return 'bad';
  if (status === 'running') return 'accent';
  if (status === 'awaiting') return 'caution';
  return 'good';
}

export interface SpanHovercardZProps {
  /** The hovered span (already resolved from the store by the console). */
  span: Span;
  /** Live on-screen box of the hovered <rect> (client coords). */
  anchor: DOMRect;
  /** Bounding box of the overlay container (.zk-app-body) for relative coords. */
  containerRect: DOMRect;
}

const CARD_WIDTH = 260;
const GAP = 8;

/**
 * The hovercard body. Positioning is computed against the container rect so the
 * card is absolutely placed within .zk-app-body. We prefer ABOVE the bar; if
 * there isn't room we flip BELOW. Horizontal placement clamps inside the
 * container. A useLayoutEffect re-measures our own height after first paint so
 * the above/below flip uses the real card height, not a guess.
 */
export function SpanHovercardZ(props: SpanHovercardZProps): ReactElement {
  const { span, anchor, containerRect } = props;
  const cardRef = useRef<HTMLDivElement | null>(null);
  const [cardH, setCardH] = useState(96);

  useLayoutEffect(() => {
    const h = cardRef.current?.offsetHeight;
    if (h && h !== cardH) setCardH(h);
  });

  // Anchor coords relative to the (position:relative) container.
  const relLeft = anchor.left - containerRect.left;
  const relTop = anchor.top - containerRect.top;
  const relBottom = anchor.bottom - containerRect.top;

  let left = relLeft;
  left = Math.max(
    GAP,
    Math.min(containerRect.width - CARD_WIDTH - GAP, left),
  );

  // Prefer above; flip below when the top would clip the container.
  let top = relTop - GAP - cardH;
  if (top < GAP) top = relBottom + GAP;
  top = Math.max(GAP, Math.min(containerRect.height - cardH - GAP, top));

  // ── content extraction (mirrors SpanPopover) ─────────────────────────────
  const status = toStatusToken(span.status);
  const agentLabel =
    actorDisplayLabel(span.agentId) ?? bareAgentName(span.agentId) ?? span.agentId;
  const duration =
    span.endMs != null
      ? `${((span.endMs - span.startMs) / 1000).toFixed(1)}s`
      : '…';

  const judgeMode = isJudgeSpan(span);
  const goldfiveMode = isGoldfiveSpan(span);
  const goldfiveInfo = goldfiveMode ? resolveGoldfiveSpanInfo(span) : null;

  // Title: judge → "judge invocation"; goldfive → its call_name; else span name.
  const title = judgeMode
    ? 'judge invocation'
    : goldfiveInfo
      ? goldfiveInfo.callName
      : span.name;

  // One-line decision/verdict summary for goldfive/judge spans.
  let verdictLine: string | null = null;
  if (judgeMode) {
    const detail = resolveJudgeDetail(span, []);
    verdictLine = judgeVerdictLabel(detail.verdictBucket, detail.severity);
  } else if (goldfiveInfo) {
    verdictLine = goldfiveInfo.decisionSummary;
  }

  // 🧠 thinking preview (short truncation) when the span carries reasoning.
  // Resolve through useReasoningText so a trace that spilled to a payload_ref
  // (role: 'reasoning') still shows a real snippet, not a bare placeholder.
  const reasoning = useReasoningText(span);
  const spanHasReasoning = hasThinking(span) || reasoning.text != null || reasoning.loading;
  const reasoningPreview = reasoning.text
    ? formatThinkingPreview(reasoning.text, 240)
    : null;

  return (
    <div
      ref={cardRef}
      className="zk-hovercard"
      data-testid="zk-hovercard"
      data-span={span.id}
      style={{ left, top, width: CARD_WIDTH }}
      role="tooltip"
      aria-hidden="true"
    >
      <div className="zk-hovercard-title" data-testid="zk-hovercard-title">
        {title}
      </div>
      <div className="zk-hovercard-pills">
        <span className="dn-pill flat">{span.kind}</span>
        <span className={`dn-pill ${statusPillClass(status)}`}>{status}</span>
      </div>
      <dl className="zk-hovercard-meta">
        <dt>agent</dt>
        <dd>{agentLabel}</dd>
        <dt>duration</dt>
        <dd className="tnum" data-testid="zk-hovercard-duration">
          {duration}
        </dd>
      </dl>
      {verdictLine && (
        <div
          className="zk-hovercard-verdict"
          data-testid="zk-hovercard-verdict"
        >
          {verdictLine}
        </div>
      )}
      {spanHasReasoning && (
        <div
          className="zk-hovercard-reasoning"
          data-testid="zk-hovercard-reasoning"
        >
          <span className="zk-hovercard-reasoning-glyph" aria-hidden="true">
            🧠
          </span>
          <span className="zk-hovercard-reasoning-text">
            {reasoningPreview ?? (reasoning.loading ? 'reasoning…' : 'reasoning captured')}
          </span>
        </div>
      )}
      <div className="zk-hovercard-hint">click for full detail</div>
    </div>
  );
}

// ── the hover-state controller (used by the console) ─────────────────────────

/** What the console tracks for the currently-hovered span. */
export interface HoveredSpan {
  spanId: string;
  rect: DOMRect;
}

/**
 * The span whose hovercard should be shown, given the selection + hover state.
 *
 * A SELECTED span (the user clicked it → drawer open) PINS its hovercard: it
 * wins over the transient hover and stays put until deselected. With nothing
 * selected we fall back to the hovered span (the original transient behaviour),
 * or null when neither is set.
 *
 * Kept pure (no React) so the console's pin logic is unit-testable in isolation.
 */
export function displayedSpanId(
  selectedSpanId: string | null,
  hovered: HoveredSpan | null,
): string | null {
  return selectedSpanId ?? hovered?.spanId ?? null;
}

/**
 * A tiny stateful hook the console uses to debounce hover enter/leave with a
 * grace delay so the card doesn't vanish the instant the pointer slides off a
 * thin (4px) bar. Enter is immediate; leave is delayed by ~120ms and can be
 * cancelled by a re-enter. Timers are cleared on unmount.
 */
export function useHoverController(): {
  hovered: HoveredSpan | null;
  report: (spanId: string, rect: DOMRect) => void;
  clear: () => void;
} {
  const [hovered, setHovered] = useState<HoveredSpan | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelLeave = (): void => {
    if (leaveTimer.current != null) {
      clearTimeout(leaveTimer.current);
      leaveTimer.current = null;
    }
  };

  const report = (spanId: string, rect: DOMRect): void => {
    cancelLeave();
    setHovered({ spanId, rect });
  };

  const clear = (): void => {
    cancelLeave();
    leaveTimer.current = setTimeout(() => {
      setHovered(null);
      leaveTimer.current = null;
    }, 120);
  };

  // Clean up the pending timer on unmount.
  useEffect(() => cancelLeave, []);

  return { hovered, report, clear };
}
