import { useEffect, useMemo, useReducer, useState } from 'react';
import { useUiStore, type DrawerTaskSubtab } from '../../state/uiStore';
import {
  collectThinkingForTask,
  extractThinkingText,
  hasThinking as spanHasThinking,
  formatThinkingInline,
  type ThinkingEntry,
} from '../../lib/thinking';
import {
  getSessionStore,
  usePayload,
  usePostAnnotation,
  useSendControl,
  useSessionWatch,
} from '../../rpc/hooks';
import type {
  Span,
  AttributeValue,
  PayloadRef,
  SpanLink,
  LinkRelation,
  TaskPlan,
} from '../../gantt/types';
import type { PlanDiff, PlanRevision, SessionStore } from '../../gantt';
import { bareAgentName } from '../../gantt';
import { parseRevisionReason } from '../../gantt/driftKinds';
import { formatDuration } from '../../lib/format';
import {
  isGoldfiveSpan,
  resolveGoldfiveSpanInfo,
} from '../../lib/goldfiveSpan';
import {
  isJudgeSpan,
  resolveJudgeDetail,
} from '../../lib/interventionDetail';
import { GoldfiveSpanDetail } from '../Interventions/GoldfiveSpanDetail';
import { JudgeInvocationDetail } from '../Interventions/JudgeInvocationDetail';
import { OrchestrationTimeline } from '../OrchestrationTimeline/OrchestrationTimeline';

type TabId =
  | 'summary'
  | 'task'
  | 'payload'
  | 'timeline'
  | 'links'
  | 'annotations'
  | 'control';

const TABS: { id: TabId; label: string; testId: string }[] = [
  { id: 'summary', label: 'Summary', testId: 'inspector-tab-overview' },
  { id: 'task', label: 'Task', testId: 'inspector-tab-task' },
  { id: 'payload', label: 'Payload', testId: 'inspector-tab-payload' },
  { id: 'timeline', label: 'Timeline', testId: 'inspector-tab-timeline' },
  { id: 'links', label: 'Links', testId: 'inspector-tab-links' },
  { id: 'annotations', label: 'Annotations', testId: 'inspector-tab-annotations' },
  { id: 'control', label: 'Control', testId: 'inspector-tab-control' },
];

export function Drawer() {
  const open = useUiStore((s) => s.drawerOpen);
  const selected = useUiStore((s) => s.selectedSpanId);
  const selectedTaskId = useUiStore((s) => s.selectedTaskId);
  const selectTask = useUiStore((s) => s.selectTask);
  const close = useUiStore((s) => s.closeDrawer);
  const sessionId = useUiStore((s) => s.currentSessionId);

  // Live-updating span subscription: re-renders whenever the span's attributes
  // or status change (e.g. task_report, thinking_text arrive via stream events).
  //
  // We route through useSessionWatch rather than the bare getSessionStore()
  // lookup so that (a) the Drawer participates in the refcounted watch and
  // keeps the stream alive while open, and (b) we get a guaranteed non-null
  // store handle even if the drawer is opened before GanttView mounted its
  // own watch (deep-link, refresh, keyboard nav, etc.).
  const watch = useSessionWatch(sessionId);
  const store = watch.store;
  // version ticks every time the span store fires a dirty notification, which
  // re-runs the span lookup below. Computing the span during render (rather
  // than mirroring it into useState via an effect) avoids cascading renders
  // and handles the "selected span not yet in store" case: every store
  // notification retries the lookup, so the drawer fills in as soon as the
  // span arrives.
  const [version, bumpVersion] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.spans.subscribe(() => bumpVersion());
  }, [store]);
  const span = useMemo<Span | null>(() => {
    if (!sessionId || !selected || !store) return null;
    void version;
    return store.spans.get(selected) ?? null;
  }, [sessionId, selected, store, version]);

  // Reverse reconciliation: when a span is selected (e.g. via popover → open
  // drawer, keyboard nav, or deep link), walk the TaskRegistry and set
  // selectedTaskId to whichever task binds to it. Runs again when tasks
  // mutate so a late-arriving boundSpanId still gets picked up. When the
  // selection clears, clear the task highlight too.
  useEffect(() => {
    if (!store) return;
    const resolve = (): string | null => {
      if (!selected) return null;
      for (const plan of store.tasks.listPlans()) {
        for (const t of plan.tasks) {
          if (t.boundSpanId === selected) return t.id;
        }
      }
      return null;
    };
    const next = resolve();
    if (next !== selectedTaskId) selectTask(next);
    return store.tasks.subscribe(() => {
      const cur = useUiStore.getState();
      const resolved = resolve();
      if (cur.selectedTaskId !== resolved) selectTask(resolved);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, selected]);

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
        <CurrentTaskSection sessionId={sessionId} selectedSpan={span} />
        {span ? (
          isJudgeSpan(span) && store ? (
            <JudgeDrawerPanel
              key={span.id}
              span={span}
              store={store}
              sessionId={sessionId}
              onClose={close}
            />
          ) : (
            <DrawerTabs key={span.id} span={span} sessionId={sessionId} />
          )
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
  // Honor deep-link tab request from the uiStore (set by the `t` shortcut).
  // Read synchronously at mount time so the first paint lands on the right
  // tab — TaskTab peeks at drawerRequestedTaskSubtab in the same render.
  const requestedTab = useUiStore.getState().drawerRequestedTab;
  const consumeRequest = useUiStore((s) => s.consumeDrawerRequestedTab);
  const [tab, setTab] = useState<TabId>(requestedTab ?? 'summary');
  useEffect(() => {
    if (requestedTab) {
      // Consume the request on a microtask so TaskTab's synchronous read of
      // drawerRequestedTaskSubtab in the same mount tick still sees it.
      queueMicrotask(() => consumeRequest());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
        {tab === 'summary' && <SummaryTab span={span} />}
        {tab === 'task' && <TaskTab span={span} sessionId={sessionId} />}
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

// --- Judge drawer panel ----------------------------------------------------
//
// When the selected span is a goldfive LLM-as-judge invocation we bypass
// the generic tab strip (Summary / Task / Payload / …) and mount the
// JudgeInvocationDetail component in its `drawer` variant instead. The
// ordinary drawer tabs are not useful here — the span has no payload,
// no trajectory children of its own, and the "Task" tab would point at
// the wrong task context (the subject's, not the judge's).
//
// Section A (what was being judged) wants the plan's task title +
// description and the session's goal summaries. We pull both from the
// SessionStore synchronously: task lookup via tasks.findPlanForTask,
// goals via the USER_MESSAGE spans stamped by the goldfive event sink
// (see rpc/goldfiveEvent.ts:synthesizeUserGoalSpan). When a piece is
// missing we pass undefined / [] and the component renders a fallback
// placeholder or omits the row.

function JudgeDrawerPanel({
  span,
  store,
  sessionId,
  onClose,
}: {
  span: Span;
  store: SessionStore;
  sessionId: string | null;
  onClose: () => void;
}) {
  // Keep the panel live — plan revisions arrive asynchronously, so the
  // steering-outcome link only materializes after the PlanRevised event
  // reaches the store. Bumping the version on task mutations retriggers
  // resolveJudgeDetail so the link pops in without a reselection.
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    return store.tasks.subscribe(() => bump());
  }, [store]);
  void sessionId;

  const detail = useMemo(() => {
    const plans: TaskPlan[] = [];
    const seen = new Set<TaskPlan>();
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) {
        if (seen.has(snap)) continue;
        seen.add(snap);
        plans.push(snap);
      }
    }
    return resolveJudgeDetail(span, plans);
  }, [span, store]);

  // Resolve the task's title / description (Section A). The judge span
  // carries `judge.target_task_id`, which interventionDetail also
  // surfaces as detail.taskId; look it up in the live plans.
  const taskLookup = detail.taskId
    ? store.tasks.findPlanForTask(detail.taskId)
    : undefined;
  const taskTitle = taskLookup?.task.title || undefined;
  const taskDescription = taskLookup?.task.description || undefined;

  // Resolve the session's goal summaries (Section A). Goals arrive as
  // USER_MESSAGE spans on the __user__ actor, one per RunStarted. We
  // de-duplicate by text (a user can re-kick the same goal) and keep
  // earliest-first order so the list reads chronologically.
  const goals = useMemo(() => {
    const out: string[] = [];
    const seen = new Set<string>();
    const scratch: Span[] = [];
    store.spans.queryAgent('__user__', 0, Number.POSITIVE_INFINITY, scratch);
    scratch.sort((a, b) => a.startMs - b.startMs);
    for (const s of scratch) {
      if (s.kind !== 'USER_MESSAGE') continue;
      const marker = s.attributes['user.goal_summary'];
      if (!marker || marker.kind !== 'string') continue;
      const text = marker.value.trim();
      if (!text || seen.has(text)) continue;
      seen.add(text);
      out.push(text);
    }
    return out;
  }, [store]);

  const selectSpan = useUiStore((s) => s.selectSpan);

  return (
    <div
      className="hg-drawer__body hg-drawer__body--judge"
      data-testid="drawer-judge-panel"
    >
      <JudgeInvocationDetail
        detail={detail}
        variant="drawer"
        resolveAgentName={(id) =>
          store.agents.get(id)?.name || bareAgentName(id) || id
        }
        onFocusAgent={(id) => {
          useUiStore.getState().setFocusedAgent(id);
        }}
        onFocusTask={(id) => {
          useUiStore.getState().selectTask(id);
        }}
        onOpenSteering={(_planId, _revIdx) => {
          // Jump into the refine: span on the goldfive lane so the user
          // lands on the existing plan-revision detail panel. The refine
          // synthesizer stamps `refine.index = <revIdx>` on a
          // __goldfive__ span (see rpc/goldfiveEvent.ts).
          void _planId;
          const spans: Span[] = [];
          store.spans.queryAgent(
            '__goldfive__',
            0,
            Number.POSITIVE_INFINITY,
            spans,
          );
          const targetRefine = spans.find(
            (s) =>
              s.name.startsWith('refine:') &&
              s.attributes['refine.index']?.kind === 'string' &&
              s.attributes['refine.index'].value === String(_revIdx),
          );
          if (targetRefine) {
            selectSpan(targetRefine.id);
          } else {
            onClose();
          }
        }}
        taskTitle={taskTitle}
        taskDescription={taskDescription}
        goals={goals}
      />
    </div>
  );
}

// --- Current task section (live, outside tabs) -----------------------------

function CurrentTaskSection({
  sessionId,
  selectedSpan,
}: {
  sessionId: string | null;
  selectedSpan: Span | null;
}) {
  const watch = useSessionWatch(sessionId);
  const store = watch.store;
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.tasks.subscribe(() => bump());
  }, [store]);

  const current = store ? store.getCurrentTask() : null;
  if (!current) return null;

  const { task } = current;
  // Highlight if the drawer is open on a span whose hgraf.task_id matches.
  const selectedTaskId =
    selectedSpan?.attributes?.['hgraf.task_id']?.kind === 'string'
      ? selectedSpan.attributes['hgraf.task_id'].value
      : undefined;
  const highlighted = selectedTaskId && selectedTaskId === task.id;
  const running = task.status === 'RUNNING';

  return (
    <div
      className={`hg-drawer__current-task${highlighted ? ' hg-drawer__current-task--highlighted' : ''}`}
      data-testid="drawer-current-task"
      data-running={running ? 'true' : 'false'}
    >
      <div className="hg-drawer__current-task-header">
        <span className="hg-drawer__current-task-label">Current task</span>
        <span
          className={`hg-strip__chip hg-strip__chip--${task.status?.toLowerCase() ?? 'pending'}`}
        >
          {task.status}
        </span>
      </div>
      <div className="hg-drawer__current-task-title">{task.title || task.id}</div>
      {task.description && (
        <div className="hg-drawer__current-task-desc">{task.description}</div>
      )}
      {task.assigneeAgentId && (
        <div className="hg-drawer__current-task-agent">
          <code title={task.assigneeAgentId}>
            {store?.agents.get(task.assigneeAgentId)?.name ||
              bareAgentName(task.assigneeAgentId) ||
              task.assigneeAgentId}
          </code>
        </div>
      )}
    </div>
  );
}

// --- Task tab ---------------------------------------------------------------

function TaskTab({ span, sessionId }: { span: Span; sessionId: string | null }) {
  // Subtab state: Overview is the original task-centric view (status, report,
  // thinking snippet, plan revisions). Trajectory is a chronological feed of
  // every thinking message captured on spans bound to the same task — the
  // primary surface for reviewing an agent's reasoning trail. Task #4.
  // Default subtab honors any pending deep-link request from the uiStore
  // (set by the `t` shortcut). Reading the request synchronously during the
  // mount render keeps the first paint on the correct subtab and sidesteps
  // the react-hooks/set-state-in-effect lint rule.
  const initialRequestedSubtab = useUiStore.getState().drawerRequestedTaskSubtab;
  const [subtab, setSubtab] = useState<DrawerTaskSubtab>(
    initialRequestedSubtab ?? 'overview',
  );
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div
        role="tablist"
        aria-label="Task subtabs"
        style={{
          display: 'flex',
          gap: 4,
          padding: '6px 12px 0',
          borderBottom: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        }}
      >
        <TaskSubtabButton
          active={subtab === 'overview'}
          onClick={() => setSubtab('overview')}
          testId="inspector-task-subtab-overview"
        >
          Overview
        </TaskSubtabButton>
        <TaskSubtabButton
          active={subtab === 'trajectory'}
          onClick={() => setSubtab('trajectory')}
          testId="inspector-task-subtab-trajectory"
        >
          Trajectory
        </TaskSubtabButton>
      </div>
      {subtab === 'overview' && <TaskOverviewPanel span={span} sessionId={sessionId} />}
      {subtab === 'trajectory' && <TaskTrajectoryPanel span={span} sessionId={sessionId} />}
    </div>
  );
}

function TaskSubtabButton({
  active,
  onClick,
  testId,
  children,
}: {
  active: boolean;
  onClick: () => void;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      data-testid={testId}
      style={{
        background: 'transparent',
        color: active ? 'var(--md-sys-color-primary, #a8c8ff)' : 'inherit',
        border: 'none',
        borderBottom: active
          ? '2px solid var(--md-sys-color-primary, #a8c8ff)'
          : '2px solid transparent',
        padding: '6px 10px',
        fontSize: 12,
        cursor: 'pointer',
        fontWeight: active ? 600 : 500,
      }}
    >
      {children}
    </button>
  );
}

// Shared reader for the reasoning-related attributes a span can carry.
// Extracted so both TaskOverviewPanel (Task → Overview subtab) and SummaryTab
// (Summary tab) can surface ``<ReasoningSection />`` without duplicating the
// attribute narrowing logic. Mirrors the attributes emitted by the goldfive
// telemetry plugin: ``llm.reasoning`` (per LLM_CALL), ``llm.reasoning_trail``
// (per INVOCATION aggregate, harmonograf#108), ``reasoning_call_count`` and
// ``has_reasoning``. Large traces spill to a payload_ref with role="reasoning".
function readReasoning(span: Span): {
  inlineText: string | undefined;
  payloadRef: PayloadRef | undefined;
  callCount: number | undefined;
  hasReasoningAttr: boolean;
  isAggregate: boolean;
  show: boolean;
} {
  const reasoningInline =
    span.attributes['llm.reasoning']?.kind === 'string'
      ? span.attributes['llm.reasoning'].value
      : undefined;
  const reasoningTrail =
    span.attributes['llm.reasoning_trail']?.kind === 'string'
      ? span.attributes['llm.reasoning_trail'].value
      : undefined;
  // Attribute rides as proto int64 → bigint on the wire. Narrow to number
  // for the props; call counts in the hundreds-of-turns range fit easily
  // inside a JS safe integer so Number() conversion is loss-less here.
  const reasoningCallCountAttr = span.attributes['reasoning_call_count'];
  const callCount =
    reasoningCallCountAttr?.kind === 'int'
      ? Number(reasoningCallCountAttr.value)
      : undefined;
  const hasReasoningAttr =
    span.attributes['has_reasoning']?.kind === 'bool' &&
    span.attributes['has_reasoning'].value === true;
  const payloadRef = (span.payloadRefs ?? []).find((r) => r.role === 'reasoning');
  const show = Boolean(
    reasoningInline || reasoningTrail || payloadRef || hasReasoningAttr,
  );
  // Prefer the INVOCATION-level trail (agent-wide context) over a single
  // LLM_CALL's reasoning. Both paths fall back to a payload_ref when the text
  // is large enough to spill off the span attribute.
  const inlineText = reasoningTrail ?? reasoningInline;
  const isAggregate = Boolean(reasoningTrail) || callCount != null;
  return {
    inlineText,
    payloadRef,
    callCount,
    hasReasoningAttr,
    isAggregate,
    show,
  };
}

function TaskOverviewPanel({ span, sessionId }: { span: Span; sessionId: string | null }) {
  // harmonograf#110 / goldfive#205: resolve the bound task (if any) so the
  // panel can render a "Cancel reason" section when this span's task
  // ended CANCELLED / FAILED. Binding happens via the span's
  // ``hgraf.task_id`` attribute — same linkage the current-task highlight
  // uses.
  const store = getSessionStore(sessionId);
  const [, _bumpCancelReason] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.tasks.subscribe(() => _bumpCancelReason());
  }, [store]);
  const spanTaskId =
    span.attributes['hgraf.task_id']?.kind === 'string'
      ? span.attributes['hgraf.task_id'].value
      : '';
  const boundTask = spanTaskId && store
    ? store.tasks.findPlanForTask(spanTaskId)?.task ?? null
    : null;
  const showCancelReason =
    boundTask &&
    (boundTask.status === 'CANCELLED' || boundTask.status === 'FAILED') &&
    Boolean(boundTask.cancelReason);
  const taskReport = (span.attributes['task_report']?.kind === 'string' ? span.attributes['task_report'].value : undefined);
  const isRunning = span.endMs == null;
  const agentDesc = (span.attributes['agent_description']?.kind === 'string' ? span.attributes['agent_description'].value : undefined);
  const {
    inlineText: reasoningInlineText,
    payloadRef: reasoningRef,
    callCount: reasoningCallCount,
    hasReasoningAttr,
    isAggregate: reasoningIsAggregate,
    show: showReasoning,
  } = readReasoning(span);

  return (
    <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Live status */}
      {isRunning && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#a8c8ff' }}>
          <span className="hg-transport__live-dot" style={{ display: 'inline-block' }} />
          <span>Running</span>
          {hasReasoningAttr && <span style={{ marginLeft: 4 }}>· 💭 Thinking</span>}
        </div>
      )}

      {/* Current task report */}
      {taskReport && (
        <section>
          <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Current Task</div>
          <div style={{ fontSize: 12, lineHeight: 1.6, background: 'rgba(255,255,255,0.05)', borderRadius: 6, padding: '8px 10px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {taskReport}
          </div>
        </section>
      )}

      {/* harmonograf#110 / goldfive#205: Cancel / fail reason section.
          Shows the structured reason string (upstream_failed:<id>,
          user_cancel:<annotation_id>, run_aborted:<reason>, ...) when
          the task this span is bound to ended CANCELLED or FAILED.
          Answers "why was this task cancelled?" without making the
          operator cross-reference the Trajectory view. */}
      {showCancelReason && boundTask && (
        <section data-testid="drawer-cancel-reason">
          <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            {boundTask.status === 'FAILED' ? 'Failure reason' : 'Cancel reason'}
          </div>
          <div
            style={{
              fontSize: 12,
              lineHeight: 1.5,
              background: 'rgba(255,200,80,0.08)',
              borderLeft: '3px solid rgba(255,200,80,0.6)',
              borderRadius: 4,
              padding: '8px 10px',
              fontFamily: 'var(--hg-mono, ui-monospace, Menlo, monospace)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {boundTask.cancelReason}
          </div>
        </section>
      )}

      {/* Agent description */}
      {agentDesc && (
        <section>
          <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Agent Role</div>
          <div style={{ fontSize: 11, lineHeight: 1.5, opacity: 0.8 }}>{agentDesc}</div>
        </section>
      )}

      {/* Reasoning — chain-of-thought captured by the telemetry plugin.
          INVOCATION spans carry the ``llm.reasoning_trail`` aggregate
          stamped by ``after_run_callback`` (harmonograf#108); LLM_CALL
          spans carry a per-call ``llm.reasoning``. Large reasoning in
          either shape lives in a payload_ref (role="reasoning") and is
          fetched on-demand. */}
      {showReasoning && (
        <ReasoningSection
          inline={reasoningInlineText}
          payloadRef={reasoningRef}
          callCount={reasoningCallCount}
          isAggregate={reasoningIsAggregate}
        />
      )}

      {(!taskReport && !agentDesc && !showReasoning) && (
        <div style={{ fontSize: 12, opacity: 0.5, textAlign: 'center', padding: '24px 0' }}>
          No task information available.
        </div>
      )}

      <PlanRevisionsSection sessionId={sessionId} span={span} />

      <section data-testid="drawer-orchestration-events">
        <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Orchestration events
        </div>
        <OrchestrationTimeline sessionId={sessionId} limit={20} />
      </section>
    </div>
  );
}

// Reasoning section: surfaces reasoning inline (small) or a payload_ref
// with role="reasoning" (large, fetched on click). Collapsed by default —
// reasoning traces can be long, and users usually only want them when
// investigating a surprising decision.
//
// Carries two variants:
//   * Per-LLM_CALL reasoning → ``llm.reasoning`` attribute. Short label
//     "Reasoning" since a single call contributed.
//   * Per-INVOCATION aggregate (harmonograf#108) → ``llm.reasoning_trail``
//     attribute with ``reasoning_call_count`` sibling. Label widens to
//     "Agent reasoning trail · N turns" so users know the text is the
//     agent's full trajectory, not one snapshot.
const REASONING_PREVIEW_CHARS = 5000;

export function ReasoningSection({
  inline,
  payloadRef,
  callCount,
  isAggregate = false,
}: {
  inline: string | undefined;
  payloadRef: PayloadRef | undefined;
  callCount?: number;
  isAggregate?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // When the reasoning rides inline we don't need to fetch; when it comes
  // as a payload_ref we trigger the load on first open via the toggle
  // handler so we stay out of useEffect (react-hooks/set-state-in-effect).
  const needsFetch = !inline && payloadRef != null;
  const { bytes, loading, error } = usePayload(
    open && needsFetch && payloadRef ? payloadRef.digest : null,
  );

  const fetchedText = useMemo(() => {
    if (!bytes) return null;
    try {
      return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    } catch {
      return null;
    }
  }, [bytes]);

  const fullText = inline ?? fetchedText ?? '';
  const truncated = fullText.length > REASONING_PREVIEW_CHARS;
  const previewText = truncated
    ? fullText.slice(0, REASONING_PREVIEW_CHARS)
    : fullText;

  return (
    <section data-testid="drawer-reasoning">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="drawer-reasoning-toggle"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          width: '100%',
          background: 'transparent',
          border: 'none',
          padding: 0,
          cursor: 'pointer',
          color: 'inherit',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          opacity: 0.6,
          marginBottom: 4,
        }}
      >
        <span style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.1s' }}>▸</span>
        <span>{isAggregate ? 'Agent reasoning trail' : 'Reasoning'}</span>
        {callCount != null && callCount > 0 && (
          <span
            data-testid="drawer-reasoning-call-count"
            style={{
              fontSize: 9,
              opacity: 0.75,
              padding: '1px 5px',
              borderRadius: 8,
              border: '1px solid currentColor',
              textTransform: 'none',
              letterSpacing: 0,
            }}
          >
            {callCount} turn{callCount === 1 ? '' : 's'}
          </span>
        )}
        {payloadRef && !inline && (
          <span style={{ marginLeft: 'auto', fontSize: 9, opacity: 0.7 }}>
            {formatBytes(payloadRef.size)}
          </span>
        )}
      </button>
      {open && (
        <div data-testid="drawer-reasoning-body">
          {loading && <div style={{ fontSize: 11, opacity: 0.6 }}>Loading reasoning…</div>}
          {error && <div className="hg-drawer__error" style={{ fontSize: 11 }}>{error}</div>}
          {!loading && !error && fullText && (
            <div
              style={{
                fontSize: 11,
                lineHeight: 1.6,
                background: 'rgba(168,200,255,0.05)',
                borderRadius: 6,
                padding: '8px 10px',
                fontFamily: "ui-monospace, 'SF Mono', Consolas, 'Liberation Mono', monospace",
                color: 'rgba(226,226,233,0.85)',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                maxHeight: 400,
                overflow: 'auto',
              }}
            >
              {previewText}
              {truncated && (
                <div style={{ fontSize: 10, opacity: 0.6, marginTop: 6 }}>
                  … truncated ({fullText.length - REASONING_PREVIEW_CHARS} more chars)
                </div>
              )}
            </div>
          )}
          {!loading && !error && !fullText && !needsFetch && (
            <div style={{ fontSize: 11, opacity: 0.5 }}>No reasoning captured.</div>
          )}
        </div>
      )}
    </section>
  );
}

// Trajectory panel: chronological feed of every LLM thinking message on the
// task bound to the selected span. Falls back to the single-span trail when
// the span isn't task-bound (e.g. selecting a raw LLM_CALL in a delegated
// agent that doesn't report task_id). Task #4.
function TaskTrajectoryPanel({
  span,
  sessionId,
}: {
  span: Span;
  sessionId: string | null;
}) {
  const store = getSessionStore(sessionId);
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.spans.subscribe(() => bump());
  }, [store]);

  const taskIdAttr =
    span.attributes?.['hgraf.task_id']?.kind === 'string'
      ? span.attributes['hgraf.task_id'].value
      : undefined;

  const entries = useMemo<ThinkingEntry[]>(() => {
    if (!store) return [];
    // Walk only the spans that matter: when a task id is known, every span
    // in the session is filtered inside collectThinkingForTask. Otherwise we
    // fall back to the currently selected span alone.
    const allSpans: Span[] = [];
    for (const agent of store.agents.list) {
      const arr = store.spans.queryAgent(
        agent.id,
        -Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
      );
      allSpans.push(...arr);
    }
    if (taskIdAttr) return collectThinkingForTask(allSpans, taskIdAttr);
    const text = extractThinkingText(span);
    if (!text) return [];
    return [
      {
        spanId: span.id,
        agentId: span.agentId,
        spanName: span.name,
        spanKind: span.kind,
        startMs: span.startMs,
        endMs: span.endMs,
        text,
        isLive: span.endMs == null,
      },
    ];
  }, [store, taskIdAttr, span]);

  return (
    <div
      data-testid="drawer-task-trajectory"
      style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}
    >
      <div style={{ fontSize: 10, opacity: 0.6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Thinking trajectory
        {taskIdAttr && <span style={{ marginLeft: 6, opacity: 0.7 }}>· task {taskIdAttr}</span>}
      </div>
      {entries.length === 0 ? (
        <div
          data-testid="drawer-task-trajectory-empty"
          style={{ fontSize: 12, opacity: 0.5, padding: '24px 0', textAlign: 'center' }}
        >
          No thinking captured for this task yet.
        </div>
      ) : (
        <ol
          style={{
            listStyle: 'none',
            padding: 0,
            margin: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          {entries.map((e) => (
            <li
              key={e.spanId}
              data-testid="drawer-task-trajectory-entry"
              data-span-id={e.spanId}
              style={{
                fontSize: 11,
                lineHeight: 1.5,
                padding: '8px 10px',
                borderLeft: `2px solid ${e.isLive ? '#a8c8ff' : 'rgba(168,200,255,0.3)'}`,
                background: 'rgba(168,200,255,0.04)',
                borderRadius: '0 6px 6px 0',
              }}
            >
              <div
                style={{
                  fontSize: 10,
                  opacity: 0.6,
                  marginBottom: 4,
                  display: 'flex',
                  gap: 6,
                  alignItems: 'center',
                }}
              >
                <span>🧠</span>
                <code style={{ fontSize: 10 }}>{e.spanName}</code>
                <span>·</span>
                <span>{e.agentId}</span>
                {e.isLive && (
                  <>
                    <span>·</span>
                    <span style={{ color: '#a8c8ff' }}>live</span>
                  </>
                )}
              </div>
              <div
                style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: "ui-monospace, 'SF Mono', Consolas, monospace",
                  color: 'rgba(226,226,233,0.88)',
                }}
              >
                {e.text}
              </div>
            </li>
          ))}
        </ol>
      )}
      <div style={{ fontSize: 10, opacity: 0.5 }}>
        {entries.length} entr{entries.length === 1 ? 'y' : 'ies'} · newest
        last · {formatThinkingInline(
          entries[entries.length - 1]?.text ?? null,
          60,
        )}
      </div>
    </div>
  );
}

// Silence unused-variable warnings on helpers that the trajectory panel only
// uses in some branches — tsc needs one reference to keep them optimized in.
void spanHasThinking;

function PlanRevisionsSection({
  sessionId,
  span,
}: {
  sessionId: string | null;
  span: Span;
}) {
  const store = getSessionStore(sessionId);
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.tasks.subscribe(() => bump());
  }, [store]);

  if (!store) return null;

  // Prefer the plan bound to this span's task; fall back to the plan that
  // owns whatever task is currently RUNNING so the tab stays informative when
  // the selected span isn't itself a task-bound span.
  const taskIdAttr =
    span.attributes?.['hgraf.task_id']?.kind === 'string'
      ? span.attributes['hgraf.task_id'].value
      : undefined;
  let planId: string | undefined;
  if (taskIdAttr) {
    planId = store.tasks.findPlanForTask(taskIdAttr)?.plan.id;
  }
  if (!planId) {
    planId = store.getCurrentTask()?.plan.id;
  }
  if (!planId) return null;

  const revisions = store.tasks.revisionsForPlan(planId);
  if (revisions.length === 0) return null;

  const ordered = [...revisions].reverse();

  return (
    <section data-testid="drawer-plan-revisions">
      <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Plan revisions
      </div>
      <div
        style={{
          maxHeight: 280,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        }}
      >
        {ordered.map((r, i) => (
          <PlanRevisionEntry
            key={`${r.revisedAtMs}-${i}`}
            revision={r}
            latest={i === 0}
          />
        ))}
      </div>
    </section>
  );
}

function PlanRevisionEntry({
  revision,
  latest,
}: {
  revision: PlanRevision;
  latest: boolean;
}) {
  // Latest expands by default so the highlighted entry already shows its diff;
  // older entries collapse to keep the history scannable.
  const [open, setOpen] = useState(latest);
  const diff = revision.diff;
  const added = diff?.added.length ?? 0;
  const removed = diff?.removed.length ?? 0;
  const modified = diff?.modified.length ?? 0;
  const hasDiff =
    !!diff &&
    (added > 0 || removed > 0 || modified > 0 || diff.edgesChanged);
  const parsed = parseRevisionReason(revision.reason);

  return (
    <div
      data-testid="drawer-plan-revision-entry"
      data-latest={latest ? 'true' : 'false'}
      data-open={open ? 'true' : 'false'}
      data-drift-kind={parsed.kind ?? 'unknown'}
      data-drift-category={parsed.meta.category}
      style={{
        fontSize: latest ? 12 : 11,
        lineHeight: 1.5,
        background: latest
          ? 'rgba(168,200,255,0.08)'
          : 'rgba(255,255,255,0.03)',
        borderLeft: `2px solid ${parsed.meta.color}`,
        borderRadius: 4,
        padding: '6px 10px',
        wordBreak: 'break-word',
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="drawer-plan-revision-toggle"
        style={{
          all: 'unset',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          width: '100%',
        }}
      >
        <span style={{ fontSize: 9, opacity: 0.6, minWidth: 8 }}>
          {open ? '▾' : '▸'}
        </span>
        <span
          data-testid="drawer-plan-revision-icon"
          aria-hidden="true"
          style={{
            fontSize: 13,
            color: parsed.meta.color,
            minWidth: 14,
            display: 'inline-flex',
            justifyContent: 'center',
          }}
        >
          {parsed.meta.icon}
        </span>
        <span
          data-testid="drawer-plan-revision-kind-label"
          style={{
            fontSize: 10,
            fontWeight: 600,
            color: parsed.meta.color,
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
          }}
        >
          {parsed.meta.label}
        </span>
        <span
          data-testid="drawer-plan-revision-category-badge"
          style={{
            fontSize: 9,
            opacity: 0.6,
            padding: '1px 5px',
            borderRadius: 8,
            border: '1px solid currentColor',
            textTransform: 'lowercase',
            letterSpacing: '0.03em',
          }}
        >
          {parsed.meta.category}
        </span>
        <span style={{ fontSize: 9, opacity: 0.55 }}>
          {latest ? 'Latest · ' : ''}
          {formatRevisionTime(revision.revisedAtMs)}
        </span>
        {hasDiff && (
          <span
            style={{
              marginLeft: 'auto',
              fontSize: 10,
              fontFamily: "ui-monospace, 'SF Mono', Consolas, monospace",
              opacity: 0.8,
            }}
            data-testid="drawer-plan-revision-counts"
          >
            +{added} -{removed} ~{modified}
            {diff!.edgesChanged ? ' ⇄' : ''}
          </span>
        )}
      </button>
      <div
        style={{ marginTop: 2 }}
        data-testid="drawer-plan-revision-detail"
        title={revision.reason}
      >
        {parsed.detail || parsed.meta.label}
      </div>
      {open && diff && hasDiff && <PlanDiffDetail diff={diff} />}
    </div>
  );
}

function PlanDiffDetail({ diff }: { diff: PlanDiff }) {
  return (
    <div
      data-testid="drawer-plan-revision-diff"
      style={{
        marginTop: 6,
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      {diff.added.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
          {diff.added.map((t) => (
            <span
              key={`add-${t.id}`}
              data-testid="plan-diff-added"
              style={{
                fontSize: 10,
                padding: '2px 6px',
                borderRadius: 10,
                background: 'rgba(120, 200, 140, 0.18)',
                color: '#b7e8c1',
                border: '1px solid rgba(120, 200, 140, 0.35)',
              }}
            >
              + {t.title || t.id}
            </span>
          ))}
        </div>
      )}
      {diff.removed.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
          {diff.removed.map((t) => (
            <span
              key={`rem-${t.id}`}
              data-testid="plan-diff-removed"
              style={{
                fontSize: 10,
                padding: '2px 6px',
                borderRadius: 10,
                background: 'rgba(240, 120, 120, 0.15)',
                color: '#f0a0a0',
                border: '1px solid rgba(240, 120, 120, 0.3)',
                textDecoration: 'line-through',
              }}
            >
              − {t.title || t.id}
            </span>
          ))}
        </div>
      )}
      {diff.modified.length > 0 && (
        <ul
          style={{
            margin: 0,
            paddingLeft: 14,
            fontSize: 10,
            opacity: 0.85,
          }}
        >
          {diff.modified.map((m) => (
            <li key={`mod-${m.id}`} data-testid="plan-diff-modified">
              <span style={{ fontStyle: 'italic' }}>{m.title || m.id}</span>
              {' — '}
              <span style={{ opacity: 0.7 }}>{m.changes.join(', ')}</span>
            </li>
          ))}
        </ul>
      )}
      {diff.edgesChanged && (
        <div
          data-testid="plan-diff-edges"
          style={{ fontSize: 10, opacity: 0.7 }}
        >
          Plan DAG restructured (edges changed)
        </div>
      )}
    </div>
  );
}

function formatRevisionTime(ms: number): string {
  try {
    return new Date(ms).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return '';
  }
}

// --- Goldfive detail helper -------------------------------------------------

// Produces the JSX injected at the top of SummaryTab when the selected span
// is a goldfive translated span. Lives outside SummaryTab so tests can
// cover it in isolation via the component and so we can memoize the
// expensive plan scan for judge / refine linkage separately from the
// reasoning-section state the rest of the tab owns.
function useGoldfiveDetailSection(
  span: Span,
  store: SessionStore | null,
): React.ReactNode {
  // Narrow to goldfive spans first — this is the common-case short-circuit.
  const isGf = isGoldfiveSpan(span);

  // Run the plan-scan memo unconditionally so the hook ordering is stable
  // across calls where `isGf` flips between renders (shouldn't happen in
  // practice, but React's hook rules demand it).
  const plans = useMemo<TaskPlan[]>(() => {
    if (!store) return [];
    const list: TaskPlan[] = [];
    const seen = new Set<TaskPlan>();
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) {
        if (seen.has(snap)) continue;
        seen.add(snap);
        list.push(snap);
      }
    }
    return list;
  }, [store]);

  const judgeDetail = useMemo(() => {
    if (!isGf) return null;
    if (!isJudgeSpan(span)) return null;
    return resolveJudgeDetail(span, plans);
  }, [isGf, span, plans]);

  const gfInfo = useMemo(() => {
    if (!isGf) return null;
    return resolveGoldfiveSpanInfo(span);
  }, [isGf, span]);

  // Linked PlanRevised lookup for refine_* spans. Resolve via
  // target_task_id + temporal proximity: the plan revision whose plan
  // contains a task with id == info.targetTaskId AND whose createdAtMs
  // is closest to (and not before) the span's startMs. Falls back to the
  // most recent revision of any plan if no task match is found.
  const linkedPlan = useMemo<TaskPlan | null>(() => {
    if (!isGf || !gfInfo) return null;
    if (gfInfo.category !== 'refine') return null;
    if (plans.length === 0) return null;
    const targetTask = gfInfo.targetTaskId;
    // Only consider real revisions (rev index > 0).
    const candidates = plans.filter((p) => (p.revisionIndex ?? 0) > 0);
    if (candidates.length === 0) return null;
    const spanAt = span.startMs;
    let best: TaskPlan | null = null;
    let bestScore = Number.POSITIVE_INFINITY;
    for (const p of candidates) {
      // Skip revisions that ended before the span even started — those
      // can't be the effect of this steer.
      if (p.createdAtMs + 1 < spanAt) continue;
      const taskMatch = targetTask && p.tasks.some((t) => t.id === targetTask);
      // Prefer task matches; among task matches pick earliest-after-span.
      const score = (taskMatch ? 0 : 100_000) + Math.abs(p.createdAtMs - spanAt);
      if (score < bestScore) {
        best = p;
        bestScore = score;
      }
    }
    return best;
  }, [isGf, gfInfo, plans, span.startMs]);

  if (!isGf || !gfInfo) return null;

  // Judge spans keep their richer detail component — it covers verdict,
  // severity, raw response, and steered-plan linkage in one panel.
  if (judgeDetail) {
    return (
      <div
        className="hg-drawer__goldfive"
        data-testid="drawer-goldfive-section"
        data-mode="judge"
      >
        <JudgeInvocationDetail
          detail={judgeDetail}
          resolveAgentName={(id) => store?.agents.get(id)?.name || bareAgentName(id) || id}
        />
      </div>
    );
  }

  const linked = linkedPlan
    ? {
        plan: linkedPlan,
        onOpen: undefined,
      }
    : null;

  return (
    <div
      className="hg-drawer__goldfive"
      data-testid="drawer-goldfive-section"
      data-mode="generic"
    >
      <GoldfiveSpanDetail span={span} info={gfInfo} linkedPlanRevision={linked} />
    </div>
  );
}

// --- Summary tab ------------------------------------------------------------

export function SummaryTab({ span }: { span: Span }) {
  const durationMs =
    span.endMs !== null ? span.endMs - span.startMs : null;
  const entries = Object.entries(span.attributes);
  // Render the reasoning section on Summary too so it isn't hidden behind the
  // Task → Overview subtab (harmonograf: Reasoning was undiscoverable from the
  // default drawer tab). This is additive — the TaskOverviewPanel still renders
  // its own copy for users who navigate there.
  const {
    inlineText: reasoningInlineText,
    payloadRef: reasoningRef,
    callCount: reasoningCallCount,
    isAggregate: reasoningIsAggregate,
    show: showReasoning,
  } = readReasoning(span);

  // Goldfive detail section: for translated goldfive spans (judge / refine /
  // plan / reflective / unknown) surface the decision at the top of the
  // summary tab so the drawer answers "what did goldfive decide?" before
  // any raw attribute table. Judge spans route to the existing
  // JudgeInvocationDetail; everything else renders the generic
  // GoldfiveSpanDetail with input/output previews + context list.
  const sessionId = useUiStore((s) => s.currentSessionId);
  const store = getSessionStore(sessionId) ?? null;
  const goldfiveSection = useGoldfiveDetailSection(span, store);

  return (
    <div className="hg-drawer__section">
      {goldfiveSection}
      {showReasoning && (
        <ReasoningSection
          inline={reasoningInlineText}
          payloadRef={reasoningRef}
          callCount={reasoningCallCount}
          isAggregate={reasoningIsAggregate}
        />
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

  const dispatch = async (args: Parameters<typeof send>[0]) => {
    setBusy(args.kind);
    setError(null);
    try {
      await send(args);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const awaiting = span.status === 'AWAITING_HUMAN';

  return (
    <div className="hg-drawer__section">
      {awaiting && (
        <div className="hg-drawer__approval">
          <p><strong>Agent is waiting for human approval.</strong></p>
          <div className="hg-drawer__row">
            <button
              onClick={() => dispatch({ sessionId, agentId: span.agentId, kind: 'APPROVE' })}
              disabled={busy !== null}
            >
              Approve
            </button>
            <button
              onClick={() =>
                dispatch({
                  sessionId,
                  agentId: span.agentId,
                  kind: 'REJECT',
                  detail: 'rejected',
                })
              }
              disabled={busy !== null}
            >
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
          onClick={() =>
            dispatch({
              sessionId,
              agentId: span.agentId,
              kind: 'STEER',
              note: steerBody,
            })
          }
          disabled={busy !== null || !steerBody.trim()}
        >
          {busy === 'STEER' ? 'Sending…' : 'Send steer'}
        </button>
      </div>
      <h3>Transport</h3>
      <div className="hg-drawer__row">
        <button
          onClick={() => dispatch({ sessionId, agentId: span.agentId, kind: 'PAUSE' })}
          disabled={busy !== null}
        >
          Pause agent
        </button>
        <button
          onClick={() => dispatch({ sessionId, agentId: span.agentId, kind: 'RESUME' })}
          disabled={busy !== null}
        >
          Resume
        </button>
        <button
          onClick={() => dispatch({ sessionId, agentId: span.agentId, kind: 'CANCEL' })}
          disabled={busy !== null}
        >
          Cancel
        </button>
        <button
          onClick={() =>
            dispatch({
              sessionId,
              agentId: span.agentId,
              kind: 'REWIND_TO',
              taskId: span.id,
            })
          }
          disabled={busy !== null}
        >
          Rewind to here
        </button>
      </div>
      {error && <p className="hg-drawer__error">{error}</p>}
    </div>
  );
}
