---
name: hgraf-debug-task-stuck
description: Diagnostic playbook when a task appears stuck — invariants, sweep log, reporting-tool interception, drift throttling, walker budget exhaustion, heartbeat timeouts.
---

# hgraf-debug-task-stuck

## When to use

- A task in the Gantt is wearing its "stuck" stripe (the server marked the agent STUCK after `STUCK_THRESHOLD_BEATS` missed heartbeats; see `ingest.py:62-65`).
- A task stays RUNNING long past its predicted duration and never transitions.
- A task is PENDING and never starts even though its dependencies are COMPLETED.
- The frontend shows a drift banner but the planner never issues a refine.
- Parallel mode "appears to hang" — the walker runs out of budget and stops without reporting.

## Prerequisites

- A reproducing session (live or persisted trace).
- Access to server stdout (or sqlite file for offline analysis).
- Know the session id and task id you are debugging (from the UI drawer, the `sessions` table, or the `tasks` table in `data/harmonograf.sqlite`).

## Diagnostic playbook

Work the list top-down. Each step has a concrete command or log pattern to look for.

### 1. Check invariant violations

The invariant checker (`client/harmonograf_client/invariants.py:78-100`) runs as a read-only validator after walker turns. It logs `InvariantViolation` records at WARN or ERROR level depending on severity. Grep the server log:

```
grep -i "invariantviolation\|invariant.*violat" <server.log>
```

If you see a violation like `task X in RUNNING with no active span` or `task completed out of dependency order`, you have a state-machine bug — not a stuck task. Jump to `hgraf-safely-modify-adk-py` for root-cause analysis.

The allowed transitions are enumerated at `invariants.py:41-55` (`_VALID_STATUSES`, `_TERMINAL_STATUSES`, `_ALLOWED_TRANSITIONS`). Any transition not in that dict is a violation by definition.

### 2. Check the sweep log

`classify_and_sweep_running_tasks()` at `client/harmonograf_client/adk.py:2608-2720` is the periodic sweeper: it classifies every RUNNING task against recent events and applies COMPLETED/FAILED/partial transitions. If a task is stuck, either:
- The sweeper is not running (bug — check the timer loop).
- The sweeper is running but classifying the task as "still in progress" (normal — wait longer, or check whether the classifier criteria are met).
- The sweeper is running but `_set_task_status` is refusing the transition (monotonic guard — `adk.py:242-280`).

Enable DEBUG logging and grep:
```
HARMONOGRAF_LOG_LEVEL=DEBUG make server-run 2>&1 | grep -i "sweep\|classify_and_sweep"
```

### 3. Check reporting-tool interception

If the agent is calling `report_task_completed` but the status never changes, the `before_tool_callback` dispatch may be failing silently. Add a breakpoint or log at `client/harmonograf_client/adk.py:1299-1316` (`before_tool_callback`). Verify:
- The dispatch function receives your tool name.
- Argument parsing does not raise (reporting tool dispatchers catch exceptions — look for `try/except` wrapping).
- `_set_task_status` returns truthy (the transition was allowed).

Run:
```bash
cd client && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_reporting_tools.py tests/test_tool_callbacks.py -q
```

A failing test here points at the interception layer; a passing suite exonerates it.

### 4. Check drift throttling

`_DRIFT_REFINE_THROTTLE_SECONDS = 2.0` at `adk.py:378` coalesces refines. If your drift fires at 10Hz, only the first refine in each 2s window runs. If the first refine was a no-op and subsequent ones would have fixed things, you will see "drift detected but task not moving."

Grep for `refine throttled` or the refine call site at `adk.py:3256-3350`. Look at `_STAMP_MISMATCH_THRESHOLD = 3` at `adk.py:373` — some drifts need to fire 3 times before they even become `DriftReason` objects. If the condition flickers (fires twice then stops), no refine ever runs.

### 5. Check walker budget exhaustion (parallel mode only)

In `parallel_mode=True`, the rigid DAG batch walker at `agent.py:1061-1071` drives sub-agents directly. It has a per-turn budget; if the budget is exhausted, the walker stops and waits for the next invocation. Symptoms:
- A task is PENDING with all dependencies COMPLETED.
- The walker log shows "budget exhausted" or "batch size 0".
- `report_task_started` for that task never fires.

Fix: bump the budget in the walker's config, or split the plan into smaller batches. Be cautious — raising the budget unbounded causes token cost explosions.

### 6. Check heartbeat timeouts (client-side hang)

`ingest.py:62-65` defines `HEARTBEAT_TIMEOUT_S = 15.0` and `STUCK_THRESHOLD_BEATS = 3`. A client that stops sending heartbeats (because it is blocked in a synchronous tool call, a long network I/O, or a debugger breakpoint) will have its agent marked STUCK after ~45s. The task stays RUNNING because the client never reported a transition.

Confirm by querying the sqlite store:
```bash
sqlite3 data/harmonograf.sqlite \
  "SELECT id, name, status, last_heartbeat FROM agents WHERE session_id = '<sid>';"
```

If `last_heartbeat` is stale vs. wall clock, the client is blocked. Attach a debugger to the agent process and inspect its stack — look for synchronous `requests`, `subprocess.run`, or `time.sleep` in a tool body. The fix is usually to make the tool async or to move the blocking call off the event loop.

### 7. Check ContextVar inheritance (parallel / sub-agent cases)

`adk.py` uses two ContextVars: `_forced_task_id_var` (lines 320-322) and `_current_root_hsession_var` (lines 1656-1659 on `_AdkState`). ContextVars are automatically inherited by `asyncio.create_task()` descendants but **not** by threads or by tasks scheduled from a different `contextvars.copy_context()`. If a sub-agent is running under the wrong forced task id, its spans bind to the wrong task and the correct task stays empty / stuck.

Look for cases where:
- The agent tool is executed via `run_in_executor` (thread pool) without `contextvars.copy_context()` wrapping.
- A manually constructed `asyncio.Task` was spawned from a lambda that captured the wrong context.

The hygienic pattern is `_forced_task_id_var.set(...)` immediately before the await, with a `_route_tokens` reset token stored for LIFO reset (see `adk.py:1661-1663`).

### 8. Check the server fan-out

If the client is transitioning tasks correctly but the UI doesn't reflect it, the bug is downstream. Verify:
- `convert.py` maps the updated task status (see `pb_task_status_from_pb` imports at `ingest.py:28-49`).
- The `SessionBus` publishes a `DELTA_TASK_STATUS` (`bus.py:25-36`).
- The `WatchSession` RPC is streaming to the frontend (check the `StreamAck` counter, or `make stats`).
- The frontend `TaskRegistry` receives the delta (`frontend/src/gantt/index.ts:183`+) — add a `console.log` inside `upsertPlan`.

## Verification

After applying a fix:

```bash
# Invariant suite
cd client && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_invariants.py tests/test_task_lifecycle.py tests/test_walker_completion_timing.py -q

# End-to-end scenario that exercises the stuck path
cd /home/sunil/git/harmonograf && uv run --extra e2e pytest tests/e2e/test_scenarios.py -q

# Live repro with sqlite persistence, then inspect
make demo
# trigger the scenario, then:
sqlite3 data/harmonograf.sqlite \
  "SELECT id, title, status, started_at, updated_at FROM tasks \
   WHERE session_id = '<sid>' ORDER BY started_at;"
```

## Common pitfalls

- **Confusing STUCK (agent) with stuck (task).** Agent-level STUCK is a heartbeat timeout; task-level stuck is a state transition that isn't happening. Different causes, different fixes. The UI stripe pattern is different too.
- **Adding logs inside `_set_task_status` without scope.** It is called on every transition attempt including rejected ones. Logging at INFO floods the log. Use DEBUG.
- **"Just unblock it" by direct mutation.** Never `task.status = COMPLETED` from a debugger to "unstick" a test. The invariant checker will not flag it at write time, but the next sweep will detect the violation and your bug report becomes bimodal. Always go through `_set_task_status`.
- **Trusting the wall-clock.** Sweep intervals and heartbeat timeouts use monotonic clocks under the hood. A laptop sleeping mid-session can trigger false STUCK on wake. Reproduce on a non-suspended machine.
- **Blaming the planner.** The refine loop is well-tested. If the planner returns `None`, the task genuinely has no fix; if it returns a revised plan that still doesn't unblock, the drift kind you detected was wrong. Work backwards from the drift detection, not forwards from the plan.
- **Missing `--store sqlite`.** The in-memory store forgets on restart. If your "stuck task" is cleared by a reboot you only proved you restarted. Use sqlite for any debugging that spans a process lifetime.
