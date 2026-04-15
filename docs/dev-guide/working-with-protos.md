# Working with protos

The proto definitions in `proto/harmonograf/v1/` are the canonical schema
for every piece of data that crosses a process boundary. Getting this right
is the single highest-leverage investment you can make: a clean proto
change ripples through all three components cleanly, and a bad one breaks
old clients silently.

## Layout

```
proto/harmonograf/v1/
├── service.proto      # RPC service definition (what the server exposes)
├── telemetry.proto    # TelemetryUp / TelemetryDown envelopes (agent ↔ server)
├── control.proto      # SubscribeControl request + ControlEvent routing
├── types.proto        # Core domain types: Span, Agent, Session, Task, etc. Shared.
└── frontend.proto     # Frontend-only request/response messages
```

### What each file owns

| File | Top-level messages | When to edit |
|---|---|---|
| `service.proto` | `service Harmonograf { … }` (RPC definitions) | You are adding, removing, or renaming an RPC. Rare. |
| `telemetry.proto` | `Hello`, `Welcome`, `SpanStart`, `SpanUpdate`, `SpanEnd`, `PayloadUpload`, `PayloadRequest`, `Heartbeat`, `Goodbye`, `ServerGoodbye`, `ControlAck`, `TaskPlan`, `UpdatedTaskStatus`, `TelemetryUp`, `TelemetryDown`, `FlowControl` | You are changing the agent wire protocol. Touching this affects every agent process. |
| `control.proto` | `SubscribeControlRequest` | Rare; control event routing layer. |
| `types.proto` | `Session`, `Agent`, `Span`, `SpanLink`, `AttributeValue`, `PayloadRef`, `ErrorInfo`, `Task`, `TaskEdge`, `TaskPlan`, `Annotation`, `ControlEvent`, `ControlAck`, plus enums (`SessionStatus`, `AgentStatus`, `Framework`, `Capability`, `SpanKind`, `SpanStatus`, `LinkRelation`, `AnnotationKind`, `TaskStatus`) | You are adding a field to a domain type. Most common place to edit. |
| `frontend.proto` | `ListSessionsRequest/Response`, `WatchSessionRequest`, `SessionUpdate`, `GetPayloadRequest/Response`, `GetSpanTreeRequest/Response`, `PostAnnotationRequest/Response`, `SendControlRequest/Response`, `DeleteSessionRequest/Response`, `GetStatsRequest/Response` | Frontend needs a new RPC or new payload shape. |

The cleanest rule of thumb: if the type is shared between agent→server and
server→frontend, it belongs in `types.proto`. If it's specific to one side
of the wire, it belongs in `telemetry.proto` or `frontend.proto`.

## Codegen

### `make proto`

One command regenerates everything:

```bash
make proto
```

This fans out to (see `Makefile:37-65`):

| Sub-target | Tool | Output |
|---|---|---|
| `proto-python` | `grpc_tools.protoc` via uv-managed envs | `server/harmonograf_server/pb/harmonograf/v1/*.py, *.pyi` and `client/harmonograf_client/pb/harmonograf/v1/*.py, *.pyi` |
| `proto-ts` | `buf generate` | `frontend/src/pb/harmonograf/v1/*.ts` (via `@bufbuild/protoc-gen-es`) |

Both flavors share the same `proto/` source tree. The Python generator
emits stub files twice — once into server's `pb/`, once into client's
`pb/`. This duplication is intentional: the two packages must not depend on
each other.

### Generated files are committed

This matters:

1. Check in the generated files (`*_pb2.py`, `*_pb2.pyi`, `*_pb.ts`)
   alongside the `.proto` change in the same commit.
2. CI verifies no drift by running `make proto` and `git diff --exit-code`.
   If the generated files don't match the source, CI fails.
3. Never hand-edit a generated file. If you need a different shape,
   change the proto and re-run `make proto`.

### When codegen fails

| Symptom | Fix |
|---|---|
| `grpc_tools.protoc: command not found` | `uv sync` under `server/` and `client/`; the tool is pulled in as a dev dep. |
| `buf: command not found` | Install buf: `brew install bufbuild/buf/buf` or the upstream instructions. |
| `Type X not found in file Y` | You're importing a type across files; check the import path matches `proto/harmonograf/v1/`. Proto imports are relative to the `proto/` root. |
| `Error: field number N already used` | Reserved or collided field. Pick a fresh number. See "Field number discipline" below. |

## Adding a field (the common case)

Suppose you want to add `cpu_percent_peak` to `Heartbeat`. Here is the exact
sequence.

### 1. Edit the proto

```proto
// telemetry.proto
message Heartbeat {
  // … existing fields …
  double cpu_self_pct = 6;
  double cpu_percent_peak = 15;  // NEW — pick the next unused number
}
```

**Pitfall:** pick a fresh field number. Grep the existing message for the
largest number and use `N+1`. Do not reuse a number that has ever been
assigned, even if the field was deleted. Protobuf wire format is tied to
field numbers forever — reusing one silently misinterprets old messages.

If you remove a field, reserve its number: `reserved 7;` — this forces the
compiler to refuse reuse.

### 2. Regenerate

```bash
make proto
```

Inspect the diff. You should see changes in:

- `server/harmonograf_server/pb/harmonograf/v1/telemetry_pb2.py`
- `server/harmonograf_server/pb/harmonograf/v1/telemetry_pb2.pyi`
- `client/harmonograf_client/pb/harmonograf/v1/telemetry_pb2.py`
- `client/harmonograf_client/pb/harmonograf/v1/telemetry_pb2.pyi`
- `frontend/src/pb/harmonograf/v1/telemetry_pb.ts`

Commit these alongside the proto change.

### 3. Update the storage dataclass

`server/harmonograf_server/storage/base.py` defines Python dataclasses that
mirror the proto types. Add the field there:

```python
@dataclass
class AgentHeartbeat:
    # … existing fields …
    cpu_self_pct: float
    cpu_percent_peak: float  # NEW
```

Use `field(default=0.0)` if the field is optional for older data.

### 4. Update the converters

`server/harmonograf_server/convert.py` holds the proto↔storage conversions.
Add both directions:

```python
def pb_heartbeat_to_storage(pb: telemetry_pb2.Heartbeat) -> AgentHeartbeat:
    return AgentHeartbeat(
        # …
        cpu_self_pct=pb.cpu_self_pct,
        cpu_percent_peak=pb.cpu_percent_peak,  # NEW
    )
```

And the inverse if the server ever sends it back.

### 5. Update the store

`server/harmonograf_server/storage/sqlite.py` (for the sqlite backend) and
`storage/memory.py` (for tests) need to carry the field. If it's a new
column on an existing table:

- Add to the `CREATE TABLE` statement (for fresh DBs).
- Add a conditional `ALTER TABLE ... ADD COLUMN cpu_percent_peak REAL
  DEFAULT 0.0` for existing databases (see `sqlite.py` for existing
  examples).
- Update read and write code paths.
- Update `test_storage_extensive.py` fixtures.

**Pitfall:** do not drop or rename existing columns. Add new, dual-write
for a release, then stop writing the old. The in-memory store is not
migrated; sqlite databases on developer machines are.

### 6. Update the client write path

On the client side, edit whichever code emits the `Heartbeat` message
(search `client/harmonograf_client/transport.py` for `Heartbeat(`). Fill
the new field. If the source data doesn't exist yet, you also need to add
the producing code (e.g., CPU sampling).

### 7. Update the frontend (if user-visible)

If the field needs to appear in the UI:

- Add a handler in `frontend/src/rpc/convert.ts` for the new field.
- Store it on the appropriate frontend type.
- Render it where needed.

### 8. Test and ship

- Unit test the converter (`server/tests/test_convert.py` or similar).
- Unit test the storage round-trip.
- Run `make test` end-to-end.
- Regenerate protos are clean: `make proto && git diff --exit-code`.
- Commit the proto change, generated stubs, dataclass updates, converters,
  storage migrations, client producer, and frontend consumer all in one
  PR. A proto change that skips a layer is hard to review piecewise.

## Forward compatibility rules

These are hard-won lessons. Violating any of them will break deployed
agents silently or cause subtle data loss.

### Additions are safe

Adding a new field to a message is forward- and backward-compatible as long
as:

1. The field has a fresh (never-used) number.
2. The field's default value (zero for numbers, empty for strings/bytes,
   `null` for messages) is a meaningful "absent" signal.
3. Consumers can cope with the default when reading old data.

### Removals are not

Removing a field in place is *wire-compatible* but *semantically* broken:
old clients will still send it, and the server's parser will silently drop
it. Instead:

1. Stop reading the field on the server side, first.
2. In a later release, stop sending it on the client side.
3. When all clients are upgraded, delete the field from the proto and add
   `reserved <number>; reserved "<name>";`.

### Renames are not

A field rename changes the JSON/text encoding and any code that references
it by name. In proto itself, renaming only changes the name — the wire
format is unchanged because numbers are what matters. But the Python and
TypeScript generators produce differently-named attributes, so every reader
breaks. Don't rename. Add a new field, migrate readers, retire the old.

### Enum changes

Adding a new enum value is forward-compatible as long as:

1. The new value has a fresh number.
2. Consumers have a default case that treats unknown values as "ignore"
   or "unknown", not "crash".

Rearranging or deleting enum values is a breaking change. Reserve the
number, same as fields.

### Oneof changes

Adding a case to a oneof is safe. Removing a case is not — old clients may
still send it and the server will fail to parse. Reserve the number.

### Message nesting

Don't reorder nested messages or change their types. Don't change a field
from singular to `repeated` or vice versa; it changes the wire format in
a subtle way and breaks parsing.

## Field number discipline

Quick reference:

| Field range | Wire cost | Use for |
|---|---|---|
| 1–15 | 1 byte tag | Frequent / small fields (enums, status, flags) |
| 16–2047 | 2 byte tag | Occasional fields |
| 2048+ | More | Rare fields |

`Heartbeat` is sent ~once per second per agent — put hot fields in 1–15.
`Span` is sent hundreds of times per minute — prioritize tag compactness
there too.

## Reserving retired fields

When a field is removed, reserve it:

```proto
message Heartbeat {
  // ...
  reserved 8;                // number we removed
  reserved "old_field_name"; // name we removed
  // ...
}
```

This forces the compiler to refuse reuse and documents the history.

## Cross-links to generated files

| Proto | Python (server) | Python (client) | TypeScript |
|---|---|---|---|
| `service.proto` | `server/harmonograf_server/pb/harmonograf/v1/service_pb2.py` | `client/harmonograf_client/pb/harmonograf/v1/service_pb2.py` | n/a (RPCs are wired by Connect-RPC at client-side from service descriptors) |
| `telemetry.proto` | `server/…/pb/harmonograf/v1/telemetry_pb2.py` | `client/…/pb/harmonograf/v1/telemetry_pb2.py` | `frontend/src/pb/harmonograf/v1/telemetry_pb.ts` |
| `types.proto` | `server/…/pb/harmonograf/v1/types_pb2.py` | `client/…/pb/harmonograf/v1/types_pb2.py` | `frontend/src/pb/harmonograf/v1/types_pb.ts` |
| `control.proto` | `server/…/pb/harmonograf/v1/control_pb2.py` | `client/…/pb/harmonograf/v1/control_pb2.py` | `frontend/src/pb/harmonograf/v1/control_pb.ts` |
| `frontend.proto` | `server/…/pb/harmonograf/v1/frontend_pb2.py` | `client/…/pb/harmonograf/v1/frontend_pb2.py` | `frontend/src/pb/harmonograf/v1/frontend_pb.ts` |

## Wire format reference

This chapter is the workflow. The byte-level wire format — every message,
every field, every enum value — lives in `docs/protocol/` (task #8).

## Next

[`testing.md`](testing.md) — how to test proto changes without breaking
existing callers.
