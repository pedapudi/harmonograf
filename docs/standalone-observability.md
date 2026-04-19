# Standalone observability — using harmonograf without goldfive

Harmonograf is a console for agent activity. The *orchestration* (plans,
tasks, drift detection, reporting tools) lives in
[goldfive](https://github.com/pedapudi/goldfive). But you don't need
goldfive to use harmonograf: if you have any agent that emits spans, you
can render those spans on the harmonograf Gantt, inspect payloads in the
drawer, replay sessions, and send control events — all without any
orchestration concepts at all.

This guide is the canonical "just observability, no goldfive" path.
Contrast with [`docs/goldfive-integration.md`](goldfive-integration.md),
which is the "plans + tasks + drift" guide.

## TL;DR

```python
from harmonograf_client import Client, SpanKind, SpanStatus

client = Client(name="my-agent", server_addr="127.0.0.1:7531")

sid = client.emit_span_start(kind=SpanKind.LLM_CALL, name="gpt-4o")
# ... call your LLM, do work ...
client.emit_span_end(sid, status=SpanStatus.COMPLETED)

client.shutdown()
```

That's the entire standalone surface. Everything else is optional.

## When to use this path

Use the standalone path when:

- **You want a Gantt of agent activity** and nothing more.
- **You already have an orchestration framework** (LangGraph, AutoGen,
  raw asyncio, plain loops) and just want the harmonograf UI.
- **You're prototyping** and don't want to commit to goldfive's task
  model yet.
- **You're writing your own orchestrator** and want to use harmonograf
  as the coordination surface (spans + control events) without
  goldfive's planner / steerer / executor.

Use [goldfive + harmonograf](goldfive-integration.md) when you want:

- Plans and tasks surfaced in the UI (Tasks panel, plan-diff drawer).
- Drift detection and drift banners.
- The reporting-tool contract (`report_task_started`,
  `report_task_completed`, etc.).
- Planner → steerer → executor orchestration out of the box.

You can switch between the two without re-plumbing: both emit to the
same harmonograf server. Add `goldfive.wrap(...)` and pass the resulting
Runner through `harmonograf_client.observe()` when you're ready.

## Honest caveat: proto coupling

Phase A of the goldfive migration (issue #6) moved plan/task/drift types
into the goldfive proto package and made harmonograf's
`TelemetryUp.goldfive_event` reference `goldfive.v1.Event` directly. The
consequence: `harmonograf_client`'s generated pb stubs import
`goldfive.pb.goldfive.v1` at *import time*, so a Python process that
loads `harmonograf_client.Client` will end up with the goldfive Python
package imported via the pb submodule.

What this means in practice:

- **`harmonograf_client` (the library) cannot be installed without
  pulling goldfive in.** That is unavoidable without reversing Phase A.
- **Your code** can be entirely goldfive-free. You can `from
  harmonograf_client import Client` and never touch `goldfive.wrap`,
  `Runner`, `HarmonografSink`, or any orchestration symbol.
- **Your package's direct dependency graph** does not need to list
  goldfive. That's what the optional `orchestration` extra is for —
  opting in to the *usage* surface while the pb compatibility is
  transparent.

The `standalone-test` CI job proves the "your code is goldfive-free"
guarantee: it `grep -i goldfive`'s `examples/standalone_observability/
spans_only.py` and fails if anything matches.

See [#29](https://github.com/pedapudi/harmonograf/issues/29) for the
full rationale and the discussion of Interpretation 1 vs. Interpretation 2.

## Install

```bash
git clone https://github.com/pedapudi/harmonograf
cd harmonograf
git submodule update --init --recursive       # pulls goldfive into third_party/
git clone --depth 1 https://github.com/google/adk-python.git third_party/adk-python
uv sync                                        # no --extra orchestration
```

For running the demo that uses `goldfive.wrap()`, also add:

```bash
uv sync --extra orchestration
```

## The public surface

Standalone observability uses only this subset of
`harmonograf_client`:

| Symbol | What it's for |
| --- | --- |
| `Client` | Construct one per process/agent; owns the buffer + transport. |
| `SpanKind` | Enum: `LLM_CALL`, `TOOL_CALL`, `INVOCATION`, `USER_MESSAGE`, `AGENT_MESSAGE`, `TRANSFER`, `WAIT_FOR_HUMAN`, `PLANNED`, `CUSTOM`. |
| `SpanStatus` | Enum: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `AWAITING_HUMAN`. |
| `Capability` | Control capabilities the agent declares it accepts. |
| `ControlAckSpec` | Return type from control handlers (below). |
| `HarmonografTelemetryPlugin` | ADK plugin that emits spans for ADK lifecycle events. |

You do **not** need:

- `HarmonografSink` — that's the goldfive.EventSink adapter.
- `observe()` — that's the goldfive.Runner helper.

Both are re-exported at the top level, but only relevant to the
goldfive path.

## Emitting spans

### Span lifecycle

Every span has three possible phases:

1. **Start** — `emit_span_start(kind=, name=, ...)` → returns `span_id`.
2. **Update** — zero or more `emit_span_update(span_id, ...)` calls
   carrying partial output, attribute changes, or a status bump.
3. **End** — `emit_span_end(span_id, status=, ...)`.

All three calls are non-blocking (O(1) under the Client's lock). The
buffer is drained on a background transport thread. No span emission
ever awaits IO.

### Attaching payloads

Any of the three calls accepts `payload=`, `payload_mime=`, and
`payload_role=`. Payloads are streamed on a separate logical channel;
the span carries a `payload_ref` that the frontend uses to fetch on
demand. This keeps the event stream small even when the payload is a
100 KB LLM completion.

Typical shape:

```python
sid = client.emit_span_start(
    kind=SpanKind.LLM_CALL,
    name="gpt-4o",
    payload=json.dumps({"prompt": ...}).encode(),
    payload_mime="application/json",
    payload_role="input",
)
# ... make the API call ...
client.emit_span_end(
    sid,
    status=SpanStatus.COMPLETED,
    payload=json.dumps({"text": reply}).encode(),
    payload_role="output",
    attributes={"tokens_in": tin, "tokens_out": tout},
)
```

### Attributes

`attributes=` accepts any `Mapping[str, Any]`. Values are coerced to
strings on the wire. Use them for anything you want searchable in the
frontend filter strip: model name, tool name, task id, user id, env,
etc.

### Parent / child spans

Pass `parent_span_id=` to nest a span under another. The Gantt renders
parent bars above children, and the inspector shows the ancestry.

```python
root = client.emit_span_start(kind=SpanKind.INVOCATION, name="handle_request")
child = client.emit_span_start(
    kind=SpanKind.LLM_CALL, name="gpt-4o", parent_span_id=root,
)
```

## Sessions

A session is the top-level scope that groups spans from one or more
agents collaborating on one task. The Client auto-creates a session on
first emission; the server assigns a canonical `sess_YYYY-MM-DD_NNNN`
id and broadcasts it back. Access via `client.session_id`.

Pass `session_id=` to the `Client(...)` constructor to join an existing
session — useful when multiple agents collaborate on the same workflow
and you want one row per agent on the same Gantt.

`session_title` is a friendly label shown in the session picker.

## Agents

By default, each Client is its own agent. Pass `name=` for the display
name shown on the Gantt row; the server derives a stable `agent_id` by
hashing name+install directory (see `identity.py`).

Set `framework=` to one of `ADK`, `CLAUDE_AGENT_SDK`, `CUSTOM`. The
frontend uses this to pick a row icon.

## Control events

The Client's connection is bidirectional. The server can deliver
control events *to* an agent — pause, cancel, annotate, steer — and
the agent can ack them. This works in standalone mode too; you don't
need goldfive to handle control.

```python
from harmonograf_client import Client, Capability, ControlAckSpec

def handle_pause(event):
    # ... pause your agent ...
    return ControlAckSpec(result="accepted")

client = Client(
    name="my-agent",
    capabilities=[Capability.PAUSE_RESUME],  # declares what you accept
)
client.on_control("PAUSE", handle_pause)
```

See `docs/protocol/control.md` for the full control protocol.

## ADK lifecycle plugin

`HarmonografTelemetryPlugin` wires `harmonograf_client` into ADK's
plugin bus and emits one span per invocation, LLM call, and tool call.
No goldfive involved. See
[`examples/standalone_observability/adk_telemetry.py`](../examples/standalone_observability/adk_telemetry.py).

```python
from google.adk.apps.app import App
from harmonograf_client import Client, HarmonografTelemetryPlugin

client = Client(name="my-adk-agent")
app = App(
    name="my-adk-agent",
    root_agent=root_agent,
    plugins=[HarmonografTelemetryPlugin(client)],
)
```

Install *alongside* goldfive's ADK plugin if you also want
orchestration — they do not interfere.

## What the frontend shows

The standalone case leaves some panels empty:

| Panel | Standalone | With goldfive |
| --- | --- | --- |
| Gantt (one row per agent) | populated | populated |
| Span inspector drawer | populated | populated |
| Messages panel | populated | populated |
| Payload viewer | populated | populated |
| Session picker | populated | populated |
| Tasks panel | **empty** | populated |
| Plan-diff drawer | **empty** | populated |
| Drift banner | **empty** | populated |

The empty states are intentional and well-handled: no spinners, no
error toasts. The panels show "No plan submitted for this session" and
similar.

## Running the examples

Three runnable examples live under
[`examples/standalone_observability/`](../examples/standalone_observability/):

1. **`spans_only.py`** — zero dependencies beyond `harmonograf_client`.
   Emits a synthetic agent flow. Start here.
2. **`adk_telemetry.py`** — a real ADK agent with
   `HarmonografTelemetryPlugin` installed. Needs model credentials.
3. **`with_orchestration.py`** — comparison baseline: goldfive's
   `Runner` + `observe()`.

Boot the stack:

```bash
make server-run    # harmonograf-server on :7531 (gRPC)
make frontend-dev  # Vite on :5173
```

Then run an example in a second terminal, or use the one-shot:

```bash
make demo-standalone   # server + frontend + spans_only.py
```

## Testing your own standalone agent

The `standalone-test` CI job is a reference: it installs without the
orchestration extra, starts a server, and runs `spans_only.py` against
it. If you fork this pattern for your own agent, the important bits
are:

- `uv sync` (not `uv sync --extra orchestration`).
- Your agent code should not contain `from goldfive` or `import
  goldfive`. Grep to verify.
- Start the server (`harmonograf-server --store sqlite --data-dir ...`)
  and wait for the gRPC port before running your emitter.
- Call `client.shutdown(flush_timeout=5.0)` at end-of-process so the
  buffer drains before exit.

## FAQ

### Why does `pip install harmonograf-client` still pull goldfive?

Because the generated pb stubs import `goldfive.pb.goldfive.v1`. See
the "honest caveat" section above and issue #29 for the full
discussion.

### Can I emit plan / task events without goldfive?

Not directly. `emit_goldfive_event(event_pb)` exists on the Client but
it takes a `goldfive.v1.Event` protobuf, so using it pulls you into
the goldfive dependency surface. If you want plans + tasks, use
goldfive. If you want a lighter-weight "what task is this span
associated with" hint, set an attribute on the span
(`attributes={"task.id": "..."}`) and filter on it in the frontend.

### Can I switch from standalone to goldfive later?

Yes — that's the whole point. Your existing `emit_span_start/update/
end` calls keep working. Add a goldfive Runner alongside, wrap it with
`harmonograf_client.observe()`, and you'll see plans / tasks / drift
appear in the UI for that session.

### How do I send control events to a standalone agent?

Register a handler with `client.on_control("KIND", callback)` and
declare capabilities in the Client constructor. The frontend's "send
control" menu lists whatever capabilities the agent declared. No
goldfive needed.

## Related docs

- [`docs/goldfive-integration.md`](goldfive-integration.md) — the
  "plans + tasks + drift" path.
- [`docs/quickstart.md`](quickstart.md) — fastest way to see anything
  on screen.
- [`docs/protocol/`](protocol/) — wire format, control protocol,
  heartbeat semantics.
- [`AGENTS.md`](../AGENTS.md) — contributor orientation.
