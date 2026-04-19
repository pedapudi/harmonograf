> **DEPRECATED (goldfive migration).** Reporting tools now live in
> [goldfive](https://github.com/pedapudi/goldfive) and are injected through
> `goldfive.reporting` + the ADK adapter there. Harmonograf no longer owns the
> reporting-tool surface or the `before_tool_callback` interception described
> below. See [goldfive-integration.md](goldfive-integration.md) for the current
> split of responsibilities and [goldfive-migration-plan.md](goldfive-migration-plan.md)
> for the design record.

# Reporting tools reference

Harmonograf injects a small set of **reporting tools** into every sub-agent wrapped by `HarmonografAgent`. These tools are how an agent explicitly communicates task state to the orchestrator instead of making harmonograf guess from span lifecycle or prose output.

The tool bodies themselves are trivial — each one returns `{"acknowledged": True}`. The real side effect happens in harmonograf's `before_tool_callback`, which intercepts the call, routes the arguments into `_AdkState` / `session.state`, and lets the tool return normally so the model sees a clean ack.

Source: `client/harmonograf_client/tools.py`. The session-state keys touched here are defined in `client/harmonograf_client/state_protocol.py` under the `harmonograf.` prefix.

## When to call what

Agents should call reporting tools at **task boundaries**, not from general chit-chat. The instruction appendix in `SUB_AGENT_INSTRUCTION_APPENDIX` already tells sub-agents the minimum contract:

- Call `report_task_started(task_id)` **before** beginning work on a planned task.
- Call `report_task_completed(task_id, summary=...)` **after** finishing it.
- Call `report_task_failed(task_id, reason=...)` if the task cannot be completed.
- Call `report_task_blocked(task_id, blocker=...)` if an external condition prevents progress.
- Call `report_new_work_discovered(parent_task_id=..., title=..., description=...)` if a new sub-task is needed.
- Call `report_plan_divergence(note=...)` if the overall plan no longer matches reality.

The current `task_id` is always available to the agent in `session.state["harmonograf.current_task_id"]`.

## Tool reference

| Tool | Signature | When | Harmonograf side effect |
|---|---|---|---|
| `report_task_started` | `(task_id: str, detail: str = "")` | Immediately before starting work on a planned task. | Marks the task RUNNING in `_AdkState`; updates `harmonograf.current_task_id` / `harmonograf.current_task_title` and records `detail` in `harmonograf.agent_note`. Opens the telemetry span's `task_started_at`. |
| `report_task_progress` | `(task_id: str, fraction: float = 0.0, detail: str = "")` | Optional mid-task ping for long-running tasks with meaningful sub-steps. | Writes `harmonograf.task_progress[task_id] = fraction`; stores `detail` in `harmonograf.agent_note`. Feeds the frontend liveness indicator and the "stuck" detector. No state transition. |
| `report_task_completed` | `(task_id: str, summary: str, artifacts: Optional[dict] = None)` | After producing the final output for a task. | Marks the task COMPLETED in `_AdkState`; stores `summary` in `harmonograf.task_outcome[task_id]` and in `harmonograf.completed_task_results` so downstream tasks see it as context; records `artifacts` on the task telemetry. In parallel mode, tells the DAG walker it may schedule dependents. |
| `report_task_failed` | `(task_id: str, reason: str, recoverable: bool = True)` | When the task cannot complete. | Marks the task FAILED in `_AdkState`; records `reason` in `harmonograf.task_outcome[task_id]`. If `recoverable=True`, fires a refine with drift kind `task_failed_recoverable` so the planner can reroute. If `recoverable=False`, fires a refine with drift kind `task_failed_fatal` which typically stops the workflow. |
| `report_task_blocked` | `(task_id: str, blocker: str, needed: str = "")` | When an external blocker (missing info, waiting on another agent, needs-human) prevents progress. | Keeps the task RUNNING but sets `harmonograf.agent_note` to `blocker`/`needed` so the UI and planner can see what would unblock. May fire a refine with drift kind `blocked` if the blocker is structural. |
| `report_new_work_discovered` | `(parent_task_id: str, title: str, description: str, assignee: str = "")` | When the agent discovers a task the plan didn't account for and that needs to exist for the parent task to complete. | Fires a refine with drift kind `new_work_discovered`, passing `parent_task_id`, `title`, `description`, `assignee` to the planner. The planner is expected to return a revised plan that adds the new task as a child of the parent. The revised plan flows back through `TaskRegistry.upsertPlan` and shows up in the frontend banner/drawer as a diff with the new task in the green `added` section. |
| `report_plan_divergence` | `(note: str, suggested_action: str = "")` | When the whole plan no longer matches what needs to happen (not just one task). | Sets `harmonograf.divergence_flag = True` and fires a refine with drift kind `plan_divergence`, passing `note` and `suggested_action` as hints. Produces a revised plan live in the UI. |

## How harmonograf intercepts a call

1. The agent emits a `TOOL_CALL` event for, say, `report_task_completed(task_id="t3", summary="…")`.
2. `HarmonografAdkPlugin.before_tool_callback` inspects the tool name against `REPORTING_TOOL_NAMES`.
3. If matched, it dispatches into `_AdkState` with the parsed arguments, applies the state transition, writes the relevant `harmonograf.*` keys into `session.state`, and may enqueue a refine call if the transition is a drift trigger.
4. The tool body runs normally and returns `{"acknowledged": True}` so the model gets a clean synchronous ack with no surprise payload.
5. Harmonograf still records the call as a telemetry span — it just does not *infer* task state from that span.

## Why this protocol exists

Earlier iterations (iter11 and before) inferred task state from span lifecycle and from prose markers like "Task complete:" in the LLM response. That worked for demos but broke under real agents:

- Sub-agents with long-running tool calls looked "done" the moment the outer LLM stopped, even when they were still working.
- Prose parsing couldn't distinguish "I will complete the task" from "task complete".
- Concurrent sub-agents racing through a parallel plan produced ordering bugs because state transitions lived in two places at once (the span closer and the event scanner).

The reporting tools make the state machine **explicit and monotonic**: one call, one transition, one source of truth in `_AdkState`. The callback-driven interception means the agent doesn't need to know harmonograf exists — it just calls tools like any other tool, and harmonograf applies the state transitions behind the scenes.

## Agents that ignore the reporting tools

Models that describe their work in prose instead of calling the tools still work, degraded:

- `after_model_callback` scans response content for structured signals (function_calls, explicit markers, embedded state_delta writes).
- `on_event_callback` watches for transfer / escalate / state_delta events.

This path is belt-and-suspenders — the reporting tools are still the canonical protocol. If you're writing a new sub-agent, prefer explicit reporting tool calls.
