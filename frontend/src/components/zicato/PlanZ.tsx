// PlanZ.tsx — the plan block: ReelZ (version timeline) over DagZ (dependency
// DAG). The version reel (v0…vN, replan-trigger seams) DRIVES the DAG —
// selecting a revision redraws that version: tasks added at the revision show an
// accent ○, tasks dropped since the prior revision show as dashed ghosts, and
// the critical path is the accent spine.
//
// Ported from compose.html reelSVG (578-598) + wireReel (461-467) +
// dagSVG (540-572). Two deltas from the study mock:
//   • the adapter's ZPlanNode carries ABSOLUTE pixel x/y (already laid into the
//     LX/LY grid), and ZPlanEdge references task ids — not array indices — so we
//     look nodes up by tid rather than re-projecting columns/rows here;
//   • the adapter already reframes node.st to 'added'/'ghost' for the selected
//     revision (buildPlan), so DagZ renders straight off node.st rather than
//     re-deriving the present/added/dropped role() the study computed in-renderer.
//
// selectedRevision ← useUiStore.selectedRevision; onSelect → setSelectedRevision
// (SHARED with the MD3 trajectory view, so both consoles auto-sync). Task node
// click → useUiStore.getState().selectTask(taskId).
//
// SIGNATURE IS FROZEN — do not change the props.

import { useUiStore } from '../../state/uiStore';
import type { ZPlan, ZPlanNode, ZSession, ZStratum } from './adapter';

export interface PlanZProps {
  z: ZSession;
}

// DAG node box geometry (compose.html dagSVG 540).
const BW = 118;
const BH = 24;

export function PlanZ({ z }: PlanZProps) {
  const selectedRevision = useUiStore((s) => s.selectedRevision);
  const setSelectedRevision = useUiStore((s) => s.setSelectedRevision);
  const plan = z.plan;

  if (plan.planId == null || plan.nodes.length === 0) {
    // Graceful fallback — never crash on an empty / loading session.
    return (
      <svg
        className="fig plan-reel"
        viewBox="0 0 940 60"
        role="img"
        aria-label={`plan ${z.id} — no plan yet`}
      >
        <text className="gm-faint" x={12} y={34}>
          no plan yet
        </text>
      </svg>
    );
  }

  return (
    <div>
      <ReelZ
        plan={plan}
        selectedRevision={selectedRevision}
        onSelectRevision={setSelectedRevision}
      />
      <DagZ plan={plan} selectedRevision={selectedRevision} />
    </div>
  );
}

// ── ReelZ — the version timeline (drives the DAG) ────────────────────────────
// compose.html reelSVG 578-598 + wireReel 461-467. Generalised from the study's
// fixed 4 stops to the real revision count: stops read OLDEST→NEWEST left to
// right, while plan.strata is stored NEWEST-FIRST, so we reverse for display.
// `plan.rem` is OLDEST→NEWEST (aligns with the reversed strata order).

interface ReelZProps {
  plan: ZPlan;
  selectedRevision: number | null;
  onSelectRevision: (rev: number | null) => void;
}

function ReelZ({ plan, selectedRevision, onSelectRevision }: ReelZProps) {
  const W = 940;
  const H = 92;
  // OLDEST→NEWEST for display (strata is stored newest-first).
  const ordered: ZStratum[] = [...plan.strata].reverse();
  const n = ordered.length;
  if (n === 0) {
    return (
      <svg
        className="fig plan-reel"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="plan reel — no revisions"
      >
        <text className="gm-faint" x={12} y={50}>
          single plan · no revisions
        </text>
      </svg>
    );
  }

  // Evenly spaced stops across the spine. With a single stop, centre it; with
  // many, span 0.12 → 0.88 of the width (matches the study's spread).
  const f0 = 0.12;
  const f1 = 0.88;
  const xAt = (i: number): number =>
    n === 1 ? Math.round(W * 0.5) : Math.round(W * (f0 + ((f1 - f0) * i) / (n - 1)));
  const xs = ordered.map((_, i) => xAt(i));

  const pick = (s: ZStratum): void => {
    // Toggle off when re-selecting the live revision (matches MD3 'latest' = null).
    const next = s.live && selectedRevision === s.v ? null : s.v;
    onSelectRevision(next);
  };

  return (
    <svg
      className="fig plan-reel"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="plan reel — click a version to drive the DAG"
    >
      {/* the accent spine */}
      {n > 1 && (
        <line className="gm-spine" x1={xs[0]} y1={50} x2={xs[n - 1]} y2={50} />
      )}
      {ordered.map((s, i) => {
        const x = xs[i];
        // Active: explicit selection of this revision, OR the live revision when
        // nothing is explicitly selected (selectedRevision === null → latest).
        const active =
          selectedRevision === s.v || (selectedRevision == null && s.live);
        const latest = s.live;
        const rem = plan.rem[i];
        const prevRem = i > 0 ? plan.rem[i - 1] : null;
        const delta = prevRem != null ? prevRem - rem : null;
        const dotFill = active || latest ? 'var(--accent)' : 'var(--panel)';
        const dotStroke = active || latest ? 'var(--accent)' : 'var(--ink-soft)';
        return (
          <g
            key={s.v}
            className="reel-stop"
            data-ver={s.v}
            tabIndex={0}
            role="button"
            aria-pressed={active}
            aria-label={`show plan v${i}${s.seam ? ` (${s.seam})` : ''}`}
            onClick={() => pick(s)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                pick(s);
              }
            }}
          >
            {/* replan-trigger seam label between this stop and the prior one */}
            {i > 0 && s.seam && (
              <text
                className="gm-tick-label"
                x={(xs[i - 1] + x) / 2 - 26}
                y={30}
              >
                ↯ {s.seam}
              </text>
            )}
            {/* per-stop task delta since the prior version */}
            {delta != null && delta !== 0 && (
              <text
                x={(xs[i - 1] + x) / 2 - 10}
                y={46}
                fontSize={9}
                fill={delta > 0 ? 'var(--good)' : 'var(--caution)'}
              >
                {delta > 0 ? `−${delta}` : `+${-delta}`}
              </text>
            )}
            {/* active ring */}
            {active && (
              <circle
                cx={x}
                cy={50}
                r={10}
                fill="none"
                stroke="var(--accent)"
                strokeWidth={1.4}
                opacity={0.9}
              />
            )}
            {/* generous hit target */}
            <circle className="reel-hit" cx={x} cy={50} r={14} fill="transparent" />
            <circle
              cx={x}
              cy={50}
              r={latest || active ? 5.5 : 4}
              fill={dotFill}
              stroke={dotStroke}
              strokeWidth={1.5}
            />
            <text
              className="gm-label"
              x={x - 8}
              y={71}
              {...(active
                ? { fill: 'var(--accent)', fontWeight: 600 }
                : {})}
            >
              v{i}
            </text>
            {rem != null && (
              <text className="gm-axis" x={x - 24} y={85}>
                {rem} tasks left
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ── DagZ — the dependency DAG (driven by the reel selection) ─────────────────
// compose.html dagSVG 540-572. Nodes carry absolute x/y from the adapter; node
// .st already encodes the selected-revision reframing (added / ghost). Critical
// path = accent spine (edge.crit); ghost endpoints → dashed faint edges.

interface DagZProps {
  plan: ZPlan;
  selectedRevision: number | null;
}

interface NodeGlyph {
  glyph: string;
  glyphColor: string;
  stroke: string;
  strokeWidth: number;
  dashed: boolean;
  ghost: boolean;
  tip: string;
}

function nodeGlyph(st: ZPlanNode['st']): NodeGlyph {
  switch (st) {
    case 'done':
      return {
        glyph: '✓',
        glyphColor: 'var(--good)',
        stroke: 'var(--good)',
        strokeWidth: 1.2,
        dashed: false,
        ghost: false,
        tip: 'done',
      };
    case 'running':
      return {
        glyph: '●',
        glyphColor: 'var(--accent)',
        stroke: 'var(--accent)',
        strokeWidth: 2,
        dashed: false,
        ghost: false,
        tip: 'running',
      };
    case 'added':
      return {
        glyph: '○',
        glyphColor: 'var(--accent)',
        stroke: 'var(--accent)',
        strokeWidth: 2,
        dashed: false,
        ghost: false,
        tip: 'added at this revision',
      };
    case 'ghost':
      return {
        glyph: '✕',
        glyphColor: 'var(--bad)',
        stroke: 'var(--rule)',
        strokeWidth: 1.2,
        dashed: true,
        ghost: true,
        tip: 'dropped since the prior revision',
      };
    default: // 'pending'
      return {
        glyph: '○',
        glyphColor: 'var(--ink-faint)',
        stroke: 'var(--ink-faint)',
        strokeWidth: 1.2,
        dashed: false,
        ghost: false,
        tip: 'pending',
      };
  }
}

function DagZ({ plan, selectedRevision }: DagZProps) {
  const W = 940;
  const H = 216;
  const selectTask = useUiStore.getState().selectTask;

  // Node lookup by task id (edges reference ids, not array indices).
  const byId = new Map<string, ZPlanNode>();
  for (const node of plan.nodes) byId.set(node.tid, node);

  const selStratum =
    selectedRevision != null
      ? plan.strata.find((s) => s.v === selectedRevision)
      : undefined;
  const caption = selStratum
    ? `v${selStratum.v} · ○ accent = added at this revision · dashed = dropped since the prior revision`
    : 'the accent spine = the critical path · ghost = dropped at replan';

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`plan dependency DAG${selStratum ? ` · v${selStratum.v}` : ''}`}
    >
      {/* edges first, under the nodes */}
      {plan.edges.map((e, i) => {
        const A = byId.get(e.from);
        const B = byId.get(e.to);
        if (!A || !B) return null;
        const x0 = A.x + BW / 2;
        const y0 = A.y;
        const x1 = B.x - BW / 2;
        const y1 = B.y;
        const c = (x0 + x1) / 2;
        const eGhost = A.st === 'ghost' || B.st === 'ghost';
        const stroke = eGhost
          ? 'var(--ink-faint)'
          : e.crit
            ? 'var(--accent)'
            : 'var(--ink-faint)';
        const extra: React.SVGProps<SVGPathElement> = eGhost
          ? { strokeDasharray: '3 4', opacity: 0.45 }
          : e.crit
            ? { strokeWidth: 2 }
            : { strokeWidth: 1, opacity: 0.6 };
        return (
          <path
            key={`e-${i}`}
            d={`M${x0},${y0} C ${c},${y0} ${c},${y1} ${x1},${y1}`}
            fill="none"
            stroke={stroke}
            vectorEffect="non-scaling-stroke"
            {...extra}
          >
            <title>
              {e.from} → {e.to}
              {e.crit && !eGhost ? ' · critical path' : ''}
            </title>
          </path>
        );
      })}

      {/* nodes */}
      {plan.nodes.map((node) => {
        const g = nodeGlyph(node.st);
        const x = node.x;
        const y = node.y;
        return (
          <g
            key={node.tid}
            style={{ cursor: 'pointer' }}
            onClick={() => selectTask(node.tid)}
          >
            <rect
              x={x - BW / 2}
              y={y - BH / 2}
              width={BW}
              height={BH}
              rx={4}
              fill="var(--panel)"
              stroke={g.stroke}
              strokeWidth={g.strokeWidth}
              vectorEffect="non-scaling-stroke"
              {...(g.dashed ? { strokeDasharray: '4 3', opacity: 0.55 } : {})}
            >
              <title>
                {node.tid} · {g.tip}
              </title>
            </rect>
            <text
              x={x - BW / 2 + 9}
              y={y + 3.5}
              fontSize={10}
              fill={g.ghost ? 'var(--ink-faint)' : 'var(--ink-soft)'}
            >
              {truncate(node.title, 14)}
            </text>
            <text
              x={x + BW / 2 - 15}
              y={y + 4}
              fontSize={9.5}
              fill={g.glyphColor}
            >
              {g.glyph}
            </text>
          </g>
        );
      })}

      <text className="gm-faint" x={10} y={H - 8}>
        {caption}
      </text>
    </svg>
  );
}

/** Clip a node label to fit the box (the study used short synthetic ids). */
function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}
