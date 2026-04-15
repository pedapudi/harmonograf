# Storage backends

Harmonograf's persistence layer is a single interface — `Store` — with
pluggable concrete backends. Two ship in-tree:

| Backend          | When to use                                                            |
|------------------|-------------------------------------------------------------------------|
| `InMemoryStore`  | Unit tests; ephemeral demos. Lost on process exit.                      |
| `SqliteStore`    | Default for `harmonograf-server`. Single-process; on-disk; WAL.         |

A `PostgresStore` *stub* lives at
`server/harmonograf_server/storage/postgres.py` as a copy-and-fill
template for contributors. The factory raises a helpful error when
asked for `kind="postgres"` so that misconfigurations fail loudly
rather than silently falling back.

This document is the canonical contract. If your backend passes
`tests/storage_conformance_test.py` and publishes the deltas this doc
describes from the ingest pipeline, the rest of the server (frontend
RPCs, retention sweeper, gRPC-Web watchers) will work against it
unchanged.

## Two-layer design

The persistence layer is intentionally split into two files:

* **`storage/base.py` — data model + interface.**
  Wire-independent dataclasses (`Session`, `Agent`, `Span`,
  `Annotation`, `PayloadMeta`, `PayloadRecord`, `Task`, `TaskPlan`,
  `ContextWindowSample`, `Stats`) plus the abstract `Store` class. No
  generated proto types appear here — the ingest layer
  (`server/harmonograf_server/ingest.py` via
  `server/harmonograf_server/convert.py`) is responsible for translating
  between proto messages and these in-memory shapes. Backends only ever
  see these dataclasses.

* **Concrete backend modules** (`memory.py`, `sqlite.py`,
  `postgres.py`). Each subclasses `Store` and implements every
  abstract method. Backends own all serialization decisions: the
  SQLite backend stores attributes/links as JSON columns, the in-memory
  backend keeps them as live Python objects.

The seam matters: it lets you change the on-disk format (or move to
another database) without touching ingest, the bus, or the frontend
RPCs. It also lets the conformance suite be written once.

## Lifecycle

```python
store = make_store("sqlite", db_path="harmonograf.db")
await store.start()   # open files / pools / run migrations
try:
    ...
finally:
    await store.close()  # idempotent — ok to call twice
```

`start()` is allowed to be heavy (open a connection, create tables,
run migrations). `close()` should be safe to call from a signal
handler. Neither is called from the hot path; ingest assumes the store
is started.

A single process owns exactly one `Store` instance. It is shared
across every gRPC stream, every gRPC-Web RPC, the retention sweeper,
and the metrics loop. Implementations must therefore be **safe for
concurrent async callers**. Both in-tree backends use a single
`asyncio.Lock` to serialize mutations; that's enough for one process.
A multi-process backend (Postgres, etc.) should use a real connection
pool and either rely on the database for serialization or take its own
locks.

## Method reference

Signatures here are the source of truth. If they drift from
`storage/base.py`, the code wins — please update this doc.

### `start() / close() / ping()`

| Method                    | Returns | Notes                                                                                       |
|---------------------------|---------|---------------------------------------------------------------------------------------------|
| `start() -> None`         |         | Open underlying handle. Idempotent in spirit; called once at startup.                       |
| `close() -> None`         |         | Release handles. Tolerant of double-close.                                                  |
| `ping() -> bool`          | bool    | Trivial readiness probe. Default returns `True`. SQLite overrides with `SELECT 1`.          |

`ping()` backs the `/readyz` endpoint. Backends that can detect a
broken connection should override it; otherwise the default suffices.

### Sessions

| Method | Semantics |
|---|---|
| `create_session(session)` | Insert if absent. **Idempotent**: re-inserting the same `id` returns the existing row unchanged. |
| `get_session(session_id)` | `Optional[Session]`; `None` if missing. |
| `list_sessions(status=None, limit=None)` | Newest-first by `created_at`; optional `status` filter and `limit`. |
| `update_session(session_id, *, title?, status?, ended_at?, metadata?)` | Partial update; **`metadata` is merged**, not replaced. Returns the new row, or `None` if missing. |
| `delete_session(session_id)` | Cascade-delete spans, agents, annotations, task plans, and context-window samples for the session. Returns `False` if the session was unknown. |

### Agents

| Method | Semantics |
|---|---|
| `register_agent(agent)` | **Upsert by `(session_id, agent_id)`.** Re-registration overwrites mutable fields and bumps `last_heartbeat`. |
| `get_agent(session_id, agent_id)` | `Optional[Agent]`. |
| `list_agents_for_session(session_id)` | All agents for a session, ordered by `connected_at`. |
| `update_agent_status(session_id, agent_id, status, last_heartbeat=None)` | No-op if the agent doesn't exist (does *not* raise). |

### Spans

| Method | Semantics |
|---|---|
| `append_span(span)` | Insert. **Idempotent on `span.id`** — supports reconnect/replay where the same span arrives twice. Second call returns the existing row, *not* the new one. |
| `update_span(span_id, *, status?, attributes?, payload_*?, error?)` | Partial update. **`attributes` is merged**, not replaced. Returns `None` if missing. |
| `end_span(span_id, end_time, status, error=None)` | Sets terminal state. Returns `None` if missing. |
| `get_span(span_id)` | `Optional[Span]`. |
| `get_spans(session_id, agent_id?, time_start?, time_end?, limit?)` | Time-window query, ordered by `start_time`. A span overlaps the window iff `start_time <= time_end AND coalesce(end_time, start_time) >= time_start`. Open-ended (running) spans are included. |

### Annotations

| Method | Semantics |
|---|---|
| `put_annotation(annotation)` | Upsert by id. |
| `list_annotations(session_id?, span_id?)` | Filter by either; ordered by `created_at`. |

### Payloads (content-addressed)

| Method | Semantics |
|---|---|
| `put_payload(digest, data, mime, summary="")` | Idempotent on `digest`. SHA-256 collisions are the only way to clobber. |
| `get_payload(digest)` | `Optional[PayloadRecord]`. |
| `has_payload(digest)` | `bool`. |
| `gc_payloads()` | Delete payloads no span references. Returns the count evicted. Safe to call any time. |

The SQLite backend stores bytes on disk under `payload_dir/{digest[:2]}/{digest}`
to keep the database file small. The memory backend keeps bytes in a
dict and refcounts via `_decref_payload`. A new backend should pick a
strategy that matches its substrate (S3 for a Postgres backend, etc.) —
just keep the digest as the only stable identifier.

### Task plans

| Method | Semantics |
|---|---|
| `put_task_plan(plan)` | **Upsert by `plan.id`. Replaces the task list wholesale** so re-emitted plans cleanly drop tasks the planner removed. |
| `get_task_plan(plan_id)` | `Optional[TaskPlan]`. |
| `list_task_plans_for_session(session_id)` | Ordered by `created_at`. |
| `update_task_status(plan_id, task_id, status, bound_span_id=None)` | Returns the updated `Task`, or `None` if the plan/task doesn't exist. |

### Context window samples

| Method | Semantics |
|---|---|
| `append_context_window_sample(sample)` | Append-only. The series stays signal-only — ingest filters out zero-valued samples before calling. |
| `list_context_window_samples(session_id, agent_id?, limit_per_agent=200)` | If `agent_id` is set, returns the newest `limit_per_agent` for that agent. If not, returns the newest `limit_per_agent` *per agent*, sorted by `(agent_id, recorded_at)`. |

The "newest N per agent" shape matters: a multi-agent session can run
for hours and the frontend only needs the recent shape of each agent's
context curve.

### Stats

| Method | Semantics |
|---|---|
| `stats() -> Stats` | Snapshot counts: sessions, agents, spans, payloads, payload bytes, total disk usage (0 for in-memory). |

## How the factory dispatches

`server/harmonograf_server/storage/factory.py` is a tiny dispatcher:

```python
def make_store(kind: StoreKind, **opts) -> Store:
    if kind == "memory":   return InMemoryStore()
    if kind == "sqlite":   return SqliteStore(db_path=opts["db_path"], ...)
    if kind == "postgres": raise NotImplementedError(...)  # stub
    raise ValueError(f"unknown store kind: {kind}")
```

Server startup (`server/harmonograf_server/main.py`,
`Harmonograf.from_config`) reads `cfg.store_backend` from
`ServerConfig`, builds the matching kwargs (path expansion, payload
directory), calls `make_store`, and then `await store.start()`. The
result is held on the `Harmonograf` instance for the lifetime of the
process. Backends should treat the constructor as cheap — heavy work
goes in `start()`.

## How the rest of the server uses the store

* **Ingest pipeline** (`server/harmonograf_server/ingest.py`,
  `IngestPipeline`) is the busiest writer. For each
  `TelemetryUp` message, it:

  1. Translates proto → storage dataclasses via `convert.py`.
  2. Calls a single `Store` method (`create_session`,
     `register_agent`, `append_span`, `update_span`, `end_span`,
     `put_payload`, `update_agent_status`,
     `append_context_window_sample`, `put_task_plan`,
     `update_task_status`).
  3. Publishes a corresponding delta on the `SessionBus` so live
     gRPC-Web watchers see the change.

  The map from store call → bus publish is:

  | Store call                             | Bus delta |
  |----------------------------------------|-----------|
  | `register_agent`                       | `publish_agent_upsert` |
  | `update_agent_status`                  | `publish_agent_status` |
  | `append_span`                          | `publish_span_start` |
  | `update_span`                          | `publish_span_update` |
  | `end_span`                             | `publish_span_end` |
  | `put_task_plan`                        | `publish_task_plan` |
  | `update_task_status`                   | `publish_task_status` |
  | `append_context_window_sample`         | `publish_context_window_sample` |
  | `put_annotation` (frontend RPC path)   | `publish_annotation` |

  **The bus publish is ingest's job, not the store's.** A backend
  must not call the bus directly — that coupling would make
  conformance testing impossible and would prevent the same store
  from being reused in tests, the retention sweeper, or one-off
  CLI tooling.

* **Frontend RPCs** (`server/harmonograf_server/rpc/frontend.py`)
  call read-only methods (`get_session`, `list_sessions`, `get_spans`,
  `list_annotations`, `list_task_plans_for_session`,
  `list_context_window_samples`, `get_payload`, `stats`) plus the
  `put_annotation` mutator.

* **Retention sweeper** (`retention.py`) periodically calls
  `list_sessions(status=COMPLETED)` and `delete_session` for ones older
  than the configured window, then `gc_payloads()` as a safety sweep.

* **Health** (`health.py`) calls `ping()` on every `/readyz`.

## When to reach for a new backend

The two in-tree backends cover most needs:

* **`memory`** — unit tests, single-process demos, anywhere lifetime
  is bounded by the process.
* **`sqlite`** — the default. WAL mode, single writer, single process.
  Easily handles thousands of sessions with millions of spans on a
  laptop. The on-disk format is the same one `harmonograf-server` uses
  in production for single-node deployments.

Reach for a new backend when:

* You need **multiple processes** to read and write the same dataset
  (HA server, a worker that builds rollups offline). SQLite's WAL
  serializes writers to one process; a second writer will block.
* You need **horizontal read scaling** for very large historical
  datasets (Postgres read replicas, ClickHouse, DuckDB on object
  storage).
* You want to push retention/aggregation to the database (window
  functions, materialized views) rather than computing it in Python.

Don't add a new backend just to use a "real" database — SQLite is a
real database. Add one when you have a concrete operational need.

## Pitfalls and invariants

* **Idempotent inserts everywhere.** `create_session`, `register_agent`,
  `append_span`, `put_payload`, `put_task_plan`, `put_annotation` are
  all called from the ingest hot path and must tolerate duplicates.
  The cheapest dedup is a primary key + `INSERT ... ON CONFLICT DO
  NOTHING` (sqlite) or a `dict` membership check (memory). The
  conformance suite tests this for you.

* **Cascade deletes.** `delete_session` must wipe **every** related
  row (spans, span links, annotations, agents, task plans, tasks,
  context window samples) and then either drop or refcount-decrement
  payloads. The conformance test (`test_delete_session_cascades`)
  enforces this. Forgetting one table is the most common bug in a new
  backend.

* **Payload bytes vs row storage.** `Span` rows reference payloads by
  digest only; the bytes live in a separate keyspace. Decide once,
  early, where to put the bytes. SQLite picks the local filesystem.
  Memory keeps them on the heap. A Postgres backend will probably
  want object storage (S3/GCS) — putting multi-megabyte BLOBs in a
  Postgres TOAST table burns IO you didn't budget for.

* **Backfill semantics.** `SqliteStore.start()` does in-place column
  backfills via `PRAGMA table_info` checks (look for the
  `payload_*` and `revision_*` ALTERs). Any backend with persistent
  state needs a migration story before its first production deploy.
  For Postgres, prefer alembic over inline ALTERs.

* **`append_context_window_sample` shape.** Samples are an
  append-only series scoped to `(session_id, agent_id)`. The unusual
  shape is in `list_context_window_samples`: the `limit_per_agent`
  parameter is *per agent*, not global. SQLite implements this with
  one query per agent (`SELECT DISTINCT agent_id` then a per-agent
  `LIMIT`) because window functions aren't worth the complexity here.
  Postgres can use `ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY
  recorded_at DESC)` and a single query.

* **Attribute merging.** `update_span(attributes=...)` and
  `update_session(metadata=...)` both **merge**, never replace. A
  backend that overwrites the dict will silently corrupt running
  sessions — and the bug only shows up under load when two updates
  race in the wrong order. The conformance suite tests this.

* **Open-ended span queries.** `get_spans(time_start=..., time_end=...)`
  must include spans whose `end_time` is `NULL` if `start_time` is
  inside the window. SQLite uses
  `COALESCE(end_time, start_time) >= ?`; the in-memory backend uses
  an `IntervalTree` with a near-zero-width interval for live spans.

## Testing

`tests/storage_conformance_test.py` is the canonical conformance
suite. It runs every test against every backend in `BACKENDS` via a
parametrized fixture. Adding a third backend means appending one
`BackendSpec` to that list.

Run just the conformance tests with:

```bash
uv run --extra e2e --with pytest --with pytest-asyncio \
    python -m pytest tests/storage_conformance_test.py -q
```

`server/tests/storage_test.py` contains the older
backend-parametrized smoke tests that pre-date this conformance
module; they're kept around as integration-level coverage but new
contract checks should land in `tests/storage_conformance_test.py`.

See also the `hgraf-add-storage-backend` skill for a step-by-step
walkthrough.
