# ADR 0024 — Per-ADK-agent Gantt rows via plugin-side id stacking and server auto-register

## Status

Accepted (2026-04, harmonograf #74 / #80).

## Context

Before this change, the harmonograf Gantt had one row per client
process — i.e. one row per `Client` instance. A `goldfive.wrap` run
with a coordinator agent delegating to specialists via `AgentTool`
rendered as a single row with every tool call stacked on top of the
coordinator's invocation. Operators couldn't tell which agent was
doing which work; the one-row-per-process collapsing was load-bearing
but completely hid multi-agent dynamics from the UI.

The obvious fix — register every ADK agent in the tree as a separate
harmonograf `Agent` — has two sub-problems:

1. **Identity:** the harmonograf `agent_id` must be stable per
   `(client, adk-agent-name)` pair across reconnects and restarts.
2. **Registration:** the server must learn about a new agent without
   requiring the client to emit a dedicated wire event (that would
   require a proto change and a new ingest path).

## Decision

### Plugin-side: stack per-agent ids on callback entry

`HarmonografTelemetryPlugin.before_agent_callback` derives a
per-agent id `{client_id}:{adk-agent-name}` and pushes it onto a
per-invocation stack. Every span emitted while that agent is active
carries that id as its `agent_id`. `after_agent_callback` pops.

The stack handles nested agents correctly: a coordinator calling
`AgentTool(specialist)` yields
`coordinator-id → coordinator-id:coordinator → coordinator-id:specialist`
and the specialist's LLM/tool spans land on the specialist row, while
the coordinator's own spans stay on the coordinator row.

### Plugin-side: metadata hints on the first span

Because the server has never seen this per-agent id before, the first
span it ever emits carries four optional attributes that describe the
agent:

- `hgraf.agent.name` — the human-readable ADK agent name.
- `hgraf.agent.parent_id` — derived per-agent id of the immediate
  parent in the ADK tree, if any.
- `hgraf.agent.kind` — ADK-framework hint (`coordinator`, `specialist`,
  `agent_tool`, `sequential_container`, etc.).
- `hgraf.agent.branch` — ADK's dotted ancestry string
  (`root.coordinator.specialist`), kept for forensic debugging.

### Server-side: harvest hints and auto-register

`IngestPipeline._ensure_route` runs on every SpanStart. If it's the
first time it's seen a given `(session_id, agent_id)` pair, it
inspects the span's attributes for `hgraf.agent.*` keys and populates
the `Agent.metadata` accordingly:

- `adk.agent.name` = `hgraf.agent.name`
- `harmonograf.parent_agent_id` = `hgraf.agent.parent_id`
- `harmonograf.agent_kind` = `hgraf.agent.kind`
- `adk.agent.branch` = `hgraf.agent.branch`

It then writes an `Agent` row (framework=ADK when the name hint is
present) and fans out an `AgentJoined` delta on the bus. Subsequent
spans from the same agent take the short path (`seen_routes`
short-circuits the whole method).

See `_ensure_route` in
[`server/harmonograf_server/ingest.py`](../../server/harmonograf_server/ingest.py)
and `_register_agent_for_ctx` in
[`client/harmonograf_client/telemetry_plugin.py`](../../client/harmonograf_client/telemetry_plugin.py).

## Consequences

**Good.**
- The Gantt renders one row per ADK agent in the tree. Coordinator /
  specialist / AgentTool / container agents all get their own row and
  the parent-child structure is reconstructable from
  `harmonograf.parent_agent_id` metadata.
- Back-compatible: clients running the old plugin (or non-ADK
  clients) don't emit hints. The server treats them as single-row
  agents and nothing breaks.
- No new wire event. The `hgraf.agent.*` attributes ride on a
  first-span-only stamp — hot-path spans don't pay the cost after
  the first-sight.
- The server decides the human-readable display name using
  `hgraf.agent.name` when present, falling back to the bare span
  name and finally to the agent_id. This avoids rendering an LLM
  model name (the usual `span.name` for `LLM_CALL`) as an agent
  label.

**Bad.**
- The plugin's stack is per-invocation. If ADK fires
  `before_agent_callback` from a code path that bypasses the
  invocation-id propagation (rare but possible with custom callback
  contexts), the stack misses the push and spans land on the root.
  A degraded path in `_stamp_agent_attrs` handles the case by
  stamping a minimal `hgraf.agent.name` derived from the id suffix.
- Agents never removed from the Gantt. If an ADK agent briefly
  exists and then is never invoked again, its row persists for the
  life of the session. This is the right tradeoff — a row that
  disappeared mid-session would be confusing — but it means
  long-running processes accumulate agent rows indefinitely.
- The alias map that `ControlRouter.register_alias` maintains maps
  the ADK agent name to the stream's root agent id. Control sent
  to the ADK-agent display name is forwarded to the stream that
  actually owns it, but the agent itself sees the control event
  at the root level — per-ADK-agent control is not implemented.

## Implemented in

- [`client/harmonograf_client/telemetry_plugin.py`](../../client/harmonograf_client/telemetry_plugin.py)
- [`server/harmonograf_server/ingest.py`](../../server/harmonograf_server/ingest.py) — `_ensure_route`, `_extract_agent_hints`.
- [Design 04 — Frontend and interaction](../design/04-frontend-and-interaction.md)
- [Design 12 — Client library and ADK](../design/12-client-library-and-adk.md)
