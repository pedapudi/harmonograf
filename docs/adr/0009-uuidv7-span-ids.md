# ADR 0009 — UUIDv7 for span identifiers

## Status

Accepted.

## Context

Every span needs a unique id. The server needs to dedup when a client
reconnects and replays buffered spans (see `telemetry.proto`'s
`Hello.resume_token` and the server ingest path). The frontend uses span
ids as React keys, as lookup keys into the spatial index, and as hash
fragments in URLs. Span ids flow through the entire data model — they are
referenced by `parent_span_id`, `SpanLink.target_span_id`,
`Annotation.target.span_id`, `Task.bound_span_id`, and the control event
targets.

Design candidates:

1. **Monotonic integer per session** — small, sortable, but needs a central
   allocator. In a multi-agent run, the server would have to mint ids, which
   means every span start is a round trip before the client can emit.
2. **Client-side random (UUIDv4)** — no coordination, collision-free in
   practice, but not sortable. Dedup-on-reconnect still works, but the
   server has to maintain an explicit seen-ids set because arrival order
   contains no ordering information.
3. **UUIDv7** — 128 bits, leading bits are a Unix millisecond timestamp,
   trailing bits are random. Sortable by creation time, collision-free,
   generated locally, standardized in 2024.

## Decision

Client-side **UUIDv7** for span ids. The `types.proto` comment on `Span`
makes this explicit:

> Span ids are UUIDv7 (sortable, collision-free across reconnects). The
> server dedups by id when a client replays buffered spans after reconnect.

The client library generates ids locally the moment a span begins (inside
`before_*` ADK callbacks) and never asks the server to mint one. The
server's ingest path is structured as "upsert by id" — a replay simply
overwrites or is deduped.

**UUIDv7 layout** — leading 48 bits are a Unix-millisecond timestamp, so
ids sort by creation time without a central allocator; the random tail keeps
two agents on the same millisecond from colliding.

```mermaid
flowchart LR
    subgraph U7["UUIDv7 (128 bits)"]
      direction LR
      T["unix_ms (48 bits)<br/>creation time → sortable"] --> V["ver (4)"] --> R1["rand_a (12)"] --> Var["var (2)"] --> R2["rand_b (62)<br/>collision-free across agents"]
    end
    Agent1[Agent A] -- mints locally --> U7
    Agent2[Agent B] -- mints locally --> U7
    U7 --> Buf[client buffer<br/>(offline-safe)]
    Buf --> Srv[Server upsert by id<br/>dedup on replay]

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class U7,Buf,Srv good
```

## Consequences

**Good.**
- **Offline-safe.** A client can emit spans into its local buffer before
  the telemetry stream is even open, and the ids are already valid. This
  is what makes the reconnect story work without a handshake.
- **Dedup is trivial.** The server keeps an index by id and drops
  duplicates. Because UUIDv7 is sortable by creation time, the index can
  be pruned by age without walking the full space.
- **Ordering is stable.** Two spans from the same client have ids that
  sort in the order they were created, modulo millisecond ties, which
  matches the order `start_time` would sort them anyway. Frontend code
  that sorts by id to tie-break equal start times gets a deterministic
  result.
- **Cross-process safe.** Two agents starting spans at exactly the same
  millisecond have distinct random tails, so there is no coordination
  needed between agent processes.

**Bad.**
- **Ids are 36 characters.** Every wire message, every row in SQLite,
  every React key carries a ~36-byte id. For a million-span session this
  is ~36 MB just in ids; on the wire proto encodes them as strings in
  `types.proto` (the field is `string id = 1`, not `bytes`). A
  byte-encoded format would be half the size. This is a real cost and
  the one the decision most clearly gives up.
- **Clock skew is visible.** Two agents on two machines with skewed
  clocks produce span ids that sort in an order that disagrees with
  wall-clock. We accept this because span sort order within a single
  agent (the common case) is what matters, and cross-agent ordering on
  the Gantt is driven by `start_time` which is corrected by the server
  receive time anyway.
- **Not browser-friendly out of the box.** The TypeScript frontend does
  not mint UUIDv7s (the server is the sole source of frontend-originated
  ids, and annotations get their ids assigned server-side on post). If
  that changes, we would need a browser UUIDv7 library.
- **Debug readability.** UUIDv7 ids are still opaque strings to a human
  reading logs. We compensate with short suffixes in debug prints, but
  pattern-matching ids across a log stream is harder than with
  `span-42` style ids.

The dedup-on-reconnect requirement plus the no-central-allocator goal
together eliminate options 1 and 2 in practice. UUIDv7 is the only
option that ticks both boxes without us reinventing one.
