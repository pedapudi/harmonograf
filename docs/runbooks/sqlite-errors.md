# Runbook: SQLite errors

The server is running but sqlite is raising. Symptoms range from "one
ingest failed" to "the whole server fell over".

## Symptoms

- **Server log**:
  - `sqlite3.OperationalError: database is locked`
  - `sqlite3.OperationalError: no such table: spans`
  - `aiosqlite.OperationalError: database disk image is malformed`
  - `ERROR harmonograf_server.main: error closing store` (`main.py:217`)
  - `ERROR harmonograf_server.retention: retention delete failed session=...`
    (`retention.py:50`)
  - `ERROR harmonograf_server.retention: retention sweep crashed; continuing`
    (`retention.py:69`)
- **Client log**: telemetry appears to flow but nothing lands; the
  server logs `INVALID_ARGUMENT` or `INTERNAL` back. Usually
  accompanied by `ingest error agent_id=...` (`rpc/telemetry.py:98`).

## Immediate checks

```bash
# Is the DB file there and writable?
ls -lh data/harmonograf.db*
# WAL mode creates harmonograf.db-wal and harmonograf.db-shm alongside.

# Does sqlite think it's well-formed?
sqlite3 data/harmonograf.db 'PRAGMA integrity_check;'
sqlite3 data/harmonograf.db 'PRAGMA quick_check;'

# Tables present?
sqlite3 data/harmonograf.db '.tables'
sqlite3 data/harmonograf.db '.schema spans'

# Lock holders (if any)?
lsof data/harmonograf.db data/harmonograf.db-wal 2>/dev/null
```

## Root cause candidates (ranked)

1. **Two server processes on the same DB** — the most common cause of
   `database is locked`. WAL mode tolerates many readers and one
   writer, but if two servers are both writing, the second one's
   writes block. Can also happen if a previous process crashed and
   something (e.g. `sqlite3` CLI) holds the lock.
2. **Long-running write transaction** — something is holding an
   exclusive lock for longer than `PRAGMA busy_timeout = 5000`
   (`storage/sqlite.py:189`). Batch inserts, big updates, or a
   payload write of a very large blob.
3. **Schema out of date** — `no such table: spans` appears when the
   DB file exists but predates a migration, or a migration only
   half-ran. Check `PRAGMA table_info(spans)` against the source.
4. **Corrupted file from kill -9** — `database disk image is
   malformed`. WAL checkpoints don't always survive SIGKILL cleanly.
5. **Disk full** — writes fail with `disk I/O error`. Check `df -h`.
6. **Filesystem doesn't support WAL** — some network filesystems
   (NFS without `nolock`, some FUSE mounts) break sqlite WAL. Server
   will hang on startup or thrash.
7. **Permission drift** — the DB file and its `-wal` / `-shm`
   siblings must be owned by the server user. If another user
   created them, sqlite opens them but can't write.

## Diagnostic steps

### 1. Two processes

```bash
ps axf | grep harmonograf_server
lsof data/harmonograf.db 2>/dev/null | awk 'NR>1 {print $2, $1}' | sort -u
```

Two server PIDs on the same DB = your bug.

### 2. Long writers

Enable `LOG_LEVEL=DEBUG` on `harmonograf_server.storage.sqlite`
(logs every SQL statement, per `dev-guide/debugging.md`). Look for a
statement that's older than ~5s and still executing before the error.

### 3. Schema out of date

```bash
sqlite3 data/harmonograf.db '.schema spans' > /tmp/schema.sql
grep -A 20 'CREATE TABLE spans' server/harmonograf_server/storage/sqlite.py
```

Diff the two shapes. If your DB is missing columns, a migration
didn't run.

### 4. Corruption

```bash
sqlite3 data/harmonograf.db 'PRAGMA integrity_check;'
```

Anything other than `ok` → the file is damaged.

### 5. Disk full

```bash
df -h $(dirname data/harmonograf.db)
```

### 6. NFS / FUSE

```bash
stat -f data/harmonograf.db | grep 'Type\|mount'
mount | grep $(dirname $(readlink -f data/harmonograf.db))
```

### 7. Permission drift

```bash
ls -l data/harmonograf.db data/harmonograf.db-wal data/harmonograf.db-shm
id -un  # whoever the server should be running as
```

## Fixes

1. **Two processes**: stop the duplicate. `systemctl stop` the
   imposter; if it's a bare `python -m` invocation, kill by PID. Never
   run two servers against one DB.
2. **Long writers**: reduce batch size; keep payload uploads
   chunked; raise `PRAGMA busy_timeout` if the workload legitimately
   needs it (rarely — usually the right answer is to not hold a
   transaction that long).
3. **Schema out of date**: restart the server — `_ensure_schema`
   should migrate on startup. If it doesn't, the data is in a pre-
   migration shape that the current code can't read; either backfill
   manually or wipe and restart.
4. **Corruption**: back the DB up and run
   ```bash
   sqlite3 data/harmonograf.db '.recover' | sqlite3 data/harmonograf-recovered.db
   ```
   In many harmonograf deployments the DB is non-authoritative (agents
   replay on reconnect), so simply removing `data/harmonograf.db*`
   and restarting is the fastest recovery. See
   [`post-crash-recovery.md`](post-crash-recovery.md).
5. **Disk full**: free space; restart.
6. **NFS / FUSE**: move the DB to local disk. Harmonograf's storage
   does not support network-attached storage.
7. **Permissions**: `chown` to the server user; restart.

## Prevention

- Run the server as a single systemd unit with `Restart=on-failure`
  — this prevents a human from starting a second copy by hand.
- Keep `data/` on local disk, never NFS.
- Scheduled integrity check: cron `PRAGMA integrity_check` once a day
  and alert on non-`ok`.
- Include `harmonograf.db-wal` size in monitoring — a growing WAL
  without checkpoints is a sign a writer is pinning the file.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"Inspecting
  the sqlite store".
- [`runbooks/post-crash-recovery.md`](post-crash-recovery.md).
- `server/harmonograf_server/storage/sqlite.py:183-194` for the
  PRAGMA setup.
