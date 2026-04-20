# Goldfive integration

> **If you want plans + tasks + drift: this is the guide.**
> If you just want a Gantt of agent activity without any orchestration
> concepts, see [standalone-observability.md](standalone-observability.md)
> instead — goldfive is optional.

Harmonograf pairs naturally with
[goldfive](https://github.com/pedapudi/goldfive) for multi-agent
orchestration. Goldfive decides *what an agent should do next*;
harmonograf records what actually happened and lets a human intervene.
The two projects compose through a single adapter — the
`HarmonografSink` — and are opted into via the `orchestration` extra:

```bash
uv sync --extra orchestration
```

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

The target form is two lines of setup — one for orchestration, one for
observability:

```python
import goldfive
import harmonograf_client

runner = harmonograf_client.observe(goldfive.wrap(root_agent))
outcome = await runner.run("make a presentation about waffles")
```

These two lines produce **both** halves of full observability in one shot:

- `HarmonografSink` attached to the goldfive `Runner` — plan rows, task
  rows, drift markers, and lifecycle events show up in the harmonograf UI.
- `HarmonografTelemetryPlugin` auto-installed on the underlying ADK
  `Runner` (when `goldfive.wrap` wrapped an ADK agent) — invocation,
  LLM-call, and tool-call spans render on the Gantt timeline.

**First `goldfive.wrap` for orchestration, then `harmonograf_client.observe`
for observability.** The layering is crystal-clear and each call owns exactly
one concern:

- `goldfive.wrap(agent)` — the *only* planning/steering injection. Picks a
  default `LLMPlanner`, `SequentialExecutor`, adapter, and goal deriver, and
  returns a ready-to-run `goldfive.Runner`.
- `harmonograf_client.observe(runner)` — the *only* observability wiring.
  Constructs a `Client`, appends a `HarmonografSink` to `runner.sinks`,
  installs `HarmonografTelemetryPlugin` on the inner ADK runner if there is
  one, and returns the same runner for chaining. Nothing about the runner's
  planning, steering, goal derivation, or execution is touched.

Neither call is implicit — if you want both you write both, and you can skip
either one when you don't. To opt out of the telemetry plugin (e.g. when one
is already installed elsewhere), pass `install_adk_telemetry=False`.

### End-to-end

```python
from __future__ import annotations

import asyncio

import goldfive
import harmonograf_client
from google.adk.agents import Agent


async def main() -> None:
    root = Agent(name="researcher", model="openai/gpt-4o-mini")

    runner = harmonograf_client.observe(goldfive.wrap(root))

    outcome = await runner.run("Summarise recent observability research.")
    print("ok" if outcome.success else outcome.reason)
    await runner.close()  # flushes every sink, including harmonograf's


asyncio.run(main())
```

### Reusing a Client or tagging the run

`observe` accepts the same identity knobs as `Client`:

```python
# Reuse a pre-built Client (e.g. shared across many runners)
client = harmonograf_client.Client(
    name="prod-agent", framework="ADK", server_addr="remote:7531"
)
runner = harmonograf_client.observe(goldfive.wrap(root_agent), client=client)

# Or let observe construct the Client, passing just the tags
runner = harmonograf_client.observe(
    goldfive.wrap(root_agent), name="presentation", framework="ADK"
)
```

When `server_addr` is omitted, `observe` reads `$HARMONOGRAF_SERVER` and
otherwise falls back to `Client`'s default (`127.0.0.1:7531`).

### Composing with other sinks

Because `observe` *appends* to `runner.sinks` it composes cleanly with any
sinks `goldfive.wrap` already configured:

```python
from goldfive.sinks import JSONLPersistenceSink

runner = goldfive.wrap(root_agent, sinks=[JSONLPersistenceSink("runs/log.jsonl")])
runner = harmonograf_client.observe(runner)  # HarmonografSink appended alongside
```

### Fully manual wiring (pre-`wrap`)

If you need to override a goldfive internal that `wrap` doesn't expose, build
the `Runner` yourself and skip `wrap` — `observe` still works the same:

```python
from goldfive import LLMPlanner, Runner, SequentialExecutor
from goldfive.adapters.adk import ADKAdapter
import harmonograf_client

runner = Runner(
    agent=ADKAdapter(root),
    planner=LLMPlanner(call_llm=my_llm_call, model="openai/gpt-4o-mini"),
    executor=SequentialExecutor(),
)
runner = harmonograf_client.observe(runner, name="researcher")
```

While `main()` runs, the frontend (started by `make demo`, served at
[http://127.0.0.1:5173](http://127.0.0.1:5173)) shows the plan and task rows
as the planner emits, each task's spans fill in as the adapter invokes the
agent, and any `DriftDetected` event the steerer raises shows as a marker on
the timeline.

## ADK span telemetry

When the wrapped agent is an ADK `BaseAgent`, `harmonograf_client.observe`
auto-installs `HarmonografTelemetryPlugin` on the underlying ADK `Runner`,
so invocation / LLM-call / tool-call spans render on the Gantt timeline
without any extra setup. Opt out with `install_adk_telemetry=False`:

```python
runner = harmonograf_client.observe(
    goldfive.wrap(root_agent), install_adk_telemetry=False
)
```

For agent trees driven through ADK's `App` (as `adk web` / `adk run` do),
add the plugin to the `App` directly — that path doesn't go through
`goldfive.wrap`:

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
make demo                       # server + frontend + adk web (both variants in picker)
make demo-presentation          # just adk web; point at an existing server via HARMONOGRAF_SERVER
```

`adk web` lists two agents in the picker:

* `presentation_agent` — observation mode: plain ADK tree, coordinator
  routes via instruction text, harmonograf attaches a telemetry plugin.
* `presentation_agent_orchestrated` — orchestration mode: same tree
  wrapped with `goldfive.wrap(...)`, so goldfive plans the specialists,
  dispatches them, and fires drift when the adapter return doesn't
  match. This is the path that exercises the full goldfive event stream.

`presentation_agent_orchestrated.agent.build_goldfive_runner(mock=True)`
also exposes an offline runnable Runner (no LLM, canned plan) that
exercises the same event stream — useful for integration smoke tests.

## Further reading

- [goldfive-migration-plan.md](goldfive-migration-plan.md) — design record of the migration.
- [../AGENTS.md](../AGENTS.md) — project vision and component boundaries.
- [protocol/telemetry-stream.md](protocol/telemetry-stream.md) — the wire stream that carries goldfive events.
- [goldfive docs](https://github.com/pedapudi/goldfive) — orchestration semantics, drift taxonomy, reporting tools.
