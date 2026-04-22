# AGENTS.md

This file provides guidance to agents — Claude Code, Antigravity, Opencode,
and anyone else — working with code in this repository. Treat it as the
durable project charter.

## Project vision

Harmonograf is the observability + HCI companion to
[goldfive](https://github.com/pedapudi/goldfive) for multi-agent
orchestration. **Goldfive is optional for read-only observation**:
harmonograf works for any agent that emits spans via
`harmonograf_client.Client` or attaches a `HarmonografTelemetryPlugin` to a
bare ADK `App`. Plans, tasks, drift, and the full intervention surface light
up when the run goes through `goldfive.wrap(...)`. See
[`docs/standalone-observability.md`](docs/standalone-observability.md) for
the no-goldfive path and
[`docs/goldfive-integration.md`](docs/goldfive-integration.md) for the
full-orchestration path.

When goldfive *is* in the picture: goldfive owns the plan, the task state
machine, the drift taxonomy, the reporting tools, and the re-invocation loop.
Harmonograf owns the session, the span timeline, the canonical storage, the
frontend (Gantt / Graph / Trajectory / Notes / Settings), the intervention
aggregator, and the control router that lets a human intervene on the same
connection they observe on.

The line is load-bearing: orchestration changes belong in goldfive;
observability and UI changes belong in harmonograf. If a change has to
straddle both, the goldfive-side change lands first and harmonograf follows.

### Proto coupling caveat

Phase A of the goldfive migration (issue #6, COMPLETED) moved plan/task/drift
types into goldfive's proto package. Harmonograf's `TelemetryUp.goldfive_event`
therefore references `goldfive.v1.Event` directly, and the generated pb stubs
import from `goldfive.pb.goldfive.v1`. Consequence: `harmonograf_client`
cannot be *installed* without goldfive present, but user code can (and
should) be goldfive-import-free in the standalone case. The
`standalone-test` CI job enforces this via
`grep -i goldfive examples/standalone_observability/spans_only.py`.

## Repository layout

```
harmonograf/
  server/harmonograf_server/       # gRPC server (asyncio, grpcio + sonora)
    ingest.py                      # telemetry ingest pipeline
    bus.py                         # per-session fan-out bus
    control_router.py              # frontend → agent control routing
    interventions.py               # intervention history aggregator (#69/#71)
    rpc/                           # RPC servicer impls (telemetry, control, frontend)
    storage/                       # SQLite + in-memory stores
    pb/                            # generated Python pb stubs (from proto/)
  client/harmonograf_client/       # Python client library
    client.py                      # top-level handle + identity/heartbeat
    transport.py                   # buffered bidi gRPC transport (lazy Hello)
    buffer.py                      # ring buffer + envelope kinds
    sink.py                        # goldfive.EventSink adapter
    observe.py                     # observe(runner) helper (issue #22)
    telemetry_plugin.py            # ADK BasePlugin for lifecycle spans (#74/#80)
    _control_bridge.py             # control-stream ↔ goldfive.ControlChannel bridge
    pb/                            # generated Python pb stubs
  frontend/                        # React + Vite + TypeScript
    src/components/shell/views/    # ActivityView, GraphView, TrajectoryView, NotesView, SettingsView
    src/components/Interventions/  # intervention timeline strip (#76)
    src/components/Gantt/          # gantt chrome (minimap, legend, ctx badges)
    src/gantt/                     # canvas renderer, layout, drift/span kinds
    src/state/                     # Zustand stores (sessions, popover, ui, annotations)
    src/rpc/                       # connect-rpc client + sessions syncer
    src/pb/                        # generated TS pb stubs
  proto/harmonograf/v1/            # harmonograf proto (imports goldfive/v1/*)
  docs/                            # durable, tree-based documentation
  tests/
    e2e/                           # end-to-end tests
    reference_agents/              # demo/reference ADK agents
  examples/standalone_observability/ # non-goldfive examples
  .agents/skills/                  # structured skill bundles for AI coders
  third_party/                     # adk-python clone lives here (git-ignored)
  Makefile                         # make install / demo / test / proto
```

## High-level architecture

Three components, and changes usually span more than one of them:

1. **Visual frontend** (`frontend/`) — a six-view shell (Sessions, Activity,
   Graph, Trajectory, Notes, Settings). Activity is the Gantt: X-axis is
   time, Y-axis is one row per ADK agent (auto-registered from span attrs
   per harmonograf#74/#80), each block is an agent activity (span, tool
   call, transfer). Trajectory is the intervention-history ribbon
   (#69/#76). Plan, task, and drift state surfaces are rendered from
   goldfive events (`PlanSubmitted`, `PlanRevised`, `TaskStarted`,
   `TaskCompleted`, `TaskFailed`, `DriftDetected`, `AgentInvocationStarted`,
   `DelegationObserved`, …) delivered through the harmonograf server.

2. **Client library** (`client/`) — embedded inside agent processes. Public
   surfaces:
   - `Client` — span transport, payload upload, control-handler
     registration, identity, heartbeat; lazy Hello (harmonograf#85) so no
     ghost session is created until the first real emit.
   - `HarmonografSink` — `goldfive.EventSink` that translates goldfive
     `Event` envelopes into `TelemetryUp.goldfive_event` frames.
   - `HarmonografTelemetryPlugin` — ADK `BasePlugin` that emits spans for
     lifecycle callbacks and stacks per-ADK-agent ids onto each span so the
     server can auto-register one Gantt row per agent in the wrapped tree.
     Dedups itself silently if installed twice (#68).
   - `observe(runner)` — one-line helper that attaches the sink + optional
     control bridge to an existing `goldfive.Runner`. See issue #22.

3. **Server process** (`server/`) — terminates gRPC connections from every
   client and gRPC-Web connections from every frontend. Owns the canonical
   timeline (sessions, agents, spans, annotations, payloads) and the
   derived plan / task / drift index built from goldfive events. Aggregates
   the intervention history view across annotations + drift ring +
   plan-revision metadata (`interventions.py`, `ListInterventions` RPC,
   #69/#71). Merges duplicate user-control interventions inside a 5-minute
   window (#81, #87). Routes control events from the frontend to the
   correct per-agent subscribers.

Key cross-cutting concerns to keep in mind when designing any piece:
- The data model for observability (session, agent, span, payload,
  annotation, control event, intervention) is defined once in
  `proto/harmonograf/v1/*.proto` and shared across all three components.
- Plan, task, drift, and reporting-tool types are defined once in
  `proto/goldfive/v1/*.proto` and imported into harmonograf's proto tree.
  Do not re-declare them in harmonograf.
- The frontend is not read-only — interactions flow back through the server
  to clients, so the client library needs a bidirectional channel, not just
  telemetry egress.
- "Coordinating" implies the server mediates control (pause, resume, steer,
  cancel), not just display — design client APIs with that in mind rather
  than treating it purely as an observability tool.
- Sessions unify the ADK session id (what the user sees in `adk web`) with
  harmonograf's session id. Goldfive events and spans route by
  `ctx.session.id` / per-event `session_id` respectively, so one outer
  session gathers everything the user drove (harmonograf#63/#66).

## Goldfive integration

Harmonograf's protocol carries goldfive events as a first-class variant:

- `proto/harmonograf/v1/telemetry.proto` imports `goldfive/v1/events.proto` and declares a `goldfive.v1.Event goldfive_event` field inside `TelemetryUp`. Plan and task state ride that variant; the old harmonograf-native `task_plan` / `task_status_update` envelopes are retired.
- `proto/harmonograf/v1/types.proto` imports goldfive's `Plan`, `Task`, `TaskEdge`, `TaskStatus`, and `DriftKind` rather than re-declaring them. Harmonograf-owned types (Session, Agent, Span, Payload, Annotation, Control*) stay local.
- `client/harmonograf_client/sink.py` (`HarmonografSink`) is the adapter: `emit(event)` pushes a `GOLDFIVE_EVENT` envelope through the existing ring-buffered transport, which serialises it to a `TelemetryUp(goldfive_event=...)` frame.
- `server/harmonograf_server/ingest.py` dispatches on `event.payload` (oneof) and updates the plan/task index, storage, and the session bus so frontend subscribers see deltas.

Orchestration semantics — session-state keys, reporting tool bodies, drift
taxonomy, refine pipeline, invariant validator, parallel DAG walker — are
goldfive concerns. When a question is "why does the agent state machine behave
this way?", the answer lives in the goldfive repo. When a question is "why did
the timeline render this span where it did?" or "why did this STEER dedupe
with that drift?", the answer lives here.

## Component boundaries

- **Span emission is harmonograf's job.** ADK callbacks (BEFORE/AFTER
  model/tool/run/agent, state_delta, transfer, escalate) are turned into
  spans by `HarmonografTelemetryPlugin`. Goldfive does not produce spans.
- **Per-ADK-agent attribution is harmonograf's job.** The plugin stacks
  `per_agent_id = "{client_id}:{adk_name}"` via before/after_agent callbacks
  and rides agent hints (name, parent, kind, branch, framework) on the first
  span each agent emits. The server's `_ensure_route` auto-registers agents
  from those hints (harmonograf#74/#80).
- **Task state is goldfive's job.** State transitions happen inside
  goldfive's `DefaultSteerer` when a reporting tool is called or when an ADK
  event implies a transition. Harmonograf learns about those transitions
  only through goldfive events on the sink.
- **Control routing is harmonograf's job.** The frontend → server → agent
  path (pause, resume, steer, cancel, annotate) rides harmonograf's control
  stream. Goldfive's `ControlChannel` exposes hooks that let an orchestrated
  run react to delivered control events, but the wire is harmonograf's.
  `ControlBridge` validates STEER body (empty / >8 KiB UTF-8 rejected, ASCII
  control chars stripped) pre-forward; the server stamps
  `SteerPayload.author` + `SteerPayload.annotation_id` before delivery
  (harmonograf#72).
- **Session is harmonograf's job.** Goldfive has a `run_id`; harmonograf
  pairs it with a `session_id` at the storage layer. Under ADK, the outer
  `adk-web` session id is pinned on `goldfive.Session.id`, so one goldfive
  run + its outer ADK chat lane collapse onto one harmonograf session
  (harmonograf#63/#66).
- **Intervention aggregation is harmonograf's job.** `interventions.py`
  derives the chronological list from annotations + ingest drift ring +
  `task_plans.revision_kind`, with a 5-minute merge window for user-control
  kinds and a tight window for autonomous drift (#81/#87). The
  `ListInterventions(session_id)` unary RPC returns it; the frontend has a
  live deriver that mirrors the shape for in-flight deltas.

## Things explicitly deleted from the pre-goldfive era

If you see these in docs or code, they are retired — do not bring them back:

- `HarmonografAgent`, `HarmonografRunner`, `attach_adk`, `make_adk_plugin`, `make_harmonograf_agent`, `make_harmonograf_runner` — replaced by `goldfive.Runner` + `HarmonografSink` + `observe()`.
- `_AdkState`, `PlannerHelper`, `LLMPlanner`, `PassthroughPlanner` — replaced by goldfive's steerer/planner.
- `state_protocol.py` with the `harmonograf.*` session-state keys — replaced by goldfive's `SessionContext`.
- `invariants.py`, `metrics.py` in the client — invariant checking is goldfive's responsibility.
- `TaskPlan` / `UpdatedTaskStatus` envelopes in `TelemetryUp` — replaced by `goldfive_event`.
- Duplicate `Task` / `TaskEdge` / `TaskStatus` / `DriftKind` messages in `types.proto` — imported from goldfive instead.

The full inventory of what moved, what stayed, and what was deleted is in
[docs/goldfive-migration-plan.md](docs/goldfive-migration-plan.md).

## Running tests

From the repo root:

```bash
make test               # server + client + frontend (pnpm build + lint)
make server-test        # just server/tests/ (pytest, asyncio)
make client-test        # just client/tests/
make frontend-test      # pnpm build + pnpm lint
make e2e                # end-to-end tests under tests/e2e/
```

The proto stubs are checked in; only regenerate them (`make proto`) when
you edit `proto/harmonograf/v1/*.proto`. `make proto` resolves goldfive's
proto tree via the installed package (editable-dep-aware; no hardcoded
path).

## PR workflow

- Branch names follow `kind/short-description` or `kind/harmonograf-NN-slug`
  when tied to an issue. Examples: `docs/top-level-refresh`,
  `fix/harmonograf-89-gantt-viewport`.
- Commits, branches, and PR titles do **not** use a Claude / AI co-author
  trailer. The user scrubs them.
- Prefer small PRs. When a change straddles server + client + frontend,
  split by layer when feasible; otherwise call out the three-way coupling
  in the PR body.
- PR CI gates: server pytest, client pytest, frontend `pnpm build &&
  pnpm lint`, `standalone-test` (proves `examples/standalone_observability/
  spans_only.py` has no `goldfive` imports).
- Self-merge after CI green unless the user requests review.

## Documentation layout

Under `docs/`:

- `tour/` — 15-minute tour, mental model, terminology map, front-door index.
- `user-guide/` — UI reference by region (Gantt, Graph, Trajectory, drawer,
  control actions, annotations, keyboard shortcuts, etc.).
- `dev-guide/` — setup, architecture, per-component internals, testing,
  debugging, protos, contribution workflow.
- `protocol/` — wire reference (telemetry, control, frontend-RPC, span
  lifecycle, payload flow, wire-ordering).
- `design/` — per-component design notes (data model, client, server,
  frontend, HCI model, information flow).
- `adr/` — Architecture Decision Records ("we decided X because Y").
- `runbooks/` — triage for common failure modes.
- `internals/` — annotated hot-path tours.
- Top-level: `quickstart.md`, `overview.md`, `operator-quickstart.md`,
  `standalone-observability.md`, `goldfive-integration.md`,
  `goldfive-migration-plan.md` (completed), `reporting-tools.md` (redirect
  to goldfive), `milestones.md`, `index.md`.

For architectural decisions reach for `docs/adr/` first; it lists *why* a
given shape exists, which is usually the thing you need before a refactor.

## Skill system

Structured skill bundles live under `.agents/skills/`, each one a focused
"how to do X" recipe with explicit file touchpoints. Categories include:

- Proto edits: `hgraf-add-proto-field`, `hgraf-add-proto-message`,
  `hgraf-bump-proto-version`.
- Taxonomy/vocabulary additions: `hgraf-add-drift-kind`,
  `hgraf-add-span-kind`, `hgraf-add-control-kind`,
  `hgraf-add-annotation-kind`, `hgraf-add-capability`.
- Frontend UX: `hgraf-add-drawer-tab`, `hgraf-add-gantt-overlay`,
  `hgraf-add-renderer-overlay`, `hgraf-add-keyboard-shortcut`,
  `hgraf-update-frontend-component`.
- Ops / debugging: `hgraf-debug-frontend-state`, `hgraf-debug-task-stuck`,
  `hgraf-profile-callback-perf`, `hgraf-tune-heartbeat`,
  `hgraf-interpret-invariant-violations`, `hgraf-run-demo`.
- Workflow: `hgraf-review-pr`, `hgraf-spawn-agent-team`,
  `hgraf-write-e2e-scenario`, `hgraf-read-memory-bank`.

When you're about to do one of these things, load the matching skill first
— they encode the exact list of files to touch.
