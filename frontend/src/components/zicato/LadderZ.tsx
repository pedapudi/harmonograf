// LadderZ.tsx — the intervention ladder, time-LOCKED to SeismographZ (same z + W
// → identical timeScale padL/padR/X, and the SAME 4-gridline domain), so a
// rung-dot at time t sits directly beneath the drift reading at t in the
// seismograph stacked above it.
//
// Ported from compose.html ladderSVG (520-535). 4 rungs nudge/refine/replan/
// escalate (rungY(i)=96-i*25); a dashed connector path through z.ladder; dots
// --bad on the escalate rung (3) else --ink-soft. Empty z.ladder → italic
// "never left the ground" empty state.
//
// SIGNATURE IS FROZEN — do not change the props.

import { type ReactNode } from 'react';
import { timeScale } from './svgUtils';
import type { ZSession } from './adapter';

export interface LadderZProps {
  z: ZSession;
  W?: number;
}

const RUNGS = ['nudge', 'refine', 'replan', 'escalate'] as const;
const rungY = (i: number): number => 96 - i * 25;

export function LadderZ({ z, W = 940 }: LadderZProps) {
  const H = 124;
  const { padL, padR, X } = timeScale(z.T, W);
  const dots = z.ladder;

  // Shared x-domain gridlines — match the seismograph's 4-division grid so the
  // ladder reads on the same time axis. Top of grid stops just above rung 3.
  const gridLines: ReactNode[] = [];
  const axisLabels: ReactNode[] = [];
  for (let i = 0; i <= 4; i++) {
    const gx = X((z.T * i) / 4);
    gridLines.push(
      <line
        key={`g${i}`}
        className="hg-gantt-grid"
        x1={gx}
        y1={2}
        x2={gx}
        y2={rungY(3) - 6}
        opacity={0.5}
      />,
    );
    axisLabels.push(
      <text key={`gl${i}`} className="gm-axis" x={gx - 7} y={H - 4}>
        {Math.round((z.T * i) / 4)}s
      </text>,
    );
  }

  // The four rung baselines + their left-hand labels.
  const rungs: ReactNode[] = [];
  RUNGS.forEach((r, i) => {
    const y = rungY(i);
    rungs.push(
      <line
        key={`r${i}`}
        x1={padL}
        y1={y}
        x2={W - padR}
        y2={y}
        stroke="var(--rule)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />,
      <text key={`rl${i}`} className="gm-faint" x={8} y={y + 3}>
        {r}
      </text>,
    );
  });

  // The dashed connector through the intervention dots (only when >1 reading).
  const connector =
    dots.length > 1 ? (
      <path
        d={dots
          .map(([t, rung], i) => `${i ? 'L' : 'M'}${X(t).toFixed(1)},${rungY(rung).toFixed(1)}`)
          .join(' ')}
        fill="none"
        stroke="var(--ink-faint)"
        strokeWidth={0.8}
        strokeDasharray="2 3"
        opacity={0.7}
        vectorEffect="non-scaling-stroke"
      />
    ) : null;

  // The dots themselves: --bad on the escalate rung (3), else --ink-soft.
  const dotNodes: ReactNode[] = dots.map(([t, rung], i) => (
    <circle
      key={`d${i}`}
      cx={X(t)}
      cy={rungY(rung)}
      r={3.2}
      fill={rung === 3 ? 'var(--bad)' : 'var(--ink-soft)'}
    >
      <title>{`${RUNGS[rung] ?? 'intervention'} @ ${Math.round(t)}s`}</title>
    </circle>
  ));

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`intervention ladder (time-aligned to the seismograph) — ${z.id}`}
    >
      {gridLines}
      {rungs}
      {connector}
      {dotNodes}
      {dots.length === 0 && (
        <text
          className="gm-faint"
          x={padL + 10}
          y={rungY(0) - 8}
          fontStyle="italic"
        >
          no interventions — never left the ground
        </text>
      )}
      {axisLabels}
    </svg>
  );
}
