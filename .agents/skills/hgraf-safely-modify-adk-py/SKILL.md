---
name: hgraf-safely-modify-adk-py
description: Retained for history. `client/harmonograf_client/adk.py` is gone — the ADK adapter logic moved to goldfive. For ADK plugin work today, see the small `telemetry_plugin.py`.
---

# hgraf-safely-modify-adk-py

## This skill is historical

`client/harmonograf_client/adk.py` (the 5900-line ADK adapter that
owned drift detection, plan walker, reporting-tool interception, and
the task state machine) was deleted during the goldfive migration
(issue #2, mid-2025). That surface is gone.

The current harmonograf client has:

- `client/harmonograf_client/telemetry_plugin.py` (~1150 lines) —
  an ADK `BasePlugin` that emits spans from ADK lifecycle callbacks.
  Pure observability, no orchestration, no invariants.
- `client/harmonograf_client/sink.py` — `HarmonografSink`, a
  `goldfive.EventSink` that passes `goldfive.v1.Event`s through the
  transport as `TelemetryUp.goldfive_event`.
- `client/harmonograf_client/_control_bridge.py` — the bridge between
  harmonograf's control channel and goldfive's steerer. Does STEER
  body validation (#72) and forwards controls.

## If you were reaching for this skill today

Pick the right one:

- **Editing `telemetry_plugin.py`** — read the inline docstring at
  the top of the file and the "Per-agent attribution (#80)" section
  in `dev-guide/client-library.md`. Invariants to preserve:
  per-invocation FIFO for model-call pairing; dedup guard
  (`_maybe_disable_as_duplicate`); root-session rollup; cancellation
  sweep (`on_cancellation`); `hgraf.agent.*` first-sight stamp.

- **Editing `_control_bridge.py`** — preserve STEER body validation
  (`STEER_BODY_MAX_BYTES = 8192`), control-character sanitation, and
  `author` / `annotation_id` propagation.

- **Orchestration logic** — goldfive repo. Look at
  `goldfive.adapters.adk.ADKAdapter`, `goldfive.DefaultSteerer`,
  `goldfive.planner.*`, `goldfive.detectors.*`.

- **Task state machine / invariants** — goldfive. Harmonograf has
  no invariant validator for task state post-migration.

## Still-applicable general discipline

A few discipline items that apply to any ADK-side Python code in
this repo:

1. **Telemetry must not raise.** Every callback in
   `telemetry_plugin.py` swallows exceptions with a DEBUG log.
   Breaking this makes a telemetry bug break the user's agent.
2. **Don't block in callbacks.** ADK callbacks run on the event
   loop. Anything longer than a ring-buffer push belongs behind a
   queue.
3. **Context handoff between Context objects is fragile.** ADK
   rebuilds `CallbackContext` between before/after hooks. Use
   `invocation_id` as the key, not `id(ctx)`. The plugin does this;
   preserve it.
4. **Duplicate-install guard.** The plugin can be installed twice
   under `goldfive.wrap` + `adk web`. Don't route any state through
   a side channel that the dedup guard wouldn't silence. See #68.

## Cross-links

- `dev-guide/client-library.md` — the small modern client surface.
- `hgraf-update-frontend-component` — the skill for harmonograf UI
  changes.
- goldfive repo — everything orchestration-shaped.
