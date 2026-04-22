# Glossary

Alphabetical list of every harmonograf-specific term that shows up in the
UI, the client library, the server, or the protocol. Definitions are
short; cross-links point at the full treatment.

If a term is missing, check [faq.md](faq.md) or the protocol reference
under `docs/protocol/`.

## A

### Activation box
A filled 16 px-wide box on an agent column in the [Graph view](graph-view.md#activation-boxes),
marking the time range of one INVOCATION span on that agent.

### ADK
Google's **Agent Development Kit**. The orchestration adapter for ADK
lives in [goldfive](https://github.com/pedapudi/goldfive)
(`goldfive.adapters.adk.ADKAdapter`). Harmonograf contributes an
optional observability plugin (`HarmonografTelemetryPlugin`) that hooks
ADK's `before_model_callback`, `after_model_callback`,
`before_tool_callback`, `after_tool_callback`, and `on_event_callback`
to emit spans. See `AGENTS.md` for the project-level split and
[goldfive-integration.md](../goldfive-integration.md) for the composition
pattern.

### Agent
One entity that reports telemetry to the harmonograf server. Identified
by a client-chosen `id` that persists across reconnects so the agent
reclaims its Gantt row. See [data-model → Agent](../protocol/data-model.md#agent).

### Agent gutter
The column on the left edge of the [Gantt view](gantt-view.md#layout-recap)
showing one row per agent with status dot, name, and focus / hide
toggles.

### Agent id
The stable identifier for an agent across its lifetime. Chosen by the
client, persisted to disk. May be shared across multiple concurrent
telemetry streams (each with its own `stream_id`).

### AgentTool
ADK primitive that wraps one `Agent` as a `Tool` callable by another
agent. Used by `presentation_agent.agent` to compose sub-agents as tools
of a coordinator. Harmonograf treats `AgentTool` invocations as regular
TOOL_CALL spans, but the delegations they trigger appear as cross-agent
edges on the [Gantt](gantt-view.md#reading-a-bar--kinds-status-decorations)
and arrows on the [Graph](graph-view.md#arrows--transfer-delegation-return).

### Annotation
A human-authored note attached to a span. Three kinds: `COMMENT`,
`STEERING`, `HUMAN_RESPONSE`. See [annotations.md](annotations.md) and
[data-model → Annotation](../protocol/data-model.md#annotation).

### Arrow (Graph view)
A line drawn between agent columns representing a cross-agent
interaction. Three variants: **transfer** (orange solid), **delegation**
(blue dashed), **return** (grey dashed italic). See
[graph-view.md → arrows](graph-view.md#arrows--transfer-delegation-return).

### Attention count
Per-session count of spans in `AWAITING_HUMAN` (plus other raised-flag
kinds the server defines). Rendered as a red `N need attention` chip in
the session picker and aggregated into the app bar's bell icon. See
[sessions.md → attention badges](sessions.md#attention-badges).

### `AWAITING_HUMAN`
`SpanStatus` value indicating a span is blocked on a human decision.
Renders with a red outline and 1 s pulse on the Gantt. Exposes the
`Approve` / `Reject` buttons in the drawer's Control tab. See
[data-model → Span](../protocol/data-model.md#span) and
[control-actions.md → approve / reject](control-actions.md#approve--reject).

## B

### `boundSpanId`
Field on a `Task` that records which span is currently executing this
task. Set when a span carrying `hgraf.task_id = <task.id>` begins
running, or explicitly via `UpdatedTaskStatus.bound_span_id`. See
[data-model → Task](../protocol/data-model.md#taskplan--task--taskedge).

### Bus
The server's fan-out mechanism that distributes session updates to every
connected frontend watcher. Implemented in
`server/harmonograf_server/bus.py`.

## C

### `CANCEL`
Control kind. Harder than pause — the agent cancels the in-flight
invocation. Tool calls abort if the framework supports it. The cancelled
span transitions to `CANCELLED`. See
[control-actions.md → cancel](control-actions.md#cancel).

### Capability
An advertised agent ability the frontend may target with control
messages: `PAUSE_RESUME`, `CANCEL`, `REWIND`, `STEERING`,
`HUMAN_IN_LOOP`, `INTERCEPT_TRANSFER`. Advertised in the client's `Hello`.
See [control-actions.md → capability negotiation](control-actions.md#capability-negotiation).

### Category (drift)
A grouping of drift kinds into coarse buckets: `error`, `divergence`,
`discovery`, `user`, `structural`. Drives the pill color on the
[PlanRevisionBanner](tasks-and-plans.md#planrevisionbanner). See
[tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds).

### Coordinator
In sequential orchestration mode, the single LLM that executes the whole
plan in one turn and reports per-task lifecycle via the reporting tools.
Contrast with the **walker** in parallel mode.

### `ContextVar`
Python primitive (`contextvars.ContextVar`) used by goldfive's
`ParallelDAGExecutor` to plumb a `forced_task_id` down to the sub-agent
call site without threading it through every argument. The harmonograf
telemetry plugin reads it and stamps it onto spans as `hgraf.task_id`.

### `ControlAck`
The response message the agent sends back for a `ControlEvent`. Carries
the `control_id`, a result enum, an optional free-text `detail`, and an
`acked_at` timestamp. See
[data-model → ControlEvent / ControlAck](../protocol/data-model.md#controlevent--controltarget--controlack).

### `ControlEvent`
The outbound control message from the frontend to an agent. Carries a
kind, a target `(agent_id, span_id?)`, and an opaque bytes payload. See
[control-stream.md](../protocol/control-stream.md).

### Control kind
Which control to execute. The full enum is in
[control-actions.md → the control kinds](control-actions.md#the-control-kinds):
`PAUSE`, `RESUME`, `CANCEL`, `REWIND_TO`, `INJECT_MESSAGE`, `APPROVE`,
`REJECT`, `INTERCEPT_TRANSFER`, `STEER`, `STATUS_QUERY`.

### Current task
The session's live "what is being worked on right now" readout. Computed
by `store.getCurrentTask()` as the RUNNING task, or the most recently
completed task if none is running. Surfaced in the
[CurrentTaskStrip](tasks-and-plans.md#currenttaskstrip) and the drawer
header.

## D

### `delegated`
Orchestration mode (`OBS` chip). The inner agent owns its own task
sequencing; harmonograf only watches for drift. See
[tasks-and-plans.md → orchestration modes](tasks-and-plans.md#orchestration-modes).

### Delegation arrow
Blue dashed arrow on the [Graph view](graph-view.md#arrows--transfer-delegation-return),
inferred from a cross-agent INVOCATION parent when no explicit TRANSFER
span exists. Contrast with **transfer arrow** (explicit).

### Digest (payload)
`sha256` of the payload bytes as hex. Content-addressed: identical bytes
share one digest across sessions. See
[data-model → PayloadRef](../protocol/data-model.md#payloadref).

### Drift
A discrepancy between the plan and reality that causes the planner to
revise the plan. Each drift has a **kind** and a **severity** (`info`,
`warning`, `critical`). See
[tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds).

### Drift kind
Structured tag for the reason a plan revision fired. Examples:
`USER_STEER`, `PLAN_DIVERGENCE`, `CONFABULATION_RISK`, `TOOL_ERROR`,
`LOOPING_REASONING`, `AGENT_REFUSAL`, `HUMAN_INTERVENTION_REQUIRED`,
`GOAL_DRIFT`. Full table in
[tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds).
Authoritative source is goldfive's `DriftKind` enum; the frontend
deriver (`frontend/src/lib/interventions.ts`) renders whatever
goldfive emits.

### Drift severity
One of `info`, `warning`, `critical`. Populated on `TaskPlan.revision_severity`.

## E

### Edge (plan DAG)
A `TaskEdge` from one task to another expressing a dependency — the `to`
task cannot start until the `from` task completes. Rendered as dashed
grey bezier curves in the Graph view's task plan overlay.

### `evicted` (payload)
A PayloadRef with the bytes dropped under client backpressure. The ref
is still delivered (so summaries still render) but `GetPayload` returns
not-found. See [drawer.md → payload tab](drawer.md#payload-tab) and
[troubleshooting.md → payloads are missing](troubleshooting.md#payloads-are-missing).

## F

### `finish_reason`
LLM finish reason attribute (`MAX_TOKENS`, `LENGTH`, etc.) stamped on
the relevant span. Used to surface context-pressure conditions —
goldfive now exposes this via `goldfive.llm.usage.*_tokens` on
per-LLM-call metrics (goldfive#172) and the frontend's per-agent
context-window overlay. See
[data-model → Span.attributes](../protocol/data-model.md#span).

### Focused agent
An agent row highlighted in the Gantt gutter via `[` / `]` or by
clicking its name. Other rows remain visible. See
[gantt-view.md → focused and hidden agents](gantt-view.md#focused-and-hidden-agents).

### `forced_task_id`
The task id goldfive's parallel walker forces onto the sub-agent's
ContextVar so every span opened by that sub-agent stamps `hgraf.task_id`
correctly. Monotonic: already-terminal tasks are refused by goldfive's
`DefaultSteerer`. See [../goldfive-integration.md](../goldfive-integration.md).

### Framework
Categorical tag on `Agent.framework`: `FRAMEWORK_ADK`,
`FRAMEWORK_CUSTOM`, `FRAMEWORK_UNSPECIFIED`.

## G

### Ghost activation
Task plan overlay render mode on the [Graph view](graph-view.md#task-plan-overlay)
that draws each task as a dashed 25 %-opacity box on the agent lifeline
at its `predictedStartMs` for `predictedDurationMs`. Shows where the
renderer expects work to land.

## H

### Handoff
Conversational term for a TRANSFER or delegation. Not a distinct entity
— use "transfer" for explicit, "delegation" for inferred.

### `has_thinking`
Boolean span attribute. True when the model is currently producing
reasoning trace for this span. Drives the pulsing blue thinking dot next
to the assignee on the [current task strip](tasks-and-plans.md#currenttaskstrip)
and in the drawer's Task tab.

### `Hello`
Handshake RPC the client sends on stream startup. Carries session id,
session title (first-write-wins), agent metadata, and capability set.
No `Hello`, no agent row. See
[telemetry-stream.md](../protocol/telemetry-stream.md).

### `hgraf.task_id`
Reserved span attribute key. Binds a span to a task in the
`TaskRegistry`. Written by the client's state protocol when a task is
currently forced or assigned. See
[data-model → Span.attributes](../protocol/data-model.md#span) and
`AGENTS.md`.

### Hidden agent
An agent row filtered out of the main Gantt plot via the gutter's hide
toggle. Persisted to the UI store; still visible on the minimap. See
[gantt-view.md → focused and hidden agents](gantt-view.md#focused-and-hidden-agents).

### Host agent
Common idiom for the coordinator / top-level agent that owns the root
invocation and delegates to sub-agents. Not a formal term in the proto.

### `hsession_id`
Internal variable name for the harmonograf session id as it flows
through ADK callback inspection; on the wire it is just `session_id`.
Now used inside goldfive's `ADKAdapter`.

### `HUMAN_RESPONSE`
Annotation kind used to reply to a span blocked in `AWAITING_HUMAN`. The
server synthesizes a `CONTROL_KIND_APPROVE` control from the annotation.
See [annotations.md → the three kinds](annotations.md#the-three-kinds).

## I

### Ingress
The server-side path that receives client telemetry streams, dedupes
spans by id, fans out to watchers via the bus. Code lives in
`server/harmonograf_server/ingest.py`.

### `INJECT_MESSAGE`
Control kind. Injects a user-turn message into the agent's conversation.
Protocol-defined but not wired to a frontend control today — see
[control-actions.md → inject message](control-actions.md#inject-message--intercept-transfer).

### Intervention
Any point in a run where the plan changed direction: a user STEER /
CANCEL, a drift the detectors fired, or an autonomous goldfive revision
(cascade cancel, refine retry, human-intervention-required). Harmonograf
surfaces these as a merged chronological list via `ListInterventions`
plus the `InterventionsTimeline` strip above the Gantt. See
[trajectory-view.md](trajectory-view.md).

### Intervention history
The chronologically-ordered list of interventions for one session.
Fetched once on session open via `ListInterventions` and kept live
from `WatchSession` deltas by the frontend deriver in
`lib/interventions.ts`.

### INVOCATION
Span kind representing one complete agent turn — the outer wrapper
around LLM calls, tool calls, transfers, etc. Renders recessed on the
Gantt because it is a container. See
[data-model → Span.kind](../protocol/data-model.md#span).

## L

### Legend
Modal opened from the `?` button on the app bar. Authoritative visual
reference for every glyph, color, and shape in the Gantt and Graph
views. See [index.md](index.md#orientation--regions-of-the-shell).

### Lifeline
The dashed vertical line under each agent column in the Graph view. Runs
the full plot height; activation boxes attach to it.

### Live-follow
Gantt viewport mode where the right edge is pinned to "now". Any manual
pan disables it. Re-attach with `L` or the **↩ Follow live** button on
the transport bar. See [gantt-view.md → live follow](gantt-view.md#live-follow).

### Liveness tracker
Server-side component that flags agents with open INVOCATION spans and
no recent progress signal as **stuck**. Drives the amber marker on Graph
agent headers. See [graph-view.md → agent headers](graph-view.md#agent-headers)
and [troubleshooting.md → plan stuck](troubleshooting.md#plan-stuck--not-progressing).

### `LLM_CALL`
Span kind for one model request. Streaming LLM bars render **streaming
ticks** on the trailing edge — one per `streaming_tick` event. See
[gantt-view.md → reading a bar](gantt-view.md#reading-a-bar--kinds-status-decorations).

## M

### Minimap (Gantt)
Fixed 240×120 overview in the bottom-left of the Gantt plot. Click or
drag to seek. See [gantt-view.md → minimap](gantt-view.md#minimap).

### Minimap (Graph)
Mirrors the Gantt minimap for the Graph view, with click/drag to pan and
a viewport rectangle showing the currently-visible region. See
[graph-view.md → zoom and minimap](graph-view.md#zoom-and-minimap).

### Mode chip
`SEQ` / `PAR` / `OBS` chip on the [current task strip](tasks-and-plans.md#currenttaskstrip)
reflecting the assignee agent's orchestration mode.

### Monotonic state machine
Task state transitions cannot go backwards. Goldfive's `DefaultSteerer`
enforces this: already-terminal tasks are refused; the walker cannot
reset a completed task even after a refine.

## O

### Orchestration events
Session-level events emitted by goldfive: `RunStarted`, `GoalDerived`,
`PlanSubmitted`, `PlanRevised`, `TaskStarted`, `TaskCompleted`,
`TaskFailed`, `DriftDetected`, `RunCompleted`, `RunAborted`. They
travel up via `TelemetryUp.goldfive_event` and surface in the drawer's
Task tab via the embedded `OrchestrationTimeline`. See
[drawer.md → orchestration events section](drawer.md#orchestration-events-section).

### Orchestration mode
One of `sequential` / `parallel` / `delegated`. See
[tasks-and-plans.md → orchestration modes](tasks-and-plans.md#orchestration-modes).

### Orchestrator
`goldfive.Runner` driving sub-agents through one of three executors
(`SequentialExecutor`, `ParallelDAGExecutor`, or delegated). The mode
chip in the UI reads `SEQ` / `PAR` / `OBS`.

## P

### `PAR`
Mode chip value for parallel orchestration.

### Parallel
Orchestration mode where a rigid DAG batch walker drives sub-agents
directly using a forced `task_id` ContextVar, respecting plan edges as
dependencies. See `AGENTS.md`.

### Payload
One attached blob on a span — a prompt, a response, a tool result, an
image. See [data-model → PayloadRef](../protocol/data-model.md#payloadref).

### Payload digest
See **Digest**.

### `PAUSE`
Control kind. Stops the agent at the next safe boundary; in-flight
tool calls finish. Scope: per-agent (drawer) or session-wide (transport
bar). See [control-actions.md → pause / resume](control-actions.md#pause--resume).

### Pin strip (annotations)
A compact row of pin markers across the top of the Gantt plot; one pin
per span with at least one annotation. Click to jump to the span. See
[annotations.md → where annotations show up](annotations.md#where-annotations-show-up).

### Pinned popover
A span popover that has been pinned via 📌 in its top-right corner.
Stacks with others; does not auto-dismiss when another span is clicked.
See [control-actions.md → span popover](control-actions.md#3-span-popover-quick-look).

### Plan
An ordered (and possibly DAG-shaped) collection of tasks plus their
edges. Carries a `revisionReason` for the most recent revision. A
session can have multiple plans. See [tasks-and-plans.md](tasks-and-plans.md).

### Plan diff
Structured delta between two plan revisions: added, removed, modified
tasks, plus a bool for edge changes. Computed by `TaskRegistry.upsertPlan`
on revision. Rendered as the `+N -M ~K` counts on pills and full diff
bodies in the drawer's Plan revisions section.

### Plan revision
A new snapshot of a plan produced by the planner mid-run in response
to a drift signal. Every revision produces a TaskRegistry snapshot,
a PlanDiff, a PlanRevisionBanner pill, and a row in the drawer's Plan
revisions section. See [tasks-and-plans.md → plan revisions](tasks-and-plans.md#plan-revisions--live-replans).

### `PlanRevisionBanner`
Transient row below the current task strip that shows pills as plan
revisions arrive. Pills auto-dismiss after ~4 s; max three stack FIFO.
See [tasks-and-plans.md → planrevisionbanner](tasks-and-plans.md#planrevisionbanner).

### `PLANNED` span
Span kind for a placeholder span representing a planned but not yet
started task. Renders dashed at 30 % opacity.

### Pre-strip
Task plan overlay render mode on the Graph view that packs task chips
into a reserved strip on the left of each agent column. See
[graph-view.md → task plan overlay](graph-view.md#task-plan-overlay).

## R

### `REJECT`
Control kind. Rejects a span blocked in `AWAITING_HUMAN`. Payload may
carry a rejection reason. See
[control-actions.md → approve / reject](control-actions.md#approve--reject).

### Reporting tool
A tool injected into every sub-agent by goldfive's `ADKAdapter` for
explicit plan-state reporting: `report_task_started`,
`report_task_progress`, `report_task_completed`, `report_task_failed`,
`report_task_blocked`, `report_new_work_discovered`,
`report_plan_divergence`. Intercepted by goldfive's `DefaultSteerer`
which fires the corresponding event (`TaskStarted`, `TaskCompleted`,
…) on every sink, including `HarmonografSink`. See
[`../reporting-tools.md`](../reporting-tools.md).

### `REPLACES`
`LinkRelation` value indicating this span supersedes a prior one — e.g.
after a rewind. See [data-model → SpanLink](../protocol/data-model.md#spanlink).

### `REWIND_TO`
Control kind. Rolls the agent back to the span id in the payload and
resumes from there. Requires checkpointing on the agent. See
[control-actions.md → rewind](control-actions.md#rewind).

### Return arrow
Grey dashed italic "return" arrow drawn at the end of a delegated
invocation in the Graph view. See
[graph-view.md → arrows](graph-view.md#arrows--transfer-delegation-return).

### Revision
See **Plan revision**.

### Revision index
Monotonic counter on `TaskPlan.revision_index`. Increments on each
re-submission of a plan with the same id.

### Revision reason
Free-form string + structured drift kind + severity on the plan. The
kind drives the pill color and label; the detail is the free-form
suffix.

## S

### `SEQ`
Mode chip value for sequential orchestration.

### Sequential
Orchestration mode where the whole plan is fed as one user turn and the
coordinator LLM executes it; per-task lifecycle reported via the
reporting tools. Implemented as `goldfive.SequentialExecutor`.

### Session
One end-to-end run identified by a `session_id`. Everything in the UI
is scoped to the currently selected session. See
[sessions.md](sessions.md) and
[data-model → Session](../protocol/data-model.md#session).

### Session picker
Modal opened with `⌘K` / `Ctrl+K` / `/`. Fuzzy substring search across
title and id; three buckets (Live / Recent / Archive). See
[sessions.md → opening the picker](sessions.md#opening-the-picker).

### `session.state`
ADK's shared mutable dict. Goldfive writes a `SessionContext` (current
task id, plan summary, available tasks, completed task results) before
each model call and reads back task progress, outcome, note, and
divergence flag. Full schema in goldfive's `SessionContext` class
(`goldfive.adapters.adk`).

### Span
The core timeline primitive. One unit of work: an invocation, an LLM
call, a tool call, a transfer, a user / agent message, a
wait-for-human, etc. Identified by a UUIDv7. See
[data-model → Span](../protocol/data-model.md#span).

### Span kind
Categorical type of a span: `INVOCATION`, `LLM_CALL`, `TOOL_CALL`,
`USER_MESSAGE`, `AGENT_MESSAGE`, `TRANSFER`, `WAIT_FOR_HUMAN`, `PLANNED`,
`CUSTOM`. See [data-model → Span](../protocol/data-model.md#span).

### Span link
Cross-span edge with a relation: `INVOKED`, `TRIGGERED_BY`,
`WAITING_ON`, `FOLLOWS`, `REPLACES`. Surfaced in the drawer's Links
tab. See [data-model → SpanLink](../protocol/data-model.md#spanlink).

### Span popover
Quick-look card anchored to a span. Shows summary, status, duration,
latest thinking, and quick action buttons (Steer / Annotate / Copy id
/ Open drawer). Pin-able. See
[drawer.md → span popover](drawer.md#span-popover) and
[control-actions.md → span popover](control-actions.md#3-span-popover-quick-look).

### Span status
Lifecycle state: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`,
`CANCELLED`, `AWAITING_HUMAN`. See
[data-model → Span](../protocol/data-model.md#span).

### Stamp (task binding)
Writing `hgraf.task_id` onto a span as it opens, so the `TaskRegistry`
can bind it back to its owning task. `HarmonografTelemetryPlugin` reads
the `forced_task_id` ContextVar set by goldfive's executor and stamps
it on the outgoing `SpanStart`.

### `STATUS_QUERY`
Control kind. Asks the agent to report its current activity. The ack's
`detail` string is surfaced via the Graph view's `↻ Status` button
and feeds `agent.taskReport`. 8 s timeout. See
[control-actions.md → status query](control-actions.md#status-query).

### `STEER`
Control kind. Delivers free-text (or a JSON mode envelope from the
popover editor) as an instruction to the agent. The client decides how
to merge it into the model turn. See
[control-actions.md → steer](control-actions.md#steer).

### Steering
The general act of course-correcting a running agent. Two flavors:
a one-shot STEER **control** or a durable `STEERING` **annotation**.
See [annotations.md → steering from the popover](annotations.md#steering-from-the-popover-is-a-control-not-an-annotation).

### Stream
One physical telemetry connection from a client process to the server.
Multiple streams can share one `agent.id`; each gets its own
`stream_id` in `Welcome`. See
[data-model → Agent](../protocol/data-model.md#agent).

### Streaming tick
A thin white mark on the trailing edge of a running LLM bar. One per
`streaming_tick` event from the client. Signals that tokens are still
arriving.

### Stuck (agent)
State flagged by the liveness tracker: open INVOCATION with no recent
progress signal. Rendered as amber border + halo + "⚠ stuck" label on
the Graph agent header. See [graph-view.md → agent headers](graph-view.md#agent-headers).

### Sub-agent
In a multi-agent configuration, any agent other than the coordinator.
Harmonograf injects reporting tools into every sub-agent so they can
report plan state directly. Not a distinct proto type.

## T

### Task
A single unit of planned work. Fields: `id`, `title`, `description`,
`assigneeAgentId`, `status`, optional `predictedStartMs` /
`predictedDurationMs`, optional `boundSpanId`. Status transitions are
monotonic: `PENDING` → `RUNNING` → terminal. See
[data-model → Task](../protocol/data-model.md#taskplan--task--taskedge).

### Task panel
Collapsible, resizable list of every task across every plan in the
session. Lives below the Gantt transport bar. Persists expanded height
to `localStorage.harmonograf.taskPanelHeight`. See
[tasks-and-plans.md → task panel](tasks-and-plans.md#task-panel-bottom-of-gantt).

### Task plan
See **Plan**.

### Task plan overlay
Optional Graph-view layer that draws the current plan on top of the
live activity, in three modes: pre-strip, ghost, hybrid. Mode persists
to `localStorage.harmonograf.taskPlanMode`. See
[graph-view.md → task plan overlay](graph-view.md#task-plan-overlay).

### `task_report`
Reserved span attribute that carries a proactive status report from the
agent. Broadcast server-side as a `TaskReport` delta and surfaced in
the drawer's Task tab.

### `TaskRegistry`
Client-side / shared component that holds the canonical plan revision
history for a session, recomputes `PlanDiff` on upsert, and feeds the
frontend's task plan surfaces.

### `TaskStatus`
Enum: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`. See
[data-model → TaskStatus](../protocol/data-model.md#taskplan--task--taskedge).

### Telemetry stream
The upstream gRPC stream from client → server carrying `SpanStart`,
`SpanUpdate`, `SpanEnd`, `TaskReport`, `UpdatedTaskStatus`, and
friends. See [telemetry-stream.md](../protocol/telemetry-stream.md).

### Thinking dot
Small pulsing blue dot on the current task strip and in the drawer
header indicating the assignee agent has a span with `has_thinking =
true`. See [tasks-and-plans.md → currenttaskstrip](tasks-and-plans.md#currenttaskstrip).

### `TOOL_CALL`
Span kind for one function / tool invocation. Typically carries an
`args` payload and a `result` payload.

### Transfer
An explicit hand-off to another agent, modeled as a `TRANSFER` span on
the source agent plus an `INVOKED` link to the destination's
invocation. Rendered as an orange solid arrow on the Graph view and as
a bezier edge on the Gantt. See
[graph-view.md → arrows](graph-view.md#arrows--transfer-delegation-return).

### Transport bar
Bar at the bottom of the Gantt view with session-wide transport
controls: pause / resume (whole session), follow-live, rewind and stop
(placeholders), zoom. See
[control-actions.md → transport bar](control-actions.md#1-transport-bar-session-wide).

## U

### `UpdatedTaskStatus`
Escape-hatch message for task state changes not tied to a span. Rides
upstream on `TelemetryUp.task_status_update`. See
[data-model → UpdatedTaskStatus](../protocol/data-model.md#updatedtaskstatus).

## V

### Viewport locked
State of the transport bar's LIVE badge after a manual pan or pause —
the viewport is no longer tracking "now". Replaced by `○ Viewport
locked`. Press `L` to re-attach. See
[gantt-view.md → live follow](gantt-view.md#live-follow).

## W

### `WAIT_FOR_HUMAN`
Span kind for a span blocked on a human decision. Drives the
`AWAITING_HUMAN` UI state.

### Walker
In parallel orchestration mode, goldfive's `ParallelDAGExecutor` batch
driver that iterates through PENDING tasks, forces their task id onto
the ContextVar, and runs the sub-agent once per task. Enforces
monotonic state via `DefaultSteerer`.

### `WatchSession`
Downstream gRPC stream from server → frontend delivering session
updates (span mutations, task plan updates, annotations, control acks)
in real time. See
[frontend-rpcs.md](../protocol/frontend-rpcs.md).

### `Welcome`
Server response to `Hello`. Carries the `stream_id` assigned to this
physical connection and any server-side metadata.

## Related pages

- [faq.md](faq.md) — short answers organized by symptom.
- [cookbook.md](cookbook.md) — goal-oriented recipes.
- [examples/](examples/) — narrative walkthroughs.
- [../protocol/](../protocol/) — wire-level reference.
- `AGENTS.md` — plan execution protocol invariants.
