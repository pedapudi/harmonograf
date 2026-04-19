# Task state machine and plan-execution protocol

> **Owned by goldfive.** The task state machine, drift taxonomy, plan /
> task data model, and reporting-tool dispatch are all in
> [goldfive](https://github.com/pedapudi/goldfive). This page used to
> describe `_AdkState`, the `harmonograf.*` session-state keys,
> `PlannerHelper.refine()`, and the invariant checker — none of which
> exist in harmonograf anymore. The body is collapsed to a redirect
> rather than preserved in partial form; the full pre-migration text is
> in git history.

## What harmonograf still owns

- **The wire format that carries the state machine events.** The
  telemetry stream transports goldfive `Event` messages inside a
  `TelemetryUp.goldfive_event` variant (field 11). See
  [telemetry-stream.md](telemetry-stream.md).
- **The server's plan / task index.** `server/harmonograf_server/ingest.py`
  dispatches on the `Event.payload` oneof (`RunStarted`, `GoalDerived`,
  `PlanSubmitted`, `PlanRevised`, `TaskStarted`, `TaskCompleted`,
  `TaskFailed`, `DriftDetected`, `RunCompleted`, `RunAborted`) and
  persists the resulting plan / task rows so frontends can read them on
  `GetSession` and reduce them on `WatchSession`.
- **The frontend's `TaskRegistry` and plan-diff drawer.** The frontend
  subscribes to `SessionUpdate.goldfive_event` deltas and renders the
  current plan, per-task status, drift markers, and `PlanRevised`
  diffs.

## What lives in goldfive

- Task state machine itself and its monotonic invariants.
- Drift taxonomy and classification (`goldfive.drift`, `goldfive.classify_*`).
- Reporting tools (`goldfive.BUILTIN_REPORTING_TOOLS`, see
  `goldfive.reporting`).
- Session-state coordination (`goldfive.adapters.adk.SessionContext`).
- The refine pipeline (`goldfive.planner.LLMPlanner.refine`, drift ->
  refine wiring inside `goldfive.DefaultSteerer`).
- Plan / task / drift types (`goldfive.v1.Plan`, `Task`, `TaskEdge`,
  `TaskStatus`, `DriftKind`).

## See also

- [../goldfive-integration.md](../goldfive-integration.md) — how
  harmonograf consumes goldfive.
- [../goldfive-migration-plan.md](../goldfive-migration-plan.md) — the
  design record of which pieces moved.
- [../internals/server-ingest-bus.md](../internals/server-ingest-bus.md) —
  how the server dispatches `goldfive_event` variants.
