# Runbook: Task stuck in PENDING

A task is rendered in the plan (Gantt, Graph, task panel, drawer) but
never transitions to `RUNNING`. This runbook covers the post-overlay
world (goldfive#141-144).

## Expected vs pathological

Goldfive's overlay model drives tasks observation-first: a task moves
to RUNNING only when an agent is observed working on it. Two states
are **expected** at invocation end:

- `COMPLETED` — the task ran and finished.
- `NOT_NEEDED` — no agent needed to do this task for the goal to be
  satisfied. Goldfive flips unassigned `PENDING` tasks to `NOT_NEEDED`
  when the outer invocation ends.

So a task that sits at `PENDING` throughout a finished run and then
flips to `NOT_NEEDED` at the end is **working as intended**. That is
not a bug.

The pathological case is:

- The agent is still actively running.
- A task sits at `PENDING` indefinitely.
- No drift fires, no refine lands.

That's what this runbook is about.

## Symptoms

- **UI**: a task's row in the Gantt gutter is greyed out for many
  minutes while other tasks have long since completed.
- **Current task strip**: shows a different running task or falls
  back to a completed one.
- **Server log**: no movement — `UpdatedTaskStatus` deltas for this
  task id never land.
- **Goldfive log**: no `TaskStarted` / `TaskProgress` for this task.

## Immediate checks

```bash
# What's the current plan's task roster + statuses?
sqlite3 data/harmonograf.db \
  "SELECT id, title, status, assignee_agent_id, bound_span_id
     FROM tasks
     WHERE plan_id=(SELECT id FROM task_plans WHERE session_id='<SID>'
                    ORDER BY revision_index DESC LIMIT 1);"

# Has the plan been revised recently?
sqlite3 data/harmonograf.db \
  "SELECT id, revision_index, revision_kind, revision_reason
     FROM task_plans
     WHERE session_id='<SID>'
     ORDER BY revision_index DESC LIMIT 5;"

# Per-agent rows in this session — is the assignee even there?
sqlite3 data/harmonograf.db \
  "SELECT id, name FROM agents WHERE session_id='<SID>';"
```

## Root-cause candidates (ranked)

1. **Assignee agent isn't in the session** — the task is assigned to
   a sub-agent that the ADK tree never instantiated. Goldfive can't
   drive a task on an agent that doesn't exist. The `agents` query
   above should include the assignee id (or its derived per-agent id
   shape `<client>:<name>`). If it doesn't, that's your cause.
2. **Reconciler hasn't observed the task yet** — goldfive's
   observation-driven overlay binds tasks to spans when an agent's
   activity matches the task's description. If no agent has done
   anything recognisable, the overlay leaves the task at PENDING.
   Common when the initial plan over-decomposed and the agent does
   one compound step that the overlay doesn't attribute to any
   specific task.
3. **Plan ID mismatch** — after a refine the task appears under a
   different plan_id, and the UI is reading a stale one. Check that
   the highest `revision_index` plan is the one rendered. The
   intervention aggregator expects monotone `revision_index`.
4. **Agent wedged** — the assignee exists but isn't making progress.
   Open
   [runbooks/task-stuck-in-running.md](task-stuck-in-running.md) for
   the other direction — a task *was* claimed, and now the agent
   that claimed it is hung.
5. **Goldfive steerer unreachable** — the agent's control subscription
   for this session never came up, so goldfive can't dispatch a
   STATUS_QUERY to see what's happening. Look for
   `subscribe control: connection closed` in the agent log. See
   [agent-disconnects-repeatedly.md](agent-disconnects-repeatedly.md).

## Diagnostic steps

### 1. Check the `agents` row shape

With per-agent Gantt rows (#80), agent ids look like
`<client.agent_id>:<adk_agent_name>`. If the task's `assignee_agent_id`
is the bare `client.agent_id` but the actual agent is registered
under a per-ADK-agent sub-id, the task won't bind.

Typical fix: refine the plan so `assignee_agent_id` matches one of the
per-agent rows the server has registered. If you author plans
programmatically, include the per-ADK-agent shape.

### 2. Wait for the invocation end

If the run is still active, goldfive will not flip the task to
`NOT_NEEDED` until the invocation ends. Give it a few minutes; if the
task flips to `NOT_NEEDED` when the agent finishes, it was never
needed and there's no bug.

### 3. Send a STATUS_QUERY to the assignee

From the Graph view, click `↻ Status` on the assignee's header. A
`STATUS_QUERY` control goes to the agent; its self-report lands in
`TaskReport`. A silent response (no reply) confirms the agent is
wedged and you're in task-stuck-in-running territory.

### 4. Inspect the latest plan

```bash
sqlite3 data/harmonograf.db \
  "SELECT revision_index, revision_kind, revision_reason, summary
     FROM task_plans
     WHERE session_id='<SID>'
     ORDER BY revision_index DESC LIMIT 3;"
```

If the latest plan's `revision_kind` is `REFINE_VALIDATION_FAILED`
the planner couldn't write a coherent plan — the task stays where
the last valid plan left it.

## Fixes

1. Make sure the task's `assignee_agent_id` matches an actual
   per-agent row the server registered.
2. If the assignee agent is present but the overlay doesn't bind the
   task, a STEER annotation with a clarification like "work on
   task X now" usually gets the reconciler's attention.
3. If the planner produced an invalid revision, check the goldfive
   log for the validation error and re-post a corrective STEER.
4. If `NOT_NEEDED` at invocation end: nothing to fix. That's the
   overlay working as intended.

## Cross-links

- [runbooks/task-stuck-in-running.md](task-stuck-in-running.md) — the
  symmetric case (task claimed but wedged).
- [user-guide/tasks-and-plans.md](../user-guide/tasks-and-plans.md) —
  the overlay-era task state machine.
- [user-guide/gantt-view.md](../user-guide/gantt-view.md) — per-agent
  Gantt rows and why assignee ids may look different from what you
  wrote in the plan.
