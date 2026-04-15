import { useEffect, useMemo, useReducer, useState } from 'react';
import type {
  OrchestrationEvent,
  OrchestrationEventKind,
} from '../../gantt/index';
import { useOrchestrationEvents } from '../../rpc/orchestration';
import { getSessionStore } from '../../rpc/hooks';
import { extractThinkingText, formatThinkingInline } from '../../lib/thinking';
import './OrchestrationTimeline.css';

interface Props {
  sessionId: string | null;
  limit?: number;
}

const KIND_LABEL: Record<OrchestrationEventKind, string> = {
  started: 'Started',
  progress: 'Progress',
  completed: 'Completed',
  failed: 'Failed',
  blocked: 'Blocked',
  discovered: 'New work',
  divergence: 'Divergence',
};

const ALL_KINDS: OrchestrationEventKind[] = [
  'started',
  'progress',
  'completed',
  'failed',
  'blocked',
  'discovered',
  'divergence',
];

type TimeWindow = '30s' | '2m' | 'all';
type Grouping = 'none' | 'task' | 'agent';

const TIME_WINDOW_MS: Record<TimeWindow, number | null> = {
  '30s': 30_000,
  '2m': 120_000,
  all: null,
};

interface CollapsedRun {
  representative: OrchestrationEvent;
  collapsedCount: number;
}

interface Group {
  key: string;
  label: string;
  items: CollapsedRun[];
}

// Build a map of spanId → first-200-chars thinking preview for every span
// in the session. We walk the span index once (fast) and look up the
// reporting-tool events' enclosing invocation on render. Rebuilt on every
// span-index tick so live reasoning shows up without a manual refresh.
function useThinkingPreviewMap(sessionId: string | null): Map<string, string> {
  const store = getSessionStore(sessionId);
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.spans.subscribe(() => bump());
  }, [store]);
  return useMemo(() => {
    const out = new Map<string, string>();
    if (!store) return out;
    for (const agent of store.agents.list) {
      const spans = store.spans.queryAgent(
        agent.id,
        -Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
      );
      for (const s of spans) {
        const text = extractThinkingText(s);
        if (text) out.set(s.id, formatThinkingInline(text, 200));
      }
    }
    return out;
  }, [store]);
}

export function OrchestrationTimeline({ sessionId, limit = 20 }: Props) {
  const events = useOrchestrationEvents(sessionId, 200);
  const thinkingBySpan = useThinkingPreviewMap(sessionId);

  const [kindFilter, setKindFilter] = useState<Set<OrchestrationEventKind>>(
    () => new Set(ALL_KINDS),
  );
  const [agentFilter, setAgentFilter] = useState<Set<string> | null>(null);
  const [hideNoise, setHideNoise] = useState(false);
  const [timeWindow, setTimeWindow] = useState<TimeWindow>('all');
  const [grouping, setGrouping] = useState<Grouping>('none');

  const agentsSeen = useMemo(() => {
    const seen = new Set<string>();
    for (const ev of events) {
      if (ev.agentId) seen.add(ev.agentId);
    }
    return Array.from(seen).sort();
  }, [events]);

  // Apply filters, noise collapse, and grouping.
  const { groups, totalAfterFilter } = useMemo(() => {
    // 1. Filter by kind.
    let filtered = events.filter((ev) => kindFilter.has(ev.kind));
    // 2. Filter by agent.
    if (agentFilter !== null) {
      filtered = filtered.filter((ev) => agentFilter.has(ev.agentId));
    }
    // 3. Filter by time window (relative to newest event).
    const windowMs = TIME_WINDOW_MS[timeWindow];
    if (windowMs !== null && filtered.length > 0) {
      const newest = Math.max(...filtered.map((ev) => ev.startMs));
      filtered = filtered.filter((ev) => ev.startMs >= newest - windowMs);
    }
    const totalAfterFilter = filtered.length;

    // 4. Collapse noisy progress runs (only if no task-grouping, to keep
    //    the grouped view showing full per-task history).
    let collapsed: CollapsedRun[];
    if (hideNoise && grouping !== 'task') {
      collapsed = collapseProgressNoise(filtered);
    } else {
      collapsed = filtered.map((ev) => ({
        representative: ev,
        collapsedCount: 0,
      }));
    }

    // 5. Group.
    let groups: Group[];
    if (grouping === 'task') {
      groups = groupBy(collapsed, (r) => {
        const tid = r.representative.taskId || '(no task)';
        const title = r.representative.title || r.representative.toolName || tid;
        return { key: `task:${tid}`, label: `${title}${r.representative.taskId ? `  #${r.representative.taskId}` : ''}` };
      });
    } else if (grouping === 'agent') {
      groups = groupBy(collapsed, (r) => ({
        key: `agent:${r.representative.agentId}`,
        label: r.representative.agentId || '(no agent)',
      }));
    } else {
      groups = [{ key: 'all', label: '', items: collapsed }];
    }

    return { groups, totalAfterFilter };
  }, [events, kindFilter, agentFilter, timeWindow, hideNoise, grouping]);

  const toggleKind = (k: OrchestrationEventKind) => {
    setKindFilter((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const toggleAgent = (agent: string) => {
    setAgentFilter((prev) => {
      const base = prev ?? new Set(agentsSeen);
      const next = new Set(base);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      // If user re-selected every seen agent, collapse back to "all" (null).
      if (next.size === agentsSeen.length) return null;
      return next;
    });
  };

  // Apply limit per group so a single task-group can't dominate the view,
  // but cap the total render too for safety.
  let renderedCount = 0;
  const renderGroups: Group[] = [];
  for (const g of groups) {
    if (renderedCount >= limit) break;
    const room = limit - renderedCount;
    const slice = g.items.slice(0, room);
    renderedCount += slice.length;
    if (slice.length > 0) {
      renderGroups.push({ ...g, items: slice });
    }
  }

  const header = (
    <TimelineControls
      kindFilter={kindFilter}
      onToggleKind={toggleKind}
      agentsSeen={agentsSeen}
      agentFilter={agentFilter}
      onToggleAgent={toggleAgent}
      onResetAgents={() => setAgentFilter(null)}
      hideNoise={hideNoise}
      onToggleNoise={() => setHideNoise((v) => !v)}
      timeWindow={timeWindow}
      onTimeWindow={setTimeWindow}
      grouping={grouping}
      onGrouping={setGrouping}
    />
  );

  if (totalAfterFilter === 0) {
    return (
      <div
        className="hg-orch-timeline__wrapper"
        data-testid="orchestration-timeline-wrapper"
      >
        {header}
        <div
          className="hg-orch-timeline__empty"
          data-testid="orchestration-timeline-empty"
        >
          {events.length === 0
            ? 'No orchestration events yet.'
            : 'No events match the current filters.'}
        </div>
      </div>
    );
  }

  return (
    <div
      className="hg-orch-timeline__wrapper"
      data-testid="orchestration-timeline-wrapper"
    >
      {header}
      <div
        className="hg-orch-timeline"
        data-testid="orchestration-timeline"
        data-grouping={grouping}
      >
        {renderGroups.map((g) => (
          <TimelineGroup
            key={g.key}
            group={g}
            showHeader={grouping !== 'none'}
            thinkingBySpan={thinkingBySpan}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Controls header
// ---------------------------------------------------------------------------

interface ControlsProps {
  kindFilter: Set<OrchestrationEventKind>;
  onToggleKind: (k: OrchestrationEventKind) => void;
  agentsSeen: string[];
  agentFilter: Set<string> | null;
  onToggleAgent: (agent: string) => void;
  onResetAgents: () => void;
  hideNoise: boolean;
  onToggleNoise: () => void;
  timeWindow: TimeWindow;
  onTimeWindow: (w: TimeWindow) => void;
  grouping: Grouping;
  onGrouping: (g: Grouping) => void;
}

function TimelineControls(props: ControlsProps) {
  const {
    kindFilter,
    onToggleKind,
    agentsSeen,
    agentFilter,
    onToggleAgent,
    onResetAgents,
    hideNoise,
    onToggleNoise,
    timeWindow,
    onTimeWindow,
    grouping,
    onGrouping,
  } = props;
  const agentSelected = (a: string) =>
    agentFilter === null || agentFilter.has(a);
  return (
    <div
      className="hg-orch-controls"
      data-testid="orchestration-timeline-controls"
    >
      <div className="hg-orch-controls__row">
        <span className="hg-orch-controls__label">Kind</span>
        {ALL_KINDS.map((k) => (
          <button
            key={k}
            type="button"
            data-testid={`orch-kind-chip-${k}`}
            data-active={kindFilter.has(k) ? 'true' : 'false'}
            className={`hg-orch-chip hg-orch-chip--kind hg-orch-chip--${k}${
              kindFilter.has(k) ? ' hg-orch-chip--active' : ''
            }`}
            onClick={() => onToggleKind(k)}
          >
            {KIND_LABEL[k]}
          </button>
        ))}
      </div>

      {agentsSeen.length > 0 && (
        <div className="hg-orch-controls__row">
          <span className="hg-orch-controls__label">Agent</span>
          {agentsSeen.map((a) => (
            <button
              key={a}
              type="button"
              data-testid={`orch-agent-chip-${a}`}
              data-active={agentSelected(a) ? 'true' : 'false'}
              className={`hg-orch-chip${
                agentSelected(a) ? ' hg-orch-chip--active' : ''
              }`}
              onClick={() => onToggleAgent(a)}
            >
              {a}
            </button>
          ))}
          {agentFilter !== null && (
            <button
              type="button"
              className="hg-orch-chip hg-orch-chip--reset"
              onClick={onResetAgents}
              data-testid="orch-agent-reset"
            >
              all
            </button>
          )}
        </div>
      )}

      <div className="hg-orch-controls__row">
        <span className="hg-orch-controls__label">Window</span>
        <div className="hg-orch-segmented" role="group" aria-label="Time window">
          {(['30s', '2m', 'all'] as TimeWindow[]).map((w) => (
            <button
              key={w}
              type="button"
              data-testid={`orch-window-${w}`}
              data-active={timeWindow === w ? 'true' : 'false'}
              className={`hg-orch-seg${
                timeWindow === w ? ' hg-orch-seg--active' : ''
              }`}
              onClick={() => onTimeWindow(w)}
            >
              {w === 'all' ? 'All' : `Last ${w}`}
            </button>
          ))}
        </div>

        <span className="hg-orch-controls__label">Group</span>
        <div className="hg-orch-segmented" role="group" aria-label="Group by">
          {(
            [
              ['none', 'None'],
              ['task', 'Task'],
              ['agent', 'Agent'],
            ] as [Grouping, string][]
          ).map(([g, label]) => (
            <button
              key={g}
              type="button"
              data-testid={`orch-group-${g}`}
              data-active={grouping === g ? 'true' : 'false'}
              className={`hg-orch-seg${
                grouping === g ? ' hg-orch-seg--active' : ''
              }`}
              onClick={() => onGrouping(g)}
            >
              {label}
            </button>
          ))}
        </div>

        <label
          className={`hg-orch-toggle${hideNoise ? ' hg-orch-toggle--on' : ''}`}
          data-testid="orch-hide-noise"
        >
          <input
            type="checkbox"
            checked={hideNoise}
            onChange={onToggleNoise}
          />
          <span>Hide noise</span>
        </label>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row / group rendering
// ---------------------------------------------------------------------------

function TimelineGroup({
  group,
  showHeader,
  thinkingBySpan,
}: {
  group: Group;
  showHeader: boolean;
  thinkingBySpan: Map<string, string>;
}) {
  return (
    <div
      className="hg-orch-group"
      data-testid="orchestration-group"
      data-group-key={group.key}
    >
      {showHeader && (
        <div className="hg-orch-group__header">{group.label || '(none)'}</div>
      )}
      <div className="hg-orch-group__items">
        {group.items.map((run) => (
          <OrchestrationRow
            key={run.representative.spanId}
            event={run.representative}
            collapsedCount={run.collapsedCount}
            thinkingPreview={thinkingBySpan.get(run.representative.spanId) ?? null}
          />
        ))}
      </div>
    </div>
  );
}

function OrchestrationRow({
  event,
  collapsedCount,
  thinkingPreview,
}: {
  event: OrchestrationEvent;
  collapsedCount: number;
  thinkingPreview: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailCollapsible = event.detail.length > 120;
  return (
    <div
      className={`hg-orch-event hg-orch-event--${event.kind}`}
      data-testid="orchestration-event"
      data-kind={event.kind}
    >
      <span className="hg-orch-event__dot" aria-hidden />
      <div className="hg-orch-event__row">
        <span className="hg-orch-event__kind">{KIND_LABEL[event.kind]}</span>
        <span className="hg-orch-event__ts">
          {formatRelative(event.startMs)}
        </span>
        <span className="hg-orch-event__agent">{event.agentId}</span>
        {collapsedCount > 0 && (
          <span
            className="hg-orch-event__collapsed"
            data-testid="orchestration-collapsed-count"
          >
            +{collapsedCount} progress updates
          </span>
        )}
        {event.recoverable !== null && (
          <span
            className={`hg-orch-event__recoverable${
              event.recoverable ? ' hg-orch-event__recoverable--yes' : ''
            }`}
          >
            {event.recoverable ? 'recoverable' : 'fatal'}
          </span>
        )}
      </div>
      {(event.title || event.taskId) && (
        <div className="hg-orch-event__title">
          {event.title || event.toolName}
          {event.taskId && (
            <span className="hg-orch-event__task-id">#{event.taskId}</span>
          )}
        </div>
      )}
      {event.detail && (
        <>
          <div
            className={`hg-orch-event__detail${
              detailCollapsible && !expanded
                ? ' hg-orch-event__detail--collapsed'
                : ''
            }`}
          >
            {event.detail}
          </div>
          {detailCollapsible && (
            <button
              type="button"
              className="hg-orch-event__detail-toggle"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? 'Show less' : 'Show more'}
            </button>
          )}
        </>
      )}
      {thinkingPreview && (
        <blockquote
          className="hg-orch-event__thinking"
          data-testid="orchestration-thinking-preview"
          style={{
            margin: '4px 0 0 0',
            padding: '4px 8px',
            borderLeft: '2px solid rgba(168,200,255,0.45)',
            fontStyle: 'italic',
            fontSize: 11,
            lineHeight: 1.45,
            color: 'rgba(226,226,233,0.78)',
            background: 'rgba(168,200,255,0.05)',
            borderRadius: '0 4px 4px 0',
          }}
        >
          <span aria-hidden="true" style={{ marginRight: 4 }}>🧠</span>
          {thinkingPreview}
        </blockquote>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Collapse consecutive progress events for the same task into a single
// representative row with a ``+N more`` count. The input is newest-first.
function collapseProgressNoise(
  events: OrchestrationEvent[],
): CollapsedRun[] {
  const out: CollapsedRun[] = [];
  let i = 0;
  while (i < events.length) {
    const ev = events[i];
    if (ev.kind !== 'progress') {
      out.push({ representative: ev, collapsedCount: 0 });
      i += 1;
      continue;
    }
    let runEnd = i + 1;
    while (
      runEnd < events.length &&
      events[runEnd].kind === 'progress' &&
      events[runEnd].taskId === ev.taskId
    ) {
      runEnd += 1;
    }
    const collapsedCount = runEnd - i - 1;
    out.push({ representative: ev, collapsedCount });
    i = runEnd;
  }
  return out;
}

function groupBy(
  runs: CollapsedRun[],
  key: (r: CollapsedRun) => { key: string; label: string },
): Group[] {
  const order: string[] = [];
  const map = new Map<string, Group>();
  for (const r of runs) {
    const { key: k, label } = key(r);
    let group = map.get(k);
    if (!group) {
      group = { key: k, label, items: [] };
      map.set(k, group);
      order.push(k);
    }
    group.items.push(r);
  }
  return order.map((k) => map.get(k)!);
}

function formatRelative(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  const totalSec = Math.floor(ms / 1000);
  const s = totalSec % 60;
  const m = Math.floor(totalSec / 60) % 60;
  const h = Math.floor(totalSec / 3600);
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  return `${m}:${String(s).padStart(2, '0')}`;
}
