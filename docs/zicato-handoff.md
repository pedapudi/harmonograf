# Zicato → Harmonograf handoff

[zicato](https://github.com/pedapudi/zicato) runs multi-agent systems
under goldfive as a competition: many runs, scored against each other,
surfaced on zicato's own bracket dashboard. Each run is one goldfive
execution and writes a goldfive `events.jsonl` — the
`goldfive.v1.Event` envelope, one JSON line per event — alongside the
run on disk:

```
.zicato/epochs/<epoch>/generations/<gen>/runs/<run-slug>/events.jsonl
```

Harmonograf is the **execution view** of a single one of those runs:
the Gantt timeline, the plan/task/drift story, the intervention
history. This document is the contract for deep-linking the two:
zicato's dashboard shows the *bracket* across runs; clicking one run
opens *that run's temporal trace* in harmonograf.

> **Conceptual split.** Harmonograf renders **one run over time** —
> spans, tasks, drift, refine attempts on a timeline. Zicato's own
> dashboard renders **many runs against each other** — the
> competition/bracket view. They are complementary, not redundant:
> zicato never grows a Gantt, harmonograf never grows a bracket.

---

## How harmonograf ingests goldfive events

Harmonograf has one ingest contract and three producers feeding it:

1. **Live gRPC stream** — an agent runs under `goldfive.wrap(...)` with a
   `HarmonografSink`; the sink ships every `goldfive.v1.Event` over the
   `StreamTelemetry` bidi RPC as it happens. This is the `make demo`
   path. See [`goldfive-integration.md`](goldfive-integration.md).
2. **Standalone spans** — any Python process emits spans directly via
   `harmonograf_client.Client`. No orchestration. See
   [`standalone-observability.md`](standalone-observability.md).
3. **File replay** — a *finished* run's `events.jsonl` replayed from
   disk. This is the zicato handoff path, described below.

All three converge on the same server-side `IngestPipeline` and the
same storage, so a replayed run is indistinguishable from a live one
once it lands: same Gantt, same task strip, same Trajectory view.

The translation logic that turns a `goldfive.v1.Event` into harmonograf
spans + deltas lives entirely in `HarmonografSink` (LLM-call events →
spans, agent-id canonicalization, dict-envelope handling). Replay
**reuses that sink unchanged** — it is just a non-live producer.

---

## Replaying a run: `harmonograf-replay`

`harmonograf-replay` is a console script (installed with
`harmonograf-client`) that reads a goldfive `events.jsonl` and feeds
every event through `HarmonografSink` into a running harmonograf
server.

```bash
# 1. Start a harmonograf server (sqlite store keeps the run after exit)
harmonograf-server --store sqlite --data-dir ~/.harmonograf/data

# 2. Replay a zicato run's events.jsonl into it
harmonograf-replay \
  .zicato/epochs/2026-05-15_e0/generations/v0/runs/transformers_lay_audience/events.jsonl

# 3. Open the harmonograf frontend; the run is now a session in the picker.
```

Output:

```
replayed .../transformers_lay_audience/events.jsonl
  emitted=116 (proto=107, dict=9) skipped=13 (unparseable=0, empty_payload=13)
  session: 5fa857509c8849bcb51288a0bd3e5338
  open:    harmonograf frontend → /session/5fa857509c8849bcb51288a0bd3e5338
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--server` | `127.0.0.1:7531` | harmonograf server gRPC address |
| `--title` | `replay: <run-dir-name>` | session title in the picker |
| `--agent-name` | `zicato-replay` | display name for the replay client's row |
| `--token` | _(none)_ | bearer token if the server has `--auth-token` |
| `--flush-timeout` | `15.0` | seconds to drain the buffer before exit |

### What replay accepts

The two on-disk event shapes goldfive writes are both handled — the same
two shapes `HarmonografSink.emit` already routes apart:

- **proto-JSON** — the protobuf JSON mapping of `goldfive.v1.Event`
  (camelCase keys, payload oneof at top level). Parsed with
  `ignore_unknown_fields=True`, so an `events.jsonl` written by a
  **newer goldfive than the harmonograf submodule pin** still replays:
  unknown event kinds are dropped, known ones render. (Concretely, the
  real zicato runs carry a `steeringDecisionMade` event kind newer than
  the pinned goldfive; replay skips it cleanly and renders the other
  ~116 events per run.)
- **dict envelope** — a JSON object with top-level `kind` + `payload`
  keys (`refine_attempted`, `refine_failed`, …). Passed to the sink
  verbatim; the sink owns the dict→proto translation.

Corrupt JSON lines and blank lines are skipped, never fatal — a single
bad line must not abort a long run's replay.

### Session identity

The replayed session id is the **run id carried in the events
themselves** (`Event.session_id`). For a zicato run that is the goldfive
run id, e.g. `5fa857509c8849bcb51288a0bd3e5338`. This is deliberate:

- The session is **stable and addressable** — replay the same file
  twice and it lands on the same session (ingest is idempotent on
  `(session_id, run_id, sequence)`).
- Zicato already knows the run id, so it can construct the harmonograf
  link **without** a round-trip — it does not need to replay first and
  read back an assigned id.

---

## The deep-link contract

For zicato's dashboard to deep-link a run into harmonograf, two things
must line up: the run's events must be **ingested** into a harmonograf
server, and the dashboard must point at the **session whose id is the
run id**.

### URL shape

Harmonograf's frontend is served at a single origin (the Vite dev
server at `http://127.0.0.1:5173` in `make demo`; a static deploy
otherwise). The session-scoped deep link is:

```
<harmonograf-frontend-origin>/#/session/<run-id>
```

e.g. `http://127.0.0.1:5173/#/session/5fa857509c8849bcb51288a0bd3e5338`

The `<run-id>` segment is exactly the goldfive run id zicato already
has. Zicato builds the link by string concatenation; no harmonograf API
call is needed.

> **Current state — honest note.** The harmonograf frontend uses a hash
> router that today recognises `#/` (the Shell) and `#/stress` (the dev
> stress harness). Session selection is in-app zustand state
> (`uiStore.currentSessionId`), set by the session picker — it is **not
> yet** parsed from the URL hash. So today the deep link lands the
> operator on the Shell and they pick the run (titled
> `replay: <run-slug>`) from the session picker. Wiring `#/session/<id>`
> to call `setCurrentSession(id)` on load is a small, isolated frontend
> change tracked as a follow-up; the `/#/session/<run-id>` shape above
> is the agreed target so zicato can emit the final URL now and have it
> resolve once that lands. Until then the run id is the **session id**
> the operator selects.

### What zicato must emit for the link to resolve

For a deep link to land on a real session, the run's events must have
been ingested. Zicato has two integration options:

1. **Replay on demand.** Zicato's dashboard shells out to
   `harmonograf-replay <run>/events.jsonl --server <addr>` (or calls
   `harmonograf_client.replay.replay_events` in-process) the first time
   a run is opened, then redirects to `/#/session/<run-id>`. Simplest;
   no harmonograf changes.
2. **Replay ahead of time.** A zicato post-run hook replays each run's
   `events.jsonl` into a long-lived harmonograf server (`--store
   sqlite`) as soon as the run finishes. The dashboard link then
   resolves immediately with no shell-out.

Either way, the only hard requirement on zicato is:

- **Emit a real goldfive `events.jsonl`** per run — the
  `goldfive.v1.Event` envelope, one JSON line per event, in `sequence`
  order. Zicato already does this; it is goldfive's recorder output.
- **Stamp `Event.session_id`** (goldfive does this since goldfive#155).
  This is what makes the replayed session addressable by run id.
- **Know the harmonograf frontend origin** to build the link.

Nothing in the event payloads is zicato-specific — harmonograf renders
a zicato run exactly as it renders a `make demo` run.

---

## Programmatic replay

`harmonograf-replay` is a thin CLI over `harmonograf_client.replay`. To
replay in-process (e.g. from a zicato hook):

```python
import asyncio
from pathlib import Path
from harmonograf_client import Client, HarmonografSink
from harmonograf_client.replay import replay_events

async def push_run(events_path: str, server: str) -> str:
    client = Client(name="zicato-replay", server_addr=server)
    sink = HarmonografSink(client)
    try:
        stats = await replay_events(Path(events_path), sink)
    finally:
        await sink.close()
        client.shutdown(flush_timeout=15.0)
    return client.session_id  # == the run id; use it to build the deep link
```

---

## Related docs

- [`goldfive-integration.md`](goldfive-integration.md) — the live
  `goldfive.wrap` + `HarmonografSink` path.
- [`standalone-observability.md`](standalone-observability.md) — the
  non-goldfive span-only path.
- [`operator-quickstart.md`](operator-quickstart.md) — server flags,
  retention, auth.
