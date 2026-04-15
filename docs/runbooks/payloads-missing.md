# Runbook: Payloads missing

A span is displayed in the UI but its payload is unavailable, either
as "not preserved (client under backpressure)" or "no payload attached"
or a spinner that never resolves.

## Symptoms

- **UI** (drawer Payload tab):
  - `No payload attached to this span.`
  - `Payload was not preserved (client under backpressure).`
  - A spinner for `Load full payload` that hangs.
- **Client log**:
  - `WARN` entries around buffer eviction (buffer stats show
    `payloads_evicted` incrementing in heartbeat).
- **Server log**:
  - `WARN harmonograf_server.ingest: payload digest mismatch expected=<d1> actual=<d2> agent_id=<id>`
    (`ingest.py:540`) — data corruption path.
- **sqlite**:
  ```sql
  SELECT digest, size, mime, evicted FROM payloads WHERE digest='DIGEST';
  ```

## Immediate checks

```bash
# Is the payload in the store?
sqlite3 data/harmonograf.db \
  "SELECT digest, size, mime, evicted, summary FROM payloads
   WHERE digest='DIGEST';"

# Did the client flag any payloads as evicted in heartbeats?
grep 'payloads_evicted' /path/to/agent.log | tail -20

# Digest mismatch events?
grep 'payload digest mismatch' data/harmonograf-server.log | tail -10

# What payload refs does the span carry?
sqlite3 data/harmonograf.db \
  "SELECT id, payload_digest, payload_mime, payload_size
   FROM spans WHERE id='SPAN_ID';"
```

## Root cause candidates (ranked)

1. **Client backpressure eviction** — the payload buffer hit its
   byte cap and the payload was evicted before upload. The client
   sends `PayloadUpload{evicted=True}` (`ingest.py:523`) and nothing
   is stored server-side. This is the common case.
2. **Span never had a payload** — the span's emit path never captured
   one. Not a failure; the tab is correct. Usually misinterpreted as
   a bug.
3. **Digest mismatch** — assembler reassembled the chunks and got a
   different SHA256 than the client declared. Server raises
   `payload digest mismatch: declared=... actual=...` and rejects the
   payload (`ingest.py:547-549`). The stream is then aborted with
   INVALID_ARGUMENT by the RPC layer.
4. **GetPayload RPC hanging** — the frontend's "Load full payload"
   button is firing against a live store that's blocking. Usually a
   sqlite lock or a slow disk.
5. **Retention swept it** — if retention is enabled
   (`retention.py:50`), old payloads can be GC'd. A payload ref exists
   on the span but the row is gone.
6. **Non-last chunk arrived but `last=True` never did** — assembler
   state leaks. Client crashed mid-upload. The ref shows but the
   payload row is absent.
7. **Payload capture disabled for this span kind** — LLM_CALL spans
   require explicit opt-in to capture prompts/responses. If off, the
   ref is empty.

## Diagnostic steps

### 1. Backpressure

```bash
grep 'payloads_evicted' /path/to/agent.log | tail -20
```

Nonzero and climbing → client is dropping payloads under pressure.
Also check `buffered_payload_bytes` vs your configured cap.

### 2. No payload on the span

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, payload_digest FROM spans WHERE id='SPAN_ID';"
```

If `payload_digest` is NULL / empty, the span never carried one.

### 3. Digest mismatch

```bash
grep 'payload digest mismatch' data/harmonograf-server.log | tail -10
```

Then hash the original yourself if you can recover it from the agent
side:

```bash
sha256sum /path/to/tool/output/file
```

If the server's `actual` matches but `declared` doesn't, the client's
hash computation is buggy (or the data was mutated between hashing and
sending).

### 4. GetPayload hang

Browser DevTools → Network tab → filter for `getPayload` — see the
state. Also check sqlite for locking:

```bash
sqlite3 data/harmonograf.db 'PRAGMA busy_timeout;'
# Try a quick read under load:
time sqlite3 data/harmonograf.db 'SELECT COUNT(*) FROM payloads;'
```

### 5. Retention swept it

```bash
grep 'retention swept' data/harmonograf-server.log | tail -10
```

### 6. Incomplete upload

If the payload row is missing AND no mismatch was logged AND the
client died mid-stream, this is the case. Check the client's crash
log around the span's time.

### 7. Capture disabled

Grep for the capture opt-in in client config:

```bash
grep -rn 'capture_prompt\|capture_response\|payload_capture' /path/to/agent_src/
```

## Fixes

1. **Backpressure**: raise the client's payload buffer byte cap,
   reduce payload size (don't dump entire HTML pages), or reduce the
   emission rate.
2. **Span had no payload**: nothing to fix; this is expected.
3. **Digest mismatch**: fix the client hashing bug. Meanwhile, disable
   payload capture for the affected tool until fixed.
4. **GetPayload hang**: see [`sqlite-errors.md`](sqlite-errors.md) for
   locks.
5. **Retention swept**: disable retention, or raise its horizon, or
   accept the loss for old sessions.
6. **Partial upload**: nothing to recover; the agent would need to
   resend. Shorten upload chunks to reduce the window.
7. **Capture disabled**: enable capture for the span kind you need.

## Prevention

- Alert on `payloads_evicted > 0` in heartbeats; this should be rare.
- Keep payload sizes under ~1MB per span; larger items should be
  referenced by URL, not bytes-in-payload.
- Test the GetPayload path under load in CI — it's easy to regress.

## Cross-links

- [`user-guide/troubleshooting.md`](../user-guide/troubleshooting.md)
  §"Payloads are missing" — UI-side counterpart.
- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"A span is
  missing from the UI".
- [`dev-guide/client-library.md`](../dev-guide/client-library.md) for
  the payload buffer design.
