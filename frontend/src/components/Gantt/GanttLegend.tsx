// GanttLegend — modal overlay that explains the icons, colors, shapes, and
// symbols used across the Gantt view. Colors below are read from
// frontend/src/theme/themes.ts (dark theme) and match the CSS custom
// properties resolved by src/gantt/colors.ts at render time. If the theme
// tokens ever change, update the values here as well — this legend is for
// humans, so the static values are fine for documentation purposes.

import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { useUiStore } from '../../state/uiStore';
import './GanttLegend.css';

// Real span-kind hues pulled from frontend/src/theme/themes.ts (dark theme).
// These map 1:1 onto --hg-kind-* CSS variables via gantt/colors.ts.
const KIND_COLORS = {
  INVOCATION: '#43474e',
  LLM_CALL: '#a8c8ff',
  TOOL_CALL: '#7cd5d2',
  USER_MESSAGE: '#7fdba0',
  AGENT_MESSAGE: '#9bf8bb',
  TRANSFER: '#ffd479',
  WAIT_FOR_HUMAN: '#ffb4ab',
  CUSTOM: '#8d9199',
} as const;

// MD3 error + status tokens (dark theme).
const ERROR = '#ffb4ab';
const ERROR_CONTAINER = '#93000a';
// Hard-coded in renderer.ts for agent CONNECTED dot.
const STATUS_OK = '#4caf50';
// Disconnected agent dot = outline token.
const STATUS_DISCONNECTED = '#8d9199';
// Amber used for "stuck" in GraphView / transport chips — matches the transfer
// hue so stuckness reads as a caution, not an error.
const STUCK_AMBER = '#ffd479';
// Hex literals hard-coded in GraphView.tsx for arrow colors.
const ARROW_TRANSFER = '#e8953a';
const ARROW_DELEGATION = '#5b8def';
const ARROW_RETURN = '#888';
// Graph-view status dot colors (GraphView.STATUS_COLOR).
const GRAPH_STATUS_CONNECTED = '#4caf7d';
const GRAPH_STATUS_CRASHED = '#e06070';
const GRAPH_STATUS_STUCK = '#f59e0b';
// Sample agent color used for the Graph-view demo entries. Picked to stay
// readable against the legend's surface-container-highest background.
const SAMPLE_AGENT = '#a8c8ff';

// Icon glyphs drawn by the renderer on each span kind (see KIND_ICON in
// frontend/src/gantt/renderer.ts). Canvas draws these as plain text at 11px
// when a bar is >=12px wide. Mirror verbatim so the legend is faithful.
const KIND_GLYPH = {
  INVOCATION: '◉',
  LLM_CALL: '✦',
  TOOL_CALL: '⚙',
  USER_MESSAGE: '👤',
  AGENT_MESSAGE: '💬',
  TRANSFER: '↪',
  WAIT_FOR_HUMAN: '⏸',
  PLANNED: '◌',
  CUSTOM: '•',
} as const;

interface Item {
  symbol: ReactNode;
  label: string;
  description: string;
}
interface Section {
  title: string;
  items: Item[];
}

export function GanttLegend() {
  const open = useUiStore((s) => s.legendOpen);
  const close = useUiStore((s) => s.closeLegend);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  if (!open) return null;

  const sections: Section[] = [
    {
      title: 'Span kinds',
      items: [
        {
          symbol: <Swatch color={KIND_COLORS.INVOCATION} opacity={0.6} />,
          label: 'Invocation',
          description: 'Top-level agent turn. Container bar that recedes behind its children.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} />,
          label: 'LLM call',
          description: 'Model request emitted by the agent.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.TOOL_CALL} />,
          label: 'Tool call',
          description: 'Function/tool invocation issued by the agent.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.USER_MESSAGE} />,
          label: 'User message',
          description: 'Inbound user turn delivered to the agent.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.AGENT_MESSAGE} />,
          label: 'Agent message',
          description: 'Outbound model/agent reply surfaced to the user.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.TRANSFER} />,
          label: 'Transfer',
          description: 'Hand-off between agents (sub-agent invocation or delegation).',
        },
        {
          symbol: <Swatch color={KIND_COLORS.WAIT_FOR_HUMAN} />,
          label: 'Wait for human',
          description: 'Agent is blocked waiting on a human-in-the-loop signal.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.CUSTOM} />,
          label: 'Custom / planned',
          description: 'Framework-specific span. PLANNED spans render dashed at 30% opacity.',
        },
      ],
    },
    {
      title: 'States',
      items: [
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} className="hg-legend__breathe" />,
          label: 'Running',
          description: 'Span is open (endMs=null); bar breathes on a 2s loop and extends with live time.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} />,
          label: 'Completed',
          description: 'Span finished successfully — standard solid fill.',
        },
        {
          symbol: <Swatch color={ERROR} />,
          label: 'Failed',
          description: 'Span errored. Fill switches to the MD3 error color and a red warning glyph is drawn.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} opacity={0.3} striped />,
          label: 'Cancelled',
          description: 'Span was cancelled before completion: diagonal hatch at 30% opacity.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} opacity={0.3} />,
          label: 'Replaced',
          description: 'Span was superseded by a REPLACES link; rendered at 30% opacity.',
        },
        {
          symbol: <Swatch color={KIND_COLORS.LLM_CALL} opacity={0.4} />,
          label: 'Pending',
          description: 'Span created but not yet running — dimmed to 40% opacity.',
        },
        {
          symbol: (
            <Swatch
              color={ERROR_CONTAINER}
              stroke={ERROR}
              className="hg-legend__pulse"
            />
          ),
          label: 'Awaiting human',
          description: 'Blocked on a human decision: error-container fill, red outline, 1s pulse.',
        },
      ],
    },
    {
      title: 'Icons & glyphs',
      items: [
        {
          symbol: <BarGlyph color={KIND_COLORS.INVOCATION} glyph={KIND_GLYPH.INVOCATION} opacity={0.6} />,
          label: 'Invocation ◉',
          description: 'Target-ring glyph drawn on invocation bars ≥12px wide.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.LLM_CALL} glyph={KIND_GLYPH.LLM_CALL} />,
          label: 'LLM call ✦',
          description: 'Four-point sparkle marks model requests. Running LLM spans also draw 1px "streaming tick" marks along the trailing edge.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.TOOL_CALL} glyph={KIND_GLYPH.TOOL_CALL} />,
          label: 'Tool call ⚙',
          description: 'Gear glyph for function/tool invocations.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.USER_MESSAGE} glyph={KIND_GLYPH.USER_MESSAGE} />,
          label: 'User message 👤',
          description: 'Person glyph for inbound user turns.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.AGENT_MESSAGE} glyph={KIND_GLYPH.AGENT_MESSAGE} />,
          label: 'Agent message 💬',
          description: 'Speech-bubble glyph for outbound agent replies.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.TRANSFER} glyph={KIND_GLYPH.TRANSFER} />,
          label: 'Transfer ↪',
          description: 'Return-arrow glyph on hand-off bars. Cross-agent bezier edges originate from these spans.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.WAIT_FOR_HUMAN} glyph={KIND_GLYPH.WAIT_FOR_HUMAN} />,
          label: 'Wait for human ⏸',
          description: 'Pause glyph for spans blocked on a human decision.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.CUSTOM} glyph={KIND_GLYPH.PLANNED} opacity={0.3} dashed />,
          label: 'Planned ◌',
          description: 'Dotted-circle glyph, dashed outline at 30% opacity — predicted future work.',
        },
        {
          symbol: <BarGlyph color={KIND_COLORS.CUSTOM} glyph={KIND_GLYPH.CUSTOM} />,
          label: 'Custom •',
          description: 'Bullet glyph for framework-specific spans.',
        },
        {
          symbol: <StreamingTickIcon color={KIND_COLORS.LLM_CALL} />,
          label: 'Streaming ticks',
          description: 'On a running LLM_CALL bar ≥8px wide, the renderer overlays thin white marks along the trailing edge — one per streaming_tick reported by the client.',
        },
        {
          symbol: <LinkEdgeIcon color={KIND_COLORS.TRANSFER} />,
          label: 'Cross-agent link',
          description: 'Cubic bezier drawn at 40% opacity between a TRANSFER span and the invoked child span, with a filled arrowhead at the target.',
        },
      ],
    },
    {
      title: 'Graph view',
      items: [
        {
          symbol: <LifelineIcon color={SAMPLE_AGENT} />,
          label: 'Agent lifeline',
          description: 'Dashed vertical in the per-agent color, 25% opacity, spans the full plot height below the agent header.',
        },
        {
          symbol: <AgentHeaderIcon color={SAMPLE_AGENT} />,
          label: 'Agent header',
          description: 'Rounded box tinted with the agent color at 15% fill. A 2px border + soft glow halo appears while the agent has a running invocation.',
        },
        {
          symbol: <AgentHeaderIcon color={GRAPH_STATUS_STUCK} pulse />,
          label: 'Stuck agent',
          description: 'Amber border and halo replace the agent color when Agent.stuck is true; header shows "⚠ stuck".',
        },
        {
          symbol: <ActivationIcon color={SAMPLE_AGENT} running />,
          label: 'Running activation',
          description: 'Tall 16px-wide box on the lifeline for an open INVOCATION span. 85% fill, 1.5px stroke, breathing pulse.',
        },
        {
          symbol: <ActivationIcon color={SAMPLE_AGENT} />,
          label: 'Completed activation',
          description: 'Same geometry at 55% fill, no stroke — marks a closed INVOCATION.',
        },
        {
          symbol: <ActivationIcon color={SAMPLE_AGENT} running thinking />,
          label: 'Thinking',
          description: 'Small pulsing blue dot on top of a running activation when the span has has_thinking=true.',
        },
        {
          symbol: <ArrowIcon color={ARROW_TRANSFER} width={2.5} />,
          label: 'Transfer arrow',
          description: 'Solid orange line with a filled arrowhead — explicit hand-off via a TRANSFER span with an INVOKED link.',
        },
        {
          symbol: <ArrowIcon color={ARROW_DELEGATION} width={1.5} dash="6 3" />,
          label: 'Delegation arrow',
          description: 'Blue dashed line — inferred from a cross-agent INVOCATION parent when no explicit transfer span exists.',
        },
        {
          symbol: <ArrowIcon color={ARROW_RETURN} width={1.2} dash="4 4" italic />,
          label: 'Return arrow',
          description: 'Grey dashed line drawn at the end of a delegated invocation, italic "↩ return" label.',
        },
        {
          symbol: <Dot color={GRAPH_STATUS_CONNECTED} />,
          label: 'Connected (graph)',
          description: 'Status dot tucked into the top-right of the agent header box.',
        },
        {
          symbol: <Dot color={GRAPH_STATUS_CRASHED} />,
          label: 'Crashed (graph)',
          description: 'Red status dot + the agent name renders with the error palette.',
        },
      ],
    },
    {
      title: 'Task plan',
      items: [
        {
          symbol: <TaskChipIcon color={SAMPLE_AGENT} variant="pending" />,
          label: 'Pending task',
          description: 'Dim surface fill at 25% opacity with the agent-colored border — task exists in the plan but has not started.',
        },
        {
          symbol: <TaskChipIcon color={SAMPLE_AGENT} variant="running" />,
          label: 'Running task',
          description: 'Agent color at 85% fill, 1px border. Mirrors the activation look so the strip reads the same way as the lifeline.',
        },
        {
          symbol: <TaskChipIcon color={SAMPLE_AGENT} variant="completed" />,
          label: 'Completed task',
          description: 'Agent color at 55% fill with a ✓ tick on the trailing edge.',
        },
        {
          symbol: <TaskChipIcon color={SAMPLE_AGENT} variant="failed" />,
          label: 'Failed task',
          description: 'Transparent fill, red dashed border (#e06070), and the task title strikes through.',
        },
        {
          symbol: <TaskChipIcon color={SAMPLE_AGENT} variant="cancelled" />,
          label: 'Cancelled task',
          description: 'Strike-through title over the pending chip style — planned but will not run.',
        },
        {
          symbol: <GhostActivationIcon color={SAMPLE_AGENT} />,
          label: 'Ghost activation',
          description: 'Dashed, 25%-opacity box placed at the task\'s predictedStartMs on the lifeline. Shows where the renderer expects real work to land.',
        },
        {
          symbol: <DepEdgeIcon />,
          label: 'Dependency edge',
          description: 'Grey (#9aa3b4) dashed cubic bezier between two task chips — encodes TaskPlan.edges from → to.',
        },
        {
          symbol: <Glyph>⊞</Glyph>,
          label: 'Render mode',
          description: 'Task plan overlay has three modes (Pre-strip / Ghost / Hybrid) picked via the Graph-view header dropdown. Pre-strip stacks chips to the left of each agent column, Ghost only draws predicted boxes on the lifeline, Hybrid shows both at reduced size.',
        },
      ],
    },
    {
      title: 'Status indicators',
      items: [
        {
          symbol: <Dot color={STATUS_OK} />,
          label: 'Agent connected',
          description: 'Green dot in the agent gutter — live stream healthy.',
        },
        {
          symbol: <Dot color={STUCK_AMBER} />,
          label: 'Stuck',
          description: 'Agent has a RUNNING invocation but no recent progress; flagged as potentially wedged.',
        },
        {
          symbol: <Dot color={ERROR} />,
          label: 'Crashed',
          description: 'Agent process crashed or errored — red dot in the gutter.',
        },
        {
          symbol: <Dot color={STATUS_DISCONNECTED} />,
          label: 'Disconnected',
          description: 'Agent stream closed cleanly; history preserved but no new spans expected.',
        },
      ],
    },
    {
      title: 'Interaction',
      items: [
        {
          symbol: <Glyph>▢</Glyph>,
          label: 'Click a bar',
          description: 'Opens the inspector drawer with payloads, attributes, and links.',
        },
        {
          symbol: <Glyph>◉</Glyph>,
          label: 'Hover a bar',
          description: 'Shows a popover with span name, status, duration, and latest payload summary.',
        },
        {
          symbol: <PulseDot color={KIND_COLORS.LLM_CALL} />,
          label: 'Breathing bar',
          description: 'Indicates the span is currently running and its end time is live.',
        },
        {
          symbol: <Swatch color="rgba(100,140,255,0.35)" stroke="rgba(100,140,255,0.75)" />,
          label: 'Minimap viewport',
          description: 'Blue rectangle in the minimap marks the main Gantt viewport. Click or drag to seek.',
        },
      ],
    },
    {
      title: 'Controls',
      items: [
        { symbol: <Glyph>⏸</Glyph>, label: 'Pause', description: 'Pause agents at next model boundary.' },
        { symbol: <Glyph>▶</Glyph>, label: 'Resume', description: 'Resume paused agents.' },
        { symbol: <Glyph>↩</Glyph>, label: 'Follow live', description: 'Return viewport to the live edge.' },
        { symbol: <Glyph>+ / −</Glyph>, label: 'Zoom', description: 'Widen or narrow the visible time window.' },
      ],
    },
  ];

  return (
    <div className="hg-legend-overlay" onClick={close} role="presentation">
      <div
        className="hg-legend"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Gantt legend"
        aria-modal="true"
      >
        <div className="hg-legend__header">
          <h2>Legend</h2>
          <button onClick={close} aria-label="Close legend" className="hg-legend__close">
            ✕
          </button>
        </div>
        <div className="hg-legend__body">
          {sections.map((section) => (
            <section key={section.title}>
              <h3>{section.title}</h3>
              <dl>
                {section.items.map((item, i) => (
                  <div key={i} className="hg-legend__item">
                    <dt>{item.symbol}</dt>
                    <dd>
                      <strong>{item.label}</strong>
                      <span>{item.description}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

// --- Symbol primitives -------------------------------------------------------

function Swatch({
  color,
  opacity = 0.85,
  striped = false,
  stroke,
  className,
}: {
  color: string;
  opacity?: number;
  striped?: boolean;
  stroke?: string;
  className?: string;
}) {
  return (
    <div
      className={className}
      style={{
        width: 22,
        height: 12,
        background: striped
          ? `repeating-linear-gradient(45deg, ${color}, ${color} 3px, transparent 3px, transparent 6px)`
          : color,
        opacity,
        borderRadius: 2,
        border: stroke ? `1px solid ${stroke}` : '1px solid rgba(255,255,255,0.1)',
      }}
    />
  );
}

function Dot({ color }: { color: string }) {
  return (
    <div
      style={{
        width: 10,
        height: 10,
        background: color,
        borderRadius: '50%',
      }}
    />
  );
}

function PulseDot({ color }: { color: string }) {
  return (
    <div
      className="hg-legend__pulse"
      style={{ background: color, width: 10, height: 10, borderRadius: '50%' }}
    />
  );
}

function Glyph({ children }: { children: ReactNode }) {
  return <span style={{ fontSize: 14, lineHeight: 1 }}>{children}</span>;
}

// --- Faithful SVG reproductions ---------------------------------------------
// All of these mirror primitives drawn by frontend/src/gantt/renderer.ts or
// frontend/src/components/shell/views/GraphView.tsx. Keep them in sync if the
// renderer changes shapes, dash patterns, or stroke widths.

// Gantt bar with the real unicode glyph the canvas text-renders on top. The
// renderer uses `${icon} ${name}` at 11px inside a rounded rect (radius 4);
// we shrink to ~26×14 and center the glyph so each kind reads like a
// miniature bar.
function BarGlyph({
  color,
  glyph,
  opacity = 0.85,
  dashed = false,
}: {
  color: string;
  glyph: string;
  opacity?: number;
  dashed?: boolean;
}) {
  return (
    <svg width={26} height={14} aria-hidden>
      <rect
        x={0.5}
        y={1}
        width={25}
        height={12}
        rx={3}
        fill={color}
        fillOpacity={opacity}
        stroke={dashed ? color : 'rgba(255,255,255,0.1)'}
        strokeWidth={1}
        strokeDasharray={dashed ? '2 2' : undefined}
      />
      <text
        x={13}
        y={7.5}
        textAnchor="middle"
        dominantBaseline="middle"
        fontSize={9}
        fill="rgba(0,0,0,0.7)"
      >
        {glyph}
      </text>
    </svg>
  );
}

// LLM_CALL streaming ticks — 1px white verticals inset into the trailing
// portion of a running bar at 0.65 alpha.
function StreamingTickIcon({ color }: { color: string }) {
  return (
    <svg width={26} height={14} aria-hidden>
      <rect x={0.5} y={1} width={25} height={12} rx={3} fill={color} fillOpacity={0.85} />
      {[10, 14, 18, 22].map((x) => (
        <rect key={x} x={x} y={4} width={1} height={6} fill="#ffffff" fillOpacity={0.65} />
      ))}
    </svg>
  );
}

// Cross-agent INVOKED link: bezier at 40% alpha + filled arrowhead.
function LinkEdgeIcon({ color }: { color: string }) {
  return (
    <svg width={30} height={16} aria-hidden>
      <defs>
        <marker id="lg-link-arrow" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={5} markerHeight={5} orient="auto">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
        </marker>
      </defs>
      <path
        d="M 2 4 C 12 4, 16 12, 26 12"
        stroke={color}
        strokeWidth={1.25}
        strokeOpacity={0.4}
        fill="none"
        markerEnd="url(#lg-link-arrow)"
      />
    </svg>
  );
}

// Graph-view agent lifeline — dashed vertical, agent color, 25% opacity.
function LifelineIcon({ color }: { color: string }) {
  return (
    <svg width={22} height={20} aria-hidden>
      <line x1={11} y1={1} x2={11} y2={19} stroke={color} strokeWidth={1} strokeDasharray="3 3" opacity={0.5} />
    </svg>
  );
}

// Graph-view agent header — rounded rect, fillOpacity 0.15, colored stroke.
// `pulse` enables the breathing halo reused from hg-legend__pulse.
function AgentHeaderIcon({ color, pulse = false }: { color: string; pulse?: boolean }) {
  return (
    <svg width={28} height={18} aria-hidden className={pulse ? 'hg-legend__breathe' : undefined}>
      <rect x={1} y={1} width={26} height={16} rx={4} fill={color} fillOpacity={0.15} stroke={color} strokeWidth={1.5} />
      <circle cx={23} cy={4.5} r={2} fill={color} />
    </svg>
  );
}

// Activation box — 16px wide tall rect on the lifeline.
function ActivationIcon({
  color,
  running = false,
  thinking = false,
}: {
  color: string;
  running?: boolean;
  thinking?: boolean;
}) {
  return (
    <svg width={22} height={22} aria-hidden>
      <line x1={11} y1={0} x2={11} y2={22} stroke={color} strokeWidth={0.75} strokeDasharray="2 2" opacity={0.4} />
      <rect
        x={5}
        y={3}
        width={12}
        height={16}
        rx={2}
        fill={color}
        fillOpacity={running ? 0.85 : 0.55}
        stroke={running ? color : 'none'}
        strokeWidth={running ? 1.5 : 0}
        className={running ? 'hg-legend__breathe' : undefined}
      />
      {thinking && (
        <circle cx={11} cy={7} r={2.2} fill="#a8c8ff" className="hg-legend__pulse" />
      )}
    </svg>
  );
}

// Horizontal arrow with a filled marker head. Matches the three arrow
// styles in GraphView: transfer (solid), delegation (dashed 6 3), return
// (dashed 4 4 grey).
function ArrowIcon({
  color,
  width,
  dash,
  italic = false,
}: {
  color: string;
  width: number;
  dash?: string;
  italic?: boolean;
}) {
  const id = `lg-arr-${color.replace('#', '')}-${width}`;
  return (
    <svg width={30} height={12} aria-hidden style={italic ? { fontStyle: 'italic' } : undefined}>
      <defs>
        <marker id={id} viewBox="0 0 10 10" refX={8} refY={5} markerWidth={6} markerHeight={6} orient="auto">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
        </marker>
      </defs>
      <line
        x1={2}
        y1={6}
        x2={26}
        y2={6}
        stroke={color}
        strokeWidth={width}
        strokeDasharray={dash}
        markerEnd={`url(#${id})`}
      />
    </svg>
  );
}

// Pre-strip task chip variants. Mirror the fills from GraphView.tsx where
// each task chip picks its style from Task.status.
function TaskChipIcon({
  color,
  variant,
}: {
  color: string;
  variant: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
}) {
  const pending = variant === 'pending';
  const running = variant === 'running';
  const done = variant === 'completed';
  const failed = variant === 'failed';
  const cancelled = variant === 'cancelled';
  const fill = failed
    ? 'transparent'
    : running || done
      ? color
      : '#1a1f2a';
  const fillOpacity = running ? 0.85 : done ? 0.55 : pending || cancelled ? 0.25 : 1;
  const stroke = failed ? '#e06070' : color;
  return (
    <svg width={30} height={14} aria-hidden>
      <rect
        x={1}
        y={1}
        width={28}
        height={12}
        rx={3}
        fill={fill}
        fillOpacity={fillOpacity}
        stroke={stroke}
        strokeWidth={1}
        strokeDasharray={failed ? '2 2' : undefined}
      />
      {done && (
        <text x={24} y={7.5} fontSize={9} fill="#ffffff" textAnchor="middle" dominantBaseline="middle">
          ✓
        </text>
      )}
      {cancelled && (
        <line x1={3} y1={7} x2={27} y2={7} stroke="#ffffff" strokeOpacity={0.6} strokeWidth={1} />
      )}
    </svg>
  );
}

// Ghost activation box drawn at predictedStartMs — dashed 2 2, 25% fill.
function GhostActivationIcon({ color }: { color: string }) {
  return (
    <svg width={22} height={22} aria-hidden>
      <line x1={11} y1={0} x2={11} y2={22} stroke={color} strokeWidth={0.75} strokeDasharray="2 2" opacity={0.4} />
      <rect
        x={5}
        y={3}
        width={12}
        height={16}
        rx={2}
        fill={color}
        fillOpacity={0.25}
        stroke={color}
        strokeWidth={1}
        strokeDasharray="2 2"
      />
    </svg>
  );
}

// Task dependency edge — grey dashed bezier between two task chips.
function DepEdgeIcon() {
  return (
    <svg width={30} height={18} aria-hidden>
      <rect x={0} y={2} width={6} height={6} rx={1} fill="#9aa3b4" fillOpacity={0.3} stroke="#9aa3b4" strokeWidth={0.75} />
      <rect x={24} y={10} width={6} height={6} rx={1} fill="#9aa3b4" fillOpacity={0.3} stroke="#9aa3b4" strokeWidth={0.75} />
      <path
        d="M 6 5 C 15 5, 15 13, 24 13"
        stroke="#9aa3b4"
        strokeWidth={1}
        strokeDasharray="3 3"
        fill="none"
        opacity={0.8}
      />
    </svg>
  );
}
