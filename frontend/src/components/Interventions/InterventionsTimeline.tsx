// Unified intervention timeline strip (issues #69, #74).
//
// Renders a horizontal strip of markers — one per Intervention — that sits
// above the Gantt in planning view. Markers are colour-coded by source
// (user / drift / goldfive), glyph-coded by kind (diamond / circle /
// chevron / square), and ringed when severity ≥ warning. Hover surfaces a
// deterministic popover (anchored to the marker, not the cursor). When two
// rows land within ~2% of the strip width of each other they collapse into
// a single "N" cluster badge whose popover lists the group.
//
// Stability contract (#74 issue #1): the strip never recomputes marker X
// from the outer `endMs` prop during a re-render caused by hover / tooltip
// state. Instead, it captures a `spanEndMs` snapshot on mount and advances
// it on a coarse 1s tick. This guarantees hovering a marker never shifts
// the other markers' X positions on a live session where `endMs` is being
// recomputed by the parent on every frame.
//
// Rendering is tree-agnostic: the component never inspects kind taxonomies
// or makes domain-specific decisions. Any kind string the server emits
// renders uniformly — new drift kinds added to goldfive tomorrow work
// without a frontend change. The only taxonomy knowledge here is the
// user/drift/goldfive "source" trichotomy, which the deriver already
// assigns in `lib/interventions.ts`.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { TaskPlan } from '../../gantt/types';
import {
  SOURCE_COLOR,
  type InterventionRow,
} from '../../lib/interventions';
import './InterventionsTimeline.css';

interface InterventionsTimelineProps {
  rows: readonly InterventionRow[];
  // Session-relative ms window the strip should span. When the planning
  // view draws multiple plans stacked, each plan passes its own window so
  // the markers align to the DAG above. `endMs` may advance as the session
  // progresses; the component snapshots it internally to avoid jitter.
  startMs: number;
  endMs: number;
  // Optional: all plan revs so the "jump to rev" button can find the target.
  revs?: readonly TaskPlan[];
  // Callback fired when the user presses the jump-to-rev button. The
  // component does not navigate on its own — this keeps the timeline
  // reusable from both planning and trajectory views.
  onJumpToRevision?: (revisionIndex: number) => void;
  // Width hint from the parent container. If omitted the strip expands to
  // fill its container.
  width?: number;
  // Internal test hook: skip the live-end rAF tick and use endMs verbatim
  // for snapshotting. Production callers should never set this.
  _liveTickMs?: number;
}

const STRIP_HEIGHT = 48;
const STRIP_PAD_X = 14;
const AXIS_Y = STRIP_HEIGHT - 10;
const MARKER_Y = STRIP_HEIGHT / 2 - 4;

// Cluster collision threshold — markers whose centres are within this
// fraction of the strip width collapse into a single badge.
const CLUSTER_THRESHOLD_FRAC = 0.02;
const CLUSTER_THRESHOLD_MIN_PX = 14;

// Default live-end refresh cadence. Coarse enough that hovering never
// coincides with a tick.
const DEFAULT_LIVE_TICK_MS = 1000;

// ---- Axis tick generation ------------------------------------------------

interface AxisTick {
  atMs: number;
  label: string;
}

function pickTickStepMs(spanMs: number): number {
  // Aim for 4–8 ticks across the strip.
  if (spanMs <= 60_000) return 10_000;           // 10s
  if (spanMs <= 120_000) return 30_000;          // 30s
  if (spanMs <= 600_000) return 60_000;          // 1m
  if (spanMs <= 1_800_000) return 300_000;       // 5m
  if (spanMs <= 3_600_000) return 600_000;       // 10m
  return 1_800_000;                              // 30m
}

function axisTickLabel(relMs: number): string {
  if (relMs <= 0) return '0m';
  if (relMs < 60_000) return `${Math.round(relMs / 1000)}s`;
  const m = Math.floor(relMs / 60_000);
  const s = Math.round((relMs % 60_000) / 1000);
  if (s === 0) return `${m}m`;
  return `${m}m${s}s`;
}

function buildAxisTicks(startMs: number, endMs: number): AxisTick[] {
  const span = Math.max(1, endMs - startMs);
  const step = pickTickStepMs(span);
  const ticks: AxisTick[] = [];
  for (let t = 0; t <= span; t += step) {
    ticks.push({ atMs: startMs + t, label: axisTickLabel(t) });
    if (ticks.length > 12) break; // safety guard
  }
  return ticks;
}

// ---- Outcome label formatting -------------------------------------------

function labelForOutcome(outcome: string): string {
  if (!outcome) return 'pending';
  if (outcome.startsWith('plan_revised:r')) {
    return `→ rev ${outcome.slice('plan_revised:r'.length)}`;
  }
  if (outcome.startsWith('cascade_cancel:')) {
    const rest = outcome.slice('cascade_cancel:'.length).replace('_', ' ');
    return `→ cancel ${rest}`;
  }
  return `→ ${outcome}`;
}

function fmtAt(atMs: number): string {
  if (!Number.isFinite(atMs) || atMs < 0) return '';
  const total = Math.max(0, Math.floor(atMs / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ---- Glyph selection -----------------------------------------------------

type Glyph = 'diamond' | 'diamond-x' | 'circle' | 'chevron' | 'square';

function glyphFor(row: InterventionRow): Glyph {
  // Kind-driven glyph selection. We look at the normalised kind emitted by
  // the deriver in lib/interventions.ts — "STEER", "CANCEL", "LOOPING_…",
  // "CASCADE_CANCEL", "REFINE_RETRY", etc. Unknown kinds fall back to the
  // source's default glyph so new server-side kinds render sanely.
  const kind = (row.kind || '').toUpperCase();
  if (row.source === 'user') {
    if (kind === 'CANCEL') return 'diamond-x';
    return 'diamond';
  }
  if (row.source === 'goldfive') {
    return 'square';
  }
  // drift
  if (row.outcome && row.outcome.startsWith('plan_revised:')) return 'chevron';
  return 'circle';
}

function severityRingColor(severity: string): string | null {
  const s = (severity || '').toLowerCase();
  if (s === 'critical') return '#e06070';
  if (s === 'warning') return '#f59e0b';
  return null;
}

// ---- Cluster grouping ----------------------------------------------------

interface Plotted {
  row: InterventionRow;
  cx: number;
}

interface Cluster {
  cx: number;
  rows: InterventionRow[];
}

function clusterPlotted(plotted: Plotted[], thresholdPx: number): Cluster[] {
  if (plotted.length === 0) return [];
  // Input is already atMs-sorted by the deriver; still sort by cx to be
  // robust if upstream order changes.
  const sorted = [...plotted].sort((a, b) => a.cx - b.cx);
  const out: Cluster[] = [];
  const rawMembers: number[][] = []; // parallel arr of raw cx's per cluster
  for (const p of sorted) {
    const last = out[out.length - 1];
    if (last && p.cx - last.cx <= thresholdPx) {
      last.rows.push(p.row);
      rawMembers[rawMembers.length - 1].push(p.cx);
      // Re-centre on the running mean so the badge doesn't drift left as
      // more markers merge into it.
      const arr = rawMembers[rawMembers.length - 1];
      last.cx = arr.reduce((sum, cx) => sum + cx, 0) / arr.length;
    } else {
      out.push({ cx: p.cx, rows: [p.row] });
      rawMembers.push([p.cx]);
    }
  }
  return out;
}

// ---- Stable live-end snapshot -------------------------------------------

/**
 * Returns a monotonically non-decreasing `spanEndMs` that advances only on
 * a coarse timer, not on every re-render. This is the fix for the "markers
 * jump on hover" bug (#74): the parent recomputes `endMs` on every render,
 * so if we anchored X to it directly, hover-induced re-renders would shift
 * every marker left by a fraction of a pixel.
 */
function useStableSpanEnd(endMs: number, tickMs: number): number {
  // The anchor is stored in React state and updated only at the two legal
  // moments:
  //   1. `endMs` regressed (parent swapped to a narrower window — e.g. a
  //      different plan). Snap down during render using the
  //      "adjust state while rendering" pattern. See:
  //      https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  //   2. The coarse tick fired — sample `endMs` into the anchor via the
  //      interval callback below.
  // For all other renders (same tick, `endMs` grew or unchanged) the
  // state is stable, so marker X positions don't move.
  const [spanEnd, setSpanEnd] = useState(endMs);

  // Regression detection — adjust state during render. React discards
  // the current render and re-runs with the new state before committing,
  // so no flicker and no cascading-render warning.
  if (endMs < spanEnd) {
    setSpanEnd(endMs);
  }

  // Growth path — coarse tick pulls the current `endMs` into the anchor.
  useEffect(() => {
    if (tickMs <= 0) return;
    const handle = window.setInterval(() => {
      setSpanEnd((prev) => (endMs > prev ? endMs : prev));
    }, tickMs);
    return () => window.clearInterval(handle);
  }, [endMs, tickMs]);

  return endMs < spanEnd ? endMs : spanEnd;
}

// ---- Entrance animation helper ------------------------------------------

function useSeenKeys(keys: readonly string[]): Set<string> {
  // Track which row keys we have already rendered so we can mark freshly
  // arrived rows with `data-new="true"` exactly once. The seen-set lives
  // in state so it's never mutated during render (satisfying the
  // `react-hooks/refs` lint rule), but we only ever GROW the set — never
  // shrink it — so consumers comparing successive renders see a stable
  // monotone sequence.
  const [seen, setSeen] = useState<Set<string>>(() => new Set());
  const [newlyAdded, setNewlyAdded] = useState<Set<string>>(() => new Set());
  const firstRenderRef = useRef(true);

  useEffect(() => {
    // First render: seed the seen-set with every current key and mark
    // nothing as new. From the user's perspective the rows were already
    // there on mount, so we don't want an entrance animation for them.
    if (firstRenderRef.current) {
      firstRenderRef.current = false;
      setSeen(new Set(keys));
      setNewlyAdded(new Set());
      return;
    }
    const added = new Set<string>();
    const next = new Set(seen);
    for (const k of keys) {
      if (!next.has(k)) {
        added.add(k);
        next.add(k);
      }
    }
    if (added.size > 0) {
      setSeen(next);
      setNewlyAdded(added);
    } else if (newlyAdded.size > 0) {
      // Clear the "new" flag on the subsequent commit so the entrance
      // animation only fires once per row.
      setNewlyAdded(new Set());
    }
    // Intentionally omit `seen` / `newlyAdded` from the dep list: they
    // are internal state updated by this very effect and including them
    // causes a render loop. `keys` is the only external trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys]);

  return newlyAdded;
}

// =========================================================================
// Component
// =========================================================================

export function InterventionsTimeline({
  rows,
  startMs,
  endMs,
  revs,
  onJumpToRevision,
  width,
  _liveTickMs,
}: InterventionsTimelineProps) {
  // Hover/selection state. Hover drives the popover; selection (click)
  // pins the popover so the user can interact with the Jump button.
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [pinnedKey, setPinnedKey] = useState<string | null>(null);

  const actualWidth = width ?? 480;

  // Snapshot endMs so hover re-renders don't shift markers (#74 issue #1).
  const spanEndMs = useStableSpanEnd(
    endMs,
    _liveTickMs ?? DEFAULT_LIVE_TICK_MS,
  );
  const span = Math.max(1, spanEndMs - startMs);

  // --- Plot + cluster ----------------------------------------------------

  const plotted: Plotted[] = useMemo(() => {
    return rows.map((row) => {
      const tNorm = Math.min(1, Math.max(0, (row.atMs - startMs) / span));
      const cx = STRIP_PAD_X + tNorm * (actualWidth - STRIP_PAD_X * 2);
      return { row, cx };
    });
  }, [rows, startMs, span, actualWidth]);

  const clusterThresholdPx = Math.max(
    CLUSTER_THRESHOLD_MIN_PX,
    actualWidth * CLUSTER_THRESHOLD_FRAC,
  );
  const clusters = useMemo(
    () => clusterPlotted(plotted, clusterThresholdPx),
    [plotted, clusterThresholdPx],
  );

  // Entrance-animation tracking.
  const rowKeys = useMemo(() => rows.map((r) => r.key), [rows]);
  const newlyAdded = useSeenKeys(rowKeys);

  // Axis ticks.
  const axisTicks = useMemo(
    () => buildAxisTicks(startMs, startMs + span),
    [startMs, span],
  );

  // --- Popover wiring ----------------------------------------------------

  const activeKey = pinnedKey ?? hoverKey;
  const activeCluster = useMemo(() => {
    if (!activeKey) return null;
    for (const c of clusters) {
      if (c.rows.some((r) => r.key === activeKey)) return c;
    }
    return null;
  }, [activeKey, clusters]);
  const activeRow = activeKey
    ? rows.find((r) => r.key === activeKey) ?? null
    : null;

  const closePopover = useCallback(() => {
    setHoverKey(null);
    setPinnedKey(null);
  }, []);

  // Dismiss a pinned popover when the user clicks outside the strip.
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!pinnedKey) return;
    const onDocClick = (e: MouseEvent) => {
      const root = rootRef.current;
      if (root && e.target instanceof Node && !root.contains(e.target)) {
        setPinnedKey(null);
        setHoverKey(null);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [pinnedKey]);

  // --- Render ------------------------------------------------------------

  return (
    <div
      ref={rootRef}
      className="hg-interventions-strip"
      data-testid="interventions-timeline"
      style={{ width: width ? `${width}px` : '100%' }}
    >
      <svg
        className="hg-interventions-strip__svg"
        width="100%"
        height={STRIP_HEIGHT}
        viewBox={`0 0 ${actualWidth} ${STRIP_HEIGHT}`}
        preserveAspectRatio="none"
      >
        {/* Rule */}
        <line
          className="hg-interventions-strip__rule"
          x1={STRIP_PAD_X}
          y1={MARKER_Y}
          x2={actualWidth - STRIP_PAD_X}
          y2={MARKER_Y}
        />

        {/* Axis ticks */}
        {axisTicks.map((tick, i) => {
          const tNorm = Math.min(1, Math.max(0, (tick.atMs - startMs) / span));
          const x = STRIP_PAD_X + tNorm * (actualWidth - STRIP_PAD_X * 2);
          return (
            <g
              key={`tick-${i}`}
              className="hg-interventions-strip__tick"
              data-testid={`axis-tick-${i}`}
            >
              <line
                className="hg-interventions-strip__tick-mark"
                x1={x}
                y1={MARKER_Y - 2}
                x2={x}
                y2={MARKER_Y + 2}
              />
              <text
                className="hg-interventions-strip__tick-label"
                x={x}
                y={AXIS_Y + 6}
                textAnchor="middle"
              >
                {tick.label}
              </text>
            </g>
          );
        })}

        {/* Markers / clusters */}
        {clusters.map((cluster) => {
          if (cluster.rows.length === 1) {
            const row = cluster.rows[0];
            const isActive = activeKey === row.key;
            const isNew = newlyAdded.has(row.key);
            return (
              <Marker
                key={row.key}
                row={row}
                cx={cluster.cx}
                cy={MARKER_Y}
                selected={isActive}
                isNew={isNew}
                onMouseEnter={() => {
                  if (!pinnedKey) setHoverKey(row.key);
                }}
                onMouseLeave={() => {
                  if (!pinnedKey) setHoverKey(null);
                }}
                onClick={() =>
                  setPinnedKey((k) => (k === row.key ? null : row.key))
                }
              />
            );
          }
          // Cluster badge
          const representativeKey = cluster.rows[0].key;
          const isActive = cluster.rows.some((r) => r.key === activeKey);
          return (
            <ClusterBadge
              key={`cluster:${representativeKey}`}
              cluster={cluster}
              cx={cluster.cx}
              cy={MARKER_Y}
              selected={isActive}
              onMouseEnter={() => {
                if (!pinnedKey) setHoverKey(representativeKey);
              }}
              onMouseLeave={() => {
                if (!pinnedKey) setHoverKey(null);
              }}
              onClick={() =>
                setPinnedKey((k) =>
                  cluster.rows.some((r) => r.key === k)
                    ? null
                    : representativeKey,
                )
              }
            />
          );
        })}
      </svg>

      {/* Deterministic popover — positioned relative to the marker's cx in
          the strip's coord space (not the cursor). */}
      {activeCluster && activeRow && activeCluster.rows.length === 1 && (
        <InterventionPopover
          row={activeRow}
          revs={revs}
          anchorXPct={(activeCluster.cx / actualWidth) * 100}
          pinned={pinnedKey !== null}
          onClose={closePopover}
          onJumpToRevision={onJumpToRevision}
        />
      )}
      {activeCluster && activeCluster.rows.length > 1 && (
        <ClusterPopover
          cluster={activeCluster}
          anchorXPct={(activeCluster.cx / actualWidth) * 100}
          pinned={pinnedKey !== null}
          revs={revs}
          onClose={closePopover}
          onJumpToRevision={onJumpToRevision}
          onRowSelect={(key) => {
            setPinnedKey(key);
            setHoverKey(key);
          }}
        />
      )}
      {rows.length === 0 && (
        <div className="hg-interventions-strip__empty">
          No interventions recorded.
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------------
// Marker glyph
// -------------------------------------------------------------------------

interface MarkerProps {
  row: InterventionRow;
  cx: number;
  cy: number;
  selected: boolean;
  isNew: boolean;
  onMouseEnter(): void;
  onMouseLeave(): void;
  onClick(): void;
}

function Marker({
  row,
  cx,
  cy,
  selected,
  isNew,
  onMouseEnter,
  onMouseLeave,
  onClick,
}: MarkerProps) {
  const color = SOURCE_COLOR[row.source] ?? SOURCE_COLOR.goldfive;
  const glyph = glyphFor(row);
  const ringColor = severityRingColor(row.severity);
  const r = 6;
  const ringR = 9;

  const classes = [
    'hg-interventions-strip__marker',
    selected && 'hg-interventions-strip__marker--selected',
    isNew && 'hg-interventions-strip__marker--new',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <g
      className={classes}
      transform={`translate(${cx}, ${cy})`}
      data-testid={`intervention-marker-${row.key}`}
      data-source={row.source}
      data-kind={row.kind}
      data-glyph={glyph}
      data-severity={row.severity || ''}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onClick={onClick}
    >
      <title>{`${row.kind} (${row.source})${
        row.outcome ? ' · ' + labelForOutcome(row.outcome) : ''
      }`}</title>

      {ringColor && (
        <circle
          className="hg-interventions-strip__marker-ring"
          r={ringR}
          fill="none"
          stroke={ringColor}
          strokeWidth={1.5}
          strokeDasharray={
            row.severity === 'critical' ? undefined : '2 2'
          }
        />
      )}

      {/* Tap-target (invisible) — larger hit area for easy hover/click. */}
      <circle
        className="hg-interventions-strip__marker-hit"
        r={ringR + 4}
        fill="transparent"
      />

      {glyph === 'diamond' && (
        <rect
          className="hg-interventions-strip__marker-shape"
          x={-r}
          y={-r}
          width={r * 2}
          height={r * 2}
          fill={color}
          transform="rotate(45)"
        />
      )}
      {glyph === 'diamond-x' && (
        <g>
          <rect
            className="hg-interventions-strip__marker-shape"
            x={-r}
            y={-r}
            width={r * 2}
            height={r * 2}
            fill={color}
            transform="rotate(45)"
          />
          <path
            className="hg-interventions-strip__marker-overlay"
            d={`M ${-r / 2} ${-r / 2} L ${r / 2} ${r / 2} M ${-r / 2} ${r / 2} L ${r / 2} ${-r / 2}`}
            stroke="#0b0d12"
            strokeWidth={1.4}
          />
        </g>
      )}
      {glyph === 'circle' && (
        <circle
          className="hg-interventions-strip__marker-shape"
          r={r}
          fill={color}
        />
      )}
      {glyph === 'chevron' && (
        // Up-chevron: signals "this intervention produced a plan revision".
        <path
          className="hg-interventions-strip__marker-shape"
          d={`M ${-r} ${r * 0.55} L 0 ${-r * 0.8} L ${r} ${r * 0.55} L ${r * 0.45} ${r * 0.55} L 0 ${-r * 0.15} L ${-r * 0.45} ${r * 0.55} Z`}
          fill={color}
        />
      )}
      {glyph === 'square' && (
        <rect
          className="hg-interventions-strip__marker-shape"
          x={-r + 1}
          y={-r + 1}
          width={(r - 1) * 2}
          height={(r - 1) * 2}
          fill="none"
          stroke={color}
          strokeWidth={1.6}
        />
      )}
    </g>
  );
}

// -------------------------------------------------------------------------
// Cluster badge
// -------------------------------------------------------------------------

interface ClusterBadgeProps {
  cluster: Cluster;
  cx: number;
  cy: number;
  selected: boolean;
  onMouseEnter(): void;
  onMouseLeave(): void;
  onClick(): void;
}

function ClusterBadge({
  cluster,
  cx,
  cy,
  selected,
  onMouseEnter,
  onMouseLeave,
  onClick,
}: ClusterBadgeProps) {
  // Pick the dominant source for the badge colour — the most "urgent" wins:
  // user > drift > goldfive (user steers are the most interesting density).
  const sources = new Set(cluster.rows.map((r) => r.source));
  const color = sources.has('user')
    ? SOURCE_COLOR.user
    : sources.has('drift')
      ? SOURCE_COLOR.drift
      : SOURCE_COLOR.goldfive;

  // If any row in the cluster is severity ≥ warning, ring the badge.
  const worstSeverity = cluster.rows.reduce((worst, r) => {
    const s = (r.severity || '').toLowerCase();
    if (s === 'critical') return 'critical';
    if (s === 'warning' && worst !== 'critical') return 'warning';
    return worst;
  }, '');
  const ringColor = severityRingColor(worstSeverity);

  const classes = [
    'hg-interventions-strip__cluster',
    selected && 'hg-interventions-strip__cluster--selected',
  ]
    .filter(Boolean)
    .join(' ');

  const testId = `intervention-cluster-${cluster.rows[0].key}`;

  return (
    <g
      className={classes}
      transform={`translate(${cx}, ${cy})`}
      data-testid={testId}
      data-count={cluster.rows.length}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onClick={onClick}
    >
      <title>{`${cluster.rows.length} interventions`}</title>
      {ringColor && (
        <circle
          className="hg-interventions-strip__cluster-ring"
          r={13}
          fill="none"
          stroke={ringColor}
          strokeWidth={1.5}
        />
      )}
      <circle
        className="hg-interventions-strip__cluster-shape"
        r={10}
        fill={color}
      />
      <text
        className="hg-interventions-strip__cluster-count"
        y={3.5}
        textAnchor="middle"
      >
        {cluster.rows.length}
      </text>
    </g>
  );
}

// -------------------------------------------------------------------------
// Popover (single row)
// -------------------------------------------------------------------------

const BODY_PREVIEW_CHARS = 120;

interface InterventionPopoverProps {
  row: InterventionRow;
  revs?: readonly TaskPlan[];
  anchorXPct: number; // 0–100, % of strip width
  pinned: boolean;
  onClose(): void;
  onJumpToRevision?: (revisionIndex: number) => void;
}

function InterventionPopover({
  row,
  revs,
  anchorXPct,
  pinned,
  onClose,
  onJumpToRevision,
}: InterventionPopoverProps) {
  const targetRev =
    row.planRevisionIndex > 0
      ? revs?.find((p) => (p.revisionIndex ?? 0) === row.planRevisionIndex) ??
        null
      : null;

  const bodyPreview =
    row.bodyOrReason && row.bodyOrReason.length > BODY_PREVIEW_CHARS
      ? row.bodyOrReason.slice(0, BODY_PREVIEW_CHARS).trimEnd() + '…'
      : row.bodyOrReason;

  // Clamp the anchor away from the edges so the popover never clips.
  const clamped = Math.min(92, Math.max(8, anchorXPct));

  return (
    <div
      className={`hg-interventions-popover${pinned ? ' hg-interventions-popover--pinned' : ''}`}
      data-testid={pinned ? 'intervention-card' : 'intervention-popover'}
      data-source={row.source}
      style={{ left: `${clamped}%` }}
      role="dialog"
    >
      <header className="hg-interventions-popover__head">
        <span
          className="hg-interventions-popover__chip"
          data-source={row.source}
          title={`source: ${row.source}`}
        >
          {row.source}
        </span>
        <span className="hg-interventions-popover__kind">{row.kind}</span>
        <span className="hg-interventions-popover__at">{fmtAt(row.atMs)}</span>
        {row.severity && (
          <span
            className="hg-interventions-popover__severity"
            data-severity={row.severity}
          >
            {row.severity}
          </span>
        )}
        {pinned && (
          <button
            type="button"
            className="hg-interventions-popover__close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        )}
      </header>
      {row.author && (
        <div className="hg-interventions-popover__author">
          by <strong>{row.author}</strong>
        </div>
      )}
      {bodyPreview && (
        <div className="hg-interventions-popover__body">{bodyPreview}</div>
      )}
      <footer className="hg-interventions-popover__foot">
        <span className="hg-interventions-popover__outcome">
          {labelForOutcome(row.outcome)}
        </span>
        {row.planRevisionIndex > 0 && targetRev && onJumpToRevision && (
          <button
            type="button"
            className="hg-interventions-popover__jump"
            onClick={() => onJumpToRevision(row.planRevisionIndex)}
            data-testid="intervention-card__jump"
          >
            Jump to rev {row.planRevisionIndex}
          </button>
        )}
      </footer>
    </div>
  );
}

// -------------------------------------------------------------------------
// Popover (cluster)
// -------------------------------------------------------------------------

interface ClusterPopoverProps {
  cluster: Cluster;
  anchorXPct: number;
  pinned: boolean;
  revs?: readonly TaskPlan[];
  onClose(): void;
  onJumpToRevision?: (revisionIndex: number) => void;
  onRowSelect(key: string): void;
}

function ClusterPopover({
  cluster,
  anchorXPct,
  pinned,
  revs,
  onClose,
  onJumpToRevision,
  onRowSelect,
}: ClusterPopoverProps) {
  const clamped = Math.min(92, Math.max(8, anchorXPct));
  return (
    <div
      className={`hg-interventions-popover hg-interventions-popover--cluster${pinned ? ' hg-interventions-popover--pinned' : ''}`}
      data-testid="intervention-cluster-popover"
      style={{ left: `${clamped}%` }}
      role="dialog"
    >
      <header className="hg-interventions-popover__head">
        <span className="hg-interventions-popover__kind">
          {cluster.rows.length} interventions
        </span>
        {pinned && (
          <button
            type="button"
            className="hg-interventions-popover__close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        )}
      </header>
      <ul className="hg-interventions-popover__list">
        {cluster.rows.map((row) => {
          const preview =
            row.bodyOrReason && row.bodyOrReason.length > 60
              ? row.bodyOrReason.slice(0, 60).trimEnd() + '…'
              : row.bodyOrReason;
          const targetRev =
            row.planRevisionIndex > 0
              ? revs?.find(
                  (p) => (p.revisionIndex ?? 0) === row.planRevisionIndex,
                ) ?? null
              : null;
          return (
            <li
              key={row.key}
              className="hg-interventions-popover__list-item"
              data-source={row.source}
              data-testid={`intervention-cluster-item-${row.key}`}
            >
              <button
                type="button"
                className="hg-interventions-popover__list-button"
                onClick={() => onRowSelect(row.key)}
              >
                <span
                  className="hg-interventions-popover__chip"
                  data-source={row.source}
                >
                  {row.source}
                </span>
                <span className="hg-interventions-popover__kind">{row.kind}</span>
                <span className="hg-interventions-popover__at">{fmtAt(row.atMs)}</span>
              </button>
              {preview && (
                <div className="hg-interventions-popover__body">{preview}</div>
              )}
              {row.planRevisionIndex > 0 && targetRev && onJumpToRevision && (
                <button
                  type="button"
                  className="hg-interventions-popover__jump"
                  onClick={() => onJumpToRevision(row.planRevisionIndex)}
                >
                  → rev {row.planRevisionIndex}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
