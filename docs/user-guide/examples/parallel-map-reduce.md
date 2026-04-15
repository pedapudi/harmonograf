# Scenario: parallel map-reduce over a task plan

An orchestrator agent in **parallel** mode drives eight map tasks across
two worker sub-agents, then a single reduce task. The walker pins each
map task to a specific sub-agent via `forced_task_id` and fans them out
respecting the DAG edges.

## Set-up

- Agents:
  - `orchestrator` — `HarmonografAgent`, `orchestrator_mode=True,
    parallel_mode=True`. Mode chip reads `PAR`.
  - `worker-a`, `worker-b` — ADK sub-agents, no harmonograf wrapper of
    their own. They are driven by the walker directly via the forced
    `task_id` ContextVar.
- Plan:
  - Tasks `m1…m8` — map, each assignee either `worker-a` or `worker-b`.
  - Task `r1` — reduce, assignee `orchestrator`, with edges from every
    `m*` → `r1`.
- See `AGENTS.md` for the walker's invariants (monotonic, forced-task
  stamping, terminal-task refusal).

## Timeline

### t=0 — session picked, walker not yet started

Gantt shows three rows: `orchestrator` (active), `worker-a` (idle),
`worker-b` (idle). The orchestrator has opened its root INVOCATION and
emitted the initial plan. Current task strip reads `Currently: m1 ·
PENDING · PAR · worker-a` (because `getCurrentTask` returns the first
RUNNING task, or the first PENDING if none are running yet). Task
panel lists nine rows, one per task.

### t=1s — walker starts the first batch

The walker picks every task whose upstream dependencies are satisfied
(all `m*` have none) and schedules them. In the default configuration
the walker runs one task per worker at a time — so `m1` starts on
`worker-a` and `m2` starts on `worker-b`.

For each scheduled task the walker:

1. Sets the `_forced_task_id_var` ContextVar to the task id.
2. Invokes the sub-agent.
3. The sub-agent's first span opens; `_stamp_attrs_with_task` reads the
   ContextVar and stamps `hgraf.task_id = "m1"` (or `m2`) on the span.
4. Every subsequent span under that invocation inherits the stamp.

On the Gantt:

- Two new INVOCATION bars open simultaneously, one on `worker-a`, one
  on `worker-b`. Both breathing.
- Task panel: `m1` and `m2` flip to RUNNING, rest still PENDING.
- The current task strip re-renders as one of the RUNNING tasks
  (whichever `getCurrentTask` resolves first).

![TODO: screenshot of the Gantt with two parallel worker invocations mid-run](_screenshots/example-parallel-fanout.png)

### t=~8s — first batch completes, walker steps forward

When `m1` completes the walker transitions its status to `COMPLETED`
and picks the next task assigned to `worker-a`. Because the walker's
state machine is **monotonic** and refuses to rebind terminal tasks
(see `AGENTS.md` and `client/tests/test_agent.py :: test_set_forced_task_id_refuses_terminal`),
you will never see a task's status go backwards on the task panel.

The Gantt shows:

- `worker-a`'s INVOCATION closed for `m1`, a new INVOCATION opened for
  `m3`.
- `worker-b` still on its original invocation for `m2`, or on `m4` if
  it finished faster.

### t=~40s — all maps complete

Task panel: `m1…m8` all `COMPLETED`. Walker promotes `r1` (reduce)
because its edge dependencies are now satisfied.

### t=~40s — reduce runs on the orchestrator

The reduce task runs on the orchestrator itself, not on a worker. A new
INVOCATION opens on `orchestrator`'s row. Mode chip stays `PAR` — mode
is a property of the orchestrator, not the task.

### t=~55s — run completes

All tasks `COMPLETED`. Session transitions to `COMPLETED` if the
orchestrator disconnects; otherwise stays `LIVE`.

## What the Gantt looks like

Eight fan-out INVOCATIONs across two worker rows, offset in time as the
walker steps through. No cross-agent edges (map-reduce is data-parallel,
not message-passing), but one **implicit** edge from every `m*`
terminal to the `r1` start — harmonograf does not draw this, because
the dependency is expressed on the **task DAG** in the task plan
overlay, not on spans.

If you toggle the [Graph view's task plan overlay](../graph-view.md#task-plan-overlay)
to **hybrid** mode, the dashed grey bezier curves between task chips
render the DAG edges visually. That is where you see the fan-in to
`r1`.

## What the drawer shows

Pick any span on `worker-a`'s row. In the drawer's Summary tab
attributes, look for `hgraf.task_id` — the walker stamps every child
span with it. Same span's Task tab shows the Plan revisions section
for the orchestrator's plan (there may be zero revisions for a clean
parallel run). See [drawer.md → task tab](../drawer.md#task-tab).

## Log lines / attributes

On every span opened inside a forced sub-agent run:

```
hgraf.task_id = "m1"   // or m2…m8, or r1 for the reduce
```

On the walker itself (server-side debug logs):

```
walker scheduled task m1 on worker-a
walker scheduled task m2 on worker-b
walker task m1 COMPLETED
walker scheduled task m3 on worker-a
...
walker all tasks done, exiting
```

The walker exits cleanly when every task reaches a terminal status —
see `client/tests/test_agent.py :: test_walker_exits_after_all_tasks_done`.

## Patterns to notice

1. **Parallelism is visible on the Gantt immediately.** Two rows with
   simultaneously-breathing bars is the shape of a parallel fan-out.
2. **`hgraf.task_id` is how the renderer knows which task each span
   belongs to.** Open the Summary tab on any worker span to verify.
3. **Task panel is the ground truth for "what's done".** The Gantt
   shows spans, which is a per-invocation view; the task panel shows
   tasks, which is the unit the walker cares about.
4. **The reduce task runs on the orchestrator in this example**, but
   it doesn't have to. Parallel mode just means the walker drives
   according to the DAG — any task's assignee can be any agent.
5. **Mode chip never changes mid-run.** Orchestration mode is fixed at
   agent construction time, not toggleable from the UI. See
   [tasks-and-plans.md → orchestration modes](../tasks-and-plans.md#orchestration-modes).

## Related

- `AGENTS.md` — parallel mode invariants and walker contract.
- [glossary.md → walker](../glossary.md#walker) and [forced_task_id](../glossary.md#forced_task_id).
- [tasks-and-plans.md → orchestration modes](../tasks-and-plans.md#orchestration-modes)
- [graph-view.md → task plan overlay](../graph-view.md#task-plan-overlay)
- `client/tests/test_agent.py` — the walker's regression suite.
