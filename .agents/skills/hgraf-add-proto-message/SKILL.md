---
name: hgraf-add-proto-message
description: Add an entirely new proto message (not just a field) end-to-end across .proto, regen, Python + TS converters, storage, server ingest, and tests.
---

# hgraf-add-proto-message

## When to use

You need a brand-new wire concept (e.g. a new envelope on a stream, a new top-level entity stored server-side). Adding a whole message is strictly larger than adding a field (see `.claude/skills/hgraf-add-proto-field.md`) because you also create converters, storage rows, RPC plumbing, and frontend types.

## Prerequisites

1. `make install` ran at least once. Proto toolchain (`grpcio-tools`, `mypy-protobuf`, `@bufbuild/protoc-gen-es`) available. Verify via `make proto` clean run.
2. You know whether the message flows on `StreamTelemetry` (client→server, upstream), `SubscribeControl` (server→client), `WatchSession` / `GetSession` (server→frontend), or is a shared type used by multiple streams.
3. Read `docs/protocol/data-model.md` and `docs/protocol/wire-ordering.md` — new top-level messages must honor the happens-before rules there.

## Step-by-step

### 1. Decide which `.proto` file owns it

- `proto/harmonograf/v1/types.proto` — shared data types (Agent, Span, Task, TaskPlan, PayloadRef, Annotation, ControlKind, ControlEvent). Use this when the message is shared between upstream/downstream streams or persisted long-term.
- `proto/harmonograf/v1/telemetry.proto` — anything that must be one of `TelemetryUp` or `TelemetryDown` for the client↔server stream.
- `proto/harmonograf/v1/frontend.proto` — session reads, watch deltas (`SessionEvent`, `NewSpan`, `AgentStatus`, `TaskReport`, etc.).
- `proto/harmonograf/v1/control.proto` — only the `SubscribeControl` RPC envelope lives there; control payloads live in `types.proto` as `ControlEvent`.
- `proto/harmonograf/v1/service.proto` — RPC declarations only; add a new RPC here if the message is request/response rather than a oneof variant.

### 2. Define the message

Append at the bottom of the chosen file, field numbers starting at 1. Example, new upstream message on telemetry.proto:

```proto
// Context window sample — lightweight alternative to heartbeat tokens for
// agents that can report them out-of-band.
message ContextWindowSample {
  string agent_id = 1;
  int64 tokens = 2;
  int64 limit_tokens = 3;
  google.protobuf.Timestamp recorded_at = 4;
}
```

Then add it to the parent oneof (if applicable):

```proto
message TelemetryUp {
  oneof msg {
    // ...existing variants 1..10...
    ContextWindowSample context_window_sample = 11;
  }
}
```

**Never reuse field numbers**, even in oneofs.

### 3. Regenerate stubs

```bash
make proto
```

Verify with `git status` that the following regenerated:

- `client/harmonograf_client/pb/harmonograf/v1/*_pb2.py` and `*_pb2.pyi`
- `server/harmonograf_server/pb/harmonograf/v1/*_pb2.py` and `*_pb2.pyi`
- `frontend/src/pb/harmonograf/v1/*_pb.ts`

If any of these are missing, something in the toolchain is stale — re-run `make client-install server-install` and try again.

### 4. Client-side emission path

If your message is upstream, find the place in `client/harmonograf_client/client.py` or `client/harmonograf_client/adk.py` that would produce it. Add a method like `emit_context_window_sample(**kwargs)` on `HarmonografClient`. The client must not import `*_pb2` directly from callsites; instead push a buffer envelope through `client/harmonograf_client/buffer.py :: EventRingBuffer.push` and let `client/harmonograf_client/transport.py :: _drain_events` build the protobuf at dequeue time.

**Buffer envelopes are opaque** (`buffer.py:55 SpanEnvelope`). For a non-span message, extend `EnvelopeKind` in `buffer.py:45` with a new kind and teach `_drain_events` in `transport.py` to serialize it. Follow the `EnvelopeKind.TASK_PLAN` / `TASK_STATUS_UPDATE` precedent.

### 5. Server-side ingest

For `StreamTelemetry`, `IngestPipeline.handle_telemetry_up` in `server/harmonograf_server/ingest.py` dispatches by `WhichOneof("msg")`. Add a branch for your new variant, writing state to the stream context or store.

For control, extend `server/harmonograf_server/control_router.py :: ControlRouter.deliver` — but only if the new message triggers outbound behavior.

### 6. Storage

If the message is persisted, edit `server/harmonograf_server/storage/base.py` to add a dataclass, then implement in both:

- `server/harmonograf_server/storage/memory.py`
- `server/harmonograf_server/storage/sqlite.py`

For SQLite, add the `CREATE TABLE` to the `SCHEMA` constant (`sqlite.py:45`) **and** add an idempotent migration block in `SqliteStore.start()` around line 193 following the `PRAGMA table_info` pattern. See skill `hgraf-migrate-sqlite-schema.md`.

### 7. Converter for server → frontend

`server/harmonograf_server/convert.py` translates between storage dataclasses and `harmonograf.v1.*_pb2` messages. Add a `_build_<message>_pb` function there and call it from the `WatchSession` / `GetSession` handlers in `server/harmonograf_server/rpc/frontend.py`.

### 8. Frontend wire conversion

`frontend/src/rpc/convert.ts` converts generated ES proto objects to the renderer-friendly types in `frontend/src/gantt/types.ts`. Add a UI type (TypeScript interface), a converter function, and wire it up inside `frontend/src/rpc/hooks.ts :: useSessionWatch`.

The `gantt/` folder must never import from `src/pb/` directly — all conversion happens in `rpc/convert.ts`. This is a load-bearing rule; keep it intact.

### 9. Tests

Minimum coverage for a new message:

- `client/tests/test_buffer.py` — envelope kind round-trip.
- `client/tests/test_transport_mock.py` — transport serializes the new kind to the right proto variant.
- `server/tests/test_telemetry_ingest.py` — ingest branch writes to the store.
- `server/tests/test_storage_extensive.py` — store round-trips (memory + sqlite).
- `server/tests/test_rpc_frontend.py` — watch/get surfaces the message.
- `frontend/src/__tests__/rpc/convert.test.ts` — wire-to-UI conversion.

### 10. Verification

```bash
make proto
uv run pytest client/tests server/tests -x -q
cd frontend && pnpm test -- --run
cd frontend && pnpm typecheck
cd frontend && pnpm build
```

All four must pass. If you touched `types.proto`, run the full suite — not just the modules you think you changed — because proto regen can break unrelated imports.

## Common pitfalls

- **Forgetting the oneof**: adding the message alone is a no-op on the wire if it isn't wired into `TelemetryUp` / `TelemetryDown` / `SessionEvent`.
- **Bypassing `buffer.py`**: emitting straight to the gRPC stream from a callback breaks reconnect semantics — buffered events must be replayable after a disconnect.
- **Direct `pb/` import from `gantt/`**: the frontend enforces a boundary. The renderer never touches protobuf.
- **Schema without migration**: a new `CREATE TABLE IF NOT EXISTS` statement handles fresh DBs, but existing deployments already ran the old schema — add the `PRAGMA table_info` + conditional `ALTER TABLE` guard.
- **Missing `_pb2.pyi`**: if stubs didn't regenerate the `.pyi`, type checkers won't see your field. Re-run `make proto-python` and commit the `.pyi` alongside the `.py`.
