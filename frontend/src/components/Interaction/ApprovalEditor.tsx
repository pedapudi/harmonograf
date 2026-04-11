import { useEffect, useMemo, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { GUTTER_WIDTH_PX, msToPx } from '../../gantt/viewport';
import { useUiStore } from '../../state/uiStore';
import { useSendControl } from '../../rpc/hooks';
import type { AttributeValue } from '../../gantt/types';

// Inline approval card that anchors to an AWAITING_HUMAN span. Shows the
// proposed tool name + args, allows free-text JSON editing, and dispatches
// APPROVE (optionally with edited payload) or REJECT via SendControl.
//
// We surface this only for the currently-selected AWAITING_HUMAN span to keep
// the overlay quiet; multi-span approval goes through the inspector drawer's
// Control tab. Optimistic rollback is handled by the caller hook — on RPC
// failure we surface the error text and leave the card open so the user can
// retry.

interface Props {
  ctx: OverlayContext;
  sessionId: string;
}

export function ApprovalEditor({ ctx, sessionId }: Props) {
  const { renderer, store, widthCss, heightCss, tick } = ctx;
  void tick;
  const selectedId = useUiStore((s) => s.selectedSpanId);
  const send = useSendControl();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState<'APPROVE' | 'REJECT' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const span = selectedId ? store.spans.get(selectedId) : null;
  const visible = !!span && span.status === 'AWAITING_HUMAN';

  // Reset local state when switching between spans.
  useEffect(() => {
    setEditing(false);
    setDraft('');
    setError(null);
    setBusy(null);
  }, [selectedId]);

  const toolName = useMemo(() => {
    if (!span) return '';
    const n = span.attributes['tool.name'];
    return n?.kind === 'string' ? n.value : span.name;
  }, [span]);

  const initialArgs = useMemo(() => {
    if (!span) return '';
    const a = span.attributes['tool.args'];
    return a ? formatAttr(a) : '';
  }, [span]);

  if (!visible || !span) return null;

  // Row top for positioning — delegate to the renderer so focus expansion and
  // hidden-agent collapse (task #13 B5.2) are honored. If the span's agent is
  // currently hidden, the approval editor has nowhere to anchor, so skip.
  const row = renderer.getRowLayout().find((r) => r.agentId === span.agentId);
  if (!row || row.hidden) return null;

  const viewport = renderer.getViewport();
  const anchorX = Math.max(
    GUTTER_WIDTH_PX + 8,
    Math.min(widthCss - 360, msToPx(viewport, widthCss, span.startMs)),
  );
  const anchorY = Math.min(heightCss - 260, row.top + row.height + 8);

  const dispatch = async (kind: 'APPROVE' | 'REJECT', payload?: Uint8Array) => {
    setBusy(kind);
    setError(null);
    try {
      await send({
        sessionId,
        agentId: span.agentId,
        spanId: span.id,
        kind,
        payload,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const encoder = new TextEncoder();

  const onApprove = () => {
    if (editing) {
      // Validate JSON before sending so we don't round-trip garbage to the
      // agent. Empty edit is allowed and treated as plain approve.
      const text = draft.trim();
      if (text.length === 0) {
        void dispatch('APPROVE');
        return;
      }
      try {
        JSON.parse(text);
      } catch (e) {
        setError(`Edited args aren't valid JSON: ${String(e)}`);
        return;
      }
      void dispatch('APPROVE', encoder.encode(text));
    } else {
      void dispatch('APPROVE');
    }
  };

  return (
    <div
      role="dialog"
      aria-label="Approve pending action"
      style={{
        position: 'absolute',
        left: anchorX,
        top: anchorY,
        width: 360,
        maxHeight: 280,
        background: 'var(--md-sys-color-error-container, #5c1a1b)',
        color: 'var(--md-sys-color-on-error-container, #ffdad6)',
        border: '1px solid var(--md-sys-color-error, #ffb4ab)',
        borderRadius: 12,
        boxShadow: '0 8px 28px rgba(0,0,0,0.55)',
        padding: 12,
        zIndex: 25,
        pointerEvents: 'auto',
        fontSize: 13,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ fontWeight: 700 }}>
        Awaiting your approval
      </div>
      <div style={{ opacity: 0.9 }}>
        <strong>{toolName}</strong>
      </div>
      {!editing && (
        <pre
          style={{
            margin: 0,
            padding: 8,
            background: 'rgba(0,0,0,0.3)',
            borderRadius: 6,
            maxHeight: 120,
            overflow: 'auto',
            fontSize: 12,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {initialArgs || '(no args)'}
        </pre>
      )}
      {editing && (
        <textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={6}
          spellCheck={false}
          style={{
            width: '100%',
            background: 'rgba(0,0,0,0.35)',
            color: 'inherit',
            border: '1px solid var(--md-sys-color-error, #ffb4ab)',
            borderRadius: 6,
            padding: 6,
            fontFamily: 'ui-monospace, monospace',
            fontSize: 12,
            resize: 'vertical',
            boxSizing: 'border-box',
          }}
          placeholder={initialArgs || '{}'}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onApprove();
            }
          }}
        />
      )}
      {error && (
        <div style={{ color: 'var(--md-sys-color-on-error-container, #ffdad6)' }}>
          {error}
        </div>
      )}
      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        {!editing ? (
          <>
            <button
              onClick={() => {
                setDraft(initialArgs);
                setEditing(true);
              }}
              disabled={busy !== null}
            >
              Edit args
            </button>
            <button
              onClick={() =>
                void dispatch('REJECT', encoder.encode('rejected via overlay'))
              }
              disabled={busy !== null}
            >
              {busy === 'REJECT' ? 'Rejecting…' : 'Reject'}
            </button>
            <button onClick={onApprove} disabled={busy !== null}>
              {busy === 'APPROVE' ? 'Approving…' : 'Approve'}
            </button>
          </>
        ) : (
          <>
            <button onClick={() => setEditing(false)} disabled={busy !== null}>
              Cancel edit
            </button>
            <button onClick={onApprove} disabled={busy !== null}>
              {busy === 'APPROVE' ? 'Approving…' : 'Approve with edits'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function formatAttr(v: AttributeValue): string {
  switch (v.kind) {
    case 'string':
      // If the string is already JSON, pretty-print it.
      try {
        return JSON.stringify(JSON.parse(v.value), null, 2);
      } catch {
        return v.value;
      }
    case 'int':
      return v.value.toString();
    case 'double':
      return String(v.value);
    case 'bool':
      return v.value ? 'true' : 'false';
    case 'bytes':
      return `<${v.value.byteLength} bytes>`;
    case 'array':
      return JSON.stringify(v.value.map(attrToJson), null, 2);
  }
}

function attrToJson(v: AttributeValue): unknown {
  switch (v.kind) {
    case 'string':
      return v.value;
    case 'int':
      return Number(v.value);
    case 'double':
      return v.value;
    case 'bool':
      return v.value;
    case 'bytes':
      return `<${v.value.byteLength} bytes>`;
    case 'array':
      return v.value.map(attrToJson);
  }
}
