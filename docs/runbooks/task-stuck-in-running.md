# Runbook: Task stuck in RUNNING

A task entered `RUNNING` and never transitions out. The agent row
has an amber "stuck" marker on the Graph view; heartbeats still
arrive; the Gantt shows an open span that doesn't close.

## Symptoms

- **Graph view**: agent header shows `⚠ stuck` (the liveness tracker
  flagged it after `ServerConfig.stuck_threshold_beats` (default 3)
  consecutive unchanged heartbeats).
- **Gantt**: an open RUNNING span that steadily grows but never ends.
- **Intervention strip**: `LOOPING_REASONING` drift has NOT (yet)
  fired — the tool-loop detector only trips after several repetitions.
- **Heartbeats**: still arriving; `progress_counter` unchanged.

## Immediate checks

```bash
# Agent last heartbeat — still connected?
sqlite3 data/harmonograf.db \
  "SELECT id, last_heartbeat, status, metadata FROM agents WHERE id='AGENT_ID';"
date +%s

# Still-open spans for this agent
sqlite3 data/harmonograf.db \
  "SELECT id, kind, name, start_time FROM spans
   WHERE agent_id='AGENT_ID' AND end_time IS NULL
   ORDER BY start_time DESC LIMIT 10;"

# Are we in a tool loop? Check the tool-call span history.
sqlite3 data/harmonograf.db \
  "SELECT name, COUNT(*) FROM spans
   WHERE agent_id='AGENT_ID' AND kind='TOOL_CALL'
     AND start_time > (strftime('%s','now')-600)
   GROUP BY name ORDER BY 2 DESC LIMIT 10;"
```

## Root-cause candidates (ranked)

1. **LLM call hung** — a still-open `LLM_CALL` span older than a
   minute or two on a local model. Check `goldfive.llm.duration_ms`
   on the most recent LLM_CALL span. Values > 60 s flag a wedged
   provider.
2. **Tool hung** — an open `TOOL_CALL` span that never ends. The
   tool's implementation is blocked (HTTP, DB, subprocess).
3. **Tool loop detected but drift threshold not yet reached** —
   goldfive's `ToolLoopTracker` (goldfive#181/#186) fires
   `LOOPING_REASONING` only after N repetitions in exact / name /
   alternating modes. If the agent is in a loop but hasn't hit the
   threshold, the overlay hasn't refined yet.
4. **Infinite loop in agent code (not ADK / model)** — the wrapper
   around the model is spinning. `py-spy top` on the process will
   show a hot frame outside ADK and the model client.
5. **`session.state` deadlock** — two parallel sub-agents waiting
   for a flag the other never sets.
6. **Cancellation never delivered** — you sent CANCEL but it didn't
   land. Check the Goldfive `ADKAdapter.invoke` side: on a cancel,
   it calls `plugin.on_cancellation(invocation_id)` which flushes
   open spans with `status=CANCELLED` (goldfive#167). If spans
   stayed RUNNING after a cancel, the plugin's cancellation hook
   didn't run — usually because the plugin was installed twice and
   one instance stayed silent (#68).

## Diagnostic steps

### 1. Identify the open span

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, kind, name, start_time, attributes
   FROM spans
   WHERE agent_id='AGENT_ID' AND end_time IS NULL
   ORDER BY start_time DESC LIMIT 5;"
```

If the newest is an `LLM_CALL`, skip to step 2. If `TOOL_CALL`,
skip to step 3. If `INVOCATION` with no in-flight child, you likely
have an agent-code loop; skip to step 4.

### 2. LLM call hang

```bash
grep -E 'goldfive.llm.duration_ms' /path/to/agent.log | tail -10
```

Large durations (> 60 s) point at the provider. On `kikuchi` /
local vLLM / Ollama, check that the upstream server is responsive:

```bash
curl -sS "$KIKUCHI_LLM_URL/v1/models" | head
```

If the provider is unresponsive, the hung span is a symptom, not a
cause. Restart the provider.

### 3. Tool hang

```bash
py-spy dump --pid $(pgrep -f 'my_agent') | head -80
```

Look for your tool's frames. If a subprocess is the culprit, kill
it and wrap the tool implementation in `asyncio.wait_for` or an
ADK tool `time_limit`.

### 4. Agent-code loop

```bash
py-spy top --pid $(pgrep -f 'my_agent')
```

A hot frame outside ADK / the model client is the culprit. Send
`CANCEL` via the control router to end the run, fix the loop, and
restart.

### 5. Loop detector threshold

If you suspect a tool loop but `LOOPING_REASONING` hasn't fired yet:

```bash
grep -E 'ToolLoopTracker|tool_loop' /path/to/agent.log | tail -30
```

You can lower the threshold in goldfive's config, but usually it's
better to let the detector fire at its natural point and refine.

### 6. Duplicate plugin (for post-cancel cleanup)

```bash
grep 'duplicate HarmonografTelemetryPlugin' /path/to/agent.log
```

If present, ensure only one `HarmonografTelemetryPlugin(client)` is
installed. The duplicate stays silent on every callback — including
`on_cancellation` — so a CANCEL won't flush its spans.

## Fixes

1. **LLM hang**: restart the upstream provider; add a client-side
   timeout on the adapter level.
2. **Tool hang**: add `asyncio.wait_for` / ADK `time_limit` to the
   tool body; kill the blocked subprocess.
3. **Agent-code loop**: send CANCEL, fix the loop, restart.
4. **Loop detector missed**: tune `ToolLoopTracker` thresholds in
   goldfive, or refine instructions so the agent breaks out earlier.
5. **Duplicate plugin**: remove the extra installation point
   (usually `observe()` or `add_plugin` that runs after
   `App(plugins=[...])`).

## Cross-links

- [runbooks/task-stuck-in-pending.md](task-stuck-in-pending.md) —
  the symmetric case (task never claimed).
- [runbooks/high-latency-callbacks.md](high-latency-callbacks.md) —
  diagnose slow LLM / tool calls with `goldfive.llm.duration_ms`.
- [dev-guide/client-library.md](../dev-guide/client-library.md) —
  the cancellation sweep (`on_cancellation` hook) and why duplicate
  plugins break it.
