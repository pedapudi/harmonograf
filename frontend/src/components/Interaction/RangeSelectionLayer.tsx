import { useEffect, useRef, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import {
  GUTTER_WIDTH_PX,
  ROW_HEIGHT_PX,
  ROW_HEIGHT_FOCUSED_PX,
  TOP_MARGIN_PX,
  pxToMs,
} from '../../gantt/viewport';
import { usePostAnnotation } from '../../rpc/hooks';
import { useAnnotationStore } from '../../state/annotationStore';

// Click-drag range annotation capture.
//
// Alt+drag (or shift+drag) on an agent row creates a range — this disambiguates
// from the renderer's own click-to-select and pan gestures. Releasing with a
// non-zero width opens a small inline compose popover anchored to the drag
// midpoint; on submit we POST a COMMENT annotation with agent_time target at
// the start and stash the end client-side in annotationStore.rangeEndMs.
//
// We deliberately don't try to catch plain left-drag — doc 04 §5.4 reserves
// that for panning empty timeline area, and the renderer's click handler
// treats a non-drag click as span selection.

interface Props {
  ctx: OverlayContext;
  sessionId: string;
}

interface DragState {
  agentId: string;
  rowTop: number;
  rowHeight: number;
  startPx: number;
  curPx: number;
}

interface PendingDraft {
  agentId: string;
  startMs: number;
  endMs: number;
  anchorX: number;
  anchorY: number;
}

export function RangeSelectionLayer({ ctx, sessionId }: Props) {
  const { renderer, store, widthCss, heightCss, tick } = ctx;
  void tick;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [draft, setDraft] = useState<PendingDraft | null>(null);
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const post = usePostAnnotation();

  // Build row metrics mirroring the renderer's layout. Read focusedAgentId via
  // the renderer's public getter surface — falls back to default row height if
  // the private field isn't accessible (e.g. after a refactor).
  const rows: { agentId: string; top: number; height: number }[] = [];
  {
    let y = TOP_MARGIN_PX;
    const focusedId =
      (renderer as unknown as { focusedAgentId: string | null }).focusedAgentId ??
      null;
    for (const agent of store.agents.list) {
      const h = agent.id === focusedId ? ROW_HEIGHT_FOCUSED_PX : ROW_HEIGHT_PX;
      rows.push({ agentId: agent.id, top: y, height: h });
      y += h;
    }
  }

  const rowAt = (yPx: number): typeof rows[number] | null => {
    for (const r of rows) {
      if (yPx >= r.top && yPx < r.top + r.height) return r;
    }
    return null;
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    if (!(e.altKey || e.shiftKey)) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (x < GUTTER_WIDTH_PX || y < TOP_MARGIN_PX) return;
    const row = rowAt(y);
    if (!row) return;
    e.stopPropagation();
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    setDrag({
      agentId: row.agentId,
      rowTop: row.top,
      rowHeight: row.height,
      startPx: x,
      curPx: x,
    });
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.max(GUTTER_WIDTH_PX, Math.min(widthCss, e.clientX - rect.left));
    setDrag({ ...drag, curPx: x });
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (!drag) return;
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      // ignore — pointer may have already been released
    }
    const x1 = Math.min(drag.startPx, drag.curPx);
    const x2 = Math.max(drag.startPx, drag.curPx);
    const width = x2 - x1;
    setDrag(null);
    if (width < 4) return; // treat as cancelled — too small to be intentional
    const viewport = renderer.getViewport();
    const startMs = pxToMs(viewport, widthCss, x1);
    const endMs = pxToMs(viewport, widthCss, x2);
    setBody('');
    setDraft({
      agentId: drag.agentId,
      startMs,
      endMs,
      anchorX: (x1 + x2) / 2,
      anchorY: drag.rowTop,
    });
  };

  const cancelDraft = () => {
    setDraft(null);
    setBody('');
  };

  const submitDraft = async () => {
    if (!draft || !body.trim()) return;
    setBusy(true);
    // Find a span under the start point on the same agent row to target the
    // annotation at — proto requires either a span or an agent-time point.
    // We prefer a span because server storage indexes by span for fast fanout.
    const store = ctx.store;
    const candidates = store.spans.queryAgent(
      draft.agentId,
      draft.startMs - 1,
      draft.startMs + 1,
    );
    const targetSpan = candidates.find(
      (s) => s.startMs <= draft.startMs && (s.endMs ?? Infinity) >= draft.startMs,
    );
    if (!targetSpan) {
      // No covering span — we don't have an agent_time path in the hook yet,
      // so fall back: attach to the nearest span in the drag range.
      const any = store.spans.queryAgent(draft.agentId, draft.startMs, draft.endMs);
      if (any.length === 0) {
        setBusy(false);
        cancelDraft();
        return;
      }
    }
    const anchorSpan = targetSpan ?? candidates[0] ?? null;
    if (!anchorSpan) {
      setBusy(false);
      cancelDraft();
      return;
    }

    // Optimistic: the hook already inserts a pending row. We write rangeEndMs
    // into the store after the hook call — we re-find the row by body+author
    // since the hook generates the temp id internally. This is a narrow race
    // (single-user console) so a scan is acceptable.
    const beforeIds = new Set(
      useAnnotationStore.getState().list(sessionId).map((a) => a.id),
    );
    try {
      await post({
        sessionId,
        spanId: anchorSpan.id,
        body: body.trim(),
        kind: 'COMMENT',
      });
      // Find any newly-added annotations for this span and stamp range on
      // the server-assigned row.
      const after = useAnnotationStore.getState().list(sessionId);
      for (const a of after) {
        if (beforeIds.has(a.id)) continue;
        if (a.spanId !== anchorSpan.id) continue;
        useAnnotationStore.getState().upsert({
          ...a,
          atMs: draft.startMs,
          rangeEndMs: draft.endMs,
        });
      }
    } catch {
      // The hook marks the optimistic row with error; the PinStrip will
      // surface retry.
    } finally {
      setBusy(false);
      cancelDraft();
    }
  };

  // ESC cancels an open draft.
  useEffect(() => {
    if (!draft) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cancelDraft();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [draft]);

  // The capture surface only intercepts events when Alt/Shift is held; we
  // detect this with a CSS-driven pointerEvents toggle via state set in
  // handlers. During an active drag the layer keeps pointer-events on to
  // receive move/up.
  return (
    <div
      ref={containerRef}
      style={{
        position: 'absolute',
        inset: 0,
        width: widthCss,
        height: heightCss,
        zIndex: 5,
        pointerEvents: drag ? 'auto' : 'none',
      }}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    >
      {/* A second transparent layer that only activates modifier-click. We
          always render this because onPointerDown needs a live target; we
          rely on the handler to early-return when no modifier is pressed. */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'auto',
          // This needs to be under pins/menus but above the canvas. Because
          // the parent is already z:5, children here inherit that.
        }}
        onPointerDown={onPointerDown}
        onContextMenu={(e) => {
          // Let the canvas context menu handle right-click — do nothing.
          void e;
        }}
      />
      {drag && (
        <div
          aria-hidden="true"
          style={{
            position: 'absolute',
            left: Math.min(drag.startPx, drag.curPx),
            top: drag.rowTop + 2,
            width: Math.abs(drag.curPx - drag.startPx),
            height: drag.rowHeight - 4,
            background: 'var(--md-sys-color-primary-container, #00468a)',
            opacity: 0.35,
            border: '1px dashed var(--md-sys-color-primary, #a8c8ff)',
            borderRadius: 4,
            pointerEvents: 'none',
          }}
        />
      )}
      {draft && (
        <div
          role="dialog"
          aria-label="Add range annotation"
          style={{
            position: 'absolute',
            left: Math.min(widthCss - 280, Math.max(GUTTER_WIDTH_PX + 4, draft.anchorX - 140)),
            top: Math.max(TOP_MARGIN_PX + 4, draft.anchorY + 16),
            width: 280,
            background: 'var(--md-sys-color-surface-container-highest, #31333c)',
            color: 'var(--md-sys-color-on-surface, #e2e2e9)',
            border: '1px solid var(--md-sys-color-outline, #4a4a53)',
            borderRadius: 8,
            boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
            padding: 10,
            zIndex: 20,
            pointerEvents: 'auto',
            fontSize: 13,
          }}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
        >
          <div style={{ fontWeight: 600, marginBottom: 6 }}>
            Range note · {formatMs(draft.startMs)}–{formatMs(draft.endMs)}
          </div>
          <textarea
            autoFocus
            placeholder="Describe the range…"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={3}
            style={{
              width: '100%',
              background: 'var(--md-sys-color-surface, #10131a)',
              color: 'inherit',
              border: '1px solid var(--md-sys-color-outline, #4a4a53)',
              borderRadius: 4,
              padding: 6,
              fontFamily: 'inherit',
              fontSize: 13,
              resize: 'vertical',
              boxSizing: 'border-box',
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void submitDraft();
              }
            }}
          />
          <div style={{ display: 'flex', gap: 6, marginTop: 8, justifyContent: 'flex-end' }}>
            <button onClick={cancelDraft} disabled={busy}>
              Cancel
            </button>
            <button onClick={() => void submitDraft()} disabled={busy || !body.trim()}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function formatMs(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}
