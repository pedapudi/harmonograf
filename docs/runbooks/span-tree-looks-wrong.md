# Runbook: Span tree looks wrong

Spans display in the UI with nonsense parenting: orphans, spans with
parents that don't exist, cross-agent links that look random, or a
tree whose shape doesn't match what the agent actually did.

## Symptoms

- **UI (Gantt / drawer)**:
  - Spans at top-level that should be children of an invocation.
  - A TOOL_CALL appearing under the wrong agent's row.
  - A span with `parent_span_id` set but the parent never shows up.
  - An LLM_CALL with no surrounding INVOCATION.
- **Server log**:
  - `DEBUG harmonograf_server.ingest: ignoring hgraf.task_id=<id> on non-leaf span kind=<k>`
    (`ingest.py:371`) — harmless, but indicates cross-wiring of task
    IDs to wrapper spans.
  - `DEBUG harmonograf_server.ingest: ignoring hgraf.task_id=<id> on ended non-leaf span kind=<k>`
    (`ingest.py:512`).
- **sqlite** (spot the shape):
  ```sql
  SELECT id, kind, parent_span_id, agent_id, start_time
  FROM spans WHERE session_id='SESSION_ID' ORDER BY start_time;
  ```

## Immediate checks

```bash
# Orphan scan: spans whose parent doesn't exist.
sqlite3 data/harmonograf.db <<'SQL'
SELECT s.id, s.kind, s.parent_span_id
FROM spans s
WHERE s.session_id='SESSION_ID'
  AND s.parent_span_id IS NOT NULL
  AND s.parent_span_id NOT IN (SELECT id FROM spans WHERE session_id='SESSION_ID');
SQL

# Cross-agent parent scan: a span whose parent belongs to a different agent.
sqlite3 data/harmonograf.db <<'SQL'
SELECT s.id, s.agent_id, p.agent_id AS parent_agent
FROM spans s JOIN spans p ON s.parent_span_id = p.id
WHERE s.agent_id <> p.agent_id AND s.session_id='SESSION_ID';
SQL

# Span count by kind:
sqlite3 data/harmonograf.db \
  "SELECT kind, COUNT(*) FROM spans WHERE session_id='SESSION_ID' GROUP BY kind;"
```

## Root cause candidates (ranked)

1. **Parent span dropped by the client buffer** — the parent was
   evicted under backpressure before upload, so the child references
   an ID the server never saw. Check `dropped_events` in client
   heartbeats; see [`agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md).
2. **`parent_span_id` set from a different span context** — a tool
   kicked off a background task with `asyncio.create_task` and the
   OpenTelemetry context bled through. The span thinks its parent is
   the enclosing invocation from a different agent.
3. **`agent_id` stamped incorrectly** — a sub-agent emitted a span
   with the parent agent's ID, or vice versa. The server's
   `_ensure_route` (`ingest.py:377`) auto-registers any pair it sees,
   so cross-wiring survives.
4. **Control alias registered the wrong way** — if a sub-agent's name
   differs from the transport's Hello ID, the server aliases the
   sub-agent name to the parent stream
   (`ingest.py:442`). If multiple sub-agents share a name, the alias
   maps all of them to one.
5. **Span ID collision** — agent regenerated the same span id (bad
   RNG, time-based IDs). The second span overrides the first in the
   local dedup cache (`seen_span_ids`, `ingest.py:335`).
6. **Out-of-order ingest** — the parent arrived *after* the child.
   Fast local dedup doesn't care; the tree still renders correctly
   because the UI resolves parents lazily. If you're seeing
   orphaning, this *isn't* it, but it does explain weird Z-ordering.
7. **Task binding on wrapper span** — you stamped `hgraf.task_id` on
   an INVOCATION or TRANSFER span; the server logs a DEBUG line and
   ignores the binding (`ingest.py:371`, `ingest.py:512`), which makes
   the task row look disconnected from the spans. See
   `_TASK_BINDING_SPAN_KINDS` in `ingest.py`.

## Diagnostic steps

### 1. Buffer drop

```bash
grep 'dropped_events\|dropped_spans_critical' /path/to/agent.log | tail -20
```

`dropped_spans_critical > 0` is a bug and will produce exactly this
symptom because critical spans (invocations) are parents. Run the
buffer eviction runbook.

### 2. OTEL context bleed

Search your agent code for `asyncio.create_task` used without binding
a fresh span context. In ADK, tools must use
`context_api.attach(...)` appropriately if they fork work.

### 3. agent_id stamp

```bash
sqlite3 data/harmonograf.db \
  "SELECT DISTINCT agent_id FROM spans WHERE session_id='SESSION_ID';"
```

Compare to the list of agents that should be active in the session.

### 4. Alias chaos

```bash
grep 'control alias registered' data/harmonograf-server.log | tail -20
```

Each registration shows `sub=NAME stream=STREAM_AGENT`. If multiple
`sub` names map to the same `stream`, or multiple streams claim the
same `sub`, aliases have collided.

### 5. Span ID collision

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, COUNT(*) FROM spans WHERE session_id='SESSION_ID' GROUP BY id HAVING COUNT(*) > 1;"
```

Zero rows = no collisions. Any rows = bug in the client ID generator.

### 6. Out-of-order

Not really a problem. Check by sorting by `received_at` if your
storage records it, but usually harmless.

### 7. Task on wrapper span

```bash
grep 'ignoring hgraf.task_id=.*on non-leaf span kind=' data/harmonograf-server.log | tail -10
```

## Fixes

1. **Buffer drop**: fix the buffer overflow (see
   [`agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md)).
2. **Context bleed**: wrap forked work in a fresh OTEL context; don't
   inherit the parent invocation's span as the parent of a background
   job.
3. **agent_id stamp**: the sub-agent emitting on behalf of another is
   usually by design (multi-agent on one stream). Verify it's
   intentional; if not, scope emitters per agent.
4. **Alias**: give each sub-agent a stable, unique name. Restart the
   server to clear stale aliases.
5. **ID collision**: fix the client's span ID generator (should be
   cryptographically random, not monotonic).
6. **Out of order**: no action.
7. **Wrapper binding**: stamp `hgraf.task_id` only on LLM_CALL and
   TOOL_CALL spans. Update the emitter.

## Prevention

- In client unit tests, assert that every emitted span's
  `parent_span_id` matches a known ancestor in the same test.
- Add a nightly sqlite check that `spans.parent_span_id` has zero
  orphans; alert if nonzero.
- Use UUIDv4 for span IDs; never time-based.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"A span is
  missing from the UI".
- [`protocol/wire-format.md`](../protocol/wire-format.md) for span
  field definitions.
- [`runbooks/agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md)
  for buffer-driven drops.
