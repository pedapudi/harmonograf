---
name: hgraf-write-e2e-scenario
description: Pattern for writing a new tests/e2e/test_scenarios.py scenario using scripted FakeLlm, StaticPlanner, and real ADK + real harmonograf server.
---

# hgraf-write-e2e-scenario

## When to use

You need a hermetic end-to-end test that exercises the full stack — real `google.adk` runtime, real `harmonograf_client`, real `harmonograf_server` (in-process) — but with deterministic, scripted LLM output. Use this for:
- Regression tests on drift detection + refine flow.
- Plan-diff rendering correctness (client→bus→frontend-fixtures).
- Reporting tool lifecycle coverage.
- Walker/walker-completion-timing scenarios that unit tests can't catch.

Do **not** use this skill for tests that don't need ADK at all — prefer the duck-typed unit tests under `client/tests/test_drift_taxonomy.py` or `test_invariants.py`. They run 10-100× faster.

## Prerequisites

1. `make install` has run (needs the ADK submodule). See `Makefile:install` which calls `git submodule update --init --recursive`.
2. Read the existing test file top to bottom: `tests/e2e/test_scenarios.py` (module docstring at lines 1-34 explains the whole approach).
3. Read the fixtures in `tests/e2e/conftest.py` — specifically:
   - `harmonograf_server` at lines 32-57 (in-memory store, ephemeral ports)
   - `real_harmonograf_server` at lines 60-94 (sqlite-backed, tmp_path)
4. Understand `FakeLlm` — built via `_build_fake_llm_class()` at `test_scenarios.py:42-79`. It takes a list of `LlmResponse` objects and a cursor; each call consumes one response. Exhausting the list raises.

## Step-by-step

### 1. Pick your fixture

Use `harmonograf_server` for speed (in-memory store, dict-backed). Use `real_harmonograf_server` only when the test verifies sqlite persistence or schema migrations — it is slower but exercises the real storage path.

```python
async def test_my_scenario(harmonograf_server):
    app = harmonograf_server["app"]
    addr = harmonograf_server["addr"]
    bus = harmonograf_server["bus"]
    store = harmonograf_server["store"]
    ...
```

### 2. Build the FakeLlm response script

`_build_fake_llm_class()` at `test_scenarios.py:42-79` returns a `(FakeLlm, LlmResponse, genai_types)` triple. Use the helpers at lines 82-102:

- `_text_response("assistant text")` — a plain text turn.
- `_function_call_response(name="report_task_started", args={"task_id": "t1"})` — a tool call.

Script the exact sequence the agent should produce. Each LLM turn pops one response. If the agent produces more turns than you scripted, `FakeLlm` raises — that is often the bug you want to see.

```python
from google.adk.models import LlmResponse  # via _build_fake_llm_class
responses = [
    _function_call_response("report_task_started", {"task_id": "t1"}),
    _text_response("Working on t1..."),
    _function_call_response("report_task_completed", {"task_id": "t1", "summary": "ok"}),
    _function_call_response("report_task_started", {"task_id": "t2"}),
    _function_call_response("report_task_completed", {"task_id": "t2", "summary": "ok"}),
]
fake_llm = FakeLlm(responses=responses, cursor=0, sleep_ms=0)
```

Set `sleep_ms` >0 only if you are testing throttling (the fake LLM sleeps that many milliseconds per turn, simulating real latency).

### 3. Build a StaticPlanner

`class StaticPlanner(PlannerHelper)` at `test_scenarios.py:110-129` is the deterministic planner: `generate()` returns `self._plan`, `refine()` returns `self._refined`. It counts calls via `generate_calls` and `refine_calls` so you can assert exactly one refine fired.

```python
planner = StaticPlanner(
    _plan=Plan(tasks=[Task(id="t1", title="do first", ...), ...], edges=[]),
    _refined=None,  # no refine on drift
)
```

Feed your planner to `HarmonografAgent(planner=planner, ...)`.

### 4. Build the agent tree

`_make_agent()` at `test_scenarios.py:137-148` shows the pattern: construct an `LlmAgent` with `model=fake_llm`, an instruction, and `sub_agents=[]`. Wrap in `HarmonografAgent(inner_agent=agent, harmonograf_client=client, ...)`. Pick the orchestration mode explicitly:

- `orchestrator_mode=True, parallel_mode=False` — sequential (default).
- `orchestrator_mode=True, parallel_mode=True` — parallel DAG walker.
- `orchestrator_mode=False` — delegated (agent is in charge of its own sequencing; observer scans afterwards).

See `AGENTS.md` → *Plan execution protocol* for when each is appropriate.

### 5. Run under InMemoryRunner

Use `google.adk.runners.InMemoryRunner` (imported in the existing test file — grep for it). It provides an in-memory session service and a synchronous `run_async` driver:

```python
runner = InMemoryRunner(agent=harmonograf_agent)
async for event in runner.run_async(user_id="u", session_id="s", new_message=...):
    pass
```

Drive until the agent emits its terminal event.

### 6. Assert on server-observed state

The `harmonograf_server` fixture exposes `store` and `bus` — you can assert on the persisted state directly:

```python
session = await store.get_session(session_id)
tasks = await store.list_tasks(session_id)
assert [t.status for t in tasks] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
assert planner.generate_calls == 1
assert planner.refine_calls == 0
```

If you need to assert on the streaming delta path (not just final state), subscribe to the bus before driving the agent:

```python
sub = bus.subscribe(session_id)
# drive agent
deltas = []
while not sub.queue.empty():
    deltas.append(await sub.queue.get())
assert any(d.kind == DELTA_TASK_STATUS for d in deltas)
```

### 7. Clean up

The async fixtures in `conftest.py` handle server teardown. If you subscribed to the bus, call `sub.close()` before the test ends or you will leak an unsubscribed queue.

## Verification

```bash
# Run just your new scenario
cd /home/sunil/git/harmonograf && uv run --extra e2e pytest tests/e2e/test_scenarios.py::test_my_scenario -q -xvs

# Full e2e suite
make e2e

# And the client suite (to catch regressions in detection logic)
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q
```

## Common pitfalls

- **Exhausted FakeLlm.** Scripting too few responses raises a cryptic IndexError inside ADK. Err on the side of an extra no-op `_text_response("done")` at the end.
- **Scripting too many responses.** If the agent stops early (e.g. completes on `report_task_completed`), trailing scripted responses are never consumed — that is fine, but if your test also asserts `fake_llm.cursor == len(responses)`, it will fail. Drop the assertion or match the exact count.
- **`InMemoryRunner` + `parallel_mode=True`.** The parallel walker spawns concurrent sub-invocations. `InMemoryRunner` is single-threaded but async-safe; do not mix in any synchronous blocking. Use asyncio primitives throughout.
- **Ephemeral port collision.** `_free_port()` at `conftest.py:26-29` grabs a port and releases it immediately before the server binds — there is a tiny TOCTOU window on busy CI boxes. If you see occasional `Address already in use`, retry the fixture (do not expand the port range; that just pushes the flake elsewhere).
- **SQLite fixture + file leakage.** `real_harmonograf_server` writes to `tmp_path`. Pytest cleans it up. Do not `rm` it yourself; do not copy the file out of `tmp_path` and assume it survives the fixture scope.
- **Reporting tool body mismatch.** The reporting tool function bodies return `{"acknowledged": True}` — the *state change* happens in `before_tool_callback`. If your FakeLlm script calls a tool but nothing transitions, the interception isn't running — check that `HarmonografAgent._auto_register_reporting_tools` found your inner agent (it walks the subtree at construction).
- **StaticPlanner returning `Plan(tasks=[], edges=[])`.** An empty plan trips the invariant checker ("no tasks in plan"). Always return at least one PENDING task.
- **ContextVar leakage between tests.** `_forced_task_id_var` is reset via LIFO tokens; if an earlier test crashes mid-walker, the token may not be reset and the next test inherits stale state. The fixture should construct a fresh client and harmonograf state per test — do not share.
- **Assuming wall-clock in assertions.** Tasks have `started_at` timestamps from the server's clock. Assertions like `started_at == <fixed value>` will flake. Use relative ordering (`t1.started_at < t2.started_at`) or epsilon windows.
