# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project vision

Harmonograf is a console for understanding, interacting with, and coordinating multi-agent frameworks. The repository is currently a blank slate (only a README) — architectural decisions below are intent, not yet implemented.

## High-level architecture

Three components are planned, and changes usually span more than one of them:

1. **Visual frontend** — a Gantt-chart-style view. X-axis is time, Y-axis is one row per agent, and each block represents an agent activity (e.g., a tool call, a step, a span). Blocks are interactive (clickable) to drill into details. This is the human-facing surface for observing and coordinating agents.

2. **Client library** — embedded inside agent implementations to emit activity to the server. It must be compatible with ADK (Google's Agent Development Kit) as a first-class integration target, so its data model and hooks should map cleanly onto ADK concepts. Multiple agents (multiple processes) will use the client library concurrently.

3. **Server process** — hosts the visualization frontend and terminates connections from client libraries across all participating agents. It is the fan-in point: many clients, one server, one UI. It owns the canonical timeline and is the bridge that lets the frontend coordinate agents (not just observe them).

Key cross-cutting concerns to keep in mind when designing any piece:
- The data model (agent, activity/block, time range, metadata payload) is shared across all three components and should be defined once.
- The frontend is not read-only — interactions flow back through the server to clients, so the client library needs a bidirectional channel, not just telemetry egress.
- "Coordinating" implies the server may mediate control, not just display — design client APIs with that in mind rather than treating it purely as an observability tool.

## Plan execution protocol

Harmonograf tracks task progression via three coordinated channels, not via span-lifecycle inference:

1. **session.state** — ADK's shared mutable dict. Harmonograf writes `harmonograf.current_task_id`, `harmonograf.plan_id`, `harmonograf.plan_summary`, `harmonograf.available_tasks`, and `harmonograf.completed_task_results` before each model call. Agents read those keys and may write back `harmonograf.task_progress`, `harmonograf.task_outcome`, `harmonograf.agent_note`, and `harmonograf.divergence_flag`. The full schema (keys, readers, writers, diffing helper) lives in `client/harmonograf_client/state_protocol.py`.

2. **Reporting tools** — `report_task_started`, `report_task_progress`, `report_task_completed`, `report_task_failed`, `report_task_blocked`, `report_new_work_discovered`, and `report_plan_divergence` are injected into every sub-agent by `HarmonografAgent`. Agents call them explicitly at task boundaries; harmonograf intercepts the calls in `before_tool_callback` and applies state transitions directly in `_AdkState` — the tool bodies themselves only return `{"acknowledged": true}`. See `docs/reporting-tools.md` for the full reference.

3. **ADK callback inspection** — `after_model_callback` parses response content for structured signals (function_calls, explicit markers like "Task complete:", state_delta writes). `on_event_callback` watches for transfer / escalate / state_delta events. These paths exist as belt-and-suspenders for models that describe their work in prose instead of calling the reporting tools.

Spans are still emitted for every ADK callback (INVOCATION / LLM_CALL / TOOL_CALL / TRANSFER / etc.), but they are **telemetry only** — they no longer drive task state. The state machine is monotonic, walker-owned for parallel mode, and callback-driven for sequential/delegated modes.

`HarmonografAgent` runs in one of three orchestration modes:

- **Sequential** (default, `orchestrator_mode=True, parallel_mode=False`) — the whole plan is fed as one user turn and the coordinator LLM executes it; per-task lifecycle is reported via the reporting tools.
- **Parallel** (`orchestrator_mode=True, parallel_mode=True`) — the rigid DAG batch walker drives sub-agents directly per task using a forced `task_id` ContextVar, respecting plan edges as dependencies.
- **Delegated** (`orchestrator_mode=False`) — a single delegation with the event observer scanning for drift afterward; the inner agent is in charge of its own task sequencing.

Dynamic replan triggers: tool errors, agent refusals, context pressure, new work discovered, plan divergence, user steer/cancel, and ~15 other drift kinds fire deferential `refine` calls that produce revised plans live in the UI. The refine path calls back into the planner with the current plan + drift context and upserts the result through `TaskRegistry`, which recomputes the diff so the frontend banner/drawer can visualize exactly what changed (see `frontend/src/gantt/index.ts :: computePlanDiff`).
