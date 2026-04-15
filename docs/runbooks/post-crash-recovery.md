# Runbook: Post-crash recovery

The server or an agent crashed mid-run. You need to bring the system
back into a consistent state, decide what to replay, and understand
what was lost.

## Symptoms

- **Server died** — `systemctl status harmonograf-server` shows failed;
  or the stdout of a bare invocation ended with a traceback.
- **Agent died** — client process exited; last log line is often a
  traceback or OOM kill.
- **On recovery**: agents reconnect, a new stream opens, some data is
  present, some is missing. You want to know which.

## Key facts about recovery

- **Storage is non-authoritative**. Agents replay on reconnect via
  `resume_token`. The resume token = last server-acked span id (see
  `client/harmonograf_client/transport.py:135-136` and `:595`). If
  the server lost state, agents will resend from their resume point.
- **Spans before the resume point are considered "delivered"** by
  the client even if the server has no record — they will NOT be
  replayed. This is a design tradeoff: it bounds buffer growth.
- **Plans** are persisted in the `task_plans` table and survive
  server restart. After restart, the in-memory
  `_task_index[session_id]` is rebuilt lazily on first plan-related
  event; `ingest.py:704` scans storage to find a plan when the index
  is cold.
- **Payloads** that were mid-upload when the server died are lost
  (the assembler state was in memory). The span's payload_ref
  remains but the row is absent — [`payloads-missing.md`](payloads-missing.md).
- **Sessions** persist. The same session ID will have the original
  metadata when the client re-Hellos.

## Immediate checks

```bash
# Did the server come back up?
systemctl status harmonograf-server
pgrep -af harmonograf_server

# Is the DB well-formed?
sqlite3 data/harmonograf.db 'PRAGMA integrity_check;'

# Last few sessions / their latest heartbeat ages
sqlite3 data/harmonograf.db <<'SQL'
SELECT s.id, s.title, MAX(a.last_heartbeat) AS last_hb, COUNT(a.id) AS agents
FROM sessions s LEFT JOIN agents a ON a.session_id=s.id
GROUP BY s.id ORDER BY last_hb DESC LIMIT 10;
SQL

# Orphan spans — parents that exist but end_time=NULL
sqlite3 data/harmonograf.db \
  "SELECT COUNT(*) FROM spans WHERE end_time IS NULL;"

# Latest plan per session:
sqlite3 data/harmonograf.db \
  "SELECT session_id, MAX(revision_index) FROM task_plans GROUP BY session_id;"
```

## Root cause candidates (ranked — for "what's in a bad state")

1. **Open spans from dead agents** — `end_time IS NULL` for spans
   that will never be ended because the agent is gone. They will
   stay "running" forever in the UI.
2. **Dangling stream contexts** — the server may hold stream state
   for agents that crashed. On restart, this isn't a problem (the
   new process has empty state). Before restart, expect heartbeat
   sweeper to eventually clean them up.
3. **Plans with in-progress tasks** — the task was RUNNING when the
   agent died. Unless the agent reconnects and re-reports, it's
   stuck at RUNNING.
4. **Payloads mid-upload** — the span's `payload_digest` references
   a payload the store never finished assembling.
5. **Duplicate sessions after restart** — rare; happens when an
   agent reconnects with a different session_id because its identity
   file was lost.
6. **Task_index cold after restart** — the first task-related event
   after server restart will pay a scan cost
   (`ingest.py:700-712`). Usually unnoticeable; but if you see a
   spike in latency on the first delta after restart, this is why.

## Diagnostic steps

### 1. Open spans

```bash
sqlite3 data/harmonograf.db <<'SQL'
SELECT id, session_id, agent_id, kind, name, start_time
FROM spans WHERE end_time IS NULL ORDER BY start_time;
SQL
```

Cross-reference with the agents table:

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, status, last_heartbeat FROM agents WHERE id='AGENT_ID';"
```

If the agent is DISCONNECTED and won't reconnect, the span will
never end on its own.

### 2. Plans with in-progress tasks

```bash
sqlite3 data/harmonograf.db \
  "SELECT plan_id, id AS task_id, status FROM task_plan_tasks
   WHERE status IN ('RUNNING', 'PENDING') AND plan_id IN (
     SELECT id FROM task_plans WHERE session_id='SESSION_ID');"
```

### 3. Stale payload references

```bash
sqlite3 data/harmonograf.db <<'SQL'
SELECT s.id, s.payload_digest FROM spans s
LEFT JOIN payloads p ON p.digest = s.payload_digest
WHERE s.payload_digest IS NOT NULL AND p.digest IS NULL LIMIT 20;
SQL
```

### 4. Duplicate sessions

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, title FROM sessions WHERE title IN (
     SELECT title FROM sessions GROUP BY title HAVING COUNT(*) > 1);"
```

## Recovery playbook

### Server died, agents alive

1. Restart the server (`systemctl restart harmonograf-server` or
   `make server-run`).
2. Watch the server log for `stream opened` (`ingest.py:233`) lines
   — every agent should reconnect within their retry window.
3. Check that the session picker still shows the session.
4. For any spans open before the crash, the agents will resume from
   their resume token. Spans whose `SpanEnd` landed server-side
   before the crash will be rolled up; spans whose `SpanEnd` was
   still in the client buffer will be resent.

### Agent died, server alive

1. Start the new agent process with the same `session_id` if you
   want to join the same session.
2. The new agent will send a `Hello` with its own `agent_id`. If
   it uses the same `agent_id` as the dead one, it will appear as a
   reconnect; if different, it will appear as a new agent in the
   same session.
3. Decide whether to close out the dead agent's open spans
   manually. Right now harmonograf has no "mark as failed on
   disconnect" sweeper for spans — only for the agent status
   itself. Consider adding one.

### Everything died

1. Start the server first.
2. Start the agents.
3. Accept that the period between the crash and the last
   `resume_token` ack may have gaps.

### Unrecoverable sqlite

1. See [`sqlite-errors.md`](sqlite-errors.md).
2. Nuclear option: stop the server, `rm data/harmonograf.db*`,
   restart. Data is non-authoritative; agents will re-stream.

## Prevention

- Run the server under systemd with `Restart=on-failure`.
- Size the client buffer so a few seconds of disconnection don't
  exceed its capacity (which would force eviction).
- Add monitoring for `end_time IS NULL` spans older than the longest
  expected run — they indicate unclosed state.
- Document the resume-token contract so engineers know what is
  guaranteed.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"Inspecting
  the sqlite store", §"Common 'but that's impossible' causes" — the
  two-sessions-same-id gotcha.
- [`runbooks/sqlite-errors.md`](sqlite-errors.md).
- [`runbooks/agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md).
- [`runbooks/payloads-missing.md`](payloads-missing.md).
