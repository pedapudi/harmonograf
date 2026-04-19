# Goldfive integration

Harmonograf is the observability console for agent workflows orchestrated by
[goldfive](https://github.com/pedapudi/goldfive). Goldfive decides *what an
agent should do next*; harmonograf records what actually happened and lets a
human intervene. The two projects compose through a single adapter — the
`HarmonografSink`.

## Split of responsibilities

| Concern | Owner | Where |
|---|---|---|
| `Plan`, `Task`, `TaskEdge`, `TaskStatus`, `DriftKind` | goldfive | `proto/goldfive/v1/types.proto` |
| Task state machine and drift taxonomy | goldfive | `goldfive.DefaultSteerer`, `goldfive.drift` |
| Reporting tools (`report_task_started`, `report_task_completed`, …) | goldfive | `goldfive.reporting` |
| Session-state protocol (`SessionContext` on ADK state) | goldfive | `goldfive.adapters.adk` |
| Orchestration modes (sequential, parallel DAG walker, delegated) | goldfive | `goldfive.executors`, `goldfive.runner` |
| Span timeline, session, storage | harmonograf | `server/`, `client/harmonograf_client/client.py` |
| Gantt / graph / inspector / plan-diff UI | harmonograf | `frontend/` |
| Control routing (pause, resume, steer, cancel, annotate) | harmonograf | `proto/harmonograf/v1/control.proto`, `server/harmonograf_server/control_router.py` |
| `goldfive.EventSink` adapter | harmonograf | `client/harmonograf_client/sink.py` |

## Wire shape

Goldfive events travel through harmonograf's existing telemetry stream as a
new variant of `TelemetryUp`:

```proto
// proto/harmonograf/v1/telemetry.proto
import "goldfive/v1/events.proto";

message TelemetryUp {
  // ... span / payload / heartbeat variants unchanged ...
  goldfive.v1.Event goldfive_event = 11;
}
```

`HarmonografSink.emit(event)` pushes a `GOLDFIVE_EVENT` envelope through the
client's ring buffer; the transport serialises it as a `TelemetryUp` frame;
the server's ingest dispatches on `event.payload` (a protobuf `oneof` over
`RunStarted`, `GoalDerived`, `PlanSubmitted`, `PlanRevised`, `TaskStarted`,
`TaskCompleted`, `TaskFailed`, `DriftDetected`, `RunCompleted`, `RunAborted`,
…) and updates storage + publishes bus deltas the frontend subscribes to.

## Minimal worked example

```python
from __future__ import annotations

import asyncio

import goldfive
from goldfive import LLMPlanner, Runner, SequentialExecutor
from goldfive.adapters.adk import ADKAdapter
from google.adk.agents import Agent
from harmonograf_client import Client, HarmonografSink

root = Agent(name="researcher", model="openai/gpt-4o-mini")

client = Client(name="researcher", server_addr="127.0.0.1:7531")
sink = HarmonografSink(client)

runner = Runner(
    agent=ADKAdapter(root),
    planner=LLMPlanner(call_llm=my_llm_call, model="openai/gpt-4o-mini"),
    executor=SequentialExecutor(),
    sinks=[sink, goldfive.sinks.LoggingSink()],
)

async def main() -> None:
    outcome = await runner.run("Summarise recent observability research.")
    print("ok" if outcome.success else outcome.reason)
    await runner.close()           # flushes every sink, including ours
    client.shutdown(flush_timeout=5.0)

asyncio.run(main())
```

While `main()` runs, the frontend (started by `make demo`, served at
[http://127.0.0.1:5173](http://127.0.0.1:5173)) shows the plan and task rows
as the planner emits, each task's spans fill in as the adapter invokes the
agent, and any `DriftDetected` event the steerer raises shows as a marker on
the timeline.

## Optional: ADK span telemetry

If the agent tree is wired through ADK's `App` (as `adk web` / `adk run` do),
add `HarmonografTelemetryPlugin` so ADK lifecycle callbacks (invocation,
LLM call, tool call) turn into harmonograf spans:

```python
from google.adk.apps.app import App
from harmonograf_client import HarmonografTelemetryPlugin

app = App(
    name="my-agent",
    root_agent=root,
    plugins=[HarmonografTelemetryPlugin(client)],
)
```

The plugin is pure observability — it makes no orchestration decisions and
composes cleanly with goldfive's own ADK adapter.

## Running the reference demo

`tests/reference_agents/presentation_agent/` is a four-subagent ADK tree
wired to goldfive + harmonograf. Boot the stack with:

```bash
make demo                       # server + frontend + adk web presentation_agent
make demo-presentation          # just adk web; point at an existing server via HARMONOGRAF_SERVER
```

`presentation_agent.agent.build_goldfive_runner(mock=True)` also exposes an
offline runnable Runner (no LLM, canned plan) that exercises the same event
stream — useful for integration smoke tests.

## Further reading

- [goldfive-migration-plan.md](goldfive-migration-plan.md) — design record of the migration.
- [../AGENTS.md](../AGENTS.md) — project vision and component boundaries.
- [protocol/telemetry-stream.md](protocol/telemetry-stream.md) — the wire stream that carries goldfive events.
- [goldfive docs](https://github.com/pedapudi/goldfive) — orchestration semantics, drift taxonomy, reporting tools.
