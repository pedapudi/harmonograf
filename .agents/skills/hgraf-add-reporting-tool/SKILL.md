---
name: hgraf-add-reporting-tool
description: Add a new reporting tool. Reporting tools moved to goldfive post-migration; harmonograf no longer owns `client/harmonograf_client/tools.py`. This skill redirects to goldfive and covers the harmonograf-side touchpoints.
---

# hgraf-add-reporting-tool

## Post-goldfive-migration scope

Reporting-tool definitions, interception, and the task-state side
effects moved to [goldfive](https://github.com/pedapudi/goldfive)
under issue #2. Harmonograf does NOT own any of:

- `client/harmonograf_client/tools.py` (deleted)
- `REPORTING_TOOL_FUNCTIONS`, `build_reporting_function_tools`,
  `SUB_AGENT_INSTRUCTION_APPENDIX` (moved to `goldfive.reporting`)
- The interception logic (lives in
  `goldfive.adapters.adk.ADKAdapter` /
  `goldfive.DefaultSteerer`)

If you want a new reporting tool, follow goldfive's
`add-reporting-tool` guide. When goldfive ships the new tool, it
emits an `Event` variant (e.g. `TaskStarted`, `TaskProgress`) that
already rides on the wire via `TelemetryUp.goldfive_event` — no
harmonograf wire change needed.

## Harmonograf-side touchpoints

If the new tool surfaces a fresh UI concept (new event kind, new
drawer tab, new intervention source), you have work on the harmonograf
side too:

### 1. A new `Event` variant

If goldfive defines a new event on
`proto/goldfive/v1/events.proto`:

- Update `server/harmonograf_server/ingest.py`'s
  `_handle_goldfive_event` dispatch to match on the new variant.
- Convert to storage if durable state is needed. Usually the new
  event is just a delta the bus can fan out unchanged.
- Add `SessionUpdate` oneof handling if the frontend consumes it
  (`proto/harmonograf/v1/frontend.proto`).
- Extend `frontend/src/rpc/goldfiveEvent.ts` to typed-convert.

### 2. A new intervention source

If the tool surfaces a new kind of user or goldfive-autonomous
intervention (e.g. "ask-human" becomes a visible intervention,
not just a span), update the aggregator:

- `server/harmonograf_server/interventions.py` — extend
  `_project_drifts` / `_project_plans` if needed.
- `frontend/src/lib/interventions.ts` — mirror any classification
  change.

See `hgraf-add-drift-kind` for the aggregator-side specifics.

### 3. A new span attribute

If the tool stamps a span attribute you want the UI to surface,
update:

- `frontend/src/rpc/convert.ts` — pass the attribute through to
  the frontend `Span` type.
- Drawer Summary tab — already renders arbitrary attributes via
  the attribute table; no wiring needed unless you want pretty
  rendering.

## Verification

```bash
# Server: goldfive_event ingest
cd server && uv run pytest tests/test_ingest_goldfive_event.py -q

# Frontend: event converter
cd frontend && pnpm test -- --run goldfiveEvent
```

## Cross-links

- [goldfive's reporting-tool guide](https://github.com/pedapudi/goldfive/blob/main/docs/how-to/add-reporting-tool.md)
  (authoritative).
- `hgraf-add-drift-kind` — when the new tool's side effects should
  surface as a drift on the intervention timeline.
- `dev-guide/server.md` — goldfive_event ingest.
