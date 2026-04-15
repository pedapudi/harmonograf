# The 15-minute tour

This is a narrative walk-through. By the end you should know what harmonograf
is, what problem it solves, what the main primitives are, how the three
components fit together, and what happens end-to-end when you type a prompt
into an agent. It's written to be read in roughly fifteen minutes.

If you want to skip straight to running the demo, read
[docs/quickstart.md](../quickstart.md) instead. Come back here afterwards to
understand what you were looking at.

---

## 1. The problem (2 min)

An LLM agent is easy to observe. You wrap each model call and each tool call
in a span, nest them into a tree, and render the tree as a waterfall. Done.

A *multi-agent* system breaks that model in four different ways.

**The plan is not the trace.** Real agent rollouts begin with a plan — a
structured list of tasks, usually with dependencies — and the whole point of
the run is to execute that plan. A span tree knows nothing about the plan. It
just shows you "whatever happened, in whatever order it happened". If the
plan changes halfway through (an agent discovers a missing step, a tool
errors, a reviewer flags an issue and a debugger is added), you see a
different tree — you don't see the *change*.

**Task state cannot be inferred from span lifecycle.** Harmonograf's own
earlier iterations tried this and got burned. A sub-agent whose span closes
is not necessarily done: it may have returned control while a background tool
continues. An LLM that emits "task complete" in prose may not have finished
anything. "I will complete the task" parses the same as "task complete".
Concurrent sub-agents running a parallel DAG produce ordering bugs whenever
state transitions are tied to callbacks instead of to the agent explicitly
announcing them.

**Observability without intervention is half a tool.** Multi-agent runs are
long, multi-step, and expensive. If you can see that an agent is stuck but
your only recourse is to kill the process and start over, you'll throw away
twenty minutes of work every time a small thing goes wrong. The console has
to let you *intervene* — pause, resume, steer, send a note — on the same
connection the telemetry came up on.

**Framework sandboxes are real.** ADK and frameworks like it run agents under
strict lifecycle hooks with tight restrictions on how you can influence
execution from outside. Any design that assumes "just attach a debugger and
patch the flow" doesn't work. Coordination has to go through the official
seams — session state, tool calls, event callbacks — or it doesn't work at
all.

Harmonograf is the console we wanted for this problem. Plan-aware. Explicit
about task state. Honest about drift. Bidirectional on the wire. Respectful
of the framework.

Longer-form version: [docs/overview.md](../overview.md).

---

## 2. The mental model in one page (3 min)

Every primitive you need to know fits on one page. Memorize these, and the
rest of the UI is self-explanatory. The deep version lives in
[mental-model.md](mental-model.md).

- **Session.** A single agent run. Has an id, a title, a start time, maybe
  an end time, and a list of agents that participated. One harmonograf
  session corresponds to one user-facing invocation — one prompt, one roll-out.
- **Agent.** An actor inside the session. `coordinator_agent`,
  `research_agent`, `reviewer_agent`, etc. Has a name, a framework (usually
  ADK), and a connection to the server.
- **Span.** An event with a duration. Every LLM call, tool call, transfer,
  user message, agent message, and invocation is a span. Spans are *telemetry
  only* — they no longer drive task state. Think of them as the fine-grained
  record of what happened.
- **Task.** A unit of work the planner emitted. Has an id, a title, a
  description, an assignee (which agent is supposed to do it), and a status
  (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`). Tasks live in a plan.
- **Plan.** The DAG of tasks the planner built up front. Nodes are tasks,
  edges are dependencies. A session can have several plans over its
  lifetime — the original, plus any revisions after a drift event.
- **Drift.** When reality diverges from the plan. A tool errored. An agent
  refused. A new sub-task was discovered. The plan is stale. There are about
  two dozen drift kinds, each with defined semantics.
- **Refine.** The planner's response to drift. Harmonograf fires a
  *deferential refine* — a structured call back into the planner with the
  current plan and the drift context. The planner returns a revised plan.
  The frontend renders the diff as a banner with added / removed / reordered
  tasks.
- **Reporting tools.** The protocol agents use to tell harmonograf what
  they're doing. `report_task_started`, `report_task_progress`,
  `report_task_completed`, `report_task_failed`, `report_task_blocked`,
  `report_new_work_discovered`, `report_plan_divergence`. Every sub-agent
  gets these tools injected automatically.
- **session.state.** ADK's shared mutable dict. Harmonograf writes the
  current task (`harmonograf.current_task_id`, `...title`, `...description`)
  before every model call, and reads back progress, outcomes, notes, and the
  divergence flag. This is how agents and the orchestrator exchange context
  without passing it through prompts.
- **Control events.** Out-of-band instructions from the frontend to the
  agents: pause, resume, steer, status query. They ride down a separate gRPC
  stream and are acknowledged upstream on the telemetry stream.

That's it. Nine primitives. Everything else in harmonograf is plumbing
around these.

---

## 3. The three components (2 min)

Harmonograf is three processes that share one data model.

```
          +--------------------------+
          |     Frontend (React)     |
          |   Gantt + Graph + Diff   |
          +-------------+------------+
                        | gRPC-Web (:7532)
          +-------------v------------+
          |   harmonograf-server     |
          |  fan-in, store, control  |
          +-------------+------------+
                        | gRPC (:7531)
         +--------------+--------------+
         |              |              |
   +-----v-----+  +-----v-----+  +-----v-----+
   |  agent A  |  |  agent B  |  |  agent C  |
   |  (ADK +   |  |  (ADK +   |  |  (ADK +   |
   |   client) |  |   client) |  |   client) |
   +-----------+  +-----------+  +-----------+
```

**Client library** (`client/`). Embedded inside each agent process. Ships an
ADK plugin (`attach_adk`) that wires reporting tools, callbacks, session-state
keys, and the ingest transport into an existing ADK agent graph with one line
of code. Owns the task state machine, the drift taxonomy, the refine
pipeline, and the buffered transport that survives server restarts.

**Server** (`server/`). Terminates every client connection. Owns the
canonical timeline. Persists it to SQLite (or in-memory for tests). Fans out
live updates to any number of frontend subscribers. Routes control messages
from the frontend to the correct agent. It is the fan-in point: many clients,
one server, one UI.

**Frontend** (`frontend/`). A React/Vite app. Talks gRPC-Web to the server.
Renders the Gantt canvas, the agent topology graph, the plan-diff banner and
drawer, the inspector drawer, the transport bar. Live-subscribes to every
session update; no refresh needed.

All three components share one data model, defined in
`proto/harmonograf/v1/*.proto` and regenerated via `make proto`. See
[docs/protocol/](../protocol/index.md) for the wire reference.

---

## 4. Click to completed Gantt chart (5 min)

Let's follow a single prompt through the whole stack. Imagine you've opened
the ADK web UI at `http://127.0.0.1:8080` and typed:

> Build a slide deck about the Python programming language with five slides,
> including an example snippet.

Here is what happens, in order, at a level of detail you can see in the
harmonograf UI.

**Step 1 — the coordinator plans.** ADK routes your prompt to
`presentation_agent`, which is wrapped by `HarmonografAgent`. The
coordinator LLM is asked to produce a plan. It emits a structured plan with
tasks like `research_python`, `design_outline`, `build_slides`, `review`.
Harmonograf's client library sees the plan and calls `TaskRegistry.upsertPlan`
on the server, which stores it and pushes it out to any subscribed frontend.

**Step 2 — the frontend learns about the session.** The harmonograf UI's
session picker auto-selects the newest live session. The Gantt view renders
one row per agent that has connected so far (initially just
`coordinator_agent`). The task panel under the Gantt shows the plan as a
flat list. The plan-revision banner briefly flashes that a new plan arrived.

**Step 3 — the first task starts.** The coordinator picks
`research_python`, writes `harmonograf.current_task_id = "t1"` into
`session.state`, and transfers to `research_agent`. The `research_agent`
row appears on the Gantt. Its first action is to call
`report_task_started(task_id="t1")` — a no-op-looking tool call that
harmonograf's `before_tool_callback` intercepts and turns into a
`PENDING → RUNNING` transition on task `t1`. The task panel recolors; the
Gantt shows a new bar.

**Step 4 — tool calls stream in.** `research_agent` makes a few tool calls:
a web search, a summarize call, maybe a payload-producing call that returns
a big document. Each becomes a span emitted upstream on the telemetry
stream. If the payload is big enough, the client library uploads it as
content-addressed chunks on the same stream. The frontend draws each tool
call as a colored bar on the `research_agent` row, breathing on a 2s loop
while it's still running. See
[docs/user-guide/gantt-view.md](../user-guide/gantt-view.md) for the bar
vocabulary.

**Step 5 — the task completes, the next one starts.** `research_agent`
calls `report_task_completed(task_id="t1", summary="Python is a...")`.
Harmonograf's interception marks the task `COMPLETED` and writes the summary
into `harmonograf.completed_task_results` so downstream tasks see it as
context. Control returns to the coordinator, which picks the next task and
transfers to `web_developer_agent`. You see a new row appear on the Gantt,
and a bezier curve — a cross-agent edge — connecting the transfer span to
the new invocation.

**Step 6 — something drifts.** The `reviewer_agent` runs and finds an
issue with the generated HTML. It calls
`report_new_work_discovered(parent_task_id="t3", title="fix HTML bug",
assignee="debugger_agent")`. Harmonograf's interception fires a *refine* —
a deferential call back into the planner with the current plan and the
drift context (drift kind: `new_work_discovered`). The planner returns a
revised plan with a new task spliced in. `TaskRegistry.upsertPlan` diffs the
old and new plans and stores the result. The frontend renders the diff as a
banner: "plan revised: +1 task". You can open the plan-diff drawer to see
the added task highlighted in green. This is drift as a first-class event.
Full mechanism: [docs/protocol/task-state-machine.md](../protocol/task-state-machine.md).

**Step 7 — you intervene.** Say you notice `debugger_agent` is taking too
long. You press Space to pause all agents, or click a specific row to pause
just that agent. The frontend sends a `SendControl` RPC to the server, which
fans it out over `SubscribeControl` to the right agents. The agents
acknowledge upstream on the telemetry stream. The frontend renders the
acknowledgments. Then you unpause by pressing Space again. See
[docs/user-guide/control-actions.md](../user-guide/control-actions.md) for
every handle you have.

**Step 8 — completion.** The coordinator calls `report_task_completed` on
the root task. The session's status flips to `COMPLETED`. The Gantt shows a
session-completed badge at the top of the timeline. You can now scroll,
zoom, click any bar to open the inspector drawer and see arguments, return
values, errors, payloads. See
[docs/user-guide/drawer.md](../user-guide/drawer.md).

That's a full rollout. Nine primitives, three components, one canonical
timeline.

---

## 5. Where to go next (1 min)

You've got the tour. The next read depends on why you're here.

- **"I want to run this on my own agents."** → [docs/quickstart.md](../quickstart.md),
  then [docs/user-guide/](../user-guide/index.md), then
  [docs/reporting-tools.md](../reporting-tools.md) for how to instrument your
  agents.
- **"I want to modify harmonograf."** → [docs/dev-guide/](../dev-guide/index.md),
  starting at [setup.md](../dev-guide/setup.md) and
  [architecture.md](../dev-guide/architecture.md).
- **"I want to understand the protocol."** →
  [docs/protocol/](../protocol/index.md), starting at
  [overview.md](../protocol/overview.md).
- **"I want the primitives explained in depth."** →
  [mental-model.md](mental-model.md).
- **"I want to see how harmonograf terminology maps to ADK / OTel / other
  frameworks."** → [terminology-map.md](terminology-map.md).
- **"I want the whole site map."** → [docs/index.md](../index.md).

---

## Self-check — ten questions

By the end of the tour you should be able to answer all ten. If you can't,
re-read the relevant section.

1. Why can't you infer task state from span lifecycle?
2. What is the difference between a span and a task?
3. What is a plan, and how is it represented?
4. Name three drift kinds and one thing each triggers.
5. What does `report_task_completed` actually do, given that its body just
   returns `{"acknowledged": true}`?
6. What is written into `session.state` under the `harmonograf.` prefix,
   and who writes it?
7. What are the three components, and which one owns the canonical
   timeline?
8. Why do control acks ride upstream on the telemetry stream instead of on
   the control stream?
9. What is a refine, and what produces a plan diff?
10. What does the frontend do when you press Space on the Gantt view?

Answers are scattered across this document. If you want one-stop lookup,
the [mental-model](mental-model.md) page covers every concept in depth, and
[docs/protocol/](../protocol/index.md) covers the wire-level answers.
