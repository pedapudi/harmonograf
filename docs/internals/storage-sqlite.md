# The SQLite storage backend

`server/harmonograf_server/storage/sqlite.py` (~1160 lines) is the
persistent backing store for every session harmonograf has seen. It
is the only storage implementation used in production; the in-memory
alternative in `memory.py` exists mainly for tests.

The file implements the abstract interface from `base.py` using an
async-friendly wrapper around sqlite3. Every public method is a
coroutine that runs the actual SQL on a thread executor — SQLite is
synchronous, but the ingest pipeline is async, so all calls are
`await`-able.

## Why SQLite

A harmonograf session fans out into thousands of spans, hundreds of
task status transitions, and potentially megabytes of payloads. The
storage requirements are modest per session but add up over time. An
embedded store beats a running Postgres for the common single-user
case: zero ops, atomic on-disk file, works with a file mount, and
the entire schema fits in a single `.db` file you can copy around.

The tradeoffs: no horizontal scaling, single-writer at a time (WAL
helps but doesn't eliminate), and query shapes have to be carefully
planned for the indexes to matter.

## PRAGMAs

Set at the top of `start()` (`sqlite.py:187-190`):

- `PRAGMA journal_mode = WAL` — write-ahead logging. Readers don't
  block writers, and writers don't block readers. This is what
  makes a watch session query storage for replay while ingest is
  actively writing new spans.
- `PRAGMA busy_timeout = 5000` — 5 second timeout on lock
  contention. Protects against stale connections deadlocking the
  startup path. Without this, a stuck reader from a previous
  process can block new writes indefinitely.
- `PRAGMA foreign_keys = ON` — enforce FK constraints, which are
  off by default in SQLite. Required for the ON DELETE CASCADE on
  `tasks.plan_id` to actually fire.
- `PRAGMA synchronous = NORMAL` — fsync on checkpoint, not on every
  write. Balances durability against throughput. FULL would halve
  write throughput; OFF would risk losing the last few writes on a
  power failure. NORMAL loses at most a fraction of a second.

## The schema

Defined at the top of the file as a multi-statement `SCHEMA` string.
Each `CREATE TABLE` corresponds to one entity. Walking through them
with the lines in `sqlite.py`:

### `sessions` (`sqlite.py:45-52`)

Top-level session container.

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL,
    ended_at REAL,
    status TEXT,
    metadata TEXT
);
```

`metadata` is a JSON blob for free-form session attributes. The
primary key is also the wire-visible session id — callers from the
ingest and RPC layers use the same id end to end.

### `agents` (`sqlite.py:54-67`)

```sql
CREATE TABLE IF NOT EXISTS agents (
    id TEXT,
    session_id TEXT,
    name TEXT,
    framework TEXT,
    framework_version TEXT,
    capabilities TEXT,
    metadata TEXT,
    connected_at REAL,
    last_heartbeat REAL,
    status TEXT,
    PRIMARY KEY (id, session_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_agents_session ON agents(session_id);
```

Composite primary key `(id, session_id)` is the subtle part: the
same agent id can appear in multiple sessions, but within a session
it must be unique. Capabilities and metadata are JSON. `status` is a
free-form string — currently CONNECTED / DISCONNECTED / STALE — not
enforced as an enum in SQL because we want to extend it without
schema migration.

### `spans` (`sqlite.py:69-94`)

The biggest table by row count. Full schema:

```sql
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    parent_span_id TEXT,
    kind INTEGER NOT NULL,       -- enum int
    kind_string TEXT,             -- human name for debug
    status INTEGER,
    name TEXT,
    start_time REAL NOT NULL,
    end_time REAL,
    attributes TEXT,              -- JSON
    payload_digest TEXT,
    payload_mime TEXT,
    payload_size INTEGER,
    payload_summary TEXT,
    payload_role TEXT,
    payload_evicted INTEGER,
    error TEXT                    -- JSON error shape
);
CREATE INDEX IF NOT EXISTS idx_spans_sa_time
    ON spans(session_id, agent_id, start_time);
CREATE INDEX IF NOT EXISTS idx_spans_s_time
    ON spans(session_id, start_time);
CREATE INDEX IF NOT EXISTS idx_spans_payload_digest
    ON spans(payload_digest);
```

Three indexes cover the hot query patterns:

- `(session_id, agent_id, start_time)` — per-agent window queries
  from the WatchSession replay. This is the index the renderer's
  burst replay depends on.
- `(session_id, start_time)` — session-wide window queries.
- `(payload_digest)` — reference counting for payload GC.

The duplicated `kind` / `kind_string` columns are not normalized on
purpose: `kind` is the enum int used in queries and indexes,
`kind_string` is the debuggable name you see in SQL clients. The
cost of the extra column is trivial compared to the friction of
joining an enum lookup table during manual debugging.

The `payload_*` columns were added in a later migration and
initially did not exist — see the migration pattern below.

### `span_links` (`sqlite.py:96-102`)

```sql
CREATE TABLE IF NOT EXISTS span_links (
    span_id TEXT,
    target_span_id TEXT,
    relation TEXT,
    target_agent_id TEXT,
    PRIMARY KEY (span_id, target_span_id, relation)
);
```

Cross-span references — e.g. a TRANSFER span points at the
INVOCATION it transferred into. Composite PK deduplicates.

### `annotations` (`sqlite.py:104-118`)

User-authored annotations. Indexed by `session_id` and
`target_span_id` so the frontend can load all annotations for a
session or for a specific span cheaply.

### `task_plans` (`sqlite.py:120-133`)

```sql
CREATE TABLE IF NOT EXISTS task_plans (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    invocation_span_id TEXT,
    planner_agent_id TEXT,
    created_at REAL,
    summary TEXT,
    edges TEXT,                   -- JSON array
    revision_reason TEXT,
    revision_kind TEXT,
    revision_severity TEXT,
    revision_index INTEGER
);
```

The `revision_*` columns are later additions (see migration pattern)
and hold the drift-context metadata that refine attaches. `edges` is
stored as a JSON array rather than as a separate table because
edges are always loaded with the plan and never queried
independently — a join would be wasted work.

### `tasks` (`sqlite.py:135-148`)

```sql
CREATE TABLE IF NOT EXISTS tasks (
    plan_id TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    assignee_agent_id TEXT,
    status TEXT,
    predicted_start_ms REAL,
    predicted_duration_ms REAL,
    bound_span_id TEXT,
    PRIMARY KEY (plan_id, id),
    FOREIGN KEY (plan_id) REFERENCES task_plans(id) ON DELETE CASCADE
);
```

**The cascade delete on `plan_id` is the only FK cascade in the
schema.** Deleting a plan row removes every task row automatically —
the task-plans have a parent/child relationship and we never want
orphaned tasks. Everything else is cleaned up by explicit DELETE
statements in `delete_session` (see below).

### `context_window_samples` (`sqlite.py:150-158`)

```sql
CREATE TABLE IF NOT EXISTS context_window_samples (
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    tokens INTEGER,
    limit_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_context_samples_sar
    ON context_window_samples(session_id, agent_id, recorded_at);
```

Append-only time series per agent. The index supports the
WatchSession replay query that reads a window of samples for each
agent.

### `payloads` (`sqlite.py:160-166`)

```sql
CREATE TABLE IF NOT EXISTS payloads (
    digest TEXT PRIMARY KEY,
    size INTEGER,
    mime TEXT,
    summary TEXT,
    path TEXT
);
```

Content-addressable payload metadata. The `path` column is the
on-disk location of the payload bytes — payloads above a threshold
live as files on disk rather than in the SQLite database, because
large blobs fragment the SQLite file. Small payloads are inlined.

## Migration pattern

`start()` at `sqlite.py:179-223` executes the base SCHEMA first
(which is idempotent thanks to `IF NOT EXISTS`), then runs
additive migrations. Each migration uses the idempotent
"check-then-alter" pattern:

```python
cols = {r[1] for r in cur.execute("PRAGMA table_info(spans)").fetchall()}
if "payload_digest" not in cols:
    cur.execute("ALTER TABLE spans ADD COLUMN payload_digest TEXT")
if "payload_mime" not in cols:
    cur.execute("ALTER TABLE spans ADD COLUMN payload_mime TEXT")
# ... and so on
```

There is no migration version table. The absence/presence of each
column is the versioning signal. This works because every migration
so far has been purely additive (ADD COLUMN, CREATE INDEX IF NOT
EXISTS). A structural migration — splitting a table, dropping a
column, renaming — would need something heavier, but we've avoided
that so far on purpose.

`sqlite.py:195-203` handles the `payload_*` column additions and
`sqlite.py:205-222` handles the `revision_*` columns on
`task_plans`. If you add a new migration, follow the same pattern:
query PRAGMA, check the column, alter if missing. Do not assume
migrations run in any order or that the previous process applied
them — reboots and crashes mean any starter might be the first to
run a given migration.

## Query patterns

### Per-agent, per-window span fetch

`get_spans(session_id, agent_id=None, time_start=None, time_end=None, limit=None)`
at `sqlite.py:664-696`. The core query:

```sql
SELECT ... FROM spans
WHERE session_id = ?
  AND (agent_id = ? OR ? IS NULL)
  AND (start_time <= ? OR ? IS NULL)
  AND (COALESCE(end_time, start_time) >= ? OR ? IS NULL)
ORDER BY start_time
```

The `COALESCE(end_time, start_time) >= ?` condition
(`sqlite.py:681-683`) is subtle but important: a span that started
before the window end but is still open (`end_time IS NULL`) should
still be included. Without the coalesce, live spans would be
excluded from replay and the frontend would see a gap.

The `idx_spans_sa_time` index on `(session_id, agent_id, start_time)`
covers the per-agent case. The session-wide case uses
`idx_spans_s_time`.

### Per-agent context window samples

`list_context_window_samples()` at `sqlite.py:1068-1123` has a
quirk: it takes a global `limit` but applies it per-agent rather
than globally. For each agent, it runs a separate query with
`LIMIT ?` (`sqlite.py:1100-1111`). This is a conscious tradeoff —
a global LIMIT with `ROW_NUMBER() OVER (PARTITION BY agent_id)`
would work but requires SQLite 3.25+, and more importantly, the
per-agent loop is simpler to reason about and the performance cost
is negligible for the 5-20 agents per session that are typical.

## Cascade delete paths

`delete_session(session_id)` at `sqlite.py:341-376` is the session
cleanup path. Because there's only one FK cascade in the schema,
most of the cleanup is explicit DELETE statements in a fixed
order:

1. Collect payload digests referenced by the session's spans
   (`sqlite.py:343-347`) so they can be GC'd afterwards.
2. `DELETE FROM span_links WHERE span_id IN (session's spans)`
   (`sqlite.py:355`).
3. `DELETE FROM spans WHERE session_id = ?` (`sqlite.py:358`).
4. `DELETE FROM annotations WHERE session_id = ?` (`sqlite.py:359`).
5. `DELETE FROM agents WHERE session_id = ?` (`sqlite.py:360`).
6. `DELETE FROM tasks WHERE plan_id IN (session's plans)`
   (`sqlite.py:362`) — could be skipped because of the FK cascade,
   but doing it explicitly means we don't depend on the cascade
   firing correctly.
7. `DELETE FROM task_plans WHERE session_id = ?`
   (`sqlite.py:365-366`).
8. `DELETE FROM context_window_samples WHERE session_id = ?`
   (`sqlite.py:368-370`).
9. `DELETE FROM sessions WHERE id = ?` (`sqlite.py:372`).
10. `gc_payloads()` for the orphaned digests (`sqlite.py:374-375`).

The order matters only for FK integrity when `foreign_keys = ON` —
you must delete from the child tables before the parent. The
explicit order makes the constraint obvious in the code rather
than relying on the schema to enforce it.

`gc_payloads()` at `sqlite.py:848-869` walks the `payloads` table
and removes any rows whose `digest` is not referenced by any live
span. Payload file cleanup on disk is handled inside the same
method.

## Public method surface

Full list with line numbers:

- `start()` — `sqlite.py:179-223`
- `close()` — `sqlite.py:225-228`
- `create_session(session)` — `sqlite.py:237-254`
- `get_session(session_id)` — `sqlite.py:256-258`
- `list_sessions(status=None, limit=None)` — `sqlite.py:285-307`
- `update_session(...)` — `sqlite.py:309-339`
- `delete_session(session_id)` — `sqlite.py:341-376`
- `register_agent(agent)` — `sqlite.py:399-429`
- `get_agent(session_id, agent_id)` — `sqlite.py:452-454`
- `list_agents_for_session(session_id)` — `sqlite.py:456-468`
- `update_agent_status(...)` — `sqlite.py:470-488`
- `append_span(span)` — `sqlite.py:491-534`
- `update_span(...)` — `sqlite.py:584-634`
- `end_span(span_id, end_time, status, error=None)` — `sqlite.py:636-658`
- `get_span(span_id)` — `sqlite.py:660-662`
- `get_spans(...)` — `sqlite.py:664-696`
- `put_annotation(annotation)` — `sqlite.py:699-726`
- `list_annotations(...)` — `sqlite.py:751-773`
- `put_payload(digest, data, mime, summary="")` — `sqlite.py:779-805`
- `get_payload(digest)` — `sqlite.py:807-829`
- `has_payload(digest)` — `sqlite.py:831-836`
- `gc_payloads()` — `sqlite.py:848-869`
- `put_task_plan(plan)` — `sqlite.py:872-945`
- `get_task_plan(plan_id)` — `sqlite.py:991-993`
- `list_task_plans_for_session(session_id)` — `sqlite.py:995-1009`
- `update_task_status(plan_id, task_id, status, bound_span_id=None)` — `sqlite.py:1011-1045`
- `append_context_window_sample(sample)` — `sqlite.py:1048-1066`
- `list_context_window_samples(...)` — `sqlite.py:1068-1123`
- `stats()` — `sqlite.py:1126-1159`

## Gotchas

- **Do not remove the busy_timeout PRAGMA.** Without it, a stuck
  reader (from a crashed previous process, or from a stale ORM
  tool pointing at the db file) can block startup forever.
- **Do not switch `journal_mode` away from WAL.** The live-replay
  pattern — read during write — requires WAL. DELETE mode would
  block the watcher's initial burst queries.
- **Do not add a migration that drops or renames a column.** The
  versionless migration pattern only handles additive changes.
  Breaking that requires introducing a proper schema_version table.
- **Do not rely on the cascade delete for cleanup.** It only fires
  for `tasks.plan_id`. Everything else needs explicit DELETEs in
  the right order. If you add a new table, add it to
  `delete_session` too.
- **Do not load a session's spans unbounded.** Always pass a time
  window or a limit. A full session scan will load tens of
  thousands of rows and serialize every attribute blob.
- **The `(session_id, agent_id, start_time)` index is load-bearing
  for WatchSession replay.** Dropping or restructuring it will
  silently degrade replay performance from log(n) to linear scan.
- **`payload_digest` is nullable.** A span without a payload has
  NULL in that column. The payload GC must skip NULLs, and the
  payload-digest index does not prevent NULL entries.
