# The harmonograf mental model

This document is not a glossary. A glossary defines terms in isolation; a
mental model explains how the pieces *compose*. By the end you should be
able to close your eyes and trace a prompt from the user through the
orchestrator, through a sub-agent, through a reporting tool call, through
the server, and onto the Gantt canvas — without reaching for a reference.

For a flat glossary, see [terminology-map.md](terminology-map.md). For the
byte-level wire protocol, see [docs/protocol/](../protocol/index.md). For
the narrative walk-through, see [15-minute-tour.md](15-minute-tour.md).

---

## The layering

Harmonograf has four conceptual layers. Every primitive in the system
lives in exactly one of them. Keeping them straight is the single biggest
investment you can make in your mental model.

1. **Telemetry layer.** Spans, payloads, links. What actually happened,
   with timestamps. Fine-grained, high-volume, append-only.
2. **Plan layer.** Tasks, edges, plans, plan revisions. What was *supposed*
   to happen, as declared by the planner. Coarse-grained, mutable only
   through explicit upserts.
3. **State layer.** Session.state keys, ContextVars, divergence flags. The
   shared working memory that agents and the orchestrator read and write
   between turns.
4. **Control layer.** Control events, acks, annotations. Out-of-band
   commands from the frontend to the agents; human commentary overlaid
   onto any of the other layers.

Harmonograf's central design bet is that these four layers are *separate
and explicit*. Span-based observability tools fold all four into the
telemetry layer and then force you to reconstruct the other three. Hence:
plans stored as data, tasks transitioned by explicit tool calls, drift as
a first-class event, control on its own channel.

---

## Sessions and agents

A **session** is a single agent rollout. One prompt, one run, possibly
many sub-agents. It has an id (regex `^[a-zA-Z0-9_-]{1,128}$`), a title,
a created-at timestamp, and a status (`LIVE`, `COMPLETED`, or `ABORTED`).
A session ends when every agent cleanly disconnects. The server owns the
authoritative session object and fans it out to every frontend subscriber.

An **agent** is an actor inside a session. It has an id, a name, a
framework (`ADK` today, extensible later), a capability set, and a
connection to the server. In ADK terms, one agent in harmonograf
corresponds to one ADK sub-agent — `coordinator_agent`, `research_agent`,
and so on. Agents arrive at the session via a `Hello` on the telemetry
stream and leave via a `Goodbye`. The server denormalizes `agent_ids`
onto the session for fast listing.

The relationship is one-to-many: one session, many agents. There is no
cross-session agent concept — an agent only exists in the context of a
session, and spans always belong to a single `(session, agent)` pair.

See [docs/protocol/data-model.md](../protocol/data-model.md) for the wire
shape of `Session` and `Agent`.

---

## Spans: the fine-grained record

A **span** is an event with a duration. Every LLM call, every tool call,
every transfer between sub-agents, every inbound user message, every
outbound agent message, every top-level invocation is a span. Spans have:

- a kind (`INVOCATION`, `LLM_CALL`, `TOOL_CALL`, `TRANSFER`,
  `USER_MESSAGE`, `AGENT_MESSAGE`, `WAIT_FOR_HUMAN`, `CUSTOM`),
- a status (running, completed, failed, cancelled, replaced),
- a start time and (eventually) an end time,
- an attribute bag (tool name, tool arguments, tool return, model id,
  token counts, anything else),
- optional payload references (content-addressed bytes for large inputs
  or outputs),
- optional links to other spans (causality, cross-agent edges,
  supersession).

Spans are append-only from the client's perspective: once emitted, only
`SpanUpdate` and `SpanEnd` can mutate them, and the server merges updates
deterministically. See [docs/protocol/span-lifecycle.md](../protocol/span-lifecycle.md)
for the full lifecycle.

The critical thing to internalize: **spans are telemetry, not state.** A
running span does not mean a task is running. A closed span does not mean
a task is done. Harmonograf explicitly decouples the two — if you find
yourself reasoning about task state from span transitions, you're back in
the world the project exists to escape.

How spans render: [docs/user-guide/gantt-view.md](../user-guide/gantt-view.md).

---

## Tasks and plans: what was supposed to happen

A **task** is a unit of work the planner emitted. It has:

- an id,
- a title and description,
- an assignee (which agent is supposed to execute it),
- a status (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`),
- predicted start and duration (for ghost bars on the Gantt),
- an optional bound span id (once the task actually starts running, we
  link it to the span it's executing under).

A **plan** is a DAG of tasks. Nodes are `Task`s, edges are `TaskEdge`s
that declare "task B can't start until task A completes". Plans have an
id, a session id, a creator ("planner_agent_id"), a revision number, a
summary, and the tasks + edges themselves.

A session usually has several plans over its lifetime: the original
plan, plus one for every refine. Each revision is stored by the server
and diffed against the previous version; the diff (added / removed /
reordered / re-parented / re-assigned) is what the frontend renders as
the plan-diff banner and drawer.

Task **state is monotonic**. `PENDING → RUNNING → COMPLETED` or
`PENDING → RUNNING → FAILED`. There is no going backwards — a failed task
that is retried shows up as a *new task* in a revised plan, not as a
re-run of the old one. This is what lets the frontend animate plan
transitions safely.

Ground truth: [docs/protocol/task-state-machine.md](../protocol/task-state-machine.md).
User-facing view: [docs/user-guide/tasks-and-plans.md](../user-guide/tasks-and-plans.md).

---

## The reporting tools: agents confess their state

Task transitions don't happen automatically. Every sub-agent wrapped by
`HarmonografAgent` gets **seven reporting tools** injected into its tool
list, and sub-agent instructions are augmented with a short contract
telling the model when to call each one:

| Tool | Effect |
|---|---|
| `report_task_started(task_id)` | `PENDING → RUNNING` |
| `report_task_progress(task_id, fraction, detail)` | no state change; feeds the liveness indicator |
| `report_task_completed(task_id, summary, artifacts)` | `RUNNING → COMPLETED`; summary written into `harmonograf.completed_task_results` for downstream tasks |
| `report_task_failed(task_id, reason, recoverable)` | `RUNNING → FAILED`; fires a refine (`task_failed_recoverable` or `task_failed_fatal`) |
| `report_task_blocked(task_id, blocker, needed)` | stays RUNNING; may fire a `blocked` refine |
| `report_new_work_discovered(parent_task_id, title, description, assignee)` | fires a `new_work_discovered` refine |
| `report_plan_divergence(note, suggested_action)` | sets `harmonograf.divergence_flag=True`; fires a `plan_divergence` refine |

The tool *bodies* return `{"acknowledged": True}` and nothing else. The
real work happens in harmonograf's `before_tool_callback`: it matches the
tool name against `REPORTING_TOOL_NAMES`, routes the arguments into
`_AdkState`, applies the state transition, writes the relevant
`harmonograf.*` keys into `session.state`, and may enqueue a refine call.
Only then does the tool body run. The model sees a clean synchronous ack.

The reporting-tool protocol is **the source of truth for task state**.
Everything else — `after_model_callback` parsing prose markers,
`on_event_callback` watching for `state_delta` writes — is
belt-and-suspenders for models that don't call the tools reliably.

Full reference: [docs/reporting-tools.md](../reporting-tools.md). See also
the derivation in [docs/overview.md §Design Principle 1](../overview.md).

---

## session.state: shared notes between turns

ADK gives every session a mutable dict called `session.state`, visible
to every sub-agent's prompt context. Harmonograf uses the `harmonograf.`
prefix to reserve a slice of that dict as its own channel.

**Harmonograf writes** (in `before_model_callback`, before every model
turn):

- `harmonograf.current_task_id`, `...title`, `...description`,
  `...assignee` — which task the agent should be working on right now
- `harmonograf.plan_id`, `...summary` — which plan the task belongs to
- `harmonograf.available_tasks` — list of `{id, title, assignee, status,
  deps}` dicts so the model can see sibling tasks
- `harmonograf.completed_task_results` — `task_id → summary` for every
  task already finished, so downstream tasks inherit context without
  you wiring prompts by hand
- `harmonograf.tools_available` — names of the reporting tools actually
  wired up for this sub-agent

**Agents write** (via `state_delta` or via reporting tools which update
these keys as a side effect):

- `harmonograf.task_progress` — `task_id → 0.0–1.0` progress hint
- `harmonograf.task_outcome` — `task_id → summary` for terminal outcomes
- `harmonograf.agent_note` — free-form latest note, surfaced in the
  inspector drawer
- `harmonograf.divergence_flag` — the agent declares the plan stale

`state_protocol.extract_agent_writes(before, after)` diffs two snapshots
and returns only the `harmonograf.*` keys the agent touched. This is
what lets `on_event_callback` and `after_model_callback` see what the
agent wrote without mixing it up with framework-internal state.

Full key schema: [docs/protocol/task-state-machine.md §session.state
schema](../protocol/task-state-machine.md). Module: `state_protocol.py`
under `client/harmonograf_client/`.

---

## ContextVars: per-invocation plumbing

`session.state` is shared across sub-agents. But some state has to be
*per-invocation* — specifically, the `task_id` that a given model call is
executing. If you only read the current task from `session.state`,
concurrent sub-agents running in parallel mode would race each other's
writes and bind spans to the wrong tasks.

Harmonograf solves this with a `task_id` **ContextVar** that the DAG
walker sets before invoking each sub-agent. The walker forces the
context, invokes the agent, and resets. Every span emitted inside that
scope inherits the `hgraf.task_id` attribute, which binds the span to
the task for drawer rendering. Parallel branches that share
`session.state` never race because each has its own ContextVar scope.

This only applies in **parallel** orchestration mode. Sequential and
delegated modes run one sub-agent at a time and don't need the
isolation. See [docs/protocol/task-state-machine.md §Orchestration
modes](../protocol/task-state-machine.md) and
[docs/dev-guide/client-library.md](../dev-guide/client-library.md) for
the walker internals.

---

## Drift: when reality departs from the plan

A plan is a prediction. Predictions go stale. **Drift** is the umbrella
term for any event that makes the current plan no longer match reality.
Harmonograf's client library has a taxonomy of about two dozen drift
kinds, each with a defined trigger and a defined refine behavior:

- `tool_error` — a tool call raised and the agent has no clean recovery
- `agent_refusal` — the model refused to execute the task
- `context_pressure` — the conversation has grown past a budget
- `new_work_discovered` — a sub-task was discovered that the plan
  didn't account for
- `plan_divergence` — the agent declared the whole plan stale via
  `report_plan_divergence`
- `user_steer` — a human intervention through the frontend steered the
  run
- `user_cancel` — a human cancelled a task
- `task_failed_recoverable` — `report_task_failed(recoverable=True)`
- `task_failed_fatal` — `report_task_failed(recoverable=False)`
- `task_blocked` — `report_task_blocked` was called with a structural
  blocker
- ... and more

Each drift kind fires a **deferential refine**: a structured call back
into the planner with the current plan, the drift context, and a hint
for what the planner should do. The planner is deferential because it
doesn't assume the drift is catastrophic — minor drift often produces
a minor plan revision.

Drift is *the thing that makes the plan layer dynamic*. Without drift,
a plan is just a one-shot prediction. With drift, the plan is a living
object that tracks reality, and the Gantt has a story arc: "we planned
X, we hit Y, we re-planned to Z".

Full taxonomy: [docs/protocol/task-state-machine.md](../protocol/task-state-machine.md).

---

## Refine: how the plan updates

A **refine** is the operation that turns a drift event into a revised
plan.

1. Something triggers a drift (reporting tool call, callback scan,
   event observer).
2. Harmonograf's client library gathers context: the current plan, the
   drift kind, the drift payload, any relevant session.state keys.
3. It calls the planner agent again, passing the current plan and the
   drift context. This is the "deferential" part — the planner is
   invited to revise, not replaced.
4. The planner returns a revised plan.
5. `TaskRegistry.upsertPlan` stores the revision and computes a diff
   against the previous plan (added / removed / reordered /
   re-parented / re-assigned tasks).
6. The server fans the diff out to every subscribed frontend.
7. The frontend renders a plan-revision banner and, if you click it,
   opens the plan-diff drawer with the new plan side-by-side against
   the old.

Refines are rate-limited and deduped — a burst of tool errors on the
same task won't trigger a refine storm. The frontend's
`computePlanDiff` in `frontend/src/gantt/index.ts` is the code that
produces the visual diff; for the wire-level plan-diff format see
[docs/protocol/data-model.md](../protocol/data-model.md).

---

## Control events: talking back to the agents

So far the flow has been one-way: agents emit telemetry, plan changes
flow down as diffs, frontend renders. **Control events** are the other
direction. When you press Space on the Gantt, the frontend emits a
`SendControl` RPC to the server. The server routes the control event to
the right agent(s) over `SubscribeControl`, a separate server-streaming
gRPC call that each agent opens right after its `Hello` / `Welcome`
handshake.

The control kinds currently include:

- `PAUSE` / `RESUME` — freeze or unfreeze an agent's next turn
- `STEER` — inject a note / nudge into the agent's next prompt
- `STATUS_QUERY` — ask the agent for an explicit status report
- (more as the coordination surface grows)

Acks come back **upstream on the telemetry stream**, not on the control
stream. This is deliberate: acks need happens-before ordering with the
spans the agent emitted before issuing the ack, so the UI can show
"paused at span X" correctly. The control stream is used only for
delivery; the telemetry stream carries the acknowledgment. Every new
client adapter forgets this exactly once. See
[docs/protocol/wire-ordering.md](../protocol/wire-ordering.md).

User-facing view: [docs/user-guide/control-actions.md](../user-guide/control-actions.md).

---

## Annotations: human commentary

An **annotation** is a human note attached to something on the timeline
— a span, a task, an agent, or a specific `(agent, time)` point.
Annotations are created through the inspector drawer or via
`PostAnnotation` on the frontend RPC surface. The server stores them,
fans them out to other subscribers, and optionally synthesizes a
`ControlEvent` to deliver the note to the target agent as a `STEER`.

Annotations are the bridge from "human observation" to "agent
instruction". You write a note on a span, and if you check the "deliver
to agent" box, that note becomes part of the agent's next prompt. It is
the shortest path from "I noticed a problem" to "the agent knows about
the problem".

See [docs/user-guide/annotations.md](../user-guide/annotations.md) and
the `Annotation` / `AnnotationTarget` messages in
[docs/protocol/data-model.md](../protocol/data-model.md).

---

## Payloads: content-addressed bytes

LLM calls and tool calls produce large blobs — multi-KB prompts,
multi-MB generated documents, long structured responses. Putting those
inline in every span would blow out the telemetry budget. Harmonograf
splits payloads out: each span can reference any number of **payloads**
by content hash (`PayloadRef{digest, size, mime, summary, role}`), and
the actual bytes ride a separate chunked upload on the same telemetry
stream.

Two consequences you can see in the UI:

- The Gantt drawer's Payload tab shows the payload's mime, size, and
  summary without the full body — and lets you fetch the full body
  on demand via `GetPayload`.
- The client library can *evict* payloads on memory pressure, and the
  server can ask for them back later via a `PayloadRequest`. Payloads
  are content-addressed, so re-upload is idempotent.

Full flow: [docs/protocol/payload-flow.md](../protocol/payload-flow.md).
User view: [docs/user-guide/drawer.md](../user-guide/drawer.md).

---

## How the layers compose

Putting it all together, a single task's lifecycle looks like this:

1. The planner emits a plan containing the task. It lands in the **plan
   layer** via a plan upsert.
2. The orchestrator writes the task into `session.state` under
   `harmonograf.current_task_*`. The **state layer** now has the
   context the sub-agent needs.
3. The sub-agent's `before_model_callback` fires; harmonograf injects
   the reporting tools and the sub-agent-instruction appendix into
   the model turn.
4. The model responds with a call to `report_task_started(task_id=...)`.
   Harmonograf intercepts in `before_tool_callback`, transitions the
   task to `RUNNING` in the **plan layer**, and binds the current span
   via `hgraf.task_id` in the **telemetry layer**.
5. The model calls tools. Each call becomes a span in the **telemetry
   layer**. Large results land as payloads. Cross-agent transfers
   create links between spans.
6. The model calls `report_task_completed` (or `report_task_failed`).
   The **plan layer** transitions to terminal state. The summary is
   written back into `session.state` so the next task inherits context.
7. If something drifted along the way, a refine fires. The planner
   produces a revised plan. The **plan layer** stores a new revision
   and a diff. The frontend renders the diff.
8. At any point, the human can issue a **control layer** event — pause,
   resume, steer, annotate — and the agent sees it on the next turn.

Every primitive in the system plays its part. The telemetry layer
records what happened. The plan layer records what was supposed to
happen. The state layer carries per-turn context. The control layer
carries human intent. The four layers are connected only through
explicit, auditable interfaces — reporting tools, session.state keys,
control events, annotations — which is exactly what lets harmonograf
claim that its task state is correct by construction rather than
inferred by luck.

---

## Where to go from here

- **[terminology-map.md](terminology-map.md)** — visual map of harmonograf
  terms against ADK, OpenTelemetry, and other agent frameworks.
- **[docs/reporting-tools.md](../reporting-tools.md)** — the canonical
  agent-facing tool contract.
- **[docs/protocol/task-state-machine.md](../protocol/task-state-machine.md)**
  — the full `session.state` schema, orchestration modes, drift taxonomy,
  and refine pipeline.
- **[docs/protocol/overview.md](../protocol/overview.md)** — the three
  RPC channels and how they fit together.
- **[docs/dev-guide/architecture.md](../dev-guide/architecture.md)** —
  an end-to-end walk-through of a span traveling from ADK callback to
  Gantt pixel.
- **[docs/user-guide/](../user-guide/index.md)** — how these primitives
  render in the UI.
