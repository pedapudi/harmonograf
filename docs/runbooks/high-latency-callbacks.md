# Runbook: High-latency callbacks

The agent's hot path is slow. Spans take a long time to end, the
client buffer backs up, the UI feels laggy. Py-spy shows most time
inside `harmonograf_client.adk` callbacks.

**Triage decision tree** — py-spy first; the dominant frame names the cause.

```mermaid
flowchart TD
    Start([Hot path slow,<br/>buffered_events climbing]):::sym --> Q1{py-spy top: dominant<br/>frame?}
    Q1 -- "tiktoken / tokenizer / encode" --> F1[Tokenizer-heavy: sample less,<br/>cache stable prefix tokens]:::fix
    Q1 -- "planner.refine" --> F2[Move refine off event loop<br/>or set refine_on_events=False]:::fix
    Q1 -- "logging / format" --> F3[LOG_LEVEL=DEBUG in prod;<br/>switch to INFO]:::fix
    Q1 -- "hashlib / sha256" --> F4[Big payload hashing —<br/>stop capturing full bodies]:::fix
    Q1 -- "before_tool_callback" --> F5[Sync IO in handler;<br/>make async / queue]:::fix
    Q1 -- "detect_drift" --> F6[O(events) scan: add cursor<br/>events_since_last_scan]:::fix
    Q1 -- "check_plan_state /<br/>_check_*" --> F7[Invariant checker hot —<br/>only run in dev/CI]:::fix
    Q1 -- "looks normal but slow" --> F8[Network slow: see<br/>agent-disconnects-repeatedly]:::fix

    classDef sym fill:#fde2e4,stroke:#c0392b,color:#000
    classDef fix fill:#d4edda,stroke:#27ae60,color:#000
```

## Symptoms

- **Client heartbeat**: `buffered_events` climbs, `cpu_self_pct` high,
  `current_activity` stays on one step for long periods.
- **Client log** (with DEBUG): callback entry / exit times far apart.
  Possibly `harmonograf_client.adk: planner.refine blocked the event
  loop for <x>s — consider disabling refine_on_events` (`adk.py:2063`).
- **UI**: spans appear in chunks instead of smoothly; transport bar
  shows transient back-pressure.
- **py-spy** top: time spent in
  `harmonograf_client.adk._AdkState.*`, `detect_drift`, `record_span`,
  or the tokenizer used for context-window accounting.

## Immediate checks

```bash
# py-spy top the running agent:
py-spy top --pid $(pgrep -f my_agent)

# Dump a sample:
py-spy dump --pid $(pgrep -f my_agent)

# Heartbeat trend — buffered_events over time:
grep buffered_events /path/to/agent.log | tail -40

# Slow refines explicitly logged:
grep 'planner.refine blocked the event loop' /path/to/agent.log
```

## Root cause candidates (ranked)

1. **Tokenizer-heavy context-window accounting** — counting tokens
   on every LLM call, especially with a pure-python tokenizer, is
   expensive. If `context_window_tokens` updates every turn, this
   adds up.
2. **Blocking planner refine on the event loop** — `planner.refine`
   is called synchronously on some drift paths and blocks the
   asyncio loop until it returns. The adapter emits a warning:
   `planner.refine blocked the event loop for %.1fs — consider
   disabling refine_on_events` (`adk.py:2063`).
3. **Excessive DEBUG logging** — running with `LOG_LEVEL=DEBUG` in
   production will turn the `harmonograf_client.adk` logger into a
   bottleneck. Every callback emits multiple lines.
4. **Big payloads on every span** — attaching prompt + response bytes
   to every LLM_CALL turns the buffer into a blob store. Hashing the
   blob also costs CPU.
5. **Synchronous reporting-tool handlers** — each reporting-tool
   invocation hops through `before_tool_callback`; if the handler
   does IO (writes to a file, hits a DB), the whole flow blocks.
6. **Drift detector scanning full event history on every call** —
   `detect_drift` in `_AdkState` may be O(events); if the session has
   thousands of events, every scan gets slower. Rare but worth
   checking.
7. **Invariant checker in the hot path** — running `check_plan_state`
   after every transition in a large plan is O(plan size).

## Diagnostic steps

### 1. Tokenizer

py-spy top → frames containing `tiktoken`, `transformers`,
`tokenizer`, or `encode`. If they dominate, the tokenizer is the
cost.

### 2. Blocking refine

```bash
grep 'planner.refine blocked the event loop' /path/to/agent.log
```

If present, the count and the duration tell you how often and how
bad. The advice in the log line — disable `refine_on_events` — is
the fix.

### 3. Debug logging

Check `LOG_LEVEL` in the agent env:

```bash
env | grep -i log
```

If it's `DEBUG`, switch to `INFO` for production.

### 4. Big payloads

```bash
sqlite3 data/harmonograf.db \
  "SELECT digest, size FROM payloads ORDER BY size DESC LIMIT 20;"
```

If the top sizes are > 1MB, you're storing blobs. Cut capture.

### 5. Synchronous tool handlers

Search for `time.sleep`, blocking IO, or long CPU work inside
reporting-tool bodies or `before_tool_callback` hooks.

### 6. detect_drift hot

py-spy top → `detect_drift` frames dominating. Profile the scan.

### 7. Invariant hot

py-spy top → `check_plan_state` / `_check_*` frames dominating.
The checker is designed for debugging, not hot-path use.

## Fixes

1. **Tokenizer**: sample less often (only N turns), or switch to a C
   tokenizer (`tiktoken`), or cache the token count across turns for
   stable prompt prefixes.
2. **Blocking refine**: move `refine` to a background task via
   `asyncio.create_task`, OR set `refine_on_events=False` and rely
   only on reporting-tool-driven refines. The adapter's warning
   documents this choice.
3. **DEBUG logging**: set `LOG_LEVEL=INFO` in production; keep DEBUG
   for investigation only.
4. **Payloads**: stop capturing full payloads by default; capture
   only the ones a user explicitly requests or that the drawer will
   need.
5. **Sync handlers**: make them `async` and `await` IO. Don't do
   work in `before_tool_callback`; queue to a background task.
6. **detect_drift**: add an `events_since_last_scan` cursor so each
   scan is bounded.
7. **Invariant checker**: don't run it on every transition in
   production; run it on demand or in tests.

## Prevention

- Keep a latency SLO for `after_model_callback` round-trip (say,
  100ms) and alert if exceeded.
- Benchmark the callbacks in CI against a canned event log, assert
  they finish within the SLO.
- Treat `buffered_events` growth > 0 in steady state as a red flag
  — either the transport is slow or the hot path is slow.

## Cross-links

- [`runbooks/agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md)
  — slow hot path → heartbeat miss → disconnect.
- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"Turning on
  debug logging" — log levels per logger.
- `client/harmonograf_client/adk.py:2062-2063` — the "blocked the
  event loop" warning.
