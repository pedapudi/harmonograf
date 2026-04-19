# Drift taxonomy catalog

> **Owned by goldfive.** The drift taxonomy — kinds, severities,
> recoverable flags, fire sites, and the refine-throttle logic — is
> maintained in [goldfive](https://github.com/pedapudi/goldfive).
> Harmonograf receives each drift event as a `DriftDetected` variant on
> `TelemetryUp.goldfive_event` and renders it in the UI; it does not
> define the kinds or fire them. The pre-migration catalog of 29 kinds
> with `adk.py` line numbers is preserved in git history.

## What harmonograf still owns

- **`frontend/src/gantt/driftKinds.ts`** maps each known
  `goldfive.v1.DriftKind` to a UI badge — glyph, color, label, category
  (refusal / merge / split / reorder / divergence / …). When goldfive
  introduces a new kind, add it here.
- **`docs/internals/renderer-pipeline.md`** describes how drift markers
  compose onto the Gantt.

## Where to look for drift logic

- Kind definitions: `goldfive.DriftKind` enum + `proto/goldfive/v1/types.proto`.
- Fire sites: `goldfive.DefaultSteerer`, `goldfive.drift`, the
  classifier helpers `classify_refusal`, `classify_stop_reason`,
  `classify_tool_error`.
- Refine throttle and severity routing: `goldfive.DefaultSteerer`.

## See also

- [../goldfive-integration.md](../goldfive-integration.md) — how
  goldfive events arrive at harmonograf.
- [../protocol/task-state-machine.md](../protocol/task-state-machine.md)
  — where task-state transitions come from.
