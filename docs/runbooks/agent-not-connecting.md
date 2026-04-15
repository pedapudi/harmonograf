# Runbook: Agent not connecting

The agent process is running but never appears in the session picker,
or appears as "0 agents" and stays that way.

## Symptoms

- **Server log** — one of:
  - `WARN harmonograf_server.rpc.telemetry: ingest error agent_id=... : Hello.agent_id is required`
  - `grpc.StatusCode.INVALID_ARGUMENT: first TelemetryUp must be Hello`
  - `grpc.StatusCode.INVALID_ARGUMENT: empty telemetry stream; expected Hello`
  - `WARN harmonograf_server.auth: ...` (bearer token rejection)
- **Client log** —
  - `WARN harmonograf_client.transport: transport disconnected: <exc>` on a tight loop
  - `WARN harmonograf_client.transport: transport circuit breaker OPEN after N consecutive failures; cooling down for Mms`
- **UI** — session picker shows `Waiting for agents to connect…` or the
  banner `Server unreachable — showing demo sessions.`

## Immediate checks

```bash
# 1. Is the server port even open?
lsof -nP -iTCP:7531 -sTCP:LISTEN
lsof -nP -iTCP:5174 -sTCP:LISTEN   # gRPC-Web

# 2. What does the server say about recent Hellos?
grep -E 'session created|stream opened|ingest error|StreamTelemetry crashed' \
    data/harmonograf-server.log | tail -40

# 3. What does the client think is happening?
grep -E 'transport disconnected|circuit breaker|Hello|Welcome' \
    /path/to/agent.log | tail -40

# 4. Is there any session row at all?
sqlite3 data/harmonograf.db \
    "SELECT id, title, status, created_at FROM sessions ORDER BY created_at DESC LIMIT 5;"
sqlite3 data/harmonograf.db \
    "SELECT id, session_id, status, last_heartbeat FROM agents ORDER BY last_heartbeat DESC LIMIT 5;"
```

## Root cause candidates (ranked)

1. **Wrong `HARMONOGRAF_SERVER` in agent env** — overwhelmingly the most
   common cause. Default is `127.0.0.1:7531` (native gRPC). If you set it
   to `localhost:5174` by mistake, the native-gRPC client will fail
   because 5174 is the gRPC-Web gateway.
2. **Server not running** — trivial but keep it in the ranking. Process
   died after startup without leaving a loud error.
3. **Auth misconfiguration** — bearer token required by the server
   (`HARMONOGRAF_AUTH_TOKEN` set) but not provided by the client, or vice
   versa. Server log will show `auth: ...` warnings; client log will
   show `UNAUTHENTICATED`.
4. **TLS mismatch** — rare in dev, common in prod. Client is plaintext,
   server is TLS, or the other way around. Manifests as `UNAVAILABLE`
   with underlying HTTP/2 protocol errors on the client side.
5. **Hello shape invalid** — custom embedder forgot to set `agent_id`.
   Server replies with `INVALID_ARGUMENT: Hello.agent_id is required`
   (see `server/harmonograf_server/ingest.py:187`).
6. **Port collision on first boot** — server bound to a port that is
   already in use, failed silently, and the frontend is hitting a
   different process. `lsof -i :7531` on the server host settles this.
7. **Client stuck in circuit-breaker cooldown** — the client opens its
   breaker after `breaker_failure_threshold` consecutive failed
   attempts; until the cooldown elapses it will not even try to connect
   (`client/harmonograf_client/transport.py:334`). If you just fixed the
   server, the client may still be sleeping.
8. **Import-time crash in embedded client** — `harmonograf_client` is
   imported during agent startup; if it raises, the agent never reaches
   the point of calling `Hello`. Look for a Python traceback before any
   transport log line.

## Diagnostic steps

### 1. `HARMONOGRAF_SERVER` wrong

```bash
# On the agent side:
env | grep HARMONOGRAF
# Or inside a Python venv:
python -c 'import os; print(os.environ.get("HARMONOGRAF_SERVER"))'
```

If it points at the gRPC-Web port (5174) or a remote host that isn't
reachable from the agent, you found it.

### 2. Server not running

```bash
ps axf | grep harmonograf_server
# or
systemctl status harmonograf-server
```

### 3. Auth

```bash
# Server side:
grep -i 'auth' data/harmonograf-server.log | tail -20
# Client side:
grep -i 'UNAUTHENTICATED\|PERMISSION_DENIED' /path/to/agent.log | tail -20
```

If either side mentions an authorisation failure, compare
`HARMONOGRAF_AUTH_TOKEN` in both processes' environment.

### 4. TLS

Try the opposite scheme:

```bash
grpcurl -plaintext 127.0.0.1:7531 list
# vs
grpcurl 127.0.0.1:7531 list
```

One should work; whichever it is, the agent must match.

### 5. Hello shape

Grep for `Hello.agent_id is required` or `ingest error`:

```bash
grep -n 'Hello.agent_id is required\|ingest error' data/harmonograf-server.log
```

Then check the agent's identity file
(`harmonograf_client/identity.py`) — if it's empty or corrupt, the
client will send a blank `agent_id`.

### 6. Port collision

```bash
lsof -nP -iTCP:7531
```

If it names a different binary, kill the imposter and restart the
server.

### 7. Circuit breaker cooldown

Look for the exact line:

```
WARN harmonograf_client.transport: transport circuit breaker OPEN after N consecutive failures; cooling down for Mms
```

Wait out the cooldown (default is in `transport.py`; override with
`HarmonografClient(breaker_cooldown_ms=...)`), or restart the agent
process to reset it.

### 8. Import-time crash

Scan the agent log **before** any transport line:

```bash
head -200 /path/to/agent.log
```

A `ModuleNotFoundError: harmonograf_client` or a proto import mismatch
will appear here.

## Fixes

1. **Wrong endpoint** — set `HARMONOGRAF_SERVER=127.0.0.1:7531` in the
   agent env and restart it. For the demo, the default is
   `127.0.0.1:$(SERVER_PORT)` (set by `make demo`, see
   `Makefile:110`).
2. **Server down** — restart via `make server-run` or
   `uv run python -m harmonograf_server --store sqlite --data-dir data`.
3. **Auth** — either unset `HARMONOGRAF_AUTH_TOKEN` on both sides for
   dev, or set the same value on both.
4. **TLS** — disable TLS in dev, or provision matching certs on both
   ends.
5. **Hello shape** — clear the stale identity file (default path is
   inside `$DATA_DIR`); the next agent boot will generate a new one.
6. **Port collision** — kill the squatter and restart; choose a
   different port if the collision is intentional.
7. **Circuit breaker** — restart the agent, or wait for the cooldown.
8. **Import crash** — fix the Python environment. For the demo,
   `uv sync` in the repo root should pull in the client extras.

## Prevention

- Bake `HARMONOGRAF_SERVER` into systemd unit files / container env,
  never rely on shell defaults.
- Have a smoke test on boot: the embedded client logs `stream opened`
  (`ingest.py:233`). If that line is absent 30s after process start,
  alert.
- In CI, run `make demo` against a throwaway dir and assert that
  `SELECT COUNT(*) FROM agents` > 0 within 30 seconds.
- Keep auth config in a single file that both server and agents read
  from, so they can't drift.

## Cross-links

- [`protocol/wire-format.md`](../protocol/wire-format.md) — exact Hello
  framing.
- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §1 "The Gantt is
  empty".
- [`runbooks/agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md)
  — for when the Hello *does* succeed but the stream keeps dying.
