import { useEffect, useMemo, useRef, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { usePopoverStore, type SpanPopover as SpanPopoverState } from '../../state/popoverStore';
import { useUiStore } from '../../state/uiStore';
import { useAgentLive, usePostAnnotation, useSendControl } from '../../rpc/hooks';
import type { Span, SpanKind } from '../../gantt/types';
import type { SessionStore } from '../../gantt/index';
import { bareAgentName } from '../../gantt/index';
import {
  extractThinkingText,
  formatThinkingPreview,
  hasThinking as spanHasThinking,
} from '../../lib/thinking';
import {
  isJudgeSpan,
  resolveJudgeDetail,
} from '../../lib/interventionDetail';
import {
  isGoldfiveSpan,
  resolveGoldfiveSpanInfo,
  truncatePreview,
  type GoldfiveSpanInfo,
} from '../../lib/goldfiveSpan';
import type { TaskPlan } from '../../gantt/types';
import { JudgeInvocationDetail } from '../Interventions/JudgeInvocationDetail';

// Local alias purely for useMemo annotation.
type TaskPlanForJudge = TaskPlan;

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
            store={store}
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
  store,
  sessionId,
  left,
  top,
  onClose,
  onTogglePin,
}: {
  state: SpanPopoverState;
  span: Span;
  store: SessionStore;
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

  // Judge spans render a specialised detail block under the meta header
  // so operators can see the judge's input + verdict + steering outcome
  // without bouncing to another view. The generic kind/status/duration
  // block still renders above; the judge detail supplants the Thinking /
  // Steer / Annotate sections (none of those apply to a synthesised
  // zero-duration event span).
  const judgeMode = isJudgeSpan(span);
  const judgeDetail = useMemo(() => {
    if (!judgeMode) return null;
    const plans: TaskPlanForJudge[] = [];
    const seen = new Set<TaskPlanForJudge>();
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) {
        if (seen.has(snap)) continue;
        seen.add(snap);
        plans.push(snap);
      }
    }
    return resolveJudgeDetail(span, plans);
  }, [span, store, judgeMode]);
  // Non-judge goldfive spans (refine_*, goal_derive, plan_generate,
  // reflective_check, unknown) get their own compact detail block —
  // decision summary headline, target row, input/output disclosures.
  // Judge spans also resolve info here so the popover header can swap
  // the bare span name for the call_name when the sink has stamped one,
  // but the judge detail component is still authoritative for verdict.
  const goldfiveMode = isGoldfiveSpan(span);
  const goldfiveInfo: GoldfiveSpanInfo | null = useMemo(
    () => (goldfiveMode ? resolveGoldfiveSpanInfo(span) : null),
    [goldfiveMode, span],
  );
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
    // suggestedAction carries the "cancel" vs "append" intent; goldfive's
    // STEER payload has both note and suggested_action by design.
    await send({
      sessionId,
      agentId: span.agentId,
      kind: 'STEER',
      note: text,
      suggestedAction: steerMode,
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

  // Judge popovers carry the most content (verdict banner + reasoning
  // input + context row — banner alone needs ~380px to stay single-line
  // on a typical laptop). Generic goldfive popovers are less heavy but
  // still carry decision summary + target row + input/output previews;
  // widen them moderately. Everything else stays compact.
  const popoverWidth = judgeMode
    ? POPOVER_WIDTH + 100
    : goldfiveMode
      ? POPOVER_WIDTH + 80
      : POPOVER_WIDTH;

  return (
    <div
      role="dialog"
      data-testid="span-popover"
      data-judge={judgeMode ? 'true' : 'false'}
      data-pinned={state.pinned ? 'true' : 'false'}
      style={{
        position: 'absolute',
        left,
        top,
        width: popoverWidth,
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
          marginBottom: judgeMode ? 8 : 4,
        }}
      >
        <div
          data-testid="span-popover-title"
          style={{
            fontWeight: 600,
            fontSize: 13,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {judgeMode
            ? 'Judge invocation'
            : goldfiveInfo
              ? goldfiveInfo.callName
              : span.name}
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
      {!judgeMode && (
        <>
          <div
            data-testid="span-popover-summary"
            style={{ opacity: 0.85, lineHeight: 1.5, marginBottom: 6 }}
          >
            {goldfiveInfo ? goldfiveInfo.decisionSummary : summary}
          </div>
          {goldfiveInfo && (goldfiveInfo.targetAgentId || goldfiveInfo.targetTaskId) && (
            <div
              data-testid="span-popover-goldfive-context"
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: '2px 10px',
                marginBottom: 6,
                fontSize: 11,
                opacity: 0.85,
              }}
            >
              {goldfiveInfo.targetAgentId && (
                <span data-testid="span-popover-goldfive-target-agent">
                  <span style={{ opacity: 0.6 }}>target </span>
                  {goldfiveInfo.targetAgentId}
                </span>
              )}
              {goldfiveInfo.targetTaskId && (
                <span data-testid="span-popover-goldfive-target-task">
                  <span style={{ opacity: 0.6 }}>task </span>
                  <code style={{ fontSize: 10 }}>{goldfiveInfo.targetTaskId}</code>
                </span>
              )}
            </div>
          )}
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
        </>
      )}
      {judgeMode && judgeDetail && (
        <div data-testid="span-popover-judge-body">
          <JudgeInvocationDetail
            detail={judgeDetail}
            variant="popover"
            resolveAgentName={(id) =>
              store.agents.get(id)?.name || bareAgentName(id) || id
            }
          />
        </div>
      )}
      {goldfiveMode && !judgeMode && goldfiveInfo && (
        <GoldfivePreviewDisclosures info={goldfiveInfo} />
      )}
      {!judgeMode && !goldfiveMode && thinkingHint && (
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
        {!judgeMode && !goldfiveMode && (
          <>
            <ActionButton onClick={toggleSteer} primary={steerOpen}>Steer</ActionButton>
            <ActionButton onClick={annotate}>Annotate</ActionButton>
          </>
        )}
        <ActionButton onClick={copyId}>Copy id</ActionButton>
        {/* Both judge and non-judge popovers open the inspector drawer;
            judge spans route to the JudgeDrawerPanel (Drawer.tsx), and
            generic goldfive spans route to GoldfiveSpanDetail inside the
            Summary tab via useGoldfiveDetailSection. */}
        <ActionButton onClick={openInDrawer} primary>
          Open drawer
        </ActionButton>
      </div>
      {!judgeMode && !goldfiveMode && steerOpen && (
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

function GoldfivePreviewDisclosures({ info }: { info: GoldfiveSpanInfo }) {
  // Both disclosures closed by default — operators usually only want one of
  // them at a time. Popover variant is ~400 chars; the drawer shows the full
  // 4 KiB preview.
  const [inputOpen, setInputOpen] = useState(false);
  const [outputOpen, setOutputOpen] = useState(false);
  const hasInput = !!info.inputPreview;
  const hasOutput = !!info.outputPreview;
  if (!hasInput && !hasOutput) return null;
  return (
    <div
      data-testid="span-popover-goldfive-previews"
      style={{
        marginTop: 10,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      {hasInput && (
        <GoldfiveDisclosure
          label="Input preview"
          open={inputOpen}
          onToggle={() => setInputOpen((v) => !v)}
          text={info.inputPreview}
          testId="span-popover-goldfive-input"
        />
      )}
      {hasOutput && (
        <GoldfiveDisclosure
          label="Output preview"
          open={outputOpen}
          onToggle={() => setOutputOpen((v) => !v)}
          text={info.outputPreview}
          testId="span-popover-goldfive-output"
        />
      )}
    </div>
  );
}

function GoldfiveDisclosure({
  label,
  open,
  onToggle,
  text,
  testId,
}: {
  label: string;
  open: boolean;
  onToggle: () => void;
  text: string;
  testId: string;
}) {
  const preview = truncatePreview(text, 400);
  return (
    <div
      data-testid={testId}
      data-open={open ? 'true' : 'false'}
      style={{
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        borderRadius: 6,
        background: 'rgba(255,255,255,0.03)',
      }}
    >
      <button
        type="button"
        data-testid={`${testId}-toggle`}
        onClick={onToggle}
        aria-expanded={open}
        style={{
          all: 'unset',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          width: '100%',
          padding: '4px 8px',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          opacity: 0.75,
        }}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>{label}</span>
      </button>
      {open && (
        <pre
          data-testid={`${testId}-body`}
          style={{
            margin: 0,
            padding: '4px 8px 6px',
            fontFamily: "ui-monospace, 'SF Mono', Consolas, monospace",
            fontSize: 11,
            lineHeight: 1.4,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            maxHeight: 180,
            overflow: 'auto',
            color: 'rgba(226,226,233,0.88)',
          }}
        >
          {preview}
        </pre>
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
