---
name: hgraf-safely-modify-adk-py
description: Survival guide for editing client/harmonograf_client/adk.py — the 5700-line ADK adapter. Covers _AdkState invariants, state transition discipline, the Aclosing wrapper, ContextVars, and happens-before constraints.
---

# hgraf-safely-modify-adk-py

## When to use

You are about to touch `client/harmonograf_client/adk.py`. This file is ~5900 lines, coordinates 20+ ADK callbacks, owns the in-memory task state for every session, and is the single point where drift detection, plan refinement, task classification, and telemetry emission come together. Mistakes here are the most expensive class of bug in the whole project because they break state monotonicity without obvious symptoms at the edit site.

Read this skill *before* you open the file. If it's too long, the minimum version is: **every status change goes through `_set_task_status`, every task_id routing goes through ContextVars, never directly mutate `_AdkState` fields from outside the class, and never call `planner.refine()` directly.**

## Prerequisites

1. Read `AGENTS.md` → *Plan execution protocol* section.
2. Read [`docs/design/12-client-library-and-adk.md`](../../../docs/design/12-client-library-and-adk.md) end-to-end.
3. Read `client/harmonograf_client/invariants.py` (short, ~100 lines) — that is the validator that will catch you if you break monotonicity.
4. Have the file open at these anchor points so you can jump between them:

| Line range | Symbol | What it does |
|---|---|---|
| 242-280 | `_set_task_status(task, new_status)` | Monotonic state machine guard. All transitions go through here. |
| 294-313 | `class PlanState` | Session-keyed plan storage holding tasks, edges, available_agents. |
| 320-322 | `_forced_task_id_var: ContextVar` | Task-local forced task id for parallel walker. |
| 326-378 | `DriftReason` + `DRIFT_KIND_*` + throttle constants | The drift taxonomy. |
| 1227-1276 | `after_model_callback` | LLM response parsing + agency drift detection. |
| 1299-1316 | `before_tool_callback` | Reporting-tool interception. |
| 1391-1409 | `on_event_callback` | Session delta router + harmonograf-agent inner substitution. |
| 1591-1705 | `class _AdkState` | In-flight span tracker, session→plan map, span→task map, lock, pending mutations. |
| 1656-1659 | `_current_root_hsession_var` | Per-instance ContextVar for root session id. |
| 1661-1663 | `_route_tokens` | invocation_id → ContextVar token (for LIFO reset). |
| 1863-1920 | `maybe_run_planner` | Planner entrypoint. |
| 2608-2720 | `classify_and_sweep_running_tasks` | Periodic classifier sweeper. |
| 2917-3050 | `detect_drift` | Span/event-driven drift detection. |
| 3256-3350 | `refine_plan_on_drift` | Refine entry — the only place planner.refine() is called. |

## The invariants that matter

These are not enforced by the compiler. Break them and you get silent state corruption that surfaces as "stuck task" or "wrong lane" hours later.

### I1. Monotonic status transitions

Status flows are: `PENDING → RUNNING → (COMPLETED | FAILED | CANCELLED)`. Once terminal, no further transitions. `_set_task_status` at lines 242-280 enforces this. Everywhere that changes a task's status **must** go through it. Never write `task.status = X` from outside `_AdkState`.

The allowed-transition table lives in `invariants.py:41-55` (`_ALLOWED_TRANSITIONS`); the runtime validator at `invariants.py:78-100` checks it after walker turns. A violation logs a warning but does not roll back — by then you've already polluted the state.

### I2. Lock discipline

`_AdkState._lock` is an `asyncio.Lock`. All reads and writes to `_active_plan_by_session`, `_span_to_task`, `_invocations`, `_llm_by_invocation`, `_tools`, `_long_running`, `_pending_mutations` must happen under the lock. The class methods acquire it; external callers should go through class methods, not poke fields directly.

Under `parallel_mode=True`, the walker spawns concurrent sub-invocations that race on these fields. Without the lock you get lost updates.

### I3. Happens-before on `pending_mutations`

`_pending_mutations` is a deferred-write queue: mutations prepared under one callback apply during the next one. This lets the state machine be consistent at callback boundaries without holding the lock across an await. Do not add an immediate-apply path that bypasses the queue — you will race with whatever is draining it.

### I4. ContextVar inheritance

`_forced_task_id_var` (line 320) and `_current_root_hsession_var` (line 1656) are the two ContextVars that make routing work. `asyncio.create_task()` descendants inherit them automatically. **Threads do not.** `run_in_executor()` does not. If you spawn work off the event loop, wrap it in `contextvars.copy_context().run(fn)` and set the vars explicitly.

The `_route_tokens` dict at line 1661 stores the LIFO reset token for each invocation. Reset tokens **must** be popped in reverse order of set — otherwise ContextVar raises. The existing code handles this via a try/finally around the invocation lifetime; do not break that structure.

### I5. Aclosing on async generators

ADK's event stream is an async generator. The Aclosing wrapper (grep for `aclosing` or `AsyncClosing` in `adk.py`) ensures that if the consumer aborts mid-iteration (exception, cancel), the generator's `aclose()` runs and cleans up ContextVar tokens + in-flight span state. If you rewrap the generator without aclosing, a cancel will leak route tokens and corrupt `_route_tokens`.

### I6. Single-writer to `planner.refine`

`refine_plan_on_drift` at lines 3256-3350 is the **only** function that calls `planner.refine()`. All drift paths (detect_drift, after_model_callback, before_tool_callback, sweeper) funnel through it. Do not call the planner directly from a new detection site — you will miss:
- Throttling (`_DRIFT_REFINE_THROTTLE_SECONDS = 2.0`, line 378)
- Severity thresholds (`_STAMP_MISMATCH_THRESHOLD = 3`, line 373)
- Unrecoverable cascade (cancels child tasks on non-recoverable drifts)
- Invariant check around the refine result

### I7. The sweeper is the fallback, not the primary

`classify_and_sweep_running_tasks` at lines 2608-2720 exists for the "model described its work in prose instead of calling a reporting tool" case. It is best-effort and should not be your first-choice way to transition a task. If you find yourself extending the sweeper to handle a new signal, first ask whether a reporting tool call would be cleaner (see `hgraf-add-reporting-tool`).

## Where to add state

If you need new state:

1. **Per-task state** → add a field to the Task or TaskPlan proto (see `hgraf-add-proto-field`).
2. **Per-session ephemeral state** → add a field to `_AdkState` initialized in `__init__`, mutated only through a new method on the class, guarded by `_lock`.
3. **Per-invocation routing state** → add a new ContextVar. Document its inheritance rules at the declaration site.
4. **Per-tool call state** → usually lives in the `_tools` dict on `_AdkState`, keyed by tool call id.

Do **not** add module-level mutable state. `adk.py` is imported once but run against multiple `_AdkState` instances (one per client), and module-level state is shared across all of them.

## Where to add callback logic

Know your callback surface:

- `before_agent_callback` / `after_agent_callback` — coarse agent-level hooks. Fire once per agent invocation.
- `before_model_callback` / `after_model_callback` — around every LLM call. Best place to read/write session.state for protocol keys.
- `before_tool_callback` / `after_tool_callback` — around every tool call. Reporting tool interception lives here (before).
- `on_event_callback` — every ADK event including session deltas and transfers. Observer for drift detection.

Pick the least-specific callback that has the information you need. Work done in `on_event_callback` runs on every event including chat turns — heavy work there is expensive.

## Workflow for editing adk.py

1. **Read the target function + 50 lines of context.** State flows through parameter threading and ContextVar reads — the 20 lines around the site are rarely enough to understand cause and effect.
2. **Grep for every caller of the function** before you change its signature or semantics. `adk.py` is not modular; one function can have 15 callers across the file.
3. **Add your change behind the lock** if it touches `_AdkState` fields, through `_set_task_status` if it changes status.
4. **Add or extend a unit test in `client/tests/test_*.py`** that exercises your new code path. Look for the closest existing file:
   - Drift: `test_drift_taxonomy.py`, `test_llm_agency_scenarios.py`
   - Callbacks: `test_model_callbacks.py`, `test_tool_callbacks.py`, `test_protocol_callbacks.py`
   - Reporting tools: `test_reporting_tools.py`, `test_reporting_registration.py`
   - Walker: `test_walker_completion_timing.py`, `test_walker_simplification.py`
   - State: `test_invariants.py`, `test_task_lifecycle.py`, `test_state_protocol.py`
5. **Run the full client test suite.** adk.py changes have non-local effects; only the full suite catches regressions.
   ```bash
   cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q
   ```
6. **Run the e2e suite.** Real ADK + real server smoke catches ContextVar bugs that unit tests miss.
   ```bash
   cd /home/sunil/git/harmonograf && uv run --extra e2e pytest tests/e2e -q
   ```
7. **Smoke with `make demo`.** Some bugs only surface with real streaming (e.g. `_route_tokens` leaks on cancel).

## Verification

```bash
cd client && uv run --with pytest --with pytest-asyncio python -m pytest \
  tests/test_invariants.py \
  tests/test_task_lifecycle.py \
  tests/test_walker_completion_timing.py \
  tests/test_walker_simplification.py \
  tests/test_drift_taxonomy.py \
  tests/test_llm_agency_scenarios.py \
  tests/test_reporting_tools.py \
  tests/test_model_callbacks.py \
  tests/test_tool_callbacks.py \
  tests/test_protocol_callbacks.py \
  tests/test_orchestration_modes.py \
  -q

# And the full suite
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q

# And the e2e suite
cd /home/sunil/git/harmonograf && make e2e
```

## Common pitfalls (in order of frequency)

1. **Direct status mutation.** Writing `task.status = TaskStatus.COMPLETED` somewhere new. It slips past `_set_task_status`, invariant checker flags it on next sweep, you spend an hour blaming the sweeper. Always go through `_set_task_status`.
2. **Calling `planner.refine()` from a new detection site.** Skips throttling + severity + cascade. The symptom is "refines fire too often" or "non-recoverable drift didn't cascel children." Always funnel through `refine_plan_on_drift`.
3. **ContextVar not inherited by threadpool.** Symptom: parallel mode binds spans to the wrong task. Fix: wrap `run_in_executor` with `contextvars.copy_context()`.
4. **Token reset out of order.** Symptom: `LookupError` or ContextVar state inconsistencies under heavy concurrency. Fix: use try/finally with LIFO discipline on `_route_tokens`.
5. **Holding `_lock` across an await.** Symptom: deadlock or serialized parallelism that defeats parallel_mode. Prefer `_pending_mutations` for deferred work.
6. **Adding logic in `on_event_callback` that should be in a more specific callback.** Symptom: the logic runs on every chat turn and tanks latency. Move to `before_tool_callback` / `after_model_callback`.
7. **Bypassing `_AdkState` methods.** Symptom: races, lost updates, or invariant violations. Always go through class methods.
8. **Forgetting the sweeper runs on a timer.** Symptom: "my test passes but the live system flips the task status seconds later." The sweeper will reclassify if your state lacks the markers it expects.
9. **Monkey-patching `_AdkState` in tests.** Symptom: tests pass, prod breaks because the monkey patch hid a real bug. Use real `_AdkState` instances in unit tests via the existing fixtures in `client/tests/_fixtures.py`.
10. **Changing the meaning of an existing `DRIFT_KIND_*` constant.** Symptom: replayed traces or frontend caches render the wrong icon. Never reassign; always add new constants.
