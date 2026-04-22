---
name: hgraf-write-e2e-scenario
description: Pattern for writing a new end-to-end test in tests/e2e/ — real ADK + real harmonograf server, scripted FakeLlm for determinism.
---

# hgraf-write-e2e-scenario

## When to use

You need a hermetic end-to-end test that exercises the full stack —
real `google.adk` runtime, real `harmonograf_client`, real
`harmonograf_server` (in-process) — but with deterministic, scripted
LLM output. Use this for:

- Regression tests that cross the client / server / storage boundary.
- Plan-diff rendering correctness (client → bus → frontend-fixtures).
- Intervention aggregator flows with real ingest.
- Cancellation sweep, lazy Hello, per-agent row auto-registration.

Do **not** use this skill for tests that don't need ADK at all —
prefer pytest unit tests under `client/tests/` or `server/tests/`.
They run 10-100× faster.

## Scope

Harmonograf's e2e suite is in `tests/e2e/`. It composes:

- A real `harmonograf_server` started in-process on an ephemeral
  port with either an in-memory or sqlite-backed store.
- A real `harmonograf_client.Client(...)` pointing at that server.
- A real ADK runner + `HarmonografTelemetryPlugin`.
- Optionally a real `goldfive.wrap(...)` for orchestration flows.
- A scripted `FakeLlm` so the model never goes off-script.

## Step-by-step

### 1. Pick your server fixture

The conftest under `tests/e2e/` exposes server fixtures. Prefer the
in-memory store for speed and the sqlite-backed one only when the
test verifies persistence / migration.

### 2. Build a FakeLlm

Construct a list of `LlmResponse` objects using ADK's own types:

```python
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part, FunctionCall

responses = [
    LlmResponse(content=Content(parts=[Part(text="I'll call the search tool.")])),
    LlmResponse(content=Content(parts=[Part(function_call=FunctionCall(
        name="web_search", args={"query": "..."}))])),
    # ...
]
```

Wrap them in a FakeLlm class that advances a cursor each call.

### 3. Drive the agent

With the client configured against the local server, run the agent.
Spans, heartbeats, and goldfive events flow through the real wire
into the in-process server.

### 4. Assert

- Query the server's store directly for spans, agents, plans,
  annotations, interventions.
- Call the server's RPCs (`ListInterventions`, `WatchSession`,
  `GetSpanTree`) to verify end-user-facing shape.
- Compare against expected structure — task state machine
  transitions, intervention dedup, plan revisions, per-agent row
  shape (#80), etc.

### 5. Scenarios worth covering

- **Lazy Hello (#85)**: construct a `Client`, assert the server sees
  nothing; emit the first envelope, assert `Hello` lands.
- **Per-agent rows (#80)**: run a tree of ADK agents, assert the
  server has one `Agent` row per ADK agent with `hgraf.agent.*`
  metadata populated.
- **Plugin dedup (#68)**: install `HarmonografTelemetryPlugin`
  twice; assert spans don't double-emit.
- **STEER idempotency (#72)**: post the same STEER annotation twice
  in quick succession; assert only one drift fires.
- **Cancellation sweep**: cancel the ADK task mid-run; assert any
  open spans close with `status=CANCELLED`.
- **Intervention dedup (#81, #87)**: trigger a STEER → USER_STEER
  drift → PlanRevised chain; assert `ListInterventions` returns one
  card, not three.

## Verification

```bash
make e2e
# or for one file:
cd tests/e2e && uv run pytest test_<scenario>.py -q
```

Some e2e tests need `KIKUCHI_LLM_URL` for real-LLM scenarios; those
are gated and skipped by default.

## Common pitfalls

- **Lazy Hello silences emit-less tests.** A test that constructs a
  `Client` and never emits will not connect at all. That's correct
  per #85 — emit something before asserting server-side state.
- **Per-agent rows change assertions.** If you assert "session has
  exactly one `Agent`", a goldfive.wrap run will now have N agents.
  Update assertions to match the per-agent shape.
- **Don't hard-code agent ids.** Use the `<client>:<name>` shape or
  query the server for the registered ids.

## Cross-links

- `dev-guide/testing.md` — the four-tier test hierarchy.
- `hgraf-add-fake-llm-scenario` — complex turn scripting.
