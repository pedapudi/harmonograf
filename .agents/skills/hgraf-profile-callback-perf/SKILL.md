---
name: hgraf-profile-callback-perf
description: Profile ADK callback overhead using the existing client/tests/test_callback_perf.py harness + ProtocolMetrics counters + pytest --durations.
---

# hgraf-profile-callback-perf

## When to use

You changed something in `client/harmonograf_client/adk.py` (callback path, walker, invariant check, state writer) and want to confirm you didn't regress per-callback latency. Harmonograf doesn't have a real profiler framework — it uses a scripted plan + `time.perf_counter_ns` harness and a lightweight `ProtocolMetrics` counter struct.

## Prerequisites

1. `make client-install` done.
2. Read `client/harmonograf_client/metrics.py` — the `ProtocolMetrics` dataclass is the entire "perf observability" surface on the client. It tracks callback fire counts, task transitions, refine fires, reporting tool invocations, walker iterations, and invariant violations. No timings, no histograms — just counters.
3. Read `client/tests/test_callback_perf.py` — the reference harness for measuring callback latency without spinning up real ADK or gRPC.

## Step-by-step

### 1. Run the existing harness first

```bash
uv run pytest client/tests/test_callback_perf.py -q --durations=10
```

Note the p50/p95 it logs to stdout. The harness budget is ~5ms p95 per callback (`test_callback_perf.py:10`) and logs-but-doesn't-assert on breach so CI stays green on slow runners.

### 2. Identify which callback you suspect

The ADK callback paths all funnel through `_AdkState` in `client/harmonograf_client/adk.py`. The common hot spots:

- `_AdkState._on_before_model_callback` — runs on every model call, writes plan context into `session.state`.
- `_AdkState._on_after_model_callback` — parses the response for reporting-tool function_calls, task markers, state_delta writes.
- `_AdkState._on_event_callback` — checks transfer / escalate / state_delta events.
- `_AdkState._on_tool_callback` — dispatches the `report_task_*` tool intercepts.
- `_AdkState._run_invariants` — the post-walker turn safety net (`client/harmonograf_client/invariants.py`).

### 3. Write a scoped micro-harness

Copy `test_callback_perf.py` wholesale, trim the plan to just the callback you care about, and add direct `perf_counter_ns()` timing around that one callback:

```python
import time, statistics
durations = []
for _ in range(1000):
    t0 = time.perf_counter_ns()
    state._on_before_model_callback(ctx, llm_request)
    durations.append(time.perf_counter_ns() - t0)
print("p50", statistics.median(durations) / 1_000_000, "ms")
print("p95", sorted(durations)[int(0.95 * len(durations))] / 1_000_000, "ms")
```

The existing harness uses `FakeClient` (`test_callback_perf.py:45`) so you don't pay gRPC costs. Reuse that fixture — anything else is a different benchmark.

### 4. Check metrics counters

`state._metrics` is a `ProtocolMetrics`. After the hot loop:

```python
from harmonograf_client.metrics import format_protocol_metrics
print(format_protocol_metrics(state._metrics))
```

Use this to catch cases where a callback quietly started firing 10x as often — that's frequently the real cause of a "slow" run rather than any single call getting slower.

### 5. Use `pytest --durations` for full-suite regressions

```bash
uv run pytest client/tests --durations=25 -q
```

This gives you the slowest 25 test cases. If your change made `test_callback_perf.py` jump from 120ms to 400ms, it will float to the top immediately.

### 6. Check for invariant-driven slowdown

`_run_invariants` iterates over every task in the plan on every walker turn. It is O(plan_size × history_depth). For a 50-task plan it's already ~30ms on a cold laptop; a doubled plan is 4x. Grep `check_plan_state` in `client/harmonograf_client/invariants.py` and confirm no new rule is doing an O(n²) scan.

### 7. Check for state-dict bloat

`_AdkState._write_plan_context_to_session_state` serializes the plan context into `session.state`. If your change added a large field (e.g. full payload bodies), every callback now pays a JSON copy. Look for this first — it's the most common regression mode.

### 8. Verification

After profiling:

```bash
uv run pytest client/tests/test_callback_perf.py -x -q
uv run pytest client/tests --durations=10 -q
```

Both must still pass. Compare the `--durations` output against a run on `main` to spot regressions.

## Common pitfalls

- **`time.time()` instead of `time.perf_counter_ns()`**: wall clock has millisecond-level jitter that swamps the signal. Always use `perf_counter_ns` for ns-precision timing.
- **Benchmarking with real gRPC**: the transport loop has its own overhead (~1–5ms per round trip). Profile with `FakeClient` unless you specifically want the end-to-end number.
- **Counter amnesia**: `ProtocolMetrics` counters are per-`_AdkState` instance. If your test creates a new state each iteration, counters reset. Create it once outside the loop.
- **Assuming `--durations` catches everything**: `--durations` measures whole test cases, not individual callbacks. A callback that got 10x slower inside a loop of 1000 will still look fine if the whole test takes <1s.
- **No CI budget enforcement**: the callback-perf test logs but does not assert. If you want to catch regressions in CI, add an explicit assert on `p95` inside your scoped harness. Pick a forgiving number (2x current p95) to avoid flakes.
