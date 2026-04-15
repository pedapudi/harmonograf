---
name: hgraf-add-storage-backend
description: Add a new persistence backend behind the Store ABC — subclass, factory wiring, conformance tests, cascade deletes, and benchmarking.
---

# hgraf-add-storage-backend

## When to use

The two in-tree backends — `InMemoryStore` (tests, demos) and
`SqliteStore` (default, single-process) — don't fit a deployment need:
multiple writer processes, horizontal read scaling, retention pushed
into the database, or a substrate the team already operates (Postgres,
DuckDB, ClickHouse). Don't add a backend just to swap one engine for
another — add one when SQLite's single-process / single-writer model is
the actual limiter.

## Prerequisites

1. Read `docs/dev-guide/storage-backends.md` end to end. It is the
   canonical contract — every method, every idempotency requirement,
   every bus delta the ingest layer expects.
2. Read `server/harmonograf_server/storage/base.py` for the `Store`
   ABC and the dataclasses your backend will round-trip.
3. Read `server/harmonograf_server/storage/sqlite.py` as the reference
   implementation. It has the same method ordering as the ABC and shows
   the schema, column backfill pattern, payload-on-disk layout, and
   transactional task-plan upsert.
4. Read `server/harmonograf_server/storage/postgres.py` — the stub you
   will copy.

## Step-by-step

### 1. Copy the stub

```bash
cp server/harmonograf_server/storage/postgres.py \
   server/harmonograf_server/storage/mybackend.py
```

Rename the class (`PostgresStore` → `MyBackendStore`) and update the
module docstring.

### 2. Implement every abstract method

`Store` has ~25 abstract methods. The conformance suite exercises all
of them; if your subclass leaves one unimplemented, instantiation will
fail with `TypeError: Can't instantiate abstract class`.

Order of work that minimizes thrash:

1. **`start` / `close` / `ping`.** Open the connection, run
   migrations (or `executescript` an inline schema), enable any
   pragmas you need. `close()` must tolerate double-close.
2. **Sessions.** `create_session` is idempotent — pick a primary key
   and use `INSERT ... ON CONFLICT DO NOTHING` (or equivalent).
   `update_session` **merges** `metadata`, never replaces it.
   `delete_session` is the cascade hub — see step 4.
3. **Agents.** Upsert by `(session_id, agent_id)`. `update_agent_status`
   is a no-op (not an error) when the agent is missing.
4. **Spans.** `append_span` is idempotent on `span.id`. `update_span`
   merges `attributes`. `get_spans` time-window query must include
   open-ended (running) spans whose `end_time` is `NULL` —
   `COALESCE(end_time, start_time) >= ?` is the SQLite trick.
5. **Annotations.** Upsert by id; filter by session or span.
6. **Payloads.** Content-addressed by SHA-256. Decide where bytes
   live (filesystem, object storage, BLOB column) and never mix
   strategies. `gc_payloads` removes anything no span references.
7. **Task plans.** `put_task_plan` **replaces** the task list
   wholesale — re-emitted plans must drop tasks the planner removed.
   Wrap the plan + tasks delete/insert in a transaction.
8. **Context window samples.** Append-only. The tricky part is
   `list_context_window_samples(limit_per_agent=...)`: the cap is per
   agent, not global. Postgres can use a `ROW_NUMBER() OVER
   (PARTITION BY agent_id ...)` window; SQLite scans grouped per
   agent. Pick whichever is idiomatic.
9. **`stats`.** Counts plus disk usage. Memory backend reports `0` for
   disk; SQLite walks the payload directory.

### 3. Wire it into the factory

`server/harmonograf_server/storage/factory.py`:

```python
from harmonograf_server.storage.mybackend import MyBackendStore

StoreKind = Literal["memory", "sqlite", "postgres", "mybackend"]

def make_store(kind: StoreKind, **opts: Any) -> Store:
    ...
    if kind == "mybackend":
        return MyBackendStore(dsn=opts["dsn"], **opts)
    ...
```

Then thread the new kind through `ServerConfig.store_backend` (the
existing literal type lives in `server/harmonograf_server/config.py`)
and `Harmonograf.from_config` in `main.py` if your backend needs custom
kwargs at startup time.

### 4. Cascade deletes — the most common bug

`delete_session(session_id)` must wipe **every** related table:

* spans (and span_links if your backend keeps them separately)
* agents
* annotations
* task_plans + tasks
* context_window_samples
* and decrement payload references / GC orphaned payloads

The conformance test `test_delete_session_cascades` checks all of these
in one call. Forgetting one table is a silent data leak.

If your substrate supports `ON DELETE CASCADE`, declare it in the
schema and let the database do the work. Otherwise issue the deletes
explicitly inside one transaction.

### 5. Idempotent inserts everywhere

The ingest pipeline calls `create_session`, `register_agent`,
`append_span`, `put_payload`, `put_task_plan`, and `put_annotation` on
the hot path and has its own dedup, but a network reconnect or a
client retry can still deliver the same message twice. Every insert
must tolerate duplicates without raising. Use primary keys + `ON
CONFLICT DO NOTHING` (or equivalent), not Python-level `if exists` checks
— the latter race under concurrent writers.

### 6. Run the conformance suite

```bash
uv run --extra e2e --with pytest --with pytest-asyncio \
    python -m pytest tests/storage_conformance_test.py -q
```

To get your backend exercised by the suite, add one entry to
`BACKENDS` at the top of `tests/storage_conformance_test.py`:

```python
BACKENDS: list[BackendSpec] = [
    BackendSpec(name="memory", build=lambda _tmp: make_store("memory")),
    BackendSpec(name="sqlite", build=lambda tmp: make_store("sqlite", db_path=str(tmp / "h.db"))),
    BackendSpec(name="mybackend", build=lambda tmp: make_store("mybackend", dsn=...)),
]
```

That is the only test-side change. Every conformance test will pick
up your new backend automatically. Iterate until all of them pass.

If your backend needs an external service (a real Postgres) the
parametrization should skip it gracefully when the service isn't
available — wrap the `build` lambda in a check or use
`pytest.skip(...)` from the fixture.

### 7. Bus deltas come from ingest, not from the store

Do **not** publish on the `SessionBus` from inside your backend. The
ingest pipeline (`server/harmonograf_server/ingest.py`) is the one and
only publisher. The mapping is in
`docs/dev-guide/storage-backends.md` — if your backend tries to
publish too, you'll get duplicate deltas in the frontend and the
retention sweeper will spam watchers.

### 8. Migration / schema versioning

For any backend with persistent state, ship versioned migrations
before the first production deploy. SQLite gets away with inline
`PRAGMA table_info` + `ALTER TABLE` checks in `start()` because there
is exactly one writer; that pattern doesn't scale to multi-writer
backends. Use alembic, sqitch, or whatever your team already runs.

### 9. Benchmark before you ship

Run the existing demo against your backend to catch perf cliffs that
the conformance suite (which uses tiny datasets) won't:

```bash
HGRAF_STORE_BACKEND=mybackend make demo
```

Watch for:
- N+1 reads in `get_spans` (the SQLite backend has a known one and
  it's still fine — but if your DB has higher per-query overhead, fold
  the link/payload joins into one query).
- Lock contention under concurrent ingest (run two clients).
- Stat call latency — `stats()` is hit on every `/metricsz` scrape.

### 10. Verification

```bash
uv run --extra e2e --with pytest --with pytest-asyncio \
    python -m pytest tests/storage_conformance_test.py server/tests/ -q
make server-test
```

All three should be green before you open a PR.

## Common pitfalls

- **Forgetting a cascade table.** `test_delete_session_cascades` in
  the conformance suite is the single test most likely to catch a
  half-finished new backend. Run it early and often.
- **Replacing instead of merging.** `update_span(attributes=...)` and
  `update_session(metadata=...)` merge, never replace. A backend that
  overwrites silently corrupts running sessions.
- **Storing payload bytes in row storage.** Multi-megabyte BLOBs in a
  Postgres TOAST table or a SQLite row burn IO you didn't budget for.
  Use object storage or a separate filesystem path keyed by digest.
- **Publishing bus deltas from the backend.** That's ingest's job. A
  backend that publishes will produce duplicate frontend updates and
  break tests that mock the bus.
- **Window-function-itis.** `list_context_window_samples` is the only
  query whose shape tempts you to reach for window functions. SQLite
  ducks them with one query per agent and is plenty fast — don't add
  Postgres-isms unless your substrate is Postgres.
- **Skipping idempotency on `put_payload`.** Two clients uploading the
  same payload concurrently is normal. The second `put_payload` must
  be a no-op, not a unique-constraint violation.
- **Closing the store inside a request handler.** `close()` is for
  process shutdown only. Backends with connection pools should let
  the pool live for the process lifetime.
