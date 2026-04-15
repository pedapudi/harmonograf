# ADR 0006 — gRPC as the wire transport

## Status

Accepted.

## Context

Harmonograf has three communicating parties (client library, server, browser
frontend) and needs:

- **Bidirectional streaming** for the telemetry channel, so both spans and
  the downstream handshake / flow-control messages share a connection.
- **Server streaming** for control delivery and for `WatchSession` (the
  frontend's live subscription).
- **Unary RPCs** for session listing, payload fetch, annotation posting,
  control send, stats, and session deletion.
- **A typed schema** the three components share without each reimplementing
  it — given that the data model (see `proto/harmonograf/v1/types.proto`) is
  the backbone of the product.
- **A browser-compatible path** because the frontend is an SPA; it has to be
  able to open a long-lived stream from a browser, not just a native client.

Candidates considered:
1. **gRPC + gRPC-Web** — one toolchain, same service definition for native
   and browser clients, streaming everywhere, strong typing via proto.
2. **Plain HTTP/WebSockets with JSON or MessagePack** — lighter, no
   codegen, but we'd reinvent the schema, the streaming framing, and the
   bidirectional ordering guarantees we lean on in ADR 0005.
3. **Server-Sent Events for the frontend + raw TCP protocol for agents** —
   splits the stack in two and doubles schema maintenance.
4. **OTLP (OpenTelemetry protocol) for telemetry + a separate custom RPC
   for control** — reuses a standard for half the product but doesn't
   speak plan / task / annotation / control events natively.

## Decision

Use **gRPC for native clients and gRPC-Web for the browser**, with a single
proto-defined service (`proto/harmonograf/v1/service.proto`) hosting every
RPC — agent-facing and frontend-facing — under one `Harmonograf` service.

Specifically:

- The server runs two listeners: gRPC on one port (`:7531`) and gRPC-Web on
  another (`:7532`). Both speak the same service.
- Protobuf definitions live under `proto/harmonograf/v1/` and are generated
  into Python (server + client lib) and TypeScript (frontend) via `make
  proto`. One schema is the source of truth.
- Streaming RPCs used: `StreamTelemetry` (bidi), `SubscribeControl` (server
  stream), `WatchSession` (server stream), `GetPayload` (server stream).
  Everything else is unary.
- The browser uses the sonora-based gRPC-Web gateway.

gRPC was chosen over plain HTTP/JSON primarily because happens-before on the
telemetry stream (ADR 0005) and the payload-chunk protocol
(`telemetry.proto#PayloadUpload`) both rely on in-order bytes on a single
stream. gRPC gives us that for free; WebSockets would require us to frame
messages ourselves and recreate flow-control semantics.

OTLP was rejected as a telemetry-only choice because it models span trees,
not plans, tasks, annotations, or control events. Adopting OTLP for half the
protocol would leave us with two wire formats for one product and no shared
schema for concepts like `TaskPlan` and `ControlEvent`.

## Consequences

**Good.**
- Same schema, three languages. A proto change regenerates Python server,
  Python client library, and TypeScript frontend with one command. The
  schema cannot drift between components because there is only one.
- Native gRPC gets us real streaming with real flow control, which the
  happens-before ack guarantee (ADR 0005) relies on.
- gRPC-Web lets the browser open a `WatchSession` stream that looks
  identical to a server-streaming RPC a native client would open. The
  frontend code does not have to know it's speaking gRPC-Web.
- Unary RPCs get consistent cancellation, deadlines, and error semantics
  without us writing middleware.

**Bad.**
- gRPC-Web is fiddly. Browser CORS, preflight bytes/str handling, and
  sonora quirks have caused real bugs (see commits `5c00817 server: shim
  sonora CORS preflight bytes/str bug` and `a14e647 server: browser-correct
  CORS + sonora trailer fix`). The gateway layer is thin but not zero.
- Debugging a gRPC stream from curl is hard. Developers reach for
  `grpcurl`, which most shops do not have installed by default. This has a
  real ergonomic cost compared to a JSON/HTTP API.
- The frontend bundle ships protobuf-ts runtime and connect-web, adding a
  few hundred KB gzipped. Not fatal, but non-trivial.
- Running two listeners (`:7531` and `:7532`) means two ports to document in
  operator docs, two ports to forward in demos, two ports to misconfigure.
- We cannot drop gRPC-Web and collapse to one port without either giving up
  the browser client or forcing every browser to speak full gRPC/HTTP2,
  which it cannot.

The gRPC decision compounds nicely with the proto-first data model — the
same `Span` type is wire format and in-memory model on all three sides.
Without gRPC we would still have wanted protobuf for the schema discipline,
and at that point half the value of gRPC is already captured.
