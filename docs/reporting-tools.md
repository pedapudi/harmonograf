# Reporting tools

> **Owned by goldfive.** After the goldfive migration, reporting tools
> (`report_task_started`, `report_task_progress`, `report_task_completed`,
> `report_task_failed`, `report_task_blocked`,
> `report_new_work_discovered`, `report_plan_divergence`) live in
> [goldfive](https://github.com/pedapudi/goldfive). The canonical
> reference, behaviour, and side-effect table are in the goldfive docs —
> this page is a redirect so stale bookmarks don't mislead.

## Where to look

- **Reference**: `goldfive.reporting` (`proto/goldfive/v1/reporting_tools.proto`
  + `goldfive.reporting` Python module + `goldfive.BUILTIN_REPORTING_TOOLS`).
- **Where they are injected**: `goldfive.adapters.adk.ADKAdapter` augments
  every sub-agent's instruction with the reporting-tool appendix and wires
  the function tools into ADK.
- **How the state transition happens**: `goldfive.DefaultSteerer` owns the
  monotonic task state machine and reacts to reporting-tool calls to fire
  `TaskStarted` / `TaskCompleted` / `TaskFailed` / `DriftDetected` events.

## Tool-loop detector exemption

Goldfive runs a tool-loop detector that treats consecutive identical tool
calls as drift. The reporting tools are **exempt** from that detector —
progress-reporting resets the detection window — so agents calling
`report_task_progress` repeatedly to announce fine-grained progress don't
accidentally trigger a drift escalation. See the detector docs in the
goldfive repo for the exact semantics.

## State protocol keys

The reporting tools update goldfive-side state (via
`DefaultSteerer` + `SessionContext`); harmonograf does not intercept them
and does not own `harmonograf.*` session-state keys (those were retired
with the pre-goldfive orchestrator). The keys the tools write live in
goldfive's `SessionContext` on ADK `session.state`, documented in the
goldfive session-state reference.

## Harmonograf's role

Harmonograf surfaces these events in the frontend — per-task bars on the
Gantt, the plan-revision banner, the task panel, and every intervention
marker on the Trajectory view — because `HarmonografSink` ships every
goldfive `Event` through `TelemetryUp.goldfive_event` (see
[goldfive-integration.md](goldfive-integration.md)). Harmonograf never
intercepts reporting tools, never applies state transitions, and never
writes session-state keys on its own.
