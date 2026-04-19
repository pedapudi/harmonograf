# Reporting tools

> **Owned by goldfive.** After the goldfive migration, reporting tools
> (`report_task_started`, `report_task_completed`, `report_task_failed`,
> `report_task_blocked`, `report_task_progress`, `report_new_work_discovered`,
> `report_plan_divergence`) live in [goldfive](https://github.com/pedapudi/goldfive).
> The canonical reference, behaviour, and side-effect table are in the
> goldfive docs — this page is a redirect so stale bookmarks don't mislead.

For current behaviour:

- **Reference**: `goldfive.reporting` (`proto/goldfive/v1/reporting_tools.proto`
  + `goldfive.reporting` Python module + `goldfive.BUILTIN_REPORTING_TOOLS`).
- **Where they are injected**: `goldfive.adapters.adk.ADKAdapter` augments
  every sub-agent's instruction with the reporting-tool appendix and wires
  the function tools into ADK.
- **How the state transition happens**: `goldfive.DefaultSteerer` owns the
  monotonic task state machine and reacts to reporting-tool calls to fire
  `TaskStarted` / `TaskCompleted` / `TaskFailed` / `DriftDetected` events.

From harmonograf's side, those events surface in the frontend as the
plan/task/drift views because `HarmonografSink` ships every goldfive
`Event` through `TelemetryUp.goldfive_event` (see
[goldfive-integration.md](goldfive-integration.md)). Harmonograf does not
intercept reporting tools, does not apply state transitions, and does not
own the `harmonograf.*` session-state keys that the old design used.
