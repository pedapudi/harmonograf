# 12. Client library and ADK integration

> **SUPERSEDED (goldfive migration).** The body of this page described the
> pre-goldfive client (`HarmonografAgent`, `_AdkState`, `attach_adk`,
> in-client reporting-tool dispatch, the in-client plan walker). All of
> that moved to [goldfive](https://github.com/pedapudi/goldfive) during
> Phases A–D of the migration. The content is preserved in git history;
> the page itself is now a redirect so nobody reads a plan-walker sequence
> diagram and thinks harmonograf still owns it.

## Where to go instead

| Topic | Current home |
|---|---|
| What the client library actually is today (`Client`, `HarmonografSink`, `HarmonografTelemetryPlugin`) | [../goldfive-integration.md](../goldfive-integration.md) |
| Why the orchestration pieces moved out | [../goldfive-migration-plan.md](../goldfive-migration-plan.md) |
| Plan / task / drift semantics, reporting tools, steerer, planner | goldfive — [github.com/pedapudi/goldfive](https://github.com/pedapudi/goldfive) |
| ADK lifecycle callback mapping (still lives in harmonograf as span emission) | [../dev-guide/client-library.md](../dev-guide/client-library.md) |
| Ring buffer, transport, reconnect, control handlers | [02-client-library.md](02-client-library.md) (also marked superseded, but accurate about the transport layer) |

## What is still true from the old content

- The client never blocks the agent. Public emit paths push into the ring
  buffer and return in microseconds; serialization and I/O happen on a
  background worker.
- Control handlers are registered per-`ControlKind` and dispatched by the
  transport worker — independent of the telemetry send path.
- `HarmonografTelemetryPlugin` is an ADK `BasePlugin` that emits spans
  for `before_run` / `after_run` / `before_model` / `after_model` /
  `before_tool` / `after_tool` / `on_event` callbacks. It does not
  touch orchestration state.

## What is no longer true

- Harmonograf does **not** own `_AdkState`, reporting-tool dispatch, the
  plan walker, the reinvocation loop, drift detection, invariants, or
  the `harmonograf.*` session-state keys. Those are goldfive's.
- `attach_adk`, `make_adk_plugin`, `make_harmonograf_agent`,
  `make_harmonograf_runner`, `HarmonografAgent`, `HarmonografRunner`,
  `LLMPlanner`, `PassthroughPlanner`, `PlannerHelper` are removed from
  `harmonograf_client`. Use `goldfive.Runner` + `goldfive.adapters.adk.ADKAdapter`
  and attach a `HarmonografSink` + optional `HarmonografTelemetryPlugin`.
