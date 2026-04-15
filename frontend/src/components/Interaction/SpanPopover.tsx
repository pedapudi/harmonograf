import { useEffect, useRef, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { usePopoverStore, type SpanPopover as SpanPopoverState } from '../../state/popoverStore';
import { useUiStore } from '../../state/uiStore';
import { useAgentLive, usePostAnnotation, useSendControl } from '../../rpc/hooks';
import type { Span, SpanKind } from '../../gantt/types';
import {
  extractThinkingText,
  formatThinkingPreview,
  hasThinking as spanHasThinking,
} from '../../lib/thinking';

// Floating quick-look popover anchored to a span block. Separate from the
// full Inspector Drawer: the drawer is the deep-dive surface, this is the
// "peek + act" surface. Multiple popovers coexist when pinned; a single
// unpinned popover is swapped out when another span is clicked.
//
// This component is rendered inside GanttCanvas's overlay tree so it can
// read the renderer's current span rectangle each render — the popover
// tracks its span through viewport pans and zooms.

const POPOVER_WIDTH = 320;

interface Props {
  ctx: OverlayContext;
  sessionId: string;
}

export function SpanPopover({ ctx, sessionId }: Props) {
  const { renderer, store, widthCss, heightCss, tick } = ctx;
  void tick;
  const popovers = usePopoverStore((s) => s.popovers);
  const close = usePopoverStore((s) => s.close);
  const togglePin = usePopoverStore((s) => s.togglePin);
  const closeUnpinned = usePopoverStore((s) => s.closeUnpinned);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Click-away dismisses unpinned popovers. Clicks that land on the canvas
  // are handled in GanttCanvas (which opens / swaps); here we only react to
  // clicks outside our DOM subtree and outside the canvas itself.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (containerRef.current?.contains(target)) return;
      // Canvas click is authoritative for open/swap; don't preempt it.
      const canvas = (target as Element | null)?.closest?.('.hg-gantt');
      if (canvas) return;
      closeUnpinned();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeUnpinned();
    };
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [closeUnpinned]);

  return (
    <div
      ref={containerRef}
      data-testid="span-popover-layer"
      style={{
        position: 'absolute',
        inset: 0,
        width: widthCss,
        height: heightCss,
        pointerEvents: 'none',
        zIndex: 20,
      }}
    >
      {[...popovers.values()].map((p) => {
        const span = store.spans.get(p.spanId);
        if (!span) return null;
        const rect = renderer.rectFor(p.spanId);
        const { left, top } = computePosition(
          rect,
          p,
          widthCss,
          heightCss,
        );
        return (
          <PopoverCard
            key={p.spanId}
            state={p}
            span={span}
            sessionId={sessionId}
            left={left}
            top={top}
            onClose={() => close(p.spanId)}
            onTogglePin={() => togglePin(p.spanId)}
          />
        );
      })}
    </div>
  );
}

function computePosition(
  rect: { x: number; y: number; w: number; h: number } | null,
  p: SpanPopoverState,
  widthCss: number,
  heightCss: number,
): { left: number; top: number } {
  // Anchor above the span block when we know where it is; fall back to the
  // click-time anchor when the span is off-screen or has been removed.
  let left = rect ? rect.x : p.anchorX;
  // Try to open above; fall back to below if not enough room.
  const approxH = 220;
  let top = rect ? rect.y - 10 - approxH : p.anchorY - approxH;
  if (top < 8) {
    top = rect ? rect.y + rect.h + 6 : p.anchorY + 20;
  }
  left = Math.max(8, Math.min(widthCss - POPOVER_WIDTH - 8, left));
  top = Math.max(8, Math.min(heightCss - 160, top));
  return { left, top };
}

function PopoverCard({
  state,
  span,
  sessionId,
  left,
  top,
  onClose,
  onTogglePin,
}: {
  state: SpanPopoverState;
  span: Span;
  sessionId: string;
  left: number;
  top: number;
  onClose: () => void;
  onTogglePin: () => void;
}) {
  const selectSpan = useUiStore((s) => s.selectSpan);
  const send = useSendControl();
  const post = usePostAnnotation();
  const agent = useAgentLive(sessionId, span.agentId);
  const agentName = agent?.name || span.agentId;
  const duration =
    span.endMs != null ? `${Math.max(0, span.endMs - span.startMs).toFixed(0)}ms` : '…';

  // Prefer the span-level task_report attribute (most specific; set directly from
  // ADK callbacks with the correct agent_id). Fall back to agent-level taskReport
  // from the heartbeat (aggregated), then to a static summary string.
  const spanTaskReport = span.attributes?.['task_report']?.kind === 'string'
    ? (span.attributes['task_report'] as { kind: 'string'; value: string }).value
    : undefined;
  const liveStatus = spanTaskReport || agent?.taskReport || agent?.currentActivity;
  const summary = liveStatus || spanSummary(span.kind, span.name, agentName);
  // Collapsible thinking section — closed by default to keep the quick-look
  // card compact. The chevron + brain icon header hints it can be expanded.
  // When only has_thinking=true is set (pre-text, streaming phase), we still
  // show the section header with a live indicator.
  const thinkingText = extractThinkingText(span);
  const thinkingHint = spanHasThinking(span);
  const [thinkingOpen, setThinkingOpen] = useState(false);

  const copyId = () => {
    void navigator.clipboard?.writeText(span.id).catch(() => {});
  };

  const openInDrawer = () => {
    selectSpan(span.id);
    onClose();
  };

  const [steerOpen, setSteerOpen] = useState(false);
  const [steerText, setSteerText] = useState('');
  const [steerSending, setSteerSending] = useState(false);
  const [steerMode, setSteerMode] = useState<'cancel' | 'append'>('cancel');

  const sendSteer = async () => {
    const text = steerText.trim();
    if (!text) return;
    setSteerSending(true);
    const encoder = new TextEncoder();
    const payload = JSON.stringify({ mode: steerMode, text });
    await send({
      sessionId,
      agentId: span.agentId,
      spanId: span.id,
      kind: 'STEER',
      payload: encoder.encode(payload),
    }).catch(() => {});
    setSteerText('');
    setSteerOpen(false);
    setSteerSending(false);
  };

  const toggleSteer = () => setSteerOpen((o) => !o);

  const annotate = () => {
    const text = window.prompt('Add a note for this span:');
    if (!text) return;
    void post({ sessionId, spanId: span.id, body: text, kind: 'COMMENT' }).catch(() => {});
  };

  return (
    <div
      role="dialog"
      data-testid="span-popover"
      data-pinned={state.pinned ? 'true' : 'false'}
      style={{
        position: 'absolute',
        left,
        top,
        width: POPOVER_WIDTH,
        pointerEvents: 'auto',
        background: 'var(--md-sys-color-surface-container-highest, #31333c)',
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        border: `1px solid ${
          state.pinned
            ? 'var(--md-sys-color-primary, #a8c8ff)'
            : 'var(--md-sys-color-outline, #4a4a53)'
        }`,
        borderRadius: 10,
        boxShadow: '0 12px 32px rgba(0,0,0,0.5)',
        padding: '10px 12px',
        fontSize: 12,
        fontFamily: 'system-ui, sans-serif',
      }}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          marginBottom: 4,
        }}
      >
        <div style={{ fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {span.name}
        </div>
        <div style={{ display: 'flex', gap: 4 }}>
          <IconButton
            label={state.pinned ? 'Unpin' : 'Pin'}
            testId="span-popover-pin"
            onClick={onTogglePin}
            active={state.pinned}
          >
            📌
          </IconButton>
          <IconButton label="Close" testId="span-popover-close" onClick={onClose}>
            ✕
          </IconButton>
        </div>
      </div>
      <div style={{ opacity: 0.85, lineHeight: 1.5, marginBottom: 6 }}>
        {summary}
      </div>
      <div style={{ opacity: 0.85, lineHeight: 1.5 }}>
        <div>
          <span style={{ opacity: 0.7 }}>kind </span>
          {span.kind}
          <span style={{ opacity: 0.7, marginLeft: 8 }}>status </span>
          {span.status}
        </div>
        <div>
          <span style={{ opacity: 0.7 }}>agent </span>
          {agentName}
        </div>
        <div>
          <span style={{ opacity: 0.7 }}>duration </span>
          {duration}
        </div>
      </div>
      {thinkingHint && (
        <div
          data-testid="span-popover-thinking"
          data-open={thinkingOpen ? 'true' : 'false'}
          style={{
            marginTop: 8,
            padding: '6px 8px',
            background: 'rgba(168,200,255,0.06)',
            border: '1px solid rgba(168,200,255,0.15)',
            borderRadius: 6,
            fontSize: 11,
            lineHeight: 1.4,
          }}
        >
          <button
            type="button"
            onClick={() => setThinkingOpen((v) => !v)}
            data-testid="span-popover-thinking-toggle"
            style={{
              all: 'unset',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              width: '100%',
              fontSize: 10,
              opacity: 0.75,
              textTransform: 'uppercase',
              letterSpacing: '0.05em',
            }}
            aria-expanded={thinkingOpen}
          >
            <span style={{ fontSize: 11 }}>{thinkingOpen ? '▾' : '▸'}</span>
            <span aria-hidden="true">🧠</span>
            <span>
              Thinking{span.endMs == null ? ' (live)' : ''}
            </span>
            {!thinkingOpen && thinkingText && (
              <span
                style={{
                  marginLeft: 'auto',
                  fontSize: 10,
                  fontStyle: 'italic',
                  opacity: 0.65,
                  textTransform: 'none',
                  letterSpacing: 0,
                  maxWidth: 180,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {formatThinkingPreview(thinkingText, 60)}
              </span>
            )}
          </button>
          {thinkingOpen && (
            thinkingText ? (
              <div
                style={{
                  marginTop: 6,
                  fontStyle: 'italic',
                  opacity: 0.85,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxHeight: 180,
                  overflow: 'auto',
                }}
              >
                {formatThinkingPreview(thinkingText, 1200)}
              </div>
            ) : (
              <div style={{ marginTop: 6, fontStyle: 'italic', opacity: 0.6 }}>
                <span className="hg-transport__live-dot" style={{ display: 'inline-block', marginRight: 6 }} />
                Thinking…
              </div>
            )
          )}
        </div>
      )}
      <div
        style={{
          display: 'flex',
          gap: 6,
          flexWrap: 'wrap',
          marginTop: 10,
          paddingTop: 8,
          borderTop: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        }}
      >
        <ActionButton onClick={toggleSteer} primary={steerOpen}>Steer</ActionButton>
        <ActionButton onClick={annotate}>Annotate</ActionButton>
        <ActionButton onClick={copyId}>Copy id</ActionButton>
        <ActionButton onClick={openInDrawer} primary>
          Open drawer
        </ActionButton>
      </div>
      {steerOpen && (
        <div
          style={{
            marginTop: 8,
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
          }}
        >
          {/* Mode toggle */}
          <div style={{ display: 'flex', gap: 4, marginBottom: 6 }}>
            <button
              onClick={() => setSteerMode('cancel')}
              style={{
                fontSize: 10,
                padding: '3px 8px',
                borderRadius: 4,
                border: '1px solid',
                cursor: 'pointer',
                background: steerMode === 'cancel' ? 'rgba(168,200,255,0.2)' : 'transparent',
                borderColor: steerMode === 'cancel' ? '#a8c8ff' : 'rgba(255,255,255,0.2)',
                color: steerMode === 'cancel' ? '#a8c8ff' : 'rgba(226,226,233,0.6)',
              }}
            >
              ⚡ Cancel &amp; redirect
            </button>
            <button
              onClick={() => setSteerMode('append')}
              style={{
                fontSize: 10,
                padding: '3px 8px',
                borderRadius: 4,
                border: '1px solid',
                cursor: 'pointer',
                background: steerMode === 'append' ? 'rgba(168,200,255,0.2)' : 'transparent',
                borderColor: steerMode === 'append' ? '#a8c8ff' : 'rgba(255,255,255,0.2)',
                color: steerMode === 'append' ? '#a8c8ff' : 'rgba(226,226,233,0.6)',
              }}
            >
              + Add to queue
            </button>
          </div>
          <textarea
            autoFocus
            value={steerText}
            onChange={(e) => setSteerText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void sendSteer();
              if (e.key === 'Escape') setSteerOpen(false);
            }}
            placeholder="Steering instruction… (⌘↵ to send)"
            rows={2}
            style={{
              width: '100%',
              resize: 'vertical',
              background: 'var(--md-sys-color-surface-container, #1d1f27)',
              color: 'inherit',
              border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
              borderRadius: 6,
              padding: '4px 8px',
              fontSize: 11,
              fontFamily: 'inherit',
              boxSizing: 'border-box',
            }}
          />
          <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
            <ActionButton onClick={() => setSteerOpen(false)}>Cancel</ActionButton>
            <ActionButton
              onClick={() => void sendSteer()}
              primary
              disabled={steerSending || !steerText.trim()}
              title={steerMode === 'cancel' ? 'Cancel current run and redirect with this message' : 'Queue this message for next model boundary'}
            >
              {steerSending ? 'Sending…' : 'Send'}
            </ActionButton>
          </div>
        </div>
      )}
    </div>
  );
}

function spanSummary(kind: SpanKind, name: string, agentName: string): string {
  switch (kind) {
    case 'INVOCATION':
      return `${agentName} is running an invocation`;
    case 'LLM_CALL':
      return `${agentName} is processing a model request`;
    case 'TOOL_CALL':
      return `${agentName} is calling ${name}`;
    case 'TRANSFER':
      return `${agentName} is transferring to ${name}`;
    default:
      return `${agentName} is handling ${name}`;
  }
}

function IconButton({
  label,
  testId,
  onClick,
  active,
  children,
}: {
  label: string;
  testId?: string;
  onClick: () => void;
  active?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      data-testid={testId}
      onClick={onClick}
      style={{
        background: active
          ? 'var(--md-sys-color-primary-container, #00468a)'
          : 'transparent',
        color: 'inherit',
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        borderRadius: 6,
        padding: '2px 6px',
        fontSize: 11,
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  );
}

function ActionButton({
  onClick,
  primary,
  disabled,
  title,
  children,
}: {
  onClick: () => void;
  primary?: boolean;
  disabled?: boolean;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        background: primary
          ? 'var(--md-sys-color-primary-container, #00468a)'
          : 'var(--md-sys-color-surface-container, #1d1f27)',
        color: primary
          ? 'var(--md-sys-color-on-primary-container, #d6e3ff)'
          : 'inherit',
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        borderRadius: 6,
        padding: '4px 10px',
        fontSize: 11,
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {children}
    </button>
  );
}
