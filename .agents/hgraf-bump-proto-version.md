---
name: hgraf-bump-proto-version
description: Stage a breaking proto change across client/server/frontend without splitting the fleet — deprecate, dual-write, migrate, remove.
---

# hgraf-bump-proto-version

## When to use

You need to make a change to `proto/harmonograf/v1/*.proto` that is *not* wire-compatible: renaming a field, changing a type, tightening an enum, removing a message. For wire-compatible additions (adding a new field, a new enum value, a new message), the recipes in `hgraf-add-proto-field.md` and `hgraf-add-proto-message.md` (batch 1 / batch 2) are sufficient — no version bump needed.

## Prerequisites

1. Read `proto/harmonograf/v1/types.proto` — the shared vocab (Agent, Span, Task, TaskPlan, Annotation, all enums). Most changes touch this file.
2. Read `proto/harmonograf/v1/telemetry.proto` and `proto/harmonograf/v1/control.proto` — the stream envelopes and control channel. These have oneofs that breaking changes can cascade through.
3. Read `proto/harmonograf/v1/service.proto` — the top-level RPC service.
4. Read `proto/harmonograf/v1/frontend.proto` — the frontend-facing view projection. If your proto change affects what the frontend sees, both server and frontend stubs must regenerate.
5. Know the generated stub locations:
   - Python (client): `client/harmonograf_client/pb/harmonograf/v1/*_pb2.py` + `.pyi`
   - Python (server): `server/harmonograf_server/pb/harmonograf/v1/*_pb2.py` + `.pyi`
   - TypeScript (frontend): `frontend/src/pb/harmonograf/v1/*_pb.ts` (buf-generated)
6. Know the codegen commands — grep the repo root Makefile or top-level `AGENTS.md` for `protoc` / `buf generate`.

## The core constraint

**Client and server run as separate processes, often updated on different cadences.** A client built last week talks to a server built today. A hard break means old clients stop working the instant the server rolls. The recipes below keep rollouts incremental.

The frontend is loaded from the server at request time, so server + frontend move together. Frontend/server is an atomic unit; client is the one that lags.

## Step-by-step: the 4-phase dance

### Phase 1 — Add the new shape alongside the old

Never remove or rename in place. Add.

- **Rename a field**: add the new field with a new proto number; mark the old field `[deprecated = true]` in the proto; keep both.
- **Change a type**: add a `new_name_v2` field with the new type; keep the old one filled.
- **Split a message**: keep the old message; add the new one; both contain the same payload for now.
- **Tighten an enum**: add a new enum for the tightened shape; mark the old enum values that will go away as `[deprecated = true]`.

Ship this change. Client + server + frontend all understand both shapes. Neither cares which the other sends.

### Phase 2 — Dual-write everywhere

Make every producer fill *both* the old and new fields. Make every consumer read the new field first and fall back to the old.

Concretely:

- **Client**: `client/harmonograf_client/convert.py` or the wire-emission path — fill both.
- **Server**: `server/harmonograf_server/convert.py` — when projecting to frontend, fill both.
- **Frontend**: `frontend/src/rpc/convert.ts` — prefer new, fall back to old.

Ship this change. A new-producer → old-consumer works because old-consumer reads the old field. Old-producer → new-consumer works because new-consumer falls back.

Wait at least one release cycle so that the oldest supported client has picked up Phase 2.

### Phase 3 — Stop reading the old field

Consumers drop the fallback. They read only the new field. Producers still dual-write (so any unupgraded consumers keep working).

Ship this change. The old field is now only written for compatibility; nothing reads it.

### Phase 4 — Remove the old field

Producers stop filling the old field. The proto still declares it as `reserved` (see "reserving numbers" below). Clean up all references in generated stubs, converters, and tests.

Ship this change. The old field is dead.

### Reserving numbers

When you finally delete a proto field, replace its declaration with `reserved <number>; reserved "<name>";`. This prevents a future author from re-using the number for a different type — which would silently corrupt data on any client that still had a mental model of the old shape.

Example:

```proto
message Span {
  reserved 4;
  reserved "old_duration_ms";
  // new fields here
}
```

## Regenerating stubs

After every proto edit, regenerate all three stub sets in one commit so CI doesn't drift:

```bash
# Python (both client and server)
uv run python -m scripts.gen_protos  # or whatever the repo uses — grep Makefile

# TypeScript (frontend)
cd frontend && pnpm proto:gen
```

Verify by diffing the generated files. If only one language regenerated, a consumer will silently see the old shape.

## Testing the migration

For each phase, a test suite should enforce:

1. **Phase 1**: old-only messages still parse; new-only messages parse.
2. **Phase 2**: old client + new server round-trip; new client + old server round-trip; both directions lose no data.
3. **Phase 3**: a message with only the old field is handled gracefully (consumer reads empty/default for the new field — is that intentional?).
4. **Phase 4**: any lingering reference to the old field is a hard error.

Cross-process round-trip tests live in `client/tests/test_transport_mock.py` and server integration tests in `server/tests/`. For a breaking change you want both.

## Frontend + server atomicity

Because the frontend bundle ships from the server, the server deploy bundles a specific frontend build. You can move frontend + server together in one phase. But:

- The **server-side streaming API** talks to clients (separate processes). That's still subject to the 4-phase dance.
- The **frontend-facing RPCs** (`frontend.proto`) are only consumed by the bundled frontend, so you can break those at will — the pair moves atomically.

If your change only touches `frontend.proto`, you can skip phases 2/3 — just regenerate stubs on both sides of the server boundary.

## Handling oneofs

`TelemetryUp`, `TelemetryDown`, `ControlUp`, `ControlDown` are oneofs in `telemetry.proto` and `control.proto`. Adding a case is wire-safe. Removing a case is a break. Follow the 4-phase dance for case removal: add the replacement case, dual-emit, stop reading old, remove old.

## Common pitfalls

- **Regenerating only one language's stubs**: half the codebase moves, half doesn't; the mismatch is silent until runtime. Always regenerate Python (client + server) AND TypeScript in the same commit.
- **Changing a field's proto number**: catastrophic. Wire data is keyed on the number. The field's *name* is metadata; the *number* is the contract. Never change a number — `reserve` the old and pick a new one.
- **Skipping Phase 2**: rolling from Phase 1 straight to Phase 3 (consumers read only new) means any unupgraded producer sends empty data. Always dual-write at least one release cycle.
- **Dropping the `reserved` clause**: a new author reuses the old number, a client in the wild with an old stub deserializes the new field into the old field's variable, and your test suite is silent because it runs against the new stubs. Always reserve.
- **Forgetting the buffer layer**: `client/harmonograf_client/buffer.py` holds opaque envelopes but the transport converts them. A field rename means the converter needs both branches during Phase 2. Grep `transport.py` for the old field name.
- **Forgetting the SQLite schema**: if the proto field is persisted on the server, see `hgraf-migrate-sqlite-schema.md`. The DB column survives a proto rename — you have to do both migrations in lockstep.
- **Assuming a staging-only rollout**: clients in dev environments are real clients. If someone's local dev agent is running a week-old build, they will be the first to hit your break.
- **Relying on `[deprecated = true]` for safety**: the deprecation annotation is a warning, not an enforcement. It prevents new code from adopting the old field — it does not stop wire traffic from flowing through it.
- **One-commit "big bang" rewrites**: tempting because the 4-phase dance takes four releases. Resist. A single-commit rewrite means every in-flight telemetry stream breaks at the instant of server deploy.
