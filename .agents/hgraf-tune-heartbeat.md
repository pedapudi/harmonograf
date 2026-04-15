---
name: hgraf-tune-heartbeat
description: Understand and safely change heartbeat interval, stuck threshold, and timeout constants — client cadence, server detection math, frontend signal.
---

# hgraf-tune-heartbeat

## When to use

You need to change how often agents emit heartbeats, how long before an agent is declared disconnected, or how many unchanged beats mean "stuck". The defaults trade detection latency against wire traffic, and changing one without the others breaks the invariants the server relies on.

## Prerequisites

1. Read the Heartbeat proto message: `proto/harmonograf/v1/telemetry.proto:94-124`. Key fields: `buffered_events`, `dropped_events`, `dropped_spans_critical`, `progress_counter`, `current_activity`, `context_window_tokens/limit_tokens`.
2. Read the client heartbeat builder: `client/harmonograf_client/heartbeat.py:21 DEFAULT_INTERVAL_SECONDS = 5.0` and `build_heartbeat()`.
3. Read the server stuck detector: `server/harmonograf_server/ingest.py:62-64`:
   - `HEARTBEAT_TIMEOUT_S = 15.0`
   - `HEARTBEAT_CHECK_INTERVAL_S = 5.0`
   - `STUCK_THRESHOLD_BEATS = 3` (3 consecutive unchanged heartbeats ≈ 15s)
4. Read the stuck update logic around `ingest.py:566` where `stuck_heartbeat_count` is incremented or cleared.

## The math

The **detection window** for "agent is stuck" is:

```
stuck_window_s ≈ HEARTBEAT_INTERVAL_CLIENT × STUCK_THRESHOLD_BEATS
```

With defaults: `5s × 3 = 15s`. That's the interval where an agent whose `progress_counter` has not changed is declared stuck.

The **disconnect window** ("agent is gone") is:

```
disconnect_window_s = HEARTBEAT_TIMEOUT_S
```

Default 15s. The server sweeper (see `HEARTBEAT_CHECK_INTERVAL_S = 5.0`) runs every 5s and marks any stream whose `last_heartbeat` is older than the timeout as `CRASHED`.

**Invariant**: `HEARTBEAT_TIMEOUT_S > HEARTBEAT_INTERVAL_CLIENT + 1 jitter slot`. With 5s interval, 15s timeout leaves room for two full missed beats + processing delay. If you halve the interval, you can halve the timeout — not before.

**Second invariant**: `HEARTBEAT_CHECK_INTERVAL_S ≤ HEARTBEAT_TIMEOUT_S`. The sweeper must run more often than the timeout to catch every stuck client within one window.

## Step-by-step to shorten the detection window (e.g. 5s stuck detection)

### 1. Pick new values

Target: detect stuck in 5s. That means:

- Client interval: 1s
- `STUCK_THRESHOLD_BEATS`: 5 (1s × 5 = 5s)
- `HEARTBEAT_TIMEOUT_S`: 4s — safe as 1s × 4, but 5s is safer for jitter.
- `HEARTBEAT_CHECK_INTERVAL_S`: 1s

### 2. Change the client interval

The transport's `heartbeat_interval_s` is configurable on `TransportConfig` (`client/harmonograf_client/transport.py:67`). Override it at `HarmonografClient.__init__` plumbing, or pass it through the adk plugin config. Do **not** hard-code it into `heartbeat.py`; `DEFAULT_INTERVAL_SECONDS` is the doc-only constant and the transport doesn't import it.

Verify with `grep -n heartbeat_interval client/harmonograf_client/transport.py` — the relevant lines are around 484 (wait with timeout) and 514 (deadline check).

### 3. Change the server constants

Edit `server/harmonograf_server/ingest.py:62`:

```python
HEARTBEAT_TIMEOUT_S = 5.0
HEARTBEAT_CHECK_INTERVAL_S = 1.0
STUCK_THRESHOLD_BEATS = 5
```

Both `HEARTBEAT_TIMEOUT_S` and the `IngestPipeline.__init__(heartbeat_timeout_s=...)` argument get read, so the constant is the default and callers can still override.

### 4. Review tests

Search for hardcoded timings in tests:

```bash
grep -rn "15.0\|STUCK_THRESHOLD\|heartbeat_interval" server/tests client/tests
```

`server/tests/test_ingest_extensive.py:296-318` directly tests the stuck detection math — update the expected beat count.

### 5. Verification

```bash
uv run pytest server/tests/test_ingest_extensive.py -x -q
uv run pytest server/tests/test_telemetry_ingest.py -x -q
uv run pytest client/tests/test_heartbeat.py client/tests/test_transport_mock.py -x -q
```

The transport test at `client/tests/test_transport_mock.py:303` uses `heartbeat_interval_s=0.2` already for fast tests — match that style.

## Trade-offs

- **Shorter interval → more wire traffic**: each heartbeat is ~100 bytes. 1s cadence × N agents = N × 100 bytes/s upstream per agent. At 100 agents that's still only 10 KB/s — cheap.
- **Shorter interval → busier transport loop**: the transport wakes every interval anyway to check its batching queue. Dropping from 5s to 1s adds 4 wakes/s per agent process. Measurable, not meaningful.
- **Shorter stuck window → more false positives**: an LLM call that takes 20s with `STUCK_THRESHOLD_BEATS=3 interval=5s` is currently NOT flagged as stuck because `progress_counter` only changes at callback boundaries — and the middle of an LLM call has no boundaries. **If you tighten the window below typical LLM latency, you'll get amber "stuck" borders on every slow model call.** Benchmark your slowest model call before tightening.
- **Longer timeout → slower disconnect detection**: users see "connected" for longer after the agent process is actually dead. 15s is the default for a reason — it's short enough to matter, long enough to ride out GC pauses.

## Frontend impact

- The frontend shows a ⚠ stuck badge when `Agent.stuck === true && hasRunning` (`frontend/src/components/shell/views/GraphView.tsx:1153`). The "has running" check keeps the badge from sticking around on agents whose invocation already ended — don't remove it.
- `Agent.stuck` in the renderer's session store (`frontend/src/gantt/types.ts:153`) is updated from `AgentStatus` deltas published by the server over `WatchSession`. Those deltas only fire on transition, not on every heartbeat — see `bus.py:138` where `stuck` is included in `publish_agent_status`.

## Common pitfalls

- **Halving the interval without halving the timeout**: disconnect detection gets slower relative to heartbeat cadence, and stuck windows shrink below LLM call latency. Keep them proportional.
- **Tuning only the server**: the client runs on its own schedule. If the server expects 1s beats and the client still sends at 5s, every legitimate agent looks stuck.
- **Assuming `progress_counter` ticks every 5s**: it ticks on *activity*, not on wall time. A long, silent LLM call will not bump it. This is intentional — stuck-ness is about forward progress, not uptime.
- **Skipping the test updates**: `test_ingest_extensive.py` encodes the count "3 consecutive heartbeats". Changing `STUCK_THRESHOLD_BEATS` without updating the test leaves you with a false-green that checks the old value.
