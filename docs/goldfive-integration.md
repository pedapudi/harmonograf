# Goldfive integration

**Status:** stub. Full reference lands in Phase D of the migration. Until then,
this page is a short map of how harmonograf consumes goldfive and where each
responsibility lives.

For the design record and file-by-file migration rationale, see
[goldfive-migration-plan.md](goldfive-migration-plan.md).

---

## Split of responsibilities

| Concern | Owner | Where |
|---|---|---|
| Plan representation (`Plan`, `Task`, `TaskEdge`, `TaskStatus`, `DriftKind`) | goldfive | `proto/goldfive/v1/types.proto` |
| Task state machine and drift taxonomy | goldfive | `goldfive.DefaultSteerer`, `goldfive.drift` |
| Reporting tools (`report_task_started`, `report_task_completed`, …) | goldfive | `goldfive.reporting` |
| Session-state protocol (`SessionContext` on ADK state) | goldfive | `goldfive.adapters.adk` |
| Orchestration modes (sequential, parallel DAG walker, delegated) | goldfive | `goldfive.executors`, `goldfive.runner` |
| Span timeline, session, storage | harmonograf | `server/`, `client/harmonograf_client/client.py` |
| Gantt / graph / inspector / plan-diff UI | harmonograf | `frontend/` |
| Control routing (pause, resume, steer, cancel, annotate) | harmonograf | `proto/harmonograf/v1/control.proto`, `server/harmonograf_server/control.py` |
| `goldfive.EventSink` adapter | harmonograf | `client/harmonograf_client/sink.py` |

## Wire shape

Goldfive events travel through harmonograf's existing telemetry stream. The
variant lives in `TelemetryUp`:

```proto
message TelemetryUp {
  // ... span / payload / heartbeat variants unchanged ...
  goldfive.v1.Event goldfive_event = 11;
}
```

`HarmonografSink.emit(event)` pushes a `GOLDFIVE_EVENT` envelope through the
client's ring buffer; the transport serialises it as a `TelemetryUp` frame; the
server's ingest dispatches on `event.payload` (oneof) and updates the plan/task
index + session bus.

## Wiring a goldfive run to harmonograf

```python
import goldfive
from harmonograf_client import Client, HarmonografSink

client = Client(agent_name="my-agent")
runner = goldfive.Runner(
    # ... planner, steerer, executor as usual ...
    sinks=[HarmonografSink(client)],
)
await runner.run(task)
```

Optional: install `HarmonografTelemetryPlugin` on the ADK agent graph to emit
spans for every ADK callback. Goldfive orchestration and harmonograf
observability are independent; either works without the other, but the demo
flow runs both.

## Further reading

- [goldfive-migration-plan.md](goldfive-migration-plan.md) — the design record.
- [../AGENTS.md](../AGENTS.md) — project vision and component boundaries.
- [protocol/telemetry-stream.md](protocol/telemetry-stream.md) — the wire stream that now carries goldfive events.
- goldfive repo — orchestration semantics: <https://github.com/pedapudi/goldfive>.
