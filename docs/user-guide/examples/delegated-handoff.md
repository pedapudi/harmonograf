# Scenario: delegated handoff between specialist agents with drift detection

A coordinator agent running in **delegated** mode (`OBS`) hands off to
a chain of specialist agents using ADK's `AgentTool`. One specialist
unexpectedly transfers to a fourth agent that is not in the plan, which
fires a `transfer_to_unplanned_agent` drift. Harmonograf only observes
— it does not drive the handoff.

This is the canonical example of how harmonograf acts as a monitor
rather than an orchestrator.

## Set-up

- Agents (all ADK):
  - `coordinator` — `HarmonografAgent`, `orchestrator_mode=False`.
    Mode chip `OBS`. Wraps the inner `presentation_agent`-style team.
  - `researcher`, `web_developer`, `reviewer`, `debugger` — sub-agents
    composed as `AgentTool` instances on the coordinator. See
    `tests/reference_agents/presentation_agent/agent.py`.
- The coordinator's LLM picks which specialist to call as a tool each
  turn. Harmonograf does not route — the `OBS` mode exists precisely
  because the inner agent is in charge.
- Initial plan: `research` → `develop` → `review`. Three tasks, three
  specialists. `debugger` is in the team but not in the plan.

## Timeline

### t=0 — session loads

Gantt shows five rows: coordinator, plus the four specialists.
Coordinator has an open INVOCATION; specialists are idle. Current task
strip reads `Currently: research · RUNNING · OBS · researcher`.

### t=5s — coordinator calls researcher (via AgentTool)

The coordinator's LLM emits a tool call for the `researcher` AgentTool.
In ADK this surfaces as a `TOOL_CALL` on the coordinator, which in turn
opens an INVOCATION on `researcher` and wires them with an `INVOKED`
link.

Harmonograf renders this as:

- A `TOOL_CALL` span on the coordinator's row with the AgentTool's name.
- A new INVOCATION on the researcher's row.
- On the **Gantt**: a bezier edge from the TOOL_CALL down to the
  researcher INVOCATION.
- On the **Graph view**: because no explicit TRANSFER span was
  emitted, harmonograf infers a **delegation** — a blue dashed 1.5 px
  arrow (contrast with orange solid for explicit transfers). See
  [graph-view.md → arrows](../graph-view.md#arrows--transfer-delegation-return).

### t=22s — researcher completes, returns

Researcher's INVOCATION closes. Grey dashed return arrow on the Graph
view. Coordinator's TOOL_CALL span completes with the researcher's
result as a payload.

### t=25s — coordinator calls web_developer

Second AgentTool call, same shape. Second blue dashed arrow on the
Graph view. Plan's `develop` task transitions to RUNNING and binds to
the web_developer's INVOCATION.

### t=40s — unexpected transfer

The web_developer's LLM decides mid-run to call `debugger` directly
(via a transfer instruction in its tool output). This was not in the
plan — the plan expected `develop` → `review`, not `develop` →
`debug` → `review`.

The harmonograf client's drift detection catches this via the
`unexpected_transfer` / `transfer_to_unplanned_agent` drift kinds (see
[tasks-and-plans.md → drift kinds](../tasks-and-plans.md#drift-kinds)).
The client fires a deferential `refine` back to the planner with the
current plan + drift context.

What you see in the UI:

- A new orange solid TRANSFER arrow on the Graph view, from the
  web_developer column to the debugger column. Amber category on the
  PlanRevisionBanner pill: `↪ Unplanned transfer`.
- The pill fires with detail text like `transfer_to_unplanned_agent:
  web_developer → debugger`.
- The Plan revisions section in the drawer gains a new row. Expanded,
  it shows: added task `debug` (assignee `debugger`), modified `review`
  dependency chain to sit downstream of `debug`, DAG edges changed
  marker `⇄`.

![TODO: screenshot of the Graph view with the unplanned transfer + pill visible](_screenshots/example-delegated-drift.png)

### t=45s — harmonograf accepts the drift

Because the agent is in `OBS` mode, harmonograf does not try to cancel
or redirect. It re-upserts the plan with the new task via
`TaskRegistry.upsertPlan`. The task panel grows a row for `debug`
(RUNNING). The Gantt task plan overlay (if on) grows a new ghost box.

### t=70s — debugger completes, returns up the chain

Debugger's INVOCATION closes. Return arrow to web_developer. A beat
later web_developer's INVOCATION closes. Return arrow to coordinator.

### t=75s — coordinator calls reviewer

Third AgentTool call. Blue dashed arrow. Reviewer runs to completion
against the original plan (with `debug` now done as an intermediate
step), then returns.

### t=95s — coordinator closes out

Coordinator's root INVOCATION closes. All tasks `COMPLETED` (including
the inserted `debug` task). One plan revision in history.

## What's specifically "delegated" here

- The mode chip is `OBS` — harmonograf only observes.
- **No forced `task_id`.** Unlike parallel mode, the walker is not in
  the picture; spans are stamped with `hgraf.task_id` only when the
  agent's reporting tools (`report_task_started`, etc.) mark a task
  RUNNING, or when an `after_model_callback` infers the binding from
  structured output.
- **Drift is still detected.** Callback inspection in
  `after_model_callback` + `on_event_callback` watches for transfer /
  escalate / state_delta events. The unplanned transfer was caught here.
  See `AGENTS.md` → "ADK callback inspection" belt-and-suspenders path.
- Delegation arrows are **inferred**, not explicit. They are blue
  dashed, not orange solid. See
  [graph-view.md → arrows](../graph-view.md#arrows--transfer-delegation-return).

## Log lines / attributes

On the web_developer's TRANSFER span:

```
hgraf.task_id         = "develop"
drift_kind            = "transfer_to_unplanned_agent"
drift_severity        = "warning"
drift_detail          = "web_developer -> debugger (not in plan)"
```

On the active INVOCATION of the web_developer, same three drift
attributes are mirrored — that is how the planner finds the drift even
when the TRANSFER span has already closed.

The subsequent plan revision carries:

```
revision_kind     = "transfer_to_unplanned_agent"
revision_severity = "warning"
revision_reason   = "transfer_to_unplanned_agent: web_developer -> debugger"
revision_index    = 1
```

## Patterns to notice

1. **`OBS` mode trusts the agent.** If you want harmonograf to step in,
   use `SEQ` or `PAR`. `OBS` is for running an existing ADK team
   with observability.
2. **Delegation arrows are blue dashed; transfer arrows are orange
   solid.** The difference matters — dashed means inferred.
3. **Return arrows may be missing for delegations.** Often the "return"
   can't be computed because the inner agent didn't emit a TRANSFER
   span to match on. The Graph renders the forward arrow without a
   return. See [graph-view.md → following message flow](../graph-view.md#following-message-flow).
4. **Drift detection still works without a walker.** Callback
   inspection is the belt-and-suspenders path; you get `tool_error`,
   `unexpected_transfer`, `context_pressure`, etc. even in `OBS` mode.
5. **The drawer's Links tab is the best way to walk a chain** in this
   scenario. `INVOKED` on the coordinator's TOOL_CALL points at the
   specialist's INVOCATION; `TRIGGERED_BY` points back. See
   [drawer.md → links tab](../drawer.md#links-tab).

## Related

- `tests/reference_agents/presentation_agent/agent.py` — a real `AgentTool`-composed team.
- [tasks-and-plans.md → orchestration modes](../tasks-and-plans.md#orchestration-modes)
- [glossary.md → delegated](../glossary.md#delegated), [AgentTool](../glossary.md#agenttool), [transfer](../glossary.md#transfer)
- [graph-view.md](../graph-view.md)
