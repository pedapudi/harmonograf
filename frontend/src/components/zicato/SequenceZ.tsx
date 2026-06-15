// SequenceZ.tsx — lifelines down, time down.
//
// Ported from compose.html seqDiagramSVG (326-363) + sessionMessages (314-323).
// One dashed lifeline per agent (agent-color via colorVar), a chip + name at the
// head; 6 horizontal gridlines with Ns axis labels; spans → narrow vertical
// activation bars (KIND hue / --bad failed / wait-for-human awaiting, fill-opacity
// by status); messages → horizontal connectors (solid call / dashed return) each
// with a SMALL SOLID arrowhead at the receiver and a tiny label; an approval-gate
// ring on an awaiting span; the accent now-line.
//
// Real-data deltas from the study:
//   • columns come from z.agents (real lane order), not the fixed study name list.
//     Lifeline colour is colorVar(agent); the activation/message x is keyed by the
//     real agentId.
//   • messages come from z.edges (the GraphView-derived transfer/delegation/return
//     arrows) rather than the study mock's sessionMessages — enriched with the
//     opening user-message span and the closing agent-message (approval-request)
//     span so the conversation reads end-to-end.
//   • graceful fallback: an empty session renders the staff + axis only (no crash).
//
// Span/activation click → useUiStore.getState().selectSpan(spanId) (opens the
// existing inspector drawer). SIGNATURE IS FROZEN.

import { useUiStore } from '../../state/uiStore';
import type { ZSession, ZAgent, ZSpan } from './adapter';
import { colorVar, KIND } from './svgUtils';

export interface SequenceZProps {
  z: ZSession;
  W?: number;
  H?: number;
}

/** A single horizontal message connector, time-ordered. */
interface Msg {
  t: number;
  from: string; // agent id
  to: string; // agent id
  /** style family: solid call vs dashed return, hue per family. */
  family: 'transfer' | 'delegation' | 'return' | 'user-message' | 'agent-message';
  label: string;
}

/** The connector hue per message family (compose.html 350-351). */
function msgColor(family: Msg['family']): string {
  switch (family) {
    case 'transfer':
      return KIND('transfer');
    case 'user-message':
      return KIND('user-message');
    case 'delegation':
      return 'var(--ink-soft)';
    case 'return':
      return 'var(--ink-faint)';
    case 'agent-message':
    default:
      return KIND('agent-message');
  }
}

/** dash-array per family — solid call / dashed return (compose.html 352). */
function msgDash(family: Msg['family']): string | undefined {
  if (family === 'transfer') return '5 3';
  if (family === 'return') return '4 3';
  return undefined; // delegation / user-message / agent-message → solid call
}

/**
 * Derive the time-ordered message list. The transfer / delegation / return arrows
 * come from the GraphView-derived `z.edges`; the opening user-message and the
 * closing agent-message (approval request) come from the matching spans so the
 * sequence reads from the user's prompt to the agent's reply. (Replaces the study
 * mock's sessionMessages, which scraped the same shapes from a flat object.)
 */
function buildMessages(z: ZSession, present: Set<string>): Msg[] {
  const msgs: Msg[] = [];
  const goalHead = z.goal ? z.goal.split(/\s+/).slice(0, 4).join(' ') + '…' : 'prompt';

  // Opening user-message: user → its receiving agent.
  const um = z.spans.find((s) => s.kind === 'user-message');
  if (um && present.has(um.agent)) {
    const fromUser = userAgentId(z);
    if (fromUser && present.has(fromUser) && fromUser !== um.agent) {
      msgs.push({
        t: um.t0,
        from: fromUser,
        to: um.agent,
        family: 'user-message',
        label: 'user: ' + goalHead,
      });
    }
  }

  // Transfer / delegation / return arrows from the derived edge set.
  for (const e of z.edges) {
    if (!present.has(e.from) || !present.has(e.to) || e.from === e.to) continue;
    if (e.t > z.T) continue;
    msgs.push({
      t: e.t,
      from: e.from,
      to: e.to,
      family: e.kind,
      label:
        e.kind === 'transfer'
          ? 'transfer'
          : e.kind === 'return'
            ? 'return'
            : 'delegate',
    });
  }

  // Closing agent-message: an approval request back to the user.
  const am = z.spans.find((s) => s.kind === 'agent-message');
  if (am) {
    const toUser = userAgentId(z);
    if (toUser && present.has(am.agent) && present.has(toUser) && am.agent !== toUser) {
      msgs.push({
        t: am.t1 || am.t0,
        from: am.agent,
        to: toUser,
        family: 'agent-message',
        label: 'approval requested',
      });
    }
  }

  msgs.sort((a, b) => a.t - b.t);
  return msgs;
}

/** The synthetic user lane id (if present), used as the human endpoint. */
function userAgentId(z: ZSession): string | null {
  return z.agents.find((a) => a.synthetic === 'user')?.id ?? null;
}

/** Activation-bar fill (compose.html 342): failed→--bad, awaiting→wait hue, else KIND. */
function actFill(sp: ZSpan): string {
  if (sp.status === 'failed') return 'var(--bad)';
  if (sp.status === 'awaiting') return KIND('wait-for-human');
  return KIND(sp.kind);
}

/** Activation-bar fill-opacity by status (compose.html 343). */
function actOpacity(sp: ZSpan): number {
  if (sp.status === 'awaiting') return 0.4;
  if (sp.status === 'failed') return 0.85;
  return 0.55;
}

export function SequenceZ({ z, W = 520, H = 380 }: SequenceZProps) {
  const padT = 52;
  const padL = 58;

  // Columns = the real lane order. Synthetic + working agents alike get a column.
  const cols: ZAgent[] = z.agents;
  const present = new Set(cols.map((a) => a.id));
  const denom = z.T > 0 ? z.T : 1;

  // Column x-positions (study geometry: padL+30 then evenly stepped).
  const step = cols.length > 1 ? (W - padL - 44) / (cols.length - 1) : 0;
  const X = new Map<string, number>();
  cols.forEach((a, i) => X.set(a.id, padL + 30 + i * step));
  const colorById = new Map<string, string>();
  cols.forEach((a) => colorById.set(a.id, colorVar(a)));

  const yT = (t: number): number => padT + (t / denom) * (H - padT - 26);

  const select = (id: string): void => useUiStore.getState().selectSpan(id);

  // Gridlines + axis labels (6 divisions).
  const grid: React.ReactNode[] = [];
  for (let i = 0; i <= 6; i++) {
    const y = yT((z.T * i) / 6);
    grid.push(
      <line
        key={`g${i}`}
        className="hg-gantt-grid"
        x1={padL - 4}
        y1={y}
        x2={W - 12}
        y2={y}
        opacity={0.5}
      />,
    );
    grid.push(
      <text key={`gl${i}`} className="gm-axis" x={26} y={y + 3}>
        {Math.round((z.T * i) / 6)}s
      </text>,
    );
  }

  // Lifelines + chips + names.
  const lifelines: React.ReactNode[] = [];
  cols.forEach((a) => {
    const x = X.get(a.id)!;
    const col = colorById.get(a.id)!;
    lifelines.push(
      <line
        key={`life-${a.id}`}
        className="sq-life"
        x1={x}
        y1={30}
        x2={x}
        y2={H - 14}
        stroke={col}
        opacity={0.8}
      />,
    );
    lifelines.push(
      <rect
        key={`chip-${a.id}`}
        className="sq-chip"
        x={x - 34}
        y={10}
        width={68}
        height={19}
        rx={4}
        stroke={col}
      />,
    );
    lifelines.push(
      <text
        key={`name-${a.id}`}
        className="sq-name"
        x={x}
        y={23}
        textAnchor="middle"
      >
        {a.label}
      </text>,
    );
  });

  // Activation bars — one per visible, non-planned, non-goldfive span.
  const acts: React.ReactNode[] = [];
  for (const sp of z.spans) {
    if (!present.has(sp.agent) || sp.status === 'planned' || sp.gf) continue;
    const x = X.get(sp.agent)!;
    const y0 = yT(sp.t0);
    const h = Math.max(6, yT(sp.t1) - y0);
    acts.push(
      <rect
        key={`act-${sp.id}`}
        className="sq-act"
        data-span={sp.id}
        x={x - 4}
        y={y0}
        width={8}
        height={h}
        rx={2.5}
        fill={actFill(sp)}
        fillOpacity={actOpacity(sp)}
        tabIndex={0}
        onClick={() => select(sp.id)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            select(sp.id);
          }
        }}
      >
        <title>{`${sp.label} · ${sp.status}`}</title>
      </rect>,
    );
    if (sp.status === 'failed') {
      acts.push(
        <text
          key={`fail-${sp.id}`}
          x={x + 9}
          y={y0 + h / 2 + 3}
          fontSize={8.5}
          fill="var(--bad)"
        >
          ✕
        </text>,
      );
    }
  }

  // Messages — min-row-gap layout so each connector gets its own row.
  const MINGAP = 24;
  let prevY = -1e9;
  const messages: React.ReactNode[] = [];
  for (const m of buildMessages(z, present)) {
    const x0 = X.get(m.from)!;
    const x1 = X.get(m.to)!;
    const y = Math.max(yT(m.t), prevY + MINGAP);
    prevY = y;
    const dir = x1 > x0 ? 1 : -1;
    const color = msgColor(m.family);
    const dash = msgDash(m.family);
    messages.push(
      <line
        key={`msg-${m.t}-${m.from}-${m.to}`}
        className="sq-msg"
        x1={x0 + 4 * dir}
        y1={y}
        x2={x1 - 9 * dir}
        y2={y}
        stroke={color}
        strokeDasharray={dash}
      >
        <title>{`${m.label} · ${Math.round(m.t)}s`}</title>
      </line>,
    );
    // SMALL SOLID arrowhead at the receiver (compose.html 356).
    messages.push(
      <path
        key={`ah-${m.t}-${m.from}-${m.to}`}
        d={`M${x1 - 6 * dir},${y - 2.2} L${x1 - 1 * dir},${y} L${x1 - 6 * dir},${y + 2.2} Z`}
        fill={color}
        stroke="none"
      />,
    );
    messages.push(
      <text
        key={`lbl-${m.t}-${m.from}-${m.to}`}
        className="sq-lbl"
        x={x0 + 16 * dir}
        y={y - 5}
        textAnchor={dir > 0 ? 'start' : 'end'}
        fill={color}
      >
        {m.label}
      </text>,
    );
  }

  // Approval gate — a ring on the first awaiting span's lifeline.
  const wait = z.spans.find((s) => s.status === 'awaiting' && present.has(s.agent));
  const gate =
    wait != null ? (
      <circle
        cx={X.get(wait.agent)!}
        cy={yT(Math.min(wait.t1, z.T))}
        r={5.5}
        fill="none"
        stroke="var(--ink-soft)"
        strokeWidth={1.4}
      >
        <title>gate · deciding…</title>
      </circle>
    ) : null;

  // Now-line.
  const ny = yT(Math.min(z.now, z.T));

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`agent-interaction sequence diagram — ${z.id || 'session'}`}
    >
      {grid}
      {lifelines}
      {acts}
      {messages}
      {gate}
      <line className="hg-gantt-now" x1={padL - 4} y1={ny} x2={W - 12} y2={ny} />
      <text className="hg-gantt-now-label" x={padL} y={ny - 5}>
        now {Math.round(z.now)}s
      </text>
    </svg>
  );
}
