// ZicatoConsole.tsx — the parallel zicato-language console shell. Mirrors
// compose.html appHybrid (742-745): topbar + strip + (rail · main · inspector)
// + transport, with the ⌘K picker overlaid. Lives BESIDE the MD3 Shell; App.tsx
// renders this when uiMode === 'zicato'.
//
// Because only ONE console mounts at a time, this console independently mounts
// the headless pieces it needs (<SessionsSyncer/>, useGlobalShortcuts()) — they
// are NOT inherited from Shell. It holds the single useSessionWatch for the
// current session; sub-views read the store via getSessionStore.
//
// Theme: scoped to the zicato subtree via `data-zicato-theme` on .zk-root, so
// the MD3 `<html data-theme>` is never touched (no MD3 regression).

import {
  useEffect,
  useReducer,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactElement,
} from 'react';
import './zicato-tokens.css';
import './zicato.css';
import { useUiStore, type ZicatoTheme } from '../../state/uiStore';
import {
  useSessionWatch,
  getSessionStore,
  useSendControl,
} from '../../rpc/hooks';
import { SessionsSyncer } from '../../rpc/SessionsSyncer';
import { SessionPicker } from '../SessionPicker/SessionPicker';
import { useGlobalShortcuts } from '../../lib/shortcuts';
import { formatDuration } from '../../lib/format';
import { bareAgentName } from '../../gantt/index';
import type { Span } from '../../gantt/types';
import { hasThinking } from '../../lib/thinking';
import { useReasoningText } from './useReasoningText';
import { actorDisplayLabel } from '../../theme/agentColors';
import {
  isGoldfiveSpan,
  resolveGoldfiveSpanInfo,
  truncatePreview,
} from '../../lib/goldfiveSpan';
import { isJudgeSpan, resolveJudgeDetail } from '../../lib/interventionDetail';
import {
  useZicatoSession,
  toStatusToken,
  gfClassForSpan,
  type ZSession,
  type ZSteer,
} from './adapter';
import { BrandMark, Wordmark } from './Brand';
import { GanttViewZ } from './GanttViewZ';
import { InstrumentsViewZ } from './InstrumentsViewZ';
import { FloatingDrawerZ, SteeringDetailBodyZ } from './FloatingDrawerZ';
import { SteerSelectContext } from './steerContext';
import { SpanHoverContext } from './hoverContext';
import {
  SpanHovercardZ,
  useHoverController,
  displayedSpanId,
} from './SpanHovercardZ';

// ── Theme picker data tables (ported from compose.html 99-123) ───────────────

const ZICATO_THEME_IDS: ZicatoTheme[] = [
  'monokai',
  'solarized-dark',
  'solarized-light',
  'google-light',
  'google-dark',
  'lunaria-light',
  'lunaria-eclipse',
  'belafonte-day',
  'belafonte-night',
  'paper',
  'zenburn',
  'selenized-black',
  'relaxed',
  'espresso',
  'dracula',
  'ubuntu',
];

const THEME_SWATCHES: Record<ZicatoTheme, string[]> = {
  monokai: ['#1e1f1c', '#272822', '#f8f8f2', '#a6e22e', '#f92672', '#66d9ef'],
  'solarized-dark': ['#04222B', '#0A2D38', '#93A1A1', '#8BB80E', '#E0483C', '#2AA198'],
  'solarized-light': ['#FDF6E3', '#FBF1D6', '#586E75', '#6B9B0B', '#DC322F', '#268BD2'],
  'google-light': ['#FFFFFF', '#F4F4F4', '#474A4E', '#34A853', '#EA4335', '#1B9CB8'],
  'google-dark': ['#202124', '#2C2D30', '#FFFFFF', '#34A853', '#EA4335', '#24C1E0'],
  'lunaria-light': ['#EBE4E1', '#E2DCD9', '#363434', '#497D46', '#783C1F', '#3778A9'],
  'lunaria-eclipse': ['#323F46', '#3B484F', '#DFE2ED', '#BEDBC1', '#BA9088', '#C8429F'],
  'belafonte-day': ['#D5CCBA', '#CCC3B2', '#34292D', '#6E6A4E', '#BE100E', '#426A79'],
  'belafonte-night': ['#20111B', '#271821', '#D5CCBA', '#A6A07A', '#D6403E', '#6F8E97'],
  paper: ['#F2EEDE', '#E6E2D3', '#1A1A1A', '#216609', '#CC3E28', '#1E6FCC'],
  zenburn: ['#3A3A3A', '#424241', '#DCDCCC', '#8FB28F', '#CC9393', '#8CD0D3'],
  'selenized-black': ['#181818', '#202020', '#DEDEDE', '#83C746', '#FF5E56', '#56D8C9'],
  relaxed: ['#353A44', '#3D424B', '#F7F7F7', '#A0AC77', '#BC5653', '#7EAAC7'],
  espresso: ['#323232', '#3A3A3A', '#FFFFFF', '#A5C261', '#D25252', '#6C99BB'],
  dracula: ['#282A36', '#343746', '#F8F8F2', '#50FA7B', '#FF5555', '#BD93F9'],
  ubuntu: ['#300A24', '#3D1530', '#EEEEEC', '#8AE234', '#CC0000', '#34E2E2'],
};

const THEME_LABELS: Record<ZicatoTheme, string> = {
  monokai: 'monokai',
  'solarized-dark': 'solarized dark',
  'solarized-light': 'solarized light',
  'google-light': 'google light',
  'google-dark': 'google dark',
  'lunaria-light': 'lunaria light',
  'lunaria-eclipse': 'lunaria eclipse',
  'belafonte-day': 'belafonte day',
  'belafonte-night': 'belafonte night',
  paper: 'paper',
  zenburn: 'zenburn',
  'selenized-black': 'selenized black',
  relaxed: 'relaxed',
  espresso: 'espresso',
  dracula: 'dracula',
  ubuntu: 'ubuntu',
};

function SwatchStrip({ id }: { id: ZicatoTheme }) {
  return (
    <span className="dt-swatch-strip">
      {THEME_SWATCHES[id].map((c, i) => (
        <span key={i} className="dt-swatch" style={{ background: c }} />
      ))}
    </span>
  );
}

// ── Theme picker (port of compose.html buildPicker/swatchStrip 98-140) ───────

function ZicatoThemePicker() {
  const theme = useUiStore((s) => s.zicatoTheme);
  const setTheme = useUiStore((s) => s.setZicatoTheme);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onDocClick = () => setOpen(false);
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  }, [open]);

  return (
    <div className={`dt-cd${open ? ' dt-cd-open' : ''}`}>
      <button
        className="dt-cd-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <SwatchStrip id={theme} />
        <span className="dt-cd-name">{THEME_LABELS[theme]}</span>
        <span className="dt-cd-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      <div className="dt-cd-list" role="listbox" aria-label="theme">
        {ZICATO_THEME_IDS.map((id) => (
          <button
            key={id}
            className="dt-cd-option"
            role="option"
            aria-selected={id === theme}
            onClick={(e) => {
              e.stopPropagation();
              setTheme(id);
              setOpen(false);
            }}
          >
            <SwatchStrip id={id} />
            <span className="dt-cd-name">{THEME_LABELS[id]}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Topbar (port of topbarHTML 623-633 + md3 toggle) ─────────────────────────

function ZicatoTopbar({
  z,
  onToMd3,
}: {
  z: ZSession;
  onToMd3: () => void;
}) {
  const openPicker = useUiStore((s) => s.openSessionPicker);
  const inFlight = z.spans.filter(
    (x) => x.status === 'running' || x.status === 'awaiting',
  ).length;
  const running = z.status === 'live';
  return (
    <div className="hg-topbar">
      <span className="hg-brand">
        <BrandMark height={20} />
        <Wordmark />
      </span>
      <span className="hg-crumbs">
        <span className="hg-crumb">~/ws</span>
        <span className="hg-crumb-sep">▸</span>
        <button className="hg-crumb hg-crumb-current" onClick={openPicker}>
          {z.id || 'select session'} ▾
        </button>
        <span className="hg-picker-kbd">⌘K</span>
      </span>
      <span className="hg-topbar-spacer" />
      <span className={`hg-status ${running ? 'is-running' : 'is-connected'}`}>
        <span className="hg-status-dot" />
        <span>{running ? 'streaming' : 'connected'}</span>
        <span className="hg-run-badge">
          <span className="hg-run-pulse" />
          <span className="hg-run-label">{z.agents.length} agents</span>
          <span>· {inFlight} in flight</span>
        </span>
      </span>
      <ZicatoThemePicker />
      <button
        className="zk-iconbtn"
        onClick={onToMd3}
        aria-label="Switch to Material console"
        title="md3 console"
        data-testid="ui-mode-toggle-z"
      >
        ▤
      </button>
    </div>
  );
}

// ── Strip (port of stripHTML 635-639) ────────────────────────────────────────

function statusPill(z: ZSession) {
  if (z.status === 'live') return <span className="dn-pill live">● live</span>;
  if (z.status === 'done') return <span className="dn-pill good">✓ done</span>;
  return <span className="dn-pill bad">✕ failed</span>;
}

function ZicatoStrip({ z }: { z: ZSession }) {
  const run =
    z.spans.find((x) => x.status === 'running') ??
    z.spans.find((x) => x.status === 'awaiting');
  return (
    <div className="hg-strip">
      <span className="hg-strip-label">goal</span>
      <span className="hg-strip-title">{z.goal || '—'}</span>
      {run && (
        <>
          <span className="hg-strip-agent">
            {bareAgentName(run.agent) || run.agent}
          </span>
          <span className={`dn-pill ${run.status === 'awaiting' ? 'caution' : 'accent'}`}>
            {run.status === 'awaiting' ? '◷ awaiting human' : `● ${run.label}`}
          </span>
        </>
      )}
      <span style={{ flex: 1 }} />
      {statusPill(z)}
    </div>
  );
}

// ── Rail (port of railHTML 737-741) ──────────────────────────────────────────

type ZicatoView = 'gantt' | 'instruments';

function ZicatoRail({
  view,
  onView,
}: {
  view: ZicatoView;
  onView: (v: ZicatoView) => void;
}) {
  const items: [ZicatoView, string][] = [
    ['gantt', '▤'],
    ['instruments', '⧉'],
  ];
  return (
    <nav className="hg-rail">
      {items.map(([v, ic]) => (
        <button
          key={v}
          className="hg-rail-item"
          aria-selected={view === v}
          onClick={() => onView(v)}
        >
          <span className="hg-rail-icon">{ic}</span>
          <span>{v}</span>
        </button>
      ))}
    </nav>
  );
}

// ── Transport (reuse of TransportBar wiring 6-66, restyled in-language) ──────

function ZicatoTransport() {
  const liveFollow = useUiStore((s) => s.liveFollow);
  const jumpToLive = useUiStore((s) => s.jumpToLive);
  const agentsPaused = useUiStore((s) => s.agentsPaused);
  const setAgentsPaused = useUiStore((s) => s.setAgentsPaused);
  const sessionId = useUiStore((s) => s.currentSessionId);
  const send = useSendControl();
  const watch = useSessionWatch(sessionId);
  const agents = watch.store.agents.list;

  const [tick, setTick] = useState(0);
  const [prevSessionId, setPrevSessionId] = useState(sessionId);
  if (prevSessionId !== sessionId) {
    setPrevSessionId(sessionId);
    setTick(0);
  }
  useEffect(() => {
    if (!sessionId) return;
    const i = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(i);
  }, [sessionId]);
  const elapsedSeconds = sessionId ? tick : 0;

  const handlePause = async () => {
    if (!sessionId) return;
    setAgentsPaused(true);
    for (const a of agents) {
      await send({ sessionId, agentId: a.id, kind: 'PAUSE' }).catch(() => {});
    }
  };
  const handleResume = async () => {
    if (!sessionId) return;
    setAgentsPaused(false);
    jumpToLive();
    for (const a of agents) {
      await send({ sessionId, agentId: a.id, kind: 'RESUME' }).catch(() => {});
    }
  };

  return (
    <footer
      className="hg-transport"
      role="toolbar"
      aria-label="Transport controls"
      data-testid="zicato-transport"
    >
      <span className="hg-transport-group">
        <button
          className="hg-transport-btn"
          disabled={!sessionId}
          title="jump to start"
        >
          ⏮
        </button>
        {agentsPaused ? (
          <button
            className="hg-transport-btn"
            onClick={handleResume}
            disabled={!sessionId}
            title="resume agents"
          >
            ▶ resume
          </button>
        ) : (
          <button
            className="hg-transport-btn"
            onClick={handlePause}
            disabled={!sessionId}
            title="pause agents"
          >
            ⏸ pause
          </button>
        )}
        <button
          className="hg-transport-btn"
          onClick={jumpToLive}
          disabled={!sessionId}
          title="jump to now"
        >
          ⏭
        </button>
      </span>
      <span className="hg-transport-divider" />
      {agentsPaused ? (
        <span className="dn-pill flat">paused</span>
      ) : liveFollow && sessionId ? (
        <span className="hg-live-badge">
          <span className="hg-live-dot" aria-hidden="true" />
          LIVE
        </span>
      ) : (
        <span className="dn-pill flat">viewport locked</span>
      )}
      <span className="hg-clock tnum">
        {sessionId ? formatDuration(elapsedSeconds) : '—'}
      </span>
      <span className="hg-transport-spacer" />
      <span className="zk-prop-note">zoom: fit</span>
    </footer>
  );
}

// ── Inspector (mirror of drawerHTML 652-681, zicato-styled) ──────────────────

function escapeJson(s: string): string {
  return s.replace(/"/g, '\\"');
}

// Compact goldfive / judge detail for the docked inspector. For a JUDGE span it
// shows the verdict pill + the judge's decision summary + the chain-of-thought
// it saw; for a non-judge goldfive span (refine / goal_derive / plan / …) it
// shows the decision summary, target, and input/output previews. Zicato-styled
// (dn-pill / hg-attrs / zk-reasoning), the in-language analogue of MD3's
// JudgeInvocationDetail + GoldfiveSpanDetail.
function SpanGoldfiveJudgeZ({ span }: { span: Span }): ReactElement | null {
  const judge = isJudgeSpan(span);
  if (judge) {
    const d = resolveJudgeDetail(span, []);
    const verdict =
      d.verdictBucket === 'on_task'
        ? 'on task'
        : d.verdictBucket === 'no_verdict'
          ? 'no verdict'
          : d.severity
            ? `off task · ${d.severity}`
            : 'off task';
    const pill =
      d.verdictBucket === 'on_task'
        ? 'good'
        : d.verdictBucket === 'no_verdict'
          ? 'flat'
          : d.severity === 'critical'
            ? 'bad'
            : 'caution';
    return (
      <section className="zk-detail-section" data-testid="zk-inspector-judge" style={{ marginTop: 14 }}>
        <h3 className="zk-detail-h">judge verdict</h3>
        <span className={`dn-pill ${pill}`}>{verdict}</span>
        {d.decisionSummary && <p className="zk-detail-text">{d.decisionSummary}</p>}
        {d.reasoningInput && (
          <pre className="zk-reasoning" data-testid="zk-inspector-judge-reasoning">
            {truncatePreview(d.reasoningInput, 800)}
          </pre>
        )}
      </section>
    );
  }
  if (!isGoldfiveSpan(span)) return null;
  const info = resolveGoldfiveSpanInfo(span);
  return (
    <section className="zk-detail-section" data-testid="zk-inspector-goldfive" style={{ marginTop: 14 }}>
      <h3 className="zk-detail-h">decision</h3>
      {info.decisionSummary && <p className="zk-detail-text">{info.decisionSummary}</p>}
      {(info.targetAgentId || info.targetTaskId) && (
        <dl className="hg-attrs">
          {info.targetAgentId && (
            <>
              <dt>target</dt>
              <dd>{info.targetAgentId}</dd>
            </>
          )}
          {info.targetTaskId && (
            <>
              <dt>task</dt>
              <dd><code>{info.targetTaskId}</code></dd>
            </>
          )}
        </dl>
      )}
      {info.inputPreview && (
        <>
          <h4 className="zk-detail-sub">input</h4>
          <pre className="zk-reasoning">{truncatePreview(info.inputPreview, 800)}</pre>
        </>
      )}
      {info.outputPreview && (
        <>
          <h4 className="zk-detail-sub">output</h4>
          <pre className="zk-reasoning">{truncatePreview(info.outputPreview, 800)}</pre>
        </>
      )}
    </section>
  );
}

function ZicatoInspector() {
  const drawerOpen = useUiStore((s) => s.drawerOpen);
  const selectedSpanId = useUiStore((s) => s.selectedSpanId);
  const closeDrawer = useUiStore((s) => s.closeDrawer);
  const sessionId = useUiStore((s) => s.currentSessionId);
  const store = getSessionStore(sessionId);
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.spans.subscribe(() => bump());
  }, [store]);

  const span = selectedSpanId ? store?.spans.get(selectedSpanId) : undefined;

  // Resolve the reasoning text up front (hooks can't be conditional). Inline
  // reasoning returns immediately; a large trace that spilled to a payload_ref
  // (role: 'reasoning') is fetched + decoded here so the inspector renders the
  // real chain-of-thought, not the "captured in a payload reference" stub.
  const reasoning = useReasoningText(span ?? null);

  let body: ReactElement;
  if (span) {
    const status = toStatusToken(span.status);
    const gf = gfClassForSpan(span);
    // Reasoning detection mirrors the adapter (lib/thinking). The docked
    // inspector renders the full 🧠 chain-of-thought inline (no redundant
    // "open in drawer" hop — the inspector IS the drawer). The text itself
    // (inline or payload-backed) was resolved by useReasoningText above; gate
    // the section on the reasoning flag OR a pending/resolved fetch.
    const spanHasReasoning =
      hasThinking(span) || reasoning.text != null || reasoning.loading;
    const t0 = span.startMs / 1000;
    const t1 = span.endMs != null ? span.endMs / 1000 : t0;
    const statusClass =
      status === 'failed'
        ? 'bad'
        : status === 'running'
          ? 'accent'
          : status === 'awaiting'
            ? 'caution'
            : 'good';
    const agentLabel =
      actorDisplayLabel(span.agentId) ?? bareAgentName(span.agentId) ?? span.agentId;
    body = (
      <>
        <div
          style={{
            display: 'flex',
            gap: 6,
            flexWrap: 'wrap',
            marginBottom: 10,
          }}
        >
          <span className="dn-pill flat">{span.kind}</span>
          <span className={`dn-pill ${statusClass}`}>{status}</span>
        </div>
        <dl className="hg-attrs">
          <dt>agent</dt>
          <dd>{agentLabel}</dd>
          <dt>label</dt>
          <dd>{span.name}</dd>
          <dt>start</dt>
          <dd className="tnum">{t0.toFixed(1)}s</dd>
          <dt>end</dt>
          <dd className="tnum">{span.endMs != null ? `${t1.toFixed(1)}s` : '—'}</dd>
          <dt>duration</dt>
          <dd className="tnum">{(t1 - t0).toFixed(1)}s</dd>
          {gf && (
            <>
              <dt>gf category</dt>
              <dd>{gf}</dd>
            </>
          )}
        </dl>
        <SpanGoldfiveJudgeZ span={span} />
        <h3
          style={{
            fontSize: 10,
            textTransform: 'uppercase',
            letterSpacing: '.1em',
            color: 'var(--ink-faint)',
            margin: '14px 0 6px',
          }}
        >
          payload
        </h3>
        <code className="hg-drawer-code">
          <span className="hg-j-punct">{'{ '}</span>
          <span className="hg-j-key">"span_id"</span>
          <span className="hg-j-punct">: </span>
          <span className="hg-j-str">"{escapeJson(span.id)}"</span>
          <span className="hg-j-punct">,</span>
          {'\n  '}
          <span className="hg-j-key">"kind"</span>
          <span className="hg-j-punct">: </span>
          <span className="hg-j-str">"{span.kind}"</span>
          <span className="hg-j-punct">,</span>
          {'\n  '}
          <span className="hg-j-key">"status"</span>
          <span className="hg-j-punct">: </span>
          <span className="hg-j-str">"{span.status}"</span>
          <span className="hg-j-punct"> {'}'}</span>
        </code>
        {spanHasReasoning && (
          <section
            className="zk-detail-section"
            data-testid="zk-inspector-reasoning"
            style={{ marginTop: 14 }}
          >
            <div className="zk-reasoning-head" style={{ marginBottom: 6 }}>
              <span className="zk-reasoning-glyph" aria-hidden="true">
                🧠
              </span>
              <h3
                style={{
                  fontSize: 10,
                  textTransform: 'uppercase',
                  letterSpacing: '.1em',
                  color: 'var(--ink-faint)',
                  margin: 0,
                }}
              >
                reasoning
              </h3>
            </div>
            {reasoning.text ? (
              <pre className="zk-reasoning" data-testid="zk-inspector-reasoning-text">
                {reasoning.text}
              </pre>
            ) : reasoning.loading ? (
              <div className="zk-reasoning-empty">loading reasoning…</div>
            ) : (
              <div className="zk-reasoning-empty">reasoning not available</div>
            )}
          </section>
        )}
      </>
    );
  } else {
    body = (
      <p style={{ fontSize: 10, color: 'var(--ink-faint)' }}>
        click any span for its detail · Esc closes
      </p>
    );
  }

  return (
    <div className={`zk-drawer-host ${drawerOpen ? 'open' : ''}`}>
      <div className="hg-drawer">
        <div className="hg-drawer-header">
          <span className="hg-drawer-title">{span ? span.name : 'session'}</span>
          <button
            className="hg-drawer-close"
            onClick={closeDrawer}
            title="close"
            aria-label="Close inspector"
          >
            ×
          </button>
        </div>
        <div className="hg-drawer-body">{body}</div>
      </div>
    </div>
  );
}

// ── The console shell ────────────────────────────────────────────────────────

// Floating-drawer content: a steering detail (the docked inspector renders
// reasoning inline, so the floating drawer is steering-only now). Kept typed at
// the boundary so the drawer body never re-derives the selection.
type ZDrawerContent = { type: 'steering'; steer: ZSteer };

export function ZicatoConsole() {
  useGlobalShortcuts(); // ⌘K / Esc / j-k — owned here (not inherited from Shell)

  const sessionId = useUiStore((s) => s.currentSessionId);
  const setUiMode = useUiStore((s) => s.setUiMode);
  const zicatoTheme = useUiStore((s) => s.zicatoTheme);

  // Keep the stream alive for all sub-views + inspector (single watch).
  useSessionWatch(sessionId);

  const [view, setView] = useState<ZicatoView>('gantt'); // local; map-shell §6
  const z = useZicatoSession(sessionId);

  // Span quick-look hovercard: GanttZ reports the hovered span (id + on-screen
  // rect) through SpanHoverContext; the controller debounces leave with a grace
  // delay. We resolve the live Span from the store and anchor the card inside
  // the (position:relative) .zk-app-body via its measured rect.
  //
  // PINNING: when a span is SELECTED (the user clicked it → drawer open) its
  // hovercard pins — it wins over the transient hover and stays visible until
  // the span is deselected (closeDrawer / Esc). The pinned span's anchor is the
  // hover rect when it happens to be hovered, otherwise a rect measured from the
  // DOM (the span's <rect> may not be under the pointer). With no selection we
  // keep the original transient hover behaviour exactly.
  const hover = useHoverController();
  const selectedSpanId = useUiStore((s) => s.selectedSpanId);
  const appBodyRef = useRef<HTMLDivElement | null>(null);
  const hoverStore = getSessionStore(sessionId);

  // The span the card should show = selection (pinned) ?? hover (transient).
  const cardSpanId = displayedSpanId(selectedSpanId, hover.hovered);

  // DOM-measured anchor for the pinned (selected) span. Refreshed whenever the
  // selection changes or that same span is (re)hovered, so a pan/zoom that moves
  // the bar keeps the card glued to it.
  const [pinnedRect, setPinnedRect] = useState<DOMRect | null>(null);
  const hoveredIsSelected =
    selectedSpanId != null && hover.hovered?.spanId === selectedSpanId;
  useEffect(() => {
    if (selectedSpanId == null) {
      setPinnedRect(null);
      return;
    }
    const el = appBodyRef.current?.querySelector(
      `[data-span="${selectedSpanId}"]`,
    );
    setPinnedRect(el ? el.getBoundingClientRect() : null);
    // `view` is a dep so the rect is re-measured when returning to the gantt
    // (the bar isn't in the DOM while the instruments view is showing).
  }, [selectedSpanId, hoveredIsSelected, hoverStore, view]);

  // Resolve the displayed Span from the live store (as before).
  const hoverSpan = cardSpanId ? hoverStore?.spans.get(cardSpanId) : undefined;

  // Anchor: a hovered span uses the live hover rect; a pinned-only span uses the
  // DOM-measured rect. Prefer the hover rect when it points at the same span.
  const cardAnchor: DOMRect | null =
    hover.hovered?.spanId === cardSpanId
      ? hover.hovered.rect
      : selectedSpanId != null && cardSpanId === selectedSpanId
        ? pinnedRect
        : null;

  // Leaving the gantt view drops the hovercard: it is anchored to gantt span
  // <rect>s, so in the instruments view it would otherwise linger at a stale
  // position with no bar left to hover off (and no way to dismiss it).
  useEffect(() => {
    if (view !== 'gantt') hover.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // Click-away to deselect: a click anywhere in the body that is NOT a span, the
  // inspector drawer, or an interactive control clears the selection (closes the
  // drawer + releases the pinned hovercard) — so you needn't find and re-click
  // the exact bar to unselect it.
  const onAppBodyClick = (e: ReactMouseEvent<HTMLDivElement>): void => {
    if (!useUiStore.getState().selectedSpanId) return;
    const t = e.target as Element | null;
    if (
      t?.closest(
        '[data-span],.zk-drawer-host,button,[role="button"],.zk-minimap,.zk-gantt-resize,input,select,textarea,a',
      )
    ) {
      return;
    }
    useUiStore.getState().selectSpan(null);
  };

  // Floating-drawer state: a steering-arrow click (via SteerSelectProvider)
  // lands a typed content union here. The drawer overlays the main area and
  // unmounts when closed.
  const [drawerContent, setDrawerContent] = useState<ZDrawerContent | null>(null);
  const openSteerDrawer = (steer: ZSteer): void =>
    setDrawerContent({ type: 'steering', steer });
  const closeFloatingDrawer = (): void => setDrawerContent(null);

  const drawerTitle =
    drawerContent?.type === 'steering' ? 'steering detail' : undefined;

  return (
    <div className="zk-root" data-zicato-theme={zicatoTheme} data-testid="zicato-console">
      <SessionsSyncer />
      <ZicatoTopbar z={z} onToMd3={() => setUiMode('md3')} />
      <ZicatoStrip z={z} />
      <SteerSelectContext.Provider value={openSteerDrawer}>
        <SpanHoverContext.Provider value={{ report: hover.report, clear: hover.clear }}>
          <div
            className="zk-app-body"
            ref={appBodyRef}
            onClick={onAppBodyClick}
            style={{ position: 'relative' }}
          >
            <ZicatoRail view={view} onView={setView} />
            <main className="zk-main" data-view={view}>
              {view === 'gantt' && <GanttViewZ z={z} />}
              {view === 'instruments' && <InstrumentsViewZ z={z} />}
            </main>
            <ZicatoInspector />
            <FloatingDrawerZ
              open={drawerContent !== null}
              onClose={closeFloatingDrawer}
              title={drawerTitle}
              testId="zk-floating-drawer"
            >
              {drawerContent?.type === 'steering' && (
                <SteeringDetailBodyZ steer={drawerContent.steer} z={z} />
              )}
            </FloatingDrawerZ>
            {view === 'gantt' && hoverSpan && cardAnchor && appBodyRef.current && (
              <SpanHovercardZ
                span={hoverSpan}
                anchor={cardAnchor}
                containerRect={appBodyRef.current.getBoundingClientRect()}
              />
            )}
          </div>
        </SpanHoverContext.Provider>
      </SteerSelectContext.Provider>
      <ZicatoTransport />
      <SessionPicker />
    </div>
  );
}
