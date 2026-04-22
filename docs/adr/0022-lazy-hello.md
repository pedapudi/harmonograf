# ADR 0022 — Lazy Hello: defer the stream-opening RPC until first emit

## Status

Accepted (2026-04, harmonograf #83 / #85).

## Context

The pre-2026-04 client library sent `Hello` immediately on
`StreamTelemetry` open — before any span had been emitted. This
produced two problems:

1. **Ghost sessions on the Gantt.** A long-lived module-level
   `Client` (the usual presentation-agent / adk-web pattern)
   connects at process start but doesn't emit anything until the
   user triggers an ADK invocation. The server would happily create
   a `sess_<date>_<nnnn>` row from the Hello and populate the
   session picker with it, even though no spans, tasks, or events
   ever landed. Pairs of these ghost rows accumulated with every
   restart.
2. **Session id can't be derived from the first span.** The
   harmonograf telemetry plugin derives the session id from
   `ctx.session.id` inside the first `before_run_callback` (see
   [ADR 0021](0021-session-id-pinning.md)). A Hello sent at stream
   open has to use whatever session id the Client was constructed
   with — not the actual adk-web session — so the server
   auto-creates a second session and the first real span has to be
   routed to it via per-span session-id override. Non-ADK clients
   that use a similar pattern lost their session rollup entirely.

## Decision

The client transport defers the `Hello` RPC until the first real
`TelemetryUp` is ready to send. When the first envelope carries a
`session_id` (because the plugin stamped the outer adk-web session id
on it), the transport uses that as `Hello.session_id`. The result:
the home session that Hello creates is already the correct session,
from the very first event.

For non-ADK clients that never emit, no Hello is sent — no session
row appears, no ghost entries in the picker, no storage footprint.

See `_maybe_send_hello` in
[`client/harmonograf_client/transport.py`](../../client/harmonograf_client/transport.py).

## Consequences

**Good.**
- No ghost `sess_*` rows from processes that open a Client but
  never emit. The session picker shows exactly the set of sessions
  that actually produced events.
- Combined with [ADR 0021](0021-session-id-pinning.md), one adk-web
  run = one harmonograf session, visible from the very first span.
- Non-ADK clients get session rollup "for free" when they stamp a
  `session_id` on their first emit — the home session is stamped
  with that id rather than whatever the Client was constructed with.
- Recoverable: if an agent starts emitting and then disconnects, the
  reconnect path re-runs lazy Hello against the reconnect's first
  envelope. Session id stays consistent.

**Bad.**
- Welcome is also delayed until the first emit. Control subscribe
  cannot open before Welcome, so `SubscribeControl` is correspondingly
  delayed. This is fine in practice — there are no control events to
  deliver before the agent has done anything — but operators
  debugging control delivery must understand that `SubscribeControl`
  appearing after the first span is expected, not a bug.
- The server sees a brief open-without-Hello state on reconnects.
  The transport is careful to send Hello before any non-Hello
  envelope to preserve the per-stream invariant (`Hello must be
  first`), but the stream is nominally "open" for a few
  milliseconds before that first Hello lands.
- Reconnect logic is slightly more subtle: the transport must clear
  `_hello_sent` on reconnect and re-derive the session id from the
  next envelope, not from the one that was in flight when the old
  stream died.

## Implemented in

- [`client/harmonograf_client/transport.py`](../../client/harmonograf_client/transport.py) — `_maybe_send_hello`, `_hello_sent` flag, `_session_id_of_envelope`.
- [Protocol — telemetry-stream](../protocol/telemetry-stream.md) — Hello semantics and the lazy-emit contract.

## See also

- [ADR 0021 — Session id pinning](0021-session-id-pinning.md).
