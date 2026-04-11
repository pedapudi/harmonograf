import { useEffect, useRef, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import {
  getSessionStore,
  usePostAnnotation,
  useSendControl,
} from '../../rpc/hooks';

export interface ContextMenuState {
  spanId: string;
  // Viewport (page) coordinates — this menu is positioned with `position: fixed`.
  x: number;
  y: number;
}

interface Props {
  state: ContextMenuState;
  onClose: () => void;
}

type Mode = 'menu' | 'annotate' | 'steer';

export function SpanContextMenu({ state, onClose }: Props) {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const send = useSendControl();
  const post = usePostAnnotation();
  const [mode, setMode] = useState<Mode>('menu');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  const span =
    sessionId != null
      ? getSessionStore(sessionId)?.spans.get(state.spanId)
      : undefined;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onClose();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const encoder = new TextEncoder();
  const disabled = !sessionId || !span || busy;

  return (
    <div
      ref={ref}
      role="menu"
      data-testid="span-context-menu"
      className="hg-ctxmenu"
      style={{
        position: 'fixed',
        left: Math.min(state.x, window.innerWidth - 240),
        top: Math.min(state.y, window.innerHeight - 200),
        minWidth: 200,
        background: 'var(--md-sys-color-surface-container-highest, #31333c)',
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        border: '1px solid var(--md-sys-color-outline, #4a4a53)',
        borderRadius: 8,
        boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
        padding: 6,
        zIndex: 1000,
        fontSize: 13,
      }}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.preventDefault()}
    >
      {mode === 'menu' && (
        <>
          <MenuItem
            label="Inspect"
            disabled={disabled}
            onClick={() => {
              selectSpan(state.spanId);
              onClose();
            }}
          />
          <MenuItem
            label="Annotate"
            disabled={disabled}
            onClick={() => setMode('annotate')}
          />
          <MenuItem
            label="Steer"
            testId="span-context-menu-steer"
            disabled={disabled || !hasCapability(span, 'STEERING')}
            onClick={() => setMode('steer')}
            hint={!hasCapability(span, 'STEERING') ? 'Agent lacks STEERING capability' : undefined}
          />
          {span?.status === 'AWAITING_HUMAN' && (
            <>
              <MenuItem
                label="Approve"
                disabled={disabled}
                onClick={() =>
                  run(async () => {
                    await send({
                      sessionId: sessionId!,
                      agentId: span.agentId,
                      spanId: span.id,
                      kind: 'APPROVE',
                    });
                  })
                }
              />
              <MenuItem
                label="Reject"
                disabled={disabled}
                onClick={() =>
                  run(async () => {
                    await send({
                      sessionId: sessionId!,
                      agentId: span.agentId,
                      spanId: span.id,
                      kind: 'REJECT',
                      payload: encoder.encode('rejected via context menu'),
                    });
                  })
                }
              />
            </>
          )}
          <MenuItem
            label="Rewind to here"
            disabled={disabled || !hasCapability(span, 'REWIND')}
            onClick={() =>
              run(async () => {
                await send({
                  sessionId: sessionId!,
                  agentId: span!.agentId,
                  spanId: span!.id,
                  kind: 'REWIND_TO',
                  payload: encoder.encode(span!.id),
                });
              })
            }
            hint={!hasCapability(span, 'REWIND') ? 'Agent lacks REWIND capability' : undefined}
          />
        </>
      )}

      {mode === 'annotate' && (
        <InlineCompose
          label="Comment"
          placeholder="Add a note…"
          value={body}
          setValue={setBody}
          busy={busy}
          onCancel={() => setMode('menu')}
          onSubmit={() =>
            run(async () => {
              await post({
                sessionId: sessionId!,
                spanId: state.spanId,
                body,
                kind: 'COMMENT',
              });
            })
          }
        />
      )}

      {mode === 'steer' && (
        <InlineCompose
          label="Steer"
          placeholder="Consider: "
          inputTestId="steer-input"
          submitTestId="steer-submit"
          value={body}
          setValue={setBody}
          busy={busy}
          onCancel={() => setMode('menu')}
          onSubmit={() =>
            run(async () => {
              await send({
                sessionId: sessionId!,
                agentId: span!.agentId,
                spanId: span!.id,
                kind: 'STEER',
                payload: encoder.encode(body),
              });
              // Mirror the steering intent as an annotation for timeline pin
              // rendering once the canvas pin layer lands; posting the
              // COMMENT here is harmless in the meantime.
              await post({
                sessionId: sessionId!,
                spanId: state.spanId,
                body,
                kind: 'STEERING',
              });
            })
          }
        />
      )}

      {err && (
        <div style={{ padding: '6px 10px', color: 'var(--md-sys-color-error, #ffb4ab)' }}>
          {err}
        </div>
      )}
    </div>
  );
}

function MenuItem({
  label,
  disabled,
  onClick,
  hint,
  testId,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  hint?: string;
  testId?: string;
}) {
  return (
    <div
      role="menuitem"
      data-testid={testId}
      title={hint}
      onClick={disabled ? undefined : onClick}
      style={{
        padding: '8px 12px',
        borderRadius: 4,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
      }}
      onMouseEnter={(e) => {
        if (!disabled)
          (e.currentTarget as HTMLDivElement).style.background =
            'var(--md-sys-color-surface-container, #1d1f27)';
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.background = 'transparent';
      }}
    >
      {label}
    </div>
  );
}

function InlineCompose({
  label,
  placeholder,
  value,
  setValue,
  busy,
  onCancel,
  onSubmit,
  inputTestId,
  submitTestId,
}: {
  label: string;
  placeholder: string;
  value: string;
  setValue: (v: string) => void;
  busy: boolean;
  onCancel: () => void;
  onSubmit: () => void;
  inputTestId?: string;
  submitTestId?: string;
}) {
  return (
    <div style={{ padding: 8, minWidth: 260 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>{label}</div>
      <textarea
        autoFocus
        data-testid={inputTestId}
        placeholder={placeholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
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
            onSubmit();
          }
        }}
      />
      <div style={{ display: 'flex', gap: 6, marginTop: 6, justifyContent: 'flex-end' }}>
        <button onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          data-testid={submitTestId}
          onClick={onSubmit}
          disabled={busy || !value.trim()}
        >
          {busy ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  );
}

function hasCapability(
  span:
    | { agentId: string }
    | undefined,
  cap: 'STEERING' | 'REWIND',
): boolean {
  if (!span) return false;
  const ui = useUiStore.getState();
  const sessionId = ui.currentSessionId;
  if (!sessionId) return false;
  const store = getSessionStore(sessionId);
  const agent = store?.agents.get(span.agentId);
  return agent?.capabilities.includes(cap) ?? false;
}
