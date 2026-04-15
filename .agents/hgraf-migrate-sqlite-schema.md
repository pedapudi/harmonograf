---
name: hgraf-migrate-sqlite-schema
description: Add a column or table to the sqlite store without nuking existing data — the idempotent PRAGMA + ALTER TABLE pattern used throughout sqlite.py.
---

# hgraf-migrate-sqlite-schema

## When to use

You are adding a column, index, or table to `server/harmonograf_server/storage/sqlite.py` and need existing databases to keep working without manual intervention. Harmonograf has no migration framework — the pattern is idempotent startup guards.

## Prerequisites

1. Read `server/harmonograf_server/storage/sqlite.py:45-168` (`SCHEMA` constant) — the canonical create-table statements, all guarded with `CREATE TABLE IF NOT EXISTS`.
2. Read `server/harmonograf_server/storage/sqlite.py:180-224 SqliteStore.start()` — the live migration block with `PRAGMA table_info` + conditional `ALTER TABLE`.
3. Understand: fresh DBs run `SCHEMA`, existing DBs also run `SCHEMA` (idempotently) then hit the guard block. Both paths must produce an identical final schema.

## Step-by-step

### 1. Add the column to the canonical `SCHEMA`

Edit the `CREATE TABLE` block in `sqlite.py:45`:

```sql
CREATE TABLE IF NOT EXISTS spans (
    ...
    payload_evicted INTEGER NOT NULL DEFAULT 0,
    memory_rank INTEGER NOT NULL DEFAULT 0,  -- new column
    error TEXT
);
```

New columns **must** have a `DEFAULT` (proto3 zero-value is the obvious choice) and typically `NOT NULL`. Without a default, `ALTER TABLE` on existing rows will fail.

If you are adding an index, append `CREATE INDEX IF NOT EXISTS ...` after the table body.

### 2. Add the idempotent `ALTER TABLE` guard

Inside `SqliteStore.start()` around line 193, extend the `PRAGMA table_info(spans)` block:

```python
async with self._db.execute("PRAGMA table_info(spans)") as cur:
    cols = {row[1] for row in await cur.fetchall()}
for name, ddl in (
    ("payload_mime", "ALTER TABLE spans ADD COLUMN payload_mime TEXT NOT NULL DEFAULT ''"),
    # ...existing entries...
    ("memory_rank", "ALTER TABLE spans ADD COLUMN memory_rank INTEGER NOT NULL DEFAULT 0"),
):
    if name not in cols:
        await self._db.execute(ddl)
```

For a different table (e.g. `task_plans`), follow the separate `PRAGMA table_info(task_plans)` block at `sqlite.py:206`. Add a new block if you're migrating a new table for the first time.

### 3. Adding a brand-new table

Just put the `CREATE TABLE IF NOT EXISTS` in `SCHEMA`. No guard block needed — fresh AND existing DBs pick it up on the next `start()`.

### 4. Adding an index to an existing table

Same as a new table: put `CREATE INDEX IF NOT EXISTS ...` in `SCHEMA`. SQLite creates it idempotently. You don't need a `PRAGMA index_list` guard.

### 5. Reading/writing the new column

Update every SELECT and INSERT that touches the table. Grep `sqlite.py` for `INSERT INTO spans` and `SELECT` — every query must include or ignore the new column consistently.

For columns with a default, existing `INSERT` statements keep working as long as you either extend them to include the new column or rely on the default (but **`INSERT INTO ... VALUES (?, ?, ...)` with explicit column positions will break** the moment the column count shifts, so always use the named-column form).

### 6. Test the migration path

You need two coverage angles:

1. **Fresh DB**: delete any existing `harmonograf.db`, start the server, confirm the new column exists.
2. **Pre-existing DB**: start an older server binary (or manually create a DB with the previous schema), upgrade the binary, start it, confirm the guard block adds the column without crashing.

A repeatable unit test lives in `server/tests/test_storage_extensive.py`. Pattern:

```python
async def test_migration_adds_memory_rank(tmp_path):
    # Create DB without the new column
    import aiosqlite
    db_path = tmp_path / "old.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(OLD_SCHEMA_WITHOUT_MEMORY_RANK)
        await db.commit()
    # Start the current store against it
    store = SqliteStore(db_path, payload_dir=tmp_path / "payloads")
    await store.start()
    async with store.db.execute("PRAGMA table_info(spans)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "memory_rank" in cols
    await store.close()
```

### 7. Rollback strategy

SQLite doesn't support `ALTER TABLE DROP COLUMN` before 3.35. Even on newer builds, don't rely on it in migrations — harmonograf supports SQLite 3.x broadly. **Rollback by shipping a reverse migration** as a fresh startup guard:

```python
if "memory_rank" in cols:
    # 3.35+ path; earlier versions need the rebuild dance
    await self._db.execute("ALTER TABLE spans DROP COLUMN memory_rank")
```

In practice: prefer to leave the column and stop writing to it. Reading old column data is fine — zero values are harmless.

### 8. Verification

```bash
uv run pytest server/tests/test_storage_extensive.py -x -q
rm -f data/harmonograf.db  # only if local dev, never in prod
uv run harmonograf-server  # starts, schema applies, no crash
```

## Common pitfalls

- **Forgetting `DEFAULT`**: `ALTER TABLE ... ADD COLUMN NOT NULL` without a default errors out on existing rows. Always pair `NOT NULL` with `DEFAULT`.
- **Column in `SCHEMA` but not in guard**: fresh DBs work, existing DBs silently lack the column until the next restart — except the next restart *also* won't run anything because `CREATE TABLE IF NOT EXISTS` is a no-op on existing tables. The guard block is the **only** path for existing DBs.
- **Guard but no `SCHEMA` entry**: fresh DBs get the column via a roundabout guard-hit path, which works but is confusing. Keep `SCHEMA` canonical.
- **INSERT positional drift**: if any `INSERT` statement uses positional `VALUES (?, ?, ?)` without named columns, adding a column in the middle of the table causes silent corruption. Audit for this every migration.
- **Foreign key with ON DELETE CASCADE**: `PRAGMA foreign_keys = ON` is set at startup (`sqlite.py:190`). New FK columns need the same parent-cleanup guarantees.
- **Multi-process startup**: `busy_timeout = 5000` (`sqlite.py:189`) gives us 5s for concurrent writers. Migrations happen before that timeout matters, but watch out if you add long-running backfills.
