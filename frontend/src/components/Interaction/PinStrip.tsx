import { useMemo } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { GUTTER_WIDTH_PX, msToPx, viewportStart } from '../../gantt/viewport';
import { useAnnotationStore, type Annotation } from '../../state/annotationStore';
import { useUiStore } from '../../state/uiStore';
import { usePostAnnotation } from '../../rpc/hooks';

// Persistent pin strip: renders annotation + steering pins in a dedicated 10px
// band at the top of each agent row. Pins survive block size changes (doc 04
// §7.2) and carry per-pin delivered/pending/error state.
//
// Layout: we iterate the agents in the store, compute a y baseline per row,
// and place each pin at (msToPx(span.startMs), rowTop). Pins are pure DOM,
// receive pointer events for hover/click-to-retry, and do not participate in
// the hot canvas render loop.

const PIN_STRIP_HEIGHT = 12;
const EMPTY: Annotation[] = [];

interface Props {
  ctx: OverlayContext;
  sessionId: string;
}

export function PinStrip({ ctx, sessionId }: Props) {
  const { renderer, store, widthCss, heightCss, tick } = ctx;
  void tick;
  // Subscribe to the bySession map directly — calling s.list(sessionId) returns
  // a fresh array each time and would trip zustand's shallow-equal render loop.
  const annotations = useAnnotationStore(
    (s) => s.bySession.get(sessionId) ?? EMPTY,
  );
  const selectSpan = useUiStore((s) => s.selectSpan);
  const post = usePostAnnotation();

  const rows = useMemo(() => {
    // Delegate to the renderer so focus expansion and hidden-agent collapse
    // (task #13 B5.2) stay in one place. Hidden rows are excluded because
    // pin dots would have nothing to anchor to in a collapsed row.
    return renderer
      .getRowLayout()
      .filter((r) => !r.hidden)
      .map((r) => ({ agentId: r.agentId, top: r.top, height: r.height }));
    // tick forces recomputation on viewport/resize changes; renderer state is
    // read imperatively and not tracked by React.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, renderer, tick]);

  const rowByAgent = useMemo(() => {
    const m = new Map<string, { top: number; height: number }>();
    for (const r of rows) m.set(r.agentId, { top: r.top, height: r.height });
    return m;
  }, [rows]);

  const viewport = renderer.getViewport();
  const vs = viewportStart(viewport);
  const ve = viewport.endMs;

  const retry = (a: Annotation) => {
    if (!a.error || !a.spanId) return;
    useAnnotationStore.getState().remove(a.sessionId, a.id);
    void post({
      sessionId: a.sessionId,
      spanId: a.spanId,
      body: a.body,
      kind: a.kind,
      author: a.author,
    }).catch(() => {});
  };

  return (
    <div
      aria-hidden="true"
      data-testid="pin-strip"
      style={{
        position: 'absolute',
        inset: 0,
        width: widthCss,
        height: heightCss,
        pointerEvents: 'none',
        zIndex: 6,
      }}
    >
      {annotations.map((a) => {
        const agentId = resolveAgentId(a, store);
        if (!agentId) return null;
        const row = rowByAgent.get(agentId);
        if (!row) return null;
        if (a.atMs < vs - viewport.windowMs || a.atMs > ve) return null;

        const x = msToPx(viewport, widthCss, a.atMs);
        if (x < GUTTER_WIDTH_PX - 6 || x > widthCss + 6) return null;

        const rangeEnd =
          a.rangeEndMs != null
            ? Math.min(widthCss, msToPx(viewport, widthCss, a.rangeEndMs))
            : null;

        return (
          <Pin
            key={a.id}
            ann={a}
            x={x}
            rangeEndX={rangeEnd}
            rowTop={row.top}
            onClick={() => a.spanId && selectSpan(a.spanId)}
            onRetry={() => retry(a)}
          />
        );
      })}
    </div>
  );
}

function resolveAgentId(
  a: Annotation,
  store: OverlayContext['store'],
): string | null {
  if (a.agentId) return a.agentId;
  if (a.spanId) {
    const s = store.spans.get(a.spanId);
    if (s) return s.agentId;
  }
  return null;
}

function Pin({
  ann,
  x,
  rangeEndX,
  rowTop,
  onClick,
  onRetry,
}: {
  ann: Annotation;
  x: number;
  rangeEndX: number | null;
  rowTop: number;
  onClick: () => void;
  onRetry: () => void;
}) {
  const color =
    ann.kind === 'STEERING'
      ? 'var(--md-sys-color-tertiary, #7dd3c0)'
      : ann.kind === 'HUMAN_RESPONSE'
        ? 'var(--md-sys-color-secondary, #b9cbb4)'
        : 'var(--md-sys-color-primary, #a8c8ff)';

  const ackGlyph = ann.error
    ? '!'
    : ann.pending
      ? '…'
      : ann.deliveredAtMs != null
        ? '✓'
        : '';

  const title = ann.error
    ? `Failed to deliver: ${ann.error}\nClick to retry.`
    : `${ann.kind.toLowerCase()} by ${ann.author}\n${ann.body}`;

  const handle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (ann.error) onRetry();
    else onClick();
  };

  return (
    <>
      {rangeEndX != null && rangeEndX > x + 1 && (
        <div
          style={{
            position: 'absolute',
            left: x,
            top: rowTop + 2,
            width: rangeEndX - x,
            height: PIN_STRIP_HEIGHT - 2,
            background: color,
            opacity: 0.2,
            borderRadius: 2,
            pointerEvents: 'none',
          }}
        />
      )}
      <button
        type="button"
        data-testid="pin"
        data-annotation-id={ann.id}
        title={title}
        onClick={handle}
        style={{
          position: 'absolute',
          left: x - 6,
          top: rowTop - 2,
          width: 12,
          height: PIN_STRIP_HEIGHT,
          padding: 0,
          margin: 0,
          border: 'none',
          background: 'transparent',
          cursor: 'pointer',
          pointerEvents: 'auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 9,
          color: '#fff',
          fontWeight: 700,
        }}
      >
        <span
          style={{
            display: 'inline-block',
            width: 10,
            height: 10,
            borderRadius: '50% 50% 50% 0',
            transform: 'rotate(-45deg)',
            background: ann.error
              ? 'var(--md-sys-color-error, #ffb4ab)'
              : color,
            boxShadow: '0 1px 3px rgba(0,0,0,0.5)',
            opacity: ann.pending ? 0.6 : 1,
            position: 'relative',
          }}
        />
        {ackGlyph && (
          <span
            style={{
              position: 'absolute',
              left: 10,
              top: -1,
              fontSize: 9,
              color: ann.error
                ? 'var(--md-sys-color-error, #ffb4ab)'
                : 'var(--md-sys-color-on-surface, #e2e2e9)',
              background: 'var(--md-sys-color-surface, #10131a)',
              borderRadius: 4,
              padding: '0 2px',
              lineHeight: '10px',
            }}
          >
            {ackGlyph}
          </span>
        )}
      </button>
    </>
  );
}
