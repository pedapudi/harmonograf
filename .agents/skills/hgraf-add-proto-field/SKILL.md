---
name: hgraf-add-proto-field
description: Add a field to a proto message end-to-end — proto edit, regen, client+server converters, frontend types, tests, and sqlite migration if persisted.
---

# hgraf-add-proto-field

## When to use

You are extending the wire format between client library, server, and frontend. Because harmonograf has three separately-generated proto bindings (Python for client + server, TypeScript for frontend), every change touches at least five files — and if the field is persisted in sqlite, another three.

## Prerequisites

1. `make install` has been run at least once.
2. Proto toolchain available: `grpcio-tools`, `mypy-protobuf` (`Makefile:proto-python`) and `frontend/buf.gen.yaml` + `@bufbuild/protoc-gen-es` (`Makefile:proto-ts`).
3. You know which `.proto` file owns the message — see `proto/harmonograf/v1/`:
   - `types.proto` — shared enums + core messages (Span, Task, TaskPlan, Agent, ContextWindowSample, etc.)
   - `telemetry.proto` — client→server stream (Hello, SpanStart, SpanEnd, Heartbeat, etc.)
   - `control.proto` — server→client control channel
   - `frontend.proto` — server→frontend (SessionUpdate, NewSpan, TaskReport, etc.)
   - `service.proto` — RPC definitions only

## Step-by-step

### 1. Edit the .proto file

Pick the next free field number. Protobuf field numbers are **permanent**: never reuse a number, never renumber an existing field. If you are adding to a message that already exists, append at the end.

```proto
message Span {
  // ... existing fields 1..12 ...
  int64 predicted_duration_ms = 13;
}
```

Use `optional` only if you need presence semantics (proto3 scalar fields default to zero value). For new fields that must be distinguishable from "unset", use `optional` or wrap in a dedicated message.

### 2. Regenerate stubs

```bash
make proto
```

This runs `proto-python` (writes under `server/harmonograf_server/pb/` and `client/harmonograf_client/pb/`) and `proto-ts` (writes under `frontend/src/pb/` via `buf generate`). If `proto-ts` prints "skipping — frontend/buf.gen.yaml not present", the TS step is not wired yet; check with the user before proceeding.

**Verify the regen actually ran**: `git status` should show modified `*_pb2.py`, `*_pb2.pyi`, and `frontend/src/pb/**/*_pb.ts` files. If not, grpcio-tools is not installed into the uv env. Re-run `make server-install client-install`.

### 3. Update the Python converters

Two converter modules touch every message: the client-side telemetry builder and the server-side storage translator.

**Server side:** `server/harmonograf_server/convert.py`
- Look for `pb_span_to_storage()`, `pb_task_plan_to_storage()`, `pb_task_status_from_pb()` etc. (imports listed at `ingest.py:28-49`).
- Add your field read: `storage_span.predicted_duration_ms = pb_span.predicted_duration_ms` (or the appropriate default handling).
- If it is a reverse direction (storage→pb), add the write symmetrically.

**Client side:** search for where the message is constructed:
```
grep -rn "pb_span\.\|SpanStart(\|SpanEnd(" client/harmonograf_client/adk.py client/harmonograf_client/client.py
```
Most fields are populated in `client.py` or `adk.py` right before `buffer.append()` — see `client/harmonograf_client/buffer.py` and `client/harmonograf_client/transport.py` for the upstream flow.

### 4. Update the storage dataclass (if persisted)

`server/harmonograf_server/storage/base.py:92-145` defines the dataclasses (`Span`, `Agent`, `Task`, etc.). Add the new field with a sensible default so existing rows don't blow up:

```python
@dataclass
class Span:
    # ... existing fields ...
    predicted_duration_ms: int = 0
```

### 5. Add a sqlite migration

`server/harmonograf_server/storage/sqlite.py:45-170` holds the canonical `SCHEMA` DDL as a single multiline string executed via `executescript()` on first boot. There are two strategies:

**A. Additive-only (preferred for new nullable/default columns):** edit the `CREATE TABLE` in `SCHEMA` *and* add an idempotent `ALTER TABLE ... ADD COLUMN ...` call next to the schema execution so existing databases get the column on next boot. Look for `_MIGRATIONS` or equivalent pattern in the same file — if it exists, append your `ALTER`. If it does not, you must add a minimal migration block that runs before `executescript`. **Do not** drop-and-recreate the table — you will delete every user's accumulated trace history.

**B. Breaking change:** document the need for a fresh `rm data/harmonograf.sqlite`. Only acceptable during pre-GA — flag it loudly in the PR.

**Tradeoff:** Additive is always safer but requires two code sites (the CREATE TABLE definition *and* the ALTER). Keeping the CREATE in sync with the ALTERs is purely for fresh installs — it does not affect existing DBs. Do not skip it or the schema "documentation" drifts from reality.

### 6. Update the frontend TypeScript types

`frontend/src/pb/harmonograf/v1/*_pb.ts` is **generated** — do not edit by hand. Instead, edit `frontend/src/gantt/types.ts` (the domain types that wrap the pb types) and the rpc converters at `frontend/src/rpc/convert.ts`:

- In `convert.ts`, find the function that maps `pbSpan → Span` (domain type). Add the field read.
- In `types.ts:59-79` (Span) or the relevant interface, add the TS field.
- If a UI component consumes the field, thread it through via props or via the appropriate store (`sessionsStore`, `uiStore`, `TaskRegistry`).

### 7. Tests

- **Client side:** `client/tests/test_transport_protocol.py`, `test_client_api.py`, or the closest existing file depending on which component writes the field. Assert round-trip: construct, serialize, deserialize, compare.
- **Server side:** `server/tests/` has storage round-trip tests; add a case that writes a Span with your new field, reads it back, compares.
- **Frontend side:** if the field drives rendering, add a vitest under `frontend/src/__tests__/`.

### 8. Documentation

If the field is user-facing (shown in the UI, reported in a tool, or part of the public client API), update `AGENTS.md` → *Plan execution protocol* or the relevant `docs/design/0*-*.md` file.

## Verification

```bash
# 1. proto regen is deterministic
make proto
git diff --stat   # should be empty unless you edited .proto

# 2. server round-trips
cd server && uv run --with pytest --with pytest-asyncio python -m pytest -q

# 3. client round-trips
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q

# 4. frontend builds
cd frontend && pnpm lint && pnpm build

# 5. migration works on an existing DB
cp data/harmonograf.sqlite /tmp/pre-migration.sqlite
make server-run &        # boots and runs migration
sleep 2 && kill %1
sqlite3 /tmp/pre-migration.sqlite ".schema spans" | grep predicted_duration_ms
```

## Common pitfalls

- **Field number reuse.** Protobuf permits it syntactically but it corrupts the wire format for any consumer still holding the old schema. Always append; never renumber.
- **Forgetting proto-ts.** `make proto-python` runs fine even if `buf generate` skips. Check `git status` after `make proto` — if only `pb2.py` files changed, the frontend will crash at runtime when it tries to decode the new field. Look for missing `frontend/buf.gen.yaml` or `pnpm install`.
- **Default values as "unset".** Proto3 scalar defaults (`0`, `""`, `false`) are indistinguishable from "field not written". If you need to distinguish, use `optional`.
- **Dropping the sqlite table.** The SCHEMA string is executed via `executescript()` at startup. Do not make `executescript` re-run a `DROP TABLE ... IF EXISTS ... CREATE TABLE` — you will silently wipe every user's trace history on the next boot. Use ALTER TABLE migrations.
- **sqlite race on first boot.** The ingest loop (`server/harmonograf_server/ingest.py`) starts handling telemetry very quickly after boot. If your migration takes >1s and runs concurrently with the first INSERT, you will see `database is locked`. Run migrations before starting the ingest task, not after.
- **Forgetting the frontend domain type.** `frontend/src/pb/` is regenerated and readable, but the UI does not usually consume it directly — it consumes `types.ts`. Adding only to `pb/` makes your field exist on the wire but invisible in the app.
- **Skipping `convert.ts`.** Same story on the rpc seam. `frontend/src/rpc/convert.ts` is where the mapping happens.
