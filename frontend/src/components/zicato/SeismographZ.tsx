// SeismographZ.tsx — ONE combined drift trace + judge heartbeat on a SHARED
// time axis, with the time-aligned intervention ladder folded in beneath via
// the same trackGeom (timeScale) the LadderZ sibling reuses.
//
// Ported from compose.html seismoSVG (468-503) + judgeBeats (171-176, in
// svgUtils) + trackGeom (timeScale in svgUtils) + heartSVG (506-515).
//
// Encoding (preserved verbatim from the study):
//   • the judge HEARTBEAT strip (top) shares the EXACT same X as the lanes:
//     ok → small goldfive-hue dot, warn → --caution ▲ tick, crit → --bad ✕ tick.
//     The beats are DERIVED from the drift the judges watch (judgeBeats), so a
//     firing mark always sits above the reading that earned it.
//   • per lane: a --good tolerance band (.gm-tol), a dashed on-plan baseline
//     (.gm-onplan), the drift TRACE (.gm-trace, --ink) jittered DETERMINISTICALLY
//     via lcg (NEVER Math.random), --bad excursion polygons (.gm-excursion) where
//     drift > TOL(8), and intervention ticks (.gm-tick) from z.ticks.
//   • an --accent now-cursor line at min(now, T).
//
// Graceful fallback: with no drift (z.judges == {}) the figure still renders the
// heartbeat baseline (all-ok beats) + a single empty-state line — never crashes.
//
// SIGNATURES ARE FROZEN — do not change the props.

import { type ReactNode } from 'react';
import { lcg, lerpKeys, timeScale, judgeBeats, SEISMO_LANES_DEFAULT } from './svgUtils';
import type { ZSession } from './adapter';

export interface SeismographZProps {
  z: ZSession;
  /** default: real busy (judged) lanes, fallback SEISMO_LANES_DEFAULT */
  lanes?: string[];
  W?: number;
  axis?: boolean;
  heart?: boolean;
}

const TOL = 8;

/** Resolve the lanes to draw: real judged lanes, else the study default. */
function resolveLanes(z: ZSession, lanes?: string[]): string[] {
  if (lanes && lanes.length) return lanes;
  const judged = Object.keys(z.judges);
  return judged.length ? judged : [...SEISMO_LANES_DEFAULT];
}

/** agentId → display label (falls back to the raw lane key, e.g. the study default). */
function laneLabel(z: ZSession, lane: string): string {
  return z.agents.find((a) => a.id === lane)?.label ?? lane;
}

export function SeismographZ({
  z,
  lanes,
  W = 940,
  axis = true,
  heart = true,
}: SeismographZProps) {
  const lanesToDraw = resolveLanes(z, lanes);
  const { X } = timeScale(z.T, W);

  const heartH = heart ? 30 : 0;
  const rowH = 54;
  const top = heartH;
  const bottomPad = axis ? 24 : 6;
  const H = top + lanesToDraw.length * rowH + bottomPad;
  const gridBottom = H - (axis ? 18 : 6);

  // Shared x-domain gridlines (and optional axis labels).
  const gridLines: ReactNode[] = [];
  for (let i = 0; i <= 4; i++) {
    const gx = X((z.T * i) / 4);
    gridLines.push(
      <line key={`g${i}`} className="hg-gantt-grid" x1={gx} y1={4} x2={gx} y2={gridBottom} />,
    );
    if (axis) {
      gridLines.push(
        <text key={`gl${i}`} className="gm-axis" x={gx - 7} y={H - 5}>
          {Math.round((z.T * i) / 4)}s
        </text>,
      );
    }
  }

  // ── judge heartbeat strip — KIND hue baseline, STATUS treatment on warn/crit ──
  const heartNodes: ReactNode[] = [];
  if (heart) {
    const hy = heartH - 10;
    heartNodes.push(
      <text key="hl" className="gm-label" x={6} y={hy + 4} fill="var(--hg-agent-goldfive)">
        judges
      </text>,
      <line key="hb" className="gm-onplan" x1={X(0)} y1={hy} x2={X(z.T)} y2={hy} />,
    );
    judgeBeats(z.judges, lanesToDraw, z.T, z.now).forEach(([t, k], i) => {
      if (t > z.T) return;
      const x = X(t);
      if (k === 'ok') {
        heartNodes.push(
          <circle key={`hk${i}`} cx={x} cy={hy} r={1.7} fill="var(--hg-agent-goldfive)" opacity={0.8}>
            <title>{`judge fired · on-task @ ${t.toFixed(0)}s`}</title>
          </circle>,
        );
      } else if (k === 'warn') {
        heartNodes.push(
          <line
            key={`hkl${i}`}
            x1={x}
            y1={hy - 6}
            x2={x}
            y2={hy + 6}
            stroke="var(--caution)"
            strokeWidth={1.4}
            vectorEffect="non-scaling-stroke"
          />,
          <text key={`hkt${i}`} x={x - 3.5} y={hy + 3} fontSize={8.5} fill="var(--caution)">
            ▲
          </text>,
        );
      } else {
        heartNodes.push(
          <line
            key={`hkl${i}`}
            x1={x}
            y1={hy - 7}
            x2={x}
            y2={hy + 7}
            stroke="var(--bad)"
            strokeWidth={1.6}
            vectorEffect="non-scaling-stroke"
          />,
          <text
            key={`hkt${i}`}
            x={x - 3.5}
            y={hy + 3.5}
            fontSize={9.5}
            fill="var(--bad)"
            fontWeight={700}
          >
            ✕
          </text>,
        );
      }
    });
    heartNodes.push(
      <line
        key="hseam"
        className="gm-seam"
        x1={X(0)}
        y1={heartH - 1}
        x2={X(z.T)}
        y2={heartH - 1}
        opacity={0.5}
      />,
    );
  }

  // ── per-lane drift traces with tolerance band + excursion fills ──
  let anyLaneData = false;
  const laneNodes: ReactNode[] = [];
  lanesToDraw.forEach((a, li) => {
    const keys = z.judges[a];
    if (!keys || !keys.length) return; // no drift for this lane → skip (study parity)
    anyLaneData = true;

    const base = top + 10 + li * rowH + rowH - 16;
    const rnd = lcg(40 + li); // DETERMINISTIC jitter (stable across renders)
    const yOf = (v: number): number => base - v * 1.9;

    laneNodes.push(
      <text key={`ll${li}`} className="gm-label" x={6} y={base - 12}>
        {laneLabel(z, a)}
      </text>,
      <rect
        key={`lt${li}`}
        className="gm-tol"
        x={X(0)}
        y={yOf(TOL)}
        width={X(z.T) - X(0)}
        height={yOf(0) - yOf(TOL)}
      />,
      <line key={`lo${li}`} className="gm-onplan" x1={X(0)} y1={base} x2={X(z.T)} y2={base} />,
    );

    // Sample the drift trace deterministically (lcg + a fixed sine wobble).
    const pts: [number, number, number][] = [];
    for (let i = 0; i <= 260; i++) {
      const t = (z.T * i) / 260;
      const v = Math.max(0, lerpKeys(keys, t) + Math.sin(t * 2.3 + li) * 0.5 + (rnd() - 0.5) * 0.9);
      pts.push([X(t), yOf(v), v]);
    }

    // Excursion polygons where drift > TOL.
    let seg: [number, number, number][] | null = null;
    const segs: [number, number, number][][] = [];
    pts.forEach((p) => {
      if (p[2] > TOL) {
        if (!seg) seg = [];
        seg.push(p);
      } else if (seg) {
        segs.push(seg);
        seg = null;
      }
    });
    if (seg) segs.push(seg);
    segs.forEach((sg, si) => {
      const tolY = yOf(TOL).toFixed(1);
      const pointStr =
        `${sg.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')} ` +
        `${sg[sg.length - 1][0].toFixed(1)},${tolY} ${sg[0][0].toFixed(1)},${tolY}`;
      laneNodes.push(<polygon key={`lx${li}-${si}`} className="gm-excursion" points={pointStr} />);
    });

    laneNodes.push(
      <path
        key={`lp${li}`}
        className="gm-trace"
        d={`M${pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L')}`}
      />,
    );

    // Intervention ticks for this lane.
    (z.ticks[a] || []).forEach(([t, lbl], ti) => {
      const tx = X(t);
      laneNodes.push(
        <line
          key={`lk${li}-${ti}`}
          className="gm-tick"
          x1={tx}
          y1={base - rowH + 22}
          x2={tx}
          y2={base + 3}
        >
          <title>{`intervention · ${lbl}`}</title>
        </line>,
        <text key={`lkt${li}-${ti}`} className="gm-tick-label" x={tx + 3} y={base - rowH + 27}>
          {lbl.split(' ')[0]}
        </text>,
      );
    });
  });

  const nx = X(Math.min(z.now, z.T));

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`drift seismograph + judge heartbeat — ${z.id}`}
    >
      {gridLines}
      {heartNodes}
      {laneNodes}
      {!anyLaneData && (
        <text className="gm-faint" x={timeScale(z.T, W).padL + 10} y={top + 24} fontStyle="italic">
          no drift recorded — judges on-task
        </text>
      )}
      <line className="hg-gantt-now" x1={nx} y1={4} x2={nx} y2={gridBottom} />
    </svg>
  );
}

export interface JudgeHeartbeatZProps {
  z: ZSession;
  W?: number;
}

// The judge heartbeat as its OWN small figure (inspector/drawer + ⌘K caption).
// Same judgeBeats data, no lanes argument → all judged lanes contribute.
// (compose.html heartSVG 506-515.)
export function JudgeHeartbeatZ({ z, W = 940 }: JudgeHeartbeatZProps) {
  const H = 40;
  const { X } = timeScale(z.T, W);
  const lanesToDraw = resolveLanes(z, undefined);

  const beats: ReactNode[] = [];
  judgeBeats(z.judges, lanesToDraw, z.T, z.now).forEach(([t, k], i) => {
    if (t > z.T) return;
    const x = X(t);
    if (k === 'ok') {
      beats.push(
        <circle key={`b${i}`} cx={x} cy={18} r={1.5} fill="var(--hg-agent-goldfive)" opacity={0.8} />,
      );
    } else if (k === 'warn') {
      beats.push(
        <text key={`b${i}`} x={x - 3.5} y={22} fontSize={9} fill="var(--caution)">
          ▲
        </text>,
      );
    } else {
      beats.push(
        <text key={`b${i}`} x={x - 3.5} y={22} fontSize={10} fill="var(--bad)" fontWeight={700}>
          ✕
        </text>,
      );
    }
  });

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`judge heartbeat — ${z.id}`}
    >
      <text className="gm-label" x={6} y={22} fill="var(--hg-agent-goldfive)">
        judges
      </text>
      <line className="gm-onplan" x1={X(0)} y1={26} x2={X(z.T)} y2={26} />
      {beats}
    </svg>
  );
}
