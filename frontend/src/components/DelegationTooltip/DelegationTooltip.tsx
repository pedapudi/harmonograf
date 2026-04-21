import type { DelegationHoverState } from '../../gantt/renderer';

// Small tooltip anchored to the pointer when the user hovers a dashed
// delegation-edge bezier on the Gantt. Matches the span-hover tooltip
// visually (same surface-container-highest background, padding, radius,
// font) so the two chrome elements read as a coherent hover family.
//
// Kept as a standalone component so it can be rendered headlessly in unit
// tests without needing a full GanttCanvas + renderer instance.

interface Props {
  hover: DelegationHoverState;
  // Agents are keyed by id on the session; the Gantt shell resolves
  // display labels (user/goldfive/agent.name/agentId fallback) so the
  // tooltip itself stays display-agnostic. Pass a resolver instead of
  // threading the whole SessionStore through.
  resolveAgentLabel: (agentId: string) => string;
}

function fmtTime(ms: number): string {
  // Match the m:ss formatter used by ActivityView / GraphView so every
  // session-relative timestamp in chrome reads the same way.
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, '0')}`;
}

function shortInvocation(id: string): string {
  // ADK invocation ids look like `inv-<uuid>`; the first ~8 chars of the
  // tail are enough to distinguish invocations within one session without
  // bloating the tooltip width.
  if (!id) return '(none)';
  const tail = id.startsWith('inv-') ? id.slice(4) : id;
  if (tail.length <= 8) return tail;
  return `${tail.slice(0, 8)}…`;
}

export function DelegationTooltip({ hover, resolveAgentLabel }: Props) {
  const { record, x, y } = hover;
  const from = resolveAgentLabel(record.fromAgentId);
  const to = resolveAgentLabel(record.toAgentId);
  return (
    <div
      role="tooltip"
      data-testid="delegation-tooltip"
      style={{
        position: 'absolute',
        left: Math.min(x + 12, 9999),
        top: Math.max(0, y - 74),
        background: 'var(--md-sys-color-surface-container-highest, #31333c)',
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        padding: '6px 10px',
        borderRadius: 6,
        pointerEvents: 'none',
        fontSize: 12,
        fontFamily: 'system-ui, sans-serif',
        boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
        zIndex: 10,
        maxWidth: 360,
      }}
    >
      <div style={{ fontWeight: 600 }}>Delegation observed</div>
      <div style={{ opacity: 0.85 }} data-testid="delegation-tooltip-agents">
        From: {from} → {to}
      </div>
      {record.taskId && (
        <div
          style={{ opacity: 0.8, fontFamily: 'monospace', fontSize: 11 }}
          data-testid="delegation-tooltip-task"
        >
          Task: {record.taskId}
        </div>
      )}
      <div
        style={{ opacity: 0.8, fontFamily: 'monospace', fontSize: 11 }}
        data-testid="delegation-tooltip-invocation"
      >
        Invocation: {shortInvocation(record.invocationId)}
      </div>
      <div style={{ opacity: 0.7 }} data-testid="delegation-tooltip-observed">
        Observed: {fmtTime(record.observedAtMs)}
      </div>
    </div>
  );
}
