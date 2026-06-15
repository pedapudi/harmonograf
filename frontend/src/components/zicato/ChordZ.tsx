// ChordZ.tsx — directional transfer chord on an upper semicircle.
//
// Ported from compose.html topoChordSVG (368-419) + wireChord (422-443).
// Agents sit on an upper-semicircle arc; each gets two concentric arc sectors
// (outbound SOLID 4.5px non-scaling cap / inbound DASHED). Every directional
// flow is a THIN gradient-stroked quadratic curve (width min(4, 0.9+√count*1.1),
// non-scaling) running faint-source → bright-target, tipped by a SMALL SOLID
// triangle arrowhead at the receiver. Hover (or click-to-pin) an agent to
// PROJECT its conversations: everything not touching that agent dims, and the
// count labels for the focused agent's flows appear.
//
// Encoding: agent hue (colorVar → --hg-agent-*) drives arcs/ribbons/arrowheads;
// token-only colors. Gradient <defs> ids are unique per instance via uniqueId.
//
// SIGNATURE IS FROZEN — do not change the props.

import { useState, type ReactElement } from 'react';
import type { ZSession, ZAgent } from './adapter';
import { colorVar, uniqueId } from './svgUtils';

export interface ChordZProps {
  z: ZSession;
  W?: number;
}

// One tallied directional flow between two displayed-agent indices.
interface Flow {
  a: number; // source index into displayed `agents`
  b: number; // target index into displayed `agents`
  c: number; // count
}

// Max agents to seat on the semicircle before it gets unreadable. Counts are
// tallied across ALL agents but only flows between seated agents are drawn —
// matching the study, which silently drops flows whose endpoints aren't shown.
const MAX_ARC_AGENTS = 8;

export function ChordZ({ z, W = 300 }: ChordZProps) {
  // Focus agent for projection: a pinned agent wins over the hovered one.
  const [pinned, setPinned] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const focus = pinned ?? hovered;

  const H = Math.round(W * 0.82);

  // Seat the first MAX_ARC_AGENTS lanes (adapter already orders join-time,
  // synthetics included). Empty/loading → arcs-only placeholder, no crash.
  const agents: ZAgent[] = z.agents.slice(0, MAX_ARC_AGENTS);
  const n = agents.length;

  if (n === 0) {
    return (
      <svg
        className="fig topo-chord"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`directional transfer chord — no agents yet · ${z.id}`}
      >
        <text x={W / 2} y={H / 2} textAnchor="middle" className="gm-faint">
          no agents yet
        </text>
      </svg>
    );
  }

  const idById = new Map<string, number>();
  agents.forEach((a, i) => idById.set(a.id, i));
  const idx = (id: string): number =>
    idById.has(id) ? (idById.get(id) as number) : -1;

  // Tally directional counts (study `bump`): user-message → its agent;
  // transfers from→to; delegation both ways; agent-message → user.
  const counts = new Map<string, number>();
  const bump = (fromIdx: number, toIdx: number): void => {
    if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return;
    const k = `${fromIdx}>${toIdx}`;
    counts.set(k, (counts.get(k) ?? 0) + 1);
  };
  const userAgent = agents.find((a) => a.synthetic === 'user');
  const userIdx = userAgent ? idx(userAgent.id) : -1;

  const um = z.spans.find((x) => x.kind === 'user-message');
  if (um && userIdx >= 0) bump(userIdx, idx(um.agent));
  for (const tr of z.transfers) bump(idx(tr.from), idx(tr.to));
  if (z.delegation) {
    bump(idx(z.delegation.from), idx(z.delegation.to));
    bump(idx(z.delegation.to), idx(z.delegation.from));
  }
  const am = z.spans.find((x) => x.kind === 'agent-message');
  if (am && userIdx >= 0) bump(idx(am.agent), userIdx);

  const flows: Flow[] = [...counts.entries()].map(([k, c]) => {
    const [a, b] = k.split('>').map(Number);
    return { a, b, c };
  });

  // Geometry (study constants).
  const cx = W / 2;
  const cyA = H - 26;
  const R = W * 0.34;
  const SECTOR = (Math.PI / n) * 0.74;
  // Even spread across the upper semicircle; single agent sits at the apex.
  const ang = (i: number): number => (n === 1 ? Math.PI / 2 : Math.PI - (Math.PI * i) / (n - 1));
  const onArc = (th: number, r: number): [number, number] => [
    cx + r * Math.cos(th),
    cyA - r * Math.sin(th),
  ];

  // Per-agent outbound/inbound totals (to distribute ribbon anchor angles).
  const outN = agents.map(() => 0);
  const inN = agents.map(() => 0);
  for (const f of flows) {
    outN[f.a] += f.c;
    inN[f.b] += f.c;
  }

  // Projection opacity: dim everything not involving the focused agent.
  // (port wireChord) — focus is an agent id; flows carry source/target ids.
  const ribbonOpacity = (fromId: string, toId: string): number | undefined =>
    !focus || fromId === focus || toId === focus ? undefined : 0.05;
  const countOpacity = (fromId: string, toId: string): number | undefined =>
    focus && (fromId === focus || toId === focus) ? 1 : undefined;
  const agentOpacity = (agentId: string): number | undefined =>
    !focus || agentId === focus ? undefined : 0.3;

  // ── arc sectors (outbound solid / inbound dashed) ──────────────────────────
  const arcNodes = agents.map((a, i) => {
    const th = ang(i);
    const oA = th + SECTOR / 2;
    const oB = th + 0.05;
    const iA = th - 0.05;
    const iB = th - SECTOR / 2;
    const arcPath = (thA: number, thB: number): string => {
      const [xA, yA] = onArc(thA, R);
      const [xB, yB] = onArc(thB, R);
      return `M${xA.toFixed(1)},${yA.toFixed(1)} A${R},${R} 0 0 ${
        thA > thB ? 1 : 0
      } ${xB.toFixed(1)},${yB.toFixed(1)}`;
    };
    const col = colorVar(a);
    const op = agentOpacity(a.id);
    return (
      <g key={`arc-${a.id}`}>
        {outN[i] > 0 && (
          <path
            data-agent={a.id}
            d={arcPath(oA, oB)}
            fill="none"
            stroke={col}
            strokeWidth={4.5}
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
            opacity={op ?? 0.9}
          />
        )}
        {inN[i] > 0 && (
          <path
            data-agent={a.id}
            d={arcPath(iA, iB)}
            fill="none"
            stroke={col}
            strokeWidth={4.5}
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
            strokeDasharray="1.5 2.4"
            opacity={op ?? 0.9}
          />
        )}
      </g>
    );
  });

  // ── ribbons + arrowheads + count labels ────────────────────────────────────
  const outUsed = agents.map(() => 0);
  const inUsed = agents.map(() => 0);
  const ribbonNodes: ReactElement[] = [];
  const countNodes: ReactElement[] = [];

  flows.forEach((flow, fi) => {
    const { a, b, c } = flow;
    const ths = ang(a);
    const thd = ang(b);
    // Spread multiple flows across each agent's sector so they fan out.
    const sF = (outUsed[a] + c / 2) / Math.max(outN[a], 1);
    outUsed[a] += c;
    const dF = (inUsed[b] + c / 2) / Math.max(inN[b], 1);
    inUsed[b] += c;
    const sTh = ths + 0.07 + (SECTOR / 2 - 0.07) * sF;
    const dTh = thd - 0.07 - (SECTOR / 2 - 0.07) * dF;

    // THIN stroked ribbon: restrained √count, hard-capped, non-scaling.
    const w = Math.min(4, 0.9 + Math.sqrt(c) * 1.1);
    const [sx, sy] = onArc(sTh, R - 4);
    const [dx, dy] = onArc(dTh, R - 6);
    const tD: [number, number] = [Math.sin(dTh), Math.cos(dTh)];
    const mid: [number, number] = [
      (sx + dx) / 2 + (cx - (sx + dx) / 2) * 0.62,
      (sy + dy) / 2 + (cyA - 34 - (sy + dy) / 2) * 0.5,
    ];
    const gid = uniqueId(`tch-${z.id}-${fi}`);

    const fromAgent = agents[a];
    const toAgent = agents[b];
    const fromId = fromAgent.id;
    const toId = toAgent.id;
    const fromCol = colorVar(fromAgent);
    const toCol = colorVar(toAgent);
    const ribbonOp = ribbonOpacity(fromId, toId);

    // small SOLID arrowhead tipping the chord onto the receiver
    const outward: [number, number] = [Math.cos(dTh), -Math.sin(dTh)];
    const tip: [number, number] = [dx + outward[0] * 4, dy + outward[1] * 4];

    ribbonNodes.push(
      <g key={`flow-${fi}`}>
        <defs>
          <linearGradient
            id={gid}
            x1={sx.toFixed(1)}
            y1={sy.toFixed(1)}
            x2={dx.toFixed(1)}
            y2={dy.toFixed(1)}
            gradientUnits="userSpaceOnUse"
          >
            <stop offset="0" stopColor={fromCol} stopOpacity={0.45} />
            <stop offset="1" stopColor={toCol} stopOpacity={0.95} />
          </linearGradient>
        </defs>
        <path
          data-ribbon="1"
          data-f={fromId}
          data-t={toId}
          fill="none"
          stroke={`url(#${gid})`}
          strokeWidth={Number(w.toFixed(2))}
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          d={`M${sx.toFixed(1)},${sy.toFixed(1)} Q${mid[0].toFixed(1)},${mid[1].toFixed(
            1,
          )} ${dx.toFixed(1)},${dy.toFixed(1)}`}
          opacity={ribbonOp}
        >
          <title>{`${fromAgent.label} → ${toAgent.label} · ${c} (${fromAgent.label} initiates)`}</title>
        </path>
        <path
          data-ahead="1"
          data-f={fromId}
          data-t={toId}
          d={`M${(dx + tD[0] * 1.4).toFixed(1)},${(dy + tD[1] * 1.4).toFixed(
            1,
          )} L${tip[0].toFixed(1)},${tip[1].toFixed(1)} L${(dx - tD[0] * 1.4).toFixed(
            1,
          )},${(dy - tD[1] * 1.4).toFixed(1)} Z`}
          fill={toCol}
          stroke="none"
          opacity={ribbonOp}
        />
      </g>,
    );

    countNodes.push(
      <text
        key={`count-${fi}`}
        className="tch-count"
        data-count="1"
        data-f={fromId}
        data-t={toId}
        x={mid[0].toFixed(1)}
        y={(mid[1] + 3).toFixed(1)}
        textAnchor="middle"
        opacity={countOpacity(fromId, toId)}
      >
        {c}×
      </text>,
    );
  });

  // ── agent nodes (hover→project / click→pin) ────────────────────────────────
  const togglePin = (id: string): void =>
    setPinned((prev) => (prev === id ? null : id));

  const agentNodes = agents.map((a, i) => {
    const th = ang(i);
    const [x, y] = onArc(th, R);
    const [lx, ly] = onArc(th, R + 14);
    const anc = lx < cx - 10 ? 'end' : lx > cx + 10 ? 'start' : 'middle';
    return (
      <g
        key={`agent-${a.id}`}
        data-agent={a.id}
        tabIndex={0}
        role="button"
        aria-label={`project ${a.label}'s conversations`}
        opacity={agentOpacity(a.id)}
        onMouseEnter={() => {
          if (!pinned) setHovered(a.id);
        }}
        onMouseLeave={() => {
          if (!pinned) setHovered(null);
        }}
        onClick={(e) => {
          e.stopPropagation();
          togglePin(a.id);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            togglePin(a.id);
          }
        }}
      >
        <circle cx={x.toFixed(1)} cy={y.toFixed(1)} r={12} fill="transparent" stroke="none" />
        <circle cx={x.toFixed(1)} cy={y.toFixed(1)} r={3} fill={colorVar(a)} />
        <text
          className="sq-lbl"
          x={lx.toFixed(1)}
          y={(ly + (ly < 36 ? -2 : 4)).toFixed(1)}
          textAnchor={anc}
          fill="var(--ink)"
        >
          {a.label}
        </text>
      </g>
    );
  });

  return (
    <svg
      className="fig topo-chord"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`directional transfer chord — who initiates and who receives · ${z.id}`}
      onClick={() => {
        // Click on empty SVG ground clears a pin (study svg-level handler).
        if (pinned) {
          setPinned(null);
          setHovered(null);
        }
      }}
    >
      {arcNodes}
      {ribbonNodes}
      {agentNodes}
      {countNodes}
    </svg>
  );
}
