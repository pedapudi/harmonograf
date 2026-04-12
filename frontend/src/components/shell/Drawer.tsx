import { useEffect, useMemo, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import {
  getSessionStore,
  usePayload,
  usePostAnnotation,
  useSendControl,
  sendStatusQuery,
} from '../../rpc/hooks';
import type {
  Span,
  AttributeValue,
  PayloadRef,
  SpanLink,
  LinkRelation,
} from '../../gantt/types';
import { formatDuration } from '../../lib/format';

type TabId =
  | 'summary'
  | 'payload'
  | 'timeline'
  | 'links'
  | 'annotations'
  | 'control';

const TABS: { id: TabId; label: string; testId: string }[] = [
  { id: 'summary', label: 'Summary', testId: 'inspector-tab-overview' },
  { id: 'payload', label: 'Payload', testId: 'inspector-tab-payload' },
  { id: 'timeline', label: 'Timeline', testId: 'inspector-tab-timeline' },
  { id: 'links', label: 'Links', testId: 'inspector-tab-links' },
  { id: 'annotations', label: 'Annotations', testId: 'inspector-tab-annotations' },
  { id: 'control', label: 'Control', testId: 'inspector-tab-control' },
];

export function Drawer() {
  const open = useUiStore((s) => s.drawerOpen);
  const selected = useUiStore((s) => s.selectedSpanId);
  const close = useUiStore((s) => s.closeDrawer);
  const sessionId = useUiStore((s) => s.currentSessionId);

  // Look up the span from whichever SessionStore the rpc hooks currently hold
  // for this session. Re-run when the selection changes so we capture newly
  // arrived spans, but we accept that mid-drawer mutations (attribute updates
  // after the drawer is open) require a close/reopen to surface — the design
  // doc calls the drawer a modal side-sheet, not a live-updating pane.
  const span = useMemo(() => {
    if (!sessionId || !selected) return null;
    const store = getSessionStore(sessionId);
    return store?.spans.get(selected) ?? null;
  }, [sessionId, selected]);

  return (
    <aside
      className={`hg-drawer${open ? ' hg-drawer--open' : ''}`}
      aria-hidden={!open}
      data-testid="inspector-drawer"
    >
      <div className="hg-drawer__inner">
        <div className="hg-drawer__header">
          <div className="hg-drawer__title" data-testid="inspector-span-name">
            {span ? `${span.kind} · ${span.name}` : (selected ?? 'Inspector')}
          </div>
          <button
            className="hg-appbar__icon-btn"
            onClick={close}
            aria-label="Close drawer"
          >
            ✕
          </button>
        </div>
        {span ? (
          <DrawerTabs key={span.id} span={span} sessionId={sessionId} />
        ) : (
          <div className="hg-drawer__body">
            <p>Select a span on the Gantt to inspect it.</p>
          </div>
        )}
      </div>
    </aside>
  );
}

function DrawerTabs({ span, sessionId }: { span: Span; sessionId: string | null }) {
  const [tab, setTab] = useState<TabId>('summary');
  return (
    <>
      <div className="hg-drawer__tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`hg-drawer__tab${tab === t.id ? ' hg-drawer__tab--active' : ''}`}
            data-testid={t.testId}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="hg-drawer__body">
        {tab === 'summary' && <SummaryTab span={span} sessionId={sessionId} />}
        {tab === 'payload' && <PayloadTab span={span} />}
        {tab === 'timeline' && <TimelineTab span={span} sessionId={sessionId} />}
        {tab === 'links' && <LinksTab span={span} />}
        {tab === 'annotations' && sessionId && (
          <AnnotationsTab span={span} sessionId={sessionId} />
        )}
        {tab === 'control' && sessionId && (
          <ControlTab span={span} sessionId={sessionId} />
        )}
      </div>
    </>
  );
}

// --- Summary tab ------------------------------------------------------------

function SummaryTab({ span, sessionId }: { span: Span; sessionId: string | null }) {
  const durationMs =
    span.endMs !== null ? span.endMs - span.startMs : null;
  const entries = Object.entries(span.attributes);
  const agent = sessionId ? getSessionStore(sessionId)?.agents.get(span.agentId) : undefined;
  const [asking, setAsking] = useState(false);

  const handleAsk = async () => {
    if (!sessionId || asking) return;
    setAsking(true);
    await sendStatusQuery(sessionId, span.agentId).catch(() => {});
    setAsking(false);
  };

  return (
    <div className="hg-drawer__section">
      {agent?.taskReport && (
        <div className="hg-drawer__section hg-drawer__section--task">
          <div className="hg-drawer__section-header">
            <h4 className="hg-drawer__section-label">Current Task</h4>
            <button
              className="hg-drawer__ask-btn"
              onClick={handleAsk}
              disabled={asking}
              title="Ask agent what it's working on"
            >
              {asking ? 'Asking…' : 'Ask ?'}
            </button>
          </div>
          <p className="hg-drawer__task-report">{agent.taskReport}</p>
        </div>
      )}
      {!agent?.taskReport && (
        <div className="hg-drawer__section hg-drawer__section--task">
          <div className="hg-drawer__section-header">
            <h4 className="hg-drawer__section-label">Current Task</h4>
            <button
              className="hg-drawer__ask-btn"
              onClick={handleAsk}
              disabled={asking}
              title="Ask agent what it's working on"
            >
              {asking ? 'Asking…' : 'Ask ?'}
            </button>
          </div>
          <p className="hg-drawer__dim">No task report yet.</p>
        </div>
      )}
      <dl className="hg-drawer__meta">
        <dt>Status</dt>
        <dd>{span.status}</dd>
        <dt>Agent</dt>
        <dd><code>{span.agentId}</code></dd>
        <dt>Span ID</dt>
        <dd><code>{span.id}</code></dd>
        {span.parentSpanId && (
          <>
            <dt>Parent</dt>
            <dd><code>{span.parentSpanId}</code></dd>
          </>
        )}
        <dt>Duration</dt>
        <dd>
          {durationMs === null
            ? 'running'
            : formatDuration(Math.max(0, Math.round(durationMs / 1000)))}
        </dd>
      </dl>
      {span.error && (
        <div className="hg-drawer__error">
          <strong>{span.error.type || 'Error'}:</strong> {span.error.message}
          {span.error.stack && (
            <pre className="hg-drawer__code hg-drawer__code--error">
              {span.error.stack}
            </pre>
          )}
        </div>
      )}
      {entries.length > 0 && (
        <>
          <h3>Attributes</h3>
          <table className="hg-drawer__attrs">
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k}>
                  <th>{k}</th>
                  <td>
                    <AttrValue value={v} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// Render an attribute value. Strings that parse as JSON (objects, arrays,
// primitives) are pretty-printed with syntax highlighting; everything else
// falls back to plain text. This is the single interesting path for the
// common ADK shape where tool.args / tool.result arrive as JSON strings.
function AttrValue({ value }: { value: AttributeValue }) {
  if (value.kind === 'string') {
    const parsed = tryParseJson(value.value);
    if (parsed !== NOT_JSON) {
      const pretty = JSON.stringify(parsed, null, 2);
      return <JsonCode text={pretty} />;
    }
    return <span>{value.value}</span>;
  }
  return <span>{formatAttr(value)}</span>;
}

const NOT_JSON: unique symbol = Symbol('not-json');

function tryParseJson(raw: string): unknown | typeof NOT_JSON {
  const trimmed = raw.trim();
  // Cheap prefilter: only attempt to parse if it looks structured. JSON.parse
  // accepts bare numbers and `"..."` strings, which would turn every numeric
  // attribute into a "JSON" payload and double-format it — avoid that.
  if (trimmed.length === 0) return NOT_JSON;
  const first = trimmed[0];
  if (first !== '{' && first !== '[') return NOT_JSON;
  try {
    return JSON.parse(trimmed);
  } catch {
    return NOT_JSON;
  }
}

// Regex-based JSON syntax highlighter. Produces a <pre> with <span> tokens
// classed by kind (key/string/number/boolean/null/punct). Cheap enough to run
// on every drawer open — we never recolor the same payload twice since the
// result is memoized by text identity at the call site.
const JSON_TOKEN_RE =
  /("(?:\\.|[^"\\])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}[\],])/g;

function highlightJson(text: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  let last = 0;
  let idx = 0;
  for (const match of text.matchAll(JSON_TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > last) parts.push(text.slice(last, start));
    const token = match[0];
    let cls = 'hg-json__punct';
    if (token.startsWith('"')) {
      cls = token.trimEnd().endsWith(':') ? 'hg-json__key' : 'hg-json__string';
    } else if (token === 'true' || token === 'false') {
      cls = 'hg-json__bool';
    } else if (token === 'null') {
      cls = 'hg-json__null';
    } else if (/^-?\d/.test(token)) {
      cls = 'hg-json__number';
    }
    parts.push(
      <span key={`t${idx++}`} className={cls}>
        {token}
      </span>,
    );
    last = start + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function JsonCode({ text }: { text: string }) {
  const nodes = useMemo(() => highlightJson(text), [text]);
  return (
    <pre className="hg-drawer__code hg-drawer__code--json">{nodes}</pre>
  );
}

function formatAttr(v: AttributeValue): string {
  switch (v.kind) {
    case 'string':
      return v.value;
    case 'int':
      return v.value.toString();
    case 'double':
      return String(v.value);
    case 'bool':
      return v.value ? 'true' : 'false';
    case 'bytes':
      return `${v.value.byteLength} bytes`;
    case 'array':
      return `[${v.value.map(formatAttr).join(', ')}]`;
  }
}

// --- Payload tab ------------------------------------------------------------

function PayloadTab({ span }: { span: Span }) {
  const [activeIdx, setActiveIdx] = useState(0);
  const refs = span.payloadRefs;
  if (refs.length === 0) {
    return <p>No payload attached to this span.</p>;
  }
  const active = refs[activeIdx] ?? refs[0];
  return (
    <div className="hg-drawer__section">
      {refs.length > 1 && (
        <div className="hg-drawer__payload-tabs">
          {refs.map((r, i) => (
            <button
              key={`${r.digest}:${i}`}
              onClick={() => setActiveIdx(i)}
              className={i === activeIdx ? 'hg-drawer__tab--active' : ''}
            >
              {r.role || `payload ${i + 1}`}
            </button>
          ))}
        </div>
      )}
      <PayloadBody payloadRef={active} />
    </div>
  );
}

function PayloadBody({ payloadRef }: { payloadRef: PayloadRef }) {
  const [load, setLoad] = useState(false);
  const { bytes, mimeType, loading, error } = usePayload(load ? payloadRef.digest : null);

  if (payloadRef.evicted) {
    return (
      <div>
        <p>Payload was not preserved (client under backpressure).</p>
        <p className="hg-drawer__dim">Summary: {payloadRef.summary}</p>
      </div>
    );
  }

  return (
    <div data-testid="payload-content">
      <div className="hg-drawer__payload-header">
        <code>{payloadRef.digest.slice(0, 12)}…</code>
        <span>{payloadRef.mime}</span>
        <span>{formatBytes(payloadRef.size)}</span>
      </div>
      {payloadRef.summary && (
        <p className="hg-drawer__dim">{payloadRef.summary}</p>
      )}
      {!load && (
        <button onClick={() => setLoad(true)}>Load full payload</button>
      )}
      {loading && <p>Loading…</p>}
      {error && <p className="hg-drawer__error">{error}</p>}
      {bytes && <RenderPayloadBytes bytes={bytes} mime={mimeType || payloadRef.mime} />}
    </div>
  );
}

function RenderImagePayload({ bytes, mime }: { bytes: Uint8Array; mime: string }) {
  const url = useMemo(() => {
    const blob = new Blob([new Uint8Array(bytes)], { type: mime });
    return URL.createObjectURL(blob);
  }, [bytes, mime]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  return <img src={url} alt="payload" style={{ maxWidth: '100%' }} />;
}

function RenderPayloadBytes({ bytes, mime }: { bytes: Uint8Array; mime: string }) {
  const text = useMemo(() => {
    if (mime.startsWith('image/')) return null;
    try {
      return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    } catch {
      return null;
    }
  }, [bytes, mime]);

  if (mime.startsWith('image/')) {
    return <RenderImagePayload bytes={bytes} mime={mime} />;
  }

  if (mime === 'application/json' && text) {
    let pretty: string | null = null;
    try {
      pretty = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      pretty = null;
    }
    if (pretty !== null) return <JsonCode text={pretty} />;
    return <pre className="hg-drawer__code">{text}</pre>;
  }

  if (mime.startsWith('text/') && text !== null) {
    return <pre className="hg-drawer__code">{text}</pre>;
  }

  // Binary fallback: hex dump of the first 4 KiB.
  const sliced = bytes.slice(0, 4096);
  const hex = Array.from(sliced)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join(' ');
  return (
    <pre className="hg-drawer__code">
      {hex}
      {bytes.byteLength > sliced.byteLength ? '\n…(truncated)' : ''}
    </pre>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

// --- Timeline tab -----------------------------------------------------------

function TimelineTab({ span, sessionId }: { span: Span; sessionId: string | null }) {
  const children = useMemo(() => {
    if (!sessionId) return [];
    const store = getSessionStore(sessionId);
    if (!store) return [];
    return store.spans
      .queryAgent(span.agentId, span.startMs, span.endMs ?? Number.MAX_SAFE_INTEGER)
      .filter((s) => s.parentSpanId === span.id)
      .sort((a, b) => a.startMs - b.startMs);
  }, [span, sessionId]);

  if (children.length === 0) {
    return <p>No children recorded for this span.</p>;
  }

  const spanEnd = span.endMs ?? span.startMs + 1;
  const totalMs = Math.max(1, spanEnd - span.startMs);

  return (
    <div className="hg-drawer__section">
      <div className="hg-drawer__waterfall">
        {children.map((c) => {
          const off = ((c.startMs - span.startMs) / totalMs) * 100;
          const width = Math.max(
            0.5,
            (((c.endMs ?? spanEnd) - c.startMs) / totalMs) * 100,
          );
          return (
            <div key={c.id} className="hg-drawer__waterfall-row">
              <div className="hg-drawer__waterfall-label">
                {c.kind}·{c.name}
              </div>
              <div className="hg-drawer__waterfall-track">
                <div
                  className="hg-drawer__waterfall-bar"
                  style={{ left: `${off}%`, width: `${width}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --- Links tab --------------------------------------------------------------

const LINK_GROUP_ORDER: LinkRelation[] = [
  'INVOKED',
  'TRIGGERED_BY',
  'WAITING_ON',
  'FOLLOWS',
  'REPLACES',
];

function LinksTab({ span }: { span: Span }) {
  const selectSpan = useUiStore((s) => s.selectSpan);
  const groups = useMemo(() => {
    const g = new Map<LinkRelation, SpanLink[]>();
    for (const link of span.links) {
      const arr = g.get(link.relation) ?? [];
      arr.push(link);
      g.set(link.relation, arr);
    }
    return g;
  }, [span.links]);

  if (groups.size === 0) {
    return <p>No links on this span.</p>;
  }

  return (
    <div className="hg-drawer__section">
      {LINK_GROUP_ORDER.filter((r) => groups.has(r)).map((rel) => (
        <div key={rel}>
          <h3>{rel}</h3>
          <ul className="hg-drawer__links">
            {groups.get(rel)!.map((l) => (
              <li
                key={`${l.targetAgentId}:${l.targetSpanId}`}
                onClick={() => selectSpan(l.targetSpanId)}
                role="button"
              >
                <code>{l.targetAgentId}</code>
                <code>{l.targetSpanId.slice(0, 12)}…</code>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

// --- Annotations tab --------------------------------------------------------

function AnnotationsTab({
  span,
  sessionId,
}: {
  span: Span;
  sessionId: string;
}) {
  const post = usePostAnnotation();
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!body.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await post({ sessionId, spanId: span.id, body, kind: 'COMMENT' });
      setBody('');
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="hg-drawer__section">
      <p className="hg-drawer__dim">Add a comment to this span.</p>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Write a comment…"
        rows={4}
        className="hg-drawer__textarea"
        data-testid="annotation-compose-input"
      />
      <div className="hg-drawer__row">
        <button
          onClick={submit}
          disabled={busy || !body.trim()}
          data-testid="annotation-submit"
        >
          {busy ? 'Posting…' : 'Post comment'}
        </button>
        {error && <span className="hg-drawer__error">{error}</span>}
      </div>
    </div>
  );
}

// --- Control tab ------------------------------------------------------------

function ControlTab({ span, sessionId }: { span: Span; sessionId: string }) {
  const send = useSendControl();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [steerBody, setSteerBody] = useState('');

  const dispatch = async (kind: Parameters<typeof send>[0]['kind'], payload?: Uint8Array) => {
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
  const awaiting = span.status === 'AWAITING_HUMAN';

  return (
    <div className="hg-drawer__section">
      {awaiting && (
        <div className="hg-drawer__approval">
          <p><strong>Agent is waiting for human approval.</strong></p>
          <div className="hg-drawer__row">
            <button onClick={() => dispatch('APPROVE')} disabled={busy !== null}>
              Approve
            </button>
            <button onClick={() => dispatch('REJECT', encoder.encode('rejected'))} disabled={busy !== null}>
              Reject
            </button>
          </div>
        </div>
      )}
      <h3>Steer</h3>
      <textarea
        value={steerBody}
        onChange={(e) => setSteerBody(e.target.value)}
        placeholder="Consider: "
        rows={3}
        className="hg-drawer__textarea"
      />
      <div className="hg-drawer__row">
        <button
          onClick={() => dispatch('STEER', encoder.encode(steerBody))}
          disabled={busy !== null || !steerBody.trim()}
        >
          {busy === 'STEER' ? 'Sending…' : 'Send steer'}
        </button>
      </div>
      <h3>Transport</h3>
      <div className="hg-drawer__row">
        <button onClick={() => dispatch('PAUSE')} disabled={busy !== null}>
          Pause agent
        </button>
        <button onClick={() => dispatch('RESUME')} disabled={busy !== null}>
          Resume
        </button>
        <button onClick={() => dispatch('CANCEL')} disabled={busy !== null}>
          Cancel
        </button>
        <button onClick={() => dispatch('REWIND_TO', encoder.encode(span.id))} disabled={busy !== null}>
          Rewind to here
        </button>
      </div>
      {error && <p className="hg-drawer__error">{error}</p>}
    </div>
  );
}
