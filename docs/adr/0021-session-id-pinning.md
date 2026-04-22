# ADR 0021 — Pin `goldfive.Session.id` to the outer adk-web session id

## Status

Accepted (2026-04, harmonograf #65 / goldfive #161 / harmonograf #62).

## Context

A single `goldfive.wrap`'d adk-web run drives a tree of ADK agents:
coordinator, specialists, `AgentTool` wrappers, sequential/parallel
containers. ADK `AgentTool` builds its own sub-`Runner` inside the tool
call, and that sub-Runner mints a fresh `InMemorySessionService` with a
brand-new session id. Left alone, every sub-Runner produces its own
`Hello` with its own session id, and one adk-web run fans out across N
harmonograf sessions — one per sub-Runner — on the Gantt.

Worse, the plan view shows `goldfive.Session.id`, which was also being
minted per sub-Runner. The plan ended up on the outer session and the
execution spans on sub-Runner sessions, so the plan pane and the Gantt
pane were rendering different session rows for the same run.

## Decision

The `HarmonografTelemetryPlugin` caches the outer adk-web
`ctx.session.id` on the ROOT `before_run_callback` and stamps it on
every span emitted for the rest of the run — root, sub-Runners, and all.
The per-ctx sub-Runner session id is still captured as the
`adk.session_id` span attribute for forensic debugging.

On the goldfive side (goldfive #161), `goldfive.Session.id` is pinned
to the same outer adk-web session id at `goldfive.wrap` time, so plan
events carry the same `session_id` the spans do.

See `HarmonografTelemetryPlugin.__init__` / `before_run_callback` /
`_stamp_session_id` in
[`client/harmonograf_client/telemetry_plugin.py`](../../client/harmonograf_client/telemetry_plugin.py).

## Consequences

**Good.**
- One adk-web run → one harmonograf session row → one unified timeline.
- The plan pane and the Gantt render on the same session, so
  operators never see "my plan is on session A but the spans are on
  session B."
- Session switching stays stable across sub-Runner invocations —
  closing and reopening the session picker doesn't produce a ghost
  session per `AgentTool` call.

**Bad.**
- The harmonograf server has no way to distinguish sub-Runner
  activity from root-Runner activity beyond the `adk.session_id`
  attribute. Forensic users who want per-sub-Runner analysis have to
  dig into span attributes.
- Non-ADK clients don't automatically get this behavior — they rely
  on whatever their own SDK's session concept produces. That's why
  the lazy-Hello ADR (0022) exists as a complementary fix.
- If the outer adk-web session id is missing or malformed for some
  reason, the plugin falls back to the per-ctx id and the old
  behavior returns. This is intentional — a weird ADK install
  should still produce a working (if split) session rather than no
  session at all.

## Implemented in

- [`client/harmonograf_client/telemetry_plugin.py`](../../client/harmonograf_client/telemetry_plugin.py)
- Goldfive: `Session.id` pinned at `goldfive.wrap` call site (goldfive #161).
- [Design 12 — Client library and ADK](../design/12-client-library-and-adk.md)

## See also

- [ADR 0022 — Lazy Hello](0022-lazy-hello.md) — complementary fix for
  non-ADK clients and multi-session adk-web trees.
