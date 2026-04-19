# AGENTS.md

This file provides guidance to agents, including Claude Code, Antigravity, Opencode, when working with code in this repository.

## Project vision

Harmonograf is the observability console for agent workflows orchestrated by
[goldfive](https://github.com/pedapudi/goldfive). Goldfive owns the plan, the
task state machine, the drift taxonomy, the reporting tools, and the re-invocation
loop. Harmonograf owns the session, the span timeline, the canonical storage, the
Gantt/graph frontend, and the control router that lets a human intervene on the
same connection they observe on.

The line is load-bearing: orchestration changes belong in goldfive; observability
and UI changes belong in harmonograf. If a change has to straddle both, the
goldfive-side change lands first and harmonograf follows.

## High-level architecture

Three components, and changes usually span more than one of them:

1. **Visual frontend** (`frontend/`) — a Gantt-chart-style view. X-axis is time, Y-axis is one row per agent, and each block represents an agent activity (span, tool call, transfer). Blocks are interactive (clickable) to drill into details. Plan and task state surfaces are rendered from goldfive events (`PlanSubmitted`, `PlanRevised`, `TaskStarted`, `TaskCompleted`, `TaskFailed`, `DriftDetected`, …) delivered through the harmonograf server. This is the human-facing surface for observing and coordinating agents.

2. **Client library** (`client/`) — embedded inside agent processes. Two public surfaces: `Client` handles the span transport, payload upload, control-handler registration, identity, heartbeat; `HarmonografSink` is a `goldfive.EventSink` that translates goldfive `Event` envelopes into harmonograf `TelemetryUp` frames. Users construct a `goldfive.Runner`, install a `HarmonografSink`, and the run's plan/task/drift events reach the frontend alongside the span stream. An optional `HarmonografTelemetryPlugin` (ADK `BasePlugin`) emits spans for ADK callbacks when users want framework-level telemetry.

3. **Server process** (`server/`) — hosts the frontend and terminates connections from client libraries across all participating agents. It is the fan-in point: many clients, one server, one UI. It owns the canonical timeline (sessions, agents, spans, annotations, payloads) and the derived plan/task index built from goldfive events. It is also the bridge that lets the frontend coordinate agents, not just observe them.

Key cross-cutting concerns to keep in mind when designing any piece:
- The data model for observability (session, agent, span, payload, annotation, control event) is defined once in `proto/harmonograf/v1/*.proto` and shared across all three components.
- Plan, task, drift, and reporting-tool types are defined once in `proto/goldfive/v1/*.proto` and imported into harmonograf's proto tree. Do not re-declare them in harmonograf.
- The frontend is not read-only — interactions flow back through the server to clients, so the client library needs a bidirectional channel, not just telemetry egress.
- "Coordinating" implies the server may mediate control (pause, resume, steer, cancel), not just display — design client APIs with that in mind rather than treating it purely as an observability tool.

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
the timeline render this span where it did?", the answer lives here.

## Component boundaries

- **Span emission is harmonograf's job.** ADK callbacks (BEFORE/AFTER model/tool/run, state_delta, transfer, escalate) are turned into spans by `HarmonografTelemetryPlugin`. Goldfive does not produce spans.
- **Task state is goldfive's job.** State transitions happen inside goldfive's `DefaultSteerer` when a reporting tool is called or when an ADK event implies a transition. Harmonograf learns about those transitions only through goldfive events on the sink.
- **Control routing is harmonograf's job.** The frontend → server → agent path (pause, resume, steer, cancel, annotate) rides harmonograf's control stream. Goldfive exposes hooks that let an orchestrated run react to delivered control events, but the wire is harmonograf's.
- **Session is harmonograf's job.** Goldfive has a `run_id`; harmonograf pairs it with a `session_id` at the storage layer. One goldfive run maps to one harmonograf session.

## Things explicitly deleted from the pre-goldfive era

If you see these in docs or code, they are retired — do not bring them back:

- `HarmonografAgent`, `HarmonografRunner`, `attach_adk`, `make_adk_plugin`, `make_harmonograf_agent`, `make_harmonograf_runner` — replaced by `goldfive.Runner` + `HarmonografSink`.
- `_AdkState`, `PlannerHelper`, `LLMPlanner`, `PassthroughPlanner` — replaced by goldfive's steerer/planner.
- `state_protocol.py` with the `harmonograf.*` session-state keys — replaced by goldfive's `SessionContext`.
- `invariants.py`, `metrics.py` in the client — invariant checking is goldfive's responsibility.
- `TaskPlan` / `UpdatedTaskStatus` envelopes in `TelemetryUp` — replaced by `goldfive_event`.
- Duplicate `Task` / `TaskEdge` / `TaskStatus` / `DriftKind` messages in `types.proto` — imported from goldfive instead.

The full inventory of what moved, what stayed, and what was deleted is in
[docs/goldfive-migration-plan.md](docs/goldfive-migration-plan.md).
