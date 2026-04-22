---
name: hgraf-read-memory-bank
description: Orient on harmonograf before making changes. Read AGENTS.md, the dev guide, design docs, and key source files in the right order.
---

# hgraf-read-memory-bank

## When to use

You're starting work on a harmonograf task and need orientation on a
subsystem you haven't touched. This is the "first 10 minutes of any
non-trivial task" skill.

## Reading order

### Layer 1 — project-level (always read first)

1. **`AGENTS.md`** — project vision + high-level architecture.
2. **`README.md`** — external framing and quickstart.
3. **`docs/dev-guide/index.md`** — map of the developer documentation.

### Layer 2 — dev-guide chapters (pick based on task)

- **`dev-guide/setup.md`** — boot the stack locally.
- **`dev-guide/architecture.md`** — the three-component (client /
  server / frontend) map.
- **`dev-guide/client-library.md`** — `HarmonografTelemetryPlugin`,
  `HarmonografSink`, lazy Hello, per-agent attribution, plugin
  dedup, cancellation sweep.
- **`dev-guide/server.md`** — ingest pipeline, bus, storage,
  auto-register agents, intervention aggregator, RPC surface
  (`WatchSession`, `ListInterventions`, `PostAnnotation`, …).
- **`dev-guide/frontend.md`** — stores, views, InterventionsTimeline,
  per-agent Gantt rows.
- **`dev-guide/working-with-protos.md`** — proto layout,
  forward-compat rules, codegen.
- **`dev-guide/storage-backends.md`** — Store ABC + SQLite schema.
- **`dev-guide/migration.md`** — goldfive migration, overlay era,
  lazy Hello era, per-agent rows era.

### Layer 3 — user-guide (for UX context)

- **`user-guide/trajectory-view.md`** — the plan review and
  intervention history surface. Read before touching intervention-
  related code.
- **`user-guide/gantt-view.md`** — per-agent rows, cross-agent edges,
  cross-links to controls.
- **`user-guide/tasks-and-plans.md`** — the overlay-era task state
  machine and the drift kind taxonomy.
- **`user-guide/control-actions.md`** — STEER / CANCEL / PAUSE
  semantics, body validation, idempotency.

### Layer 4 — design docs

`docs/design/` numbered ADRs and architecture notes. Look up by
topic (data model, protocol, internals). Often older than the
dev-guide — cross-check.

### Layer 5 — runbooks

`docs/runbooks/*.md` — operator-oriented symptom → cause → fix.
Useful even when writing code because they enumerate real failure
modes.

## Key source anchors

When the docs and the code disagree, **the code is the ground truth**.
Anchor your reading with these files:

- Client plugin: `client/harmonograf_client/telemetry_plugin.py`
  (~1150 lines).
- Client sink: `client/harmonograf_client/sink.py`.
- Client control bridge: `client/harmonograf_client/_control_bridge.py`.
- Server ingest: `server/harmonograf_server/ingest.py`.
- Server intervention aggregator: `server/harmonograf_server/interventions.py`.
- Server storage: `server/harmonograf_server/storage/sqlite.py`.
- Proto: `proto/harmonograf/v1/{types,telemetry,frontend,control,service}.proto`.
- Frontend session store: `frontend/src/gantt/index.ts`.
- Frontend intervention deriver: `frontend/src/lib/interventions.ts`.
- Frontend InterventionsTimeline: `frontend/src/components/Interventions/InterventionsTimeline.tsx`.
- Keyboard shortcuts: `frontend/src/lib/shortcuts.ts`.

## Task → recommended reading

| Task | Read in this order |
|---|---|
| Add a drift kind | goldfive docs first → `user-guide/tasks-and-plans.md` → `dev-guide/server.md#listinterventions-71` → `frontend/src/lib/interventions.ts` |
| Add a proto field | `dev-guide/working-with-protos.md` → relevant `.proto` file → `convert.py` → frontend `rpc/convert.ts` |
| Modify ingest | `dev-guide/server.md` → `server/harmonograf_server/ingest.py` |
| Add a Gantt overlay | `dev-guide/frontend.md` → `frontend/src/gantt/renderer.ts` → `ContextWindowBadgeStrip` as a reference |
| Debug stuck task | `runbooks/task-stuck-in-{pending,running}.md` → `dev-guide/debugging.md` |
| Understand the intervention timeline | `user-guide/trajectory-view.md` → `server/harmonograf_server/interventions.py` → `frontend/src/lib/interventions.ts` + `InterventionsTimeline.tsx` |

## Verification

You've read enough when you can answer:

1. Which harmonograf surface does my change cross (proto, client,
   server, frontend, storage)?
2. Which goldfive surface does my change interact with (events,
   detectors, planner, steerer)? Is this actually a goldfive change?
3. Does my change affect the intervention aggregator? If yes, do
   both the server (`interventions.py`) and the frontend
   (`lib/interventions.ts`) need updates?
4. Does my change require a sqlite migration? If yes, update
   `SCHEMA` + the backfill block in `SqliteStore.start()`.
5. Does my change affect the InterventionsTimeline's render? If
   yes, eyeball the dev-guide/frontend.md "InterventionsTimeline"
   section and verify the component's invariants (stable X anchor,
   clustering, deterministic popover).
