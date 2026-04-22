# Runbook: High-latency callbacks

The agent's hot path is slow. Spans take a long time to end, the
client buffer backs up, the UI feels laggy, STEER acks take
seconds.

Post-goldfive-migration this is almost always about the LLM itself,
not harmonograf's callback machinery. The harmonograf side is span
marshal + ring-buffer push, which is microseconds per envelope.

## Symptoms

- **Client heartbeat**: `buffered_events` climbs, `cpu_self_pct`
  high, `current_activity` stays on one step for long periods.
- **UI**: spans appear in chunks instead of smoothly; the Gantt
  shows multi-second gaps between span closes on the live edge.
- **Intervention strip**: STEER cards stay at `pending` outcome
  for many seconds.
- **Goldfive log**: `goldfive.llm.duration_ms` values in the
  tens of thousands.

## Immediate checks

```bash
# py-spy the running agent to find the dominant frame
py-spy top --pid $(pgrep -f my_agent)

# Recent LLM call durations
grep -E 'goldfive.llm.duration_ms' /path/to/agent.log | tail -20

# Per-agent context-window usage
grep -E 'goldfive.llm.request.chars|context_window_tokens' /path/to/agent.log | tail -20

# Heartbeat trend
grep -E 'buffered_events|progress_counter' /path/to/agent.log | tail -30
```

## Root-cause candidates (ranked)

1. **Slow LLM provider** — a local Qwen3.5-35B routinely takes
   20-60 s per call; a planner refine with a long task list can hit
   90 s on a laptop. This is 99% of "high latency callback"
   complaints. The fix is upstream, not in harmonograf.
2. **Runaway context growth post-STEER** — every STEER adds the
   drift body + task state into the planner's prompt. Over several
   STEERs the prompt grows linearly. Check
   `goldfive.llm.request.chars` on consecutive calls; linear growth
   is the signature.
3. **Tool loop not yet detected** — an agent calling the same tool
   N times in a row. Each call takes its normal duration, but the
   overall wall-clock balloons. Look for repeating TOOL_CALL names
   in the Gantt; goldfive#181/#186's `ToolLoopTracker` will
   eventually fire `LOOPING_REASONING` but may take several
   repetitions.
4. **Large payload hashing** — spans with multi-MB tool outputs /
   LLM responses. Harmonograf hashes and chunks them; >50 MB
   outputs are slow. Check `has_payload` on hot spans.
5. **Logging pressure** — `LOG_LEVEL=DEBUG` in production with a
   chatty logger format doing field-formatting on every span.
6. **Duplicate plugin** (#68) — two `HarmonografTelemetryPlugin`
   instances on the same PluginManager. One stays silent, but the
   active one does the work twice on some paths if the user also
   calls it directly. Not a common cause of latency but worth
   checking.

## Diagnostic steps

### 1. Confirm it's the LLM

```bash
grep -E 'goldfive.llm.duration_ms' /path/to/agent.log | awk '{print $NF}' | sort -n | tail -20
```

If the tail is dominated by multi-thousand-ms values, the LLM is
the bottleneck. If values are all < 500, look elsewhere.

### 2. Check context growth

```bash
grep -E 'goldfive.llm.request.chars' /path/to/agent.log | tail -40
```

If the numbers climb strictly monotonically across STEERs, you've
got context accumulation. Possible remedies:

- Truncate the STEER body before forwarding.
- Have goldfive prune completed tasks from the planner prompt
  (off by default; an opt-in).
- Move to a larger-context model and accept the growth.

### 3. Loop detection

```bash
sqlite3 data/harmonograf.db \
  "SELECT name, COUNT(*) FROM spans
   WHERE agent_id='<AID>' AND kind='TOOL_CALL'
     AND start_time > (strftime('%s','now')-300)
   GROUP BY name HAVING COUNT(*) > 3 ORDER BY 2 DESC;"
```

More than 3 identical tool names in 5 minutes = loop candidate.
Goldfive's tracker covers exact, name-only, and alternating
patterns; thresholds are configurable.

### 4. Payload pressure

```bash
sqlite3 data/harmonograf.db \
  "SELECT digest, size, mime FROM payloads ORDER BY size DESC LIMIT 10;"
```

If the top entries are 50 MB+, the tool or LLM is producing
unreasonable outputs.

## Fixes

1. **Slow LLM**: use a faster provider or a smaller model for the
   planner. The planner refine is disproportionately slow because
   of prompt size; smaller faster models run refine better.
2. **Context growth**: truncate STEER bodies to ~1 KB at the UI
   edge; prune completed tasks from the planner prompt in goldfive.
3. **Tool loop**: tighten `ToolLoopTracker` thresholds; improve the
   agent's instructions so it doesn't retry the same tool call.
4. **Large payloads**: stop capturing full bodies on cumulative
   LLM outputs; use `payload_ref` summaries.
5. **Log noise**: set `LOG_LEVEL=INFO` in production.
6. **Duplicate plugin**: deinstall the duplicate. Only the earliest
   instance on the `PluginManager` is active; subsequent ones log
   `duplicate HarmonografTelemetryPlugin instance detected` once
   at INFO.

## Cross-links

- [dev-guide/performance-tuning.md](../dev-guide/performance-tuning.md)
  — the full map of hot paths and their costs.
- [runbooks/context-window-exceeded.md](context-window-exceeded.md)
  — when the LLM is truncating inputs.
- [runbooks/task-stuck-in-running.md](task-stuck-in-running.md) —
  when a slow LLM call wedges a task outright.
