# Tasks and plans

Harmonograf tracks plan execution through three coordinated channels —
session state, reporting tools, and ADK callback inspection. The UI
surfaces the resulting task registry in a few places; this page maps those
surfaces to what they mean and how to read them.

If you want the protocol-level view of how plan execution is tracked, read
`AGENTS.md` in the repo root. This page is about the frontend.

## What's a task? What's a plan?

A **task** is a single unit of planned work: title, description, assignee,
status, optional `predictedStartMs` / `predictedDurationMs`, optional
`boundSpanId`. Tasks have `PENDING` / `RUNNING` / `COMPLETED` / `FAILED` /
`CANCELLED` status.

A **plan** is an ordered (and possibly DAG-shaped) collection of tasks
plus `edges` expressing task dependencies. A plan also carries a
`revisionReason` — the drift kind + detail that caused the most recent
revision. Plans can be revised multiple times during a session; the
frontend keeps the full revision history per plan.

A session can have multiple plans — typically one per orchestrator agent,
but nothing stops a session from carrying several parallel plans. The
[task panel](gantt-view.md#task-panel) and the task-plan overlay in the
[Graph view](graph-view.md#task-plan-overlay) list all plans in the session.

## CurrentTaskStrip

The slim strip directly below the app bar is the **CurrentTaskStrip**.
It always reflects the live "current task" for the session:

![TODO: screenshot of the current task strip showing "Currently: Write summary · RUNNING · PAR · writer-agent ·💭· search"](_screenshots/current-task-strip.png)

The strip decides what to show by calling `store.getCurrentTask()`, which
returns the RUNNING task when one exists and otherwise the most recently
completed task, so the strip never goes blank mid-session.

Left to right:

| Element | Meaning |
|---|---|
| `Currently:` | Static label. |
| Title | `task.title` (falls back to `task.id`). Tooltip shows the description. |
| **Status chip** | PENDING / RUNNING / COMPLETED / FAILED / CANCELLED. The strip turns slightly "hotter" (border color + `data-running="true"`) while RUNNING. |
| **Mode chip** | `SEQ`, `PAR`, or `OBS` — the [orchestration mode](#orchestration-modes) of the assignee agent. Hover for a tooltip that explains the mode. |
| Assignee | `task.assigneeAgentId` in monospace. |
| **Thinking dot** | A tiny pulsing blue dot next to the assignee when the assignee agent currently has a span with `has_thinking=true`. |
| **Tool badge** | The name of the in-flight tool call, if the current task's span is inside a TOOL_CALL right now. |

All of this is driven by live subscriptions to `store.tasks`, `store.spans`,
and `store.agents`. Any signal can disappear or change without a page
reload. If the current task ends and no new RUNNING task is promoted, the
strip sticks to the completed task so you still have context.

### Orchestration modes

The mode chip corresponds to one of three `HarmonografAgent` modes:

| Chip | Mode | Tooltip |
|---|---|---|
| `SEQ` | **Sequential** | Single-pass coordinator LLM executes the full plan; lifecycle reported via reporting tools. |
| `PAR` | **Parallel** | Rigid DAG batch walker drives sub-agents directly, respecting plan edge dependencies. |
| `OBS` | **Delegated / Observer** | Inner agent owns its sequencing; harmonograf only watches for drift. |

The strip reads the mode from the assignee agent's metadata. Agents
without metadata don't render a chip.

When is each mode in use? All three are available to `HarmonografAgent`
(see `AGENTS.md`). Which one actually runs is determined by how the agent
was constructed, not by anything the UI controls.

## PlanRevisionBanner

Whenever a plan's `revisionReason` changes, a pill appears in the
**PlanRevisionBanner** row directly below the current-task strip. Each
pill announces one revision and auto-dismisses after 4 seconds. Up to
three pills stack FIFO when revisions arrive in a burst.

![TODO: screenshot of the banner with two pills: one "Tool error" (red) and one "Merged tasks" (blue)](_screenshots/plan-revision-banner.png)

A pill contains:

- **Drift icon + color** in a left border. See [drift kinds](#drift-kinds)
  below.
- **Label** (e.g. "Tool error", "New work", "Reordered").
- **Detail** — the part of the `revisionReason` after the `kind:` prefix,
  or the label if there's no detail.
- **Diff counts** `+N -M ~K` when a `PlanDiff` is attached to this
  revision (computed by `TaskRegistry.upsertPlan` when it recognizes a
  change).

The banner is transient. To see the full revision history, open the
[drawer → Task tab](drawer.md#plan-revisions-section).

## Drift kinds

Every revision reason is tagged with a **drift kind** — the reason the
planner decided to revise. The full table (`frontend/src/gantt/driftKinds.ts`):

| Kind | Icon | Label | Category |
|---|---|---|---|
| `tool_error` | ⚠ | Tool error | error |
| `tool_returned_error` | 🔻 | Bad result | error |
| `tool_unexpected_result` | ❓ | Odd result | error |
| `task_failed` | ✗ | Task failed | error |
| `task_blocked` | ⛔ | Blocked | error |
| `task_empty_result` | ○ | Empty result | error |
| `new_work_discovered` / `task_result_new_work` | ✨ | New work | discovery |
| `task_result_contradicts_plan` | ⟷ | Contradicts plan | divergence |
| `plan_divergence` | ⟷ | Divergence | divergence |
| `agent_reported_divergence` | ⟷ | Agent flagged divergence | divergence |
| `llm_refused` | 🚫 | Refused | error |
| `llm_merged_tasks` | ⊕ | Merged tasks | structural |
| `llm_split_task` | ⊗ | Split task | structural |
| `llm_reordered_work` | ⇄ | Reordered | structural |
| `context_pressure` | ⚡ | Context limit | structural |
| `user_steer` | 👆 | User steered | user |
| `user_cancel` | ⏹ | User cancelled | user |
| `unexpected_transfer` | ↪ | Unexpected transfer | divergence |
| `agent_escalated` | ⚠ | Escalated | divergence |
| `multiple_stamp_mismatches` | ≠ | Plan drift | divergence |
| `tool_call_wrong_agent` | ↪ | Wrong agent | structural |
| `transfer_to_unplanned_agent` | ↪ | Unplanned transfer | divergence |
| `failed_span` | ✗ | Failed span | error |
| `task_completion_out_of_order` | ≠ | Out of order | structural |
| `external_signal` | ⟶ | External | user |
| `coordinator_early_stop` | ⏸ | Early stop | divergence |

**Category** groups the kinds by color:

- `error` (red) — something went wrong mechanically.
- `divergence` (amber) — the plan and reality don't agree.
- `discovery` (green) — the agent found work it didn't expect.
- `user` (blue/grey) — you (or something external) steered the run.
- `structural` (blue / grey) — the plan shape changed for reasons unrelated
  to correctness.

Unknown kinds (legacy revisions, new kinds added to the planner that
haven't landed on the frontend yet) fall back to a `Plan revised` grey
pill. You can still read the raw reason in the pill body.

## Task panel (bottom of Gantt)

Below the Gantt's transport bar there's a resizable **task panel**
(`gantt-task-panel`). It's a live list of every task across every plan in
the session:

- **Collapsible** — click the resize handle or collapse button. Collapsed
  it's 28px tall with just a header; expanded, the height is persisted to
  `localStorage` under `harmonograf.taskPanelHeight`.
- Columns: title, status, assignee name, predicted start/duration.
- Click a task to select it; if the task is bound to a span, the
  [drawer](drawer.md) opens on that span too.
- The task panel **mirrors** the chips the Gantt renderer paints inside
  the plot. If you see a task here but not on the Gantt, the task's
  assignee may be hidden — unhide from the gutter (see
  [gantt-view.md](gantt-view.md#focused-and-hidden-agents)).

## Task-plan overlay on the Graph view

See [Graph view → Task plan overlay](graph-view.md#task-plan-overlay). The
overlay comes in three modes (`pre-strip`, `ghost`, `hybrid`) persisted to
`localStorage`, with hover cards and clickable task chips that hop into
the drawer when a span binding exists.

## Plan revisions — live replans

The planner can revise plans mid-run. Every revision produces:

1. A new plan snapshot in the TaskRegistry.
2. A `PlanDiff` describing the delta (added, removed, modified tasks;
   whether the DAG edges changed).
3. A **PlanRevisionBanner** pill (auto-dismissing).
4. A new entry in the **Plan revisions** section of the
   [drawer's Task tab](drawer.md#plan-revisions-section).

The revision flow is triggered by drift detection in the client library
(`refine` calls back into the planner with the current plan + drift
context). The frontend doesn't drive replans — it only visualizes them.

When you see a pill, the corresponding plan history entry is already in
the drawer; open the drawer on any span in that plan and the latest
revision expands by default.

## Related pages

- [Drawer: Task tab](drawer.md#task-tab) — full plan revision history and orchestration events timeline for one task.
- [Graph view: Task plan overlay](graph-view.md#task-plan-overlay) — chips and ghosts on the sequence diagram.
- [Control actions: STATUS_QUERY](control-actions.md#status-query) — asking an agent what it's working on.
- `AGENTS.md` — protocol-level detail on how tasks are tracked (state keys, reporting tools, callback inspection).
