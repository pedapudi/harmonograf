# Standalone observability examples

**Use harmonograf as an observability console — without goldfive
orchestration.** Emit spans via `harmonograf_client.Client`, and the
frontend's Sessions / Activity (Gantt) / Graph views render them the
same as any other source. Plans, tasks, drift, and intervention history
(goldfive's signals) stay out of the picture: the task panel and
Trajectory views are empty.

See [`docs/standalone-observability.md`](../../docs/standalone-observability.md)
for the full story, including the lazy-Hello / home-session semantics
for non-ADK clients (harmonograf#85).

## What's in this directory

| File | What it does | Imports goldfive? |
| --- | --- | --- |
| [`spans_only.py`](spans_only.py) | Hand-authored synthetic spans via `Client.emit_span_*` — the "no-framework, no-ADK" path. | No. |
| [`adk_telemetry.py`](adk_telemetry.py) | A real ADK agent instrumented with `HarmonografTelemetryPlugin` on `App(plugins=[...])`. Telemetry only — no `goldfive.wrap`. | No. |
| [`with_orchestration.py`](with_orchestration.py) | Counterpoint: `goldfive.wrap(...)` + `harmonograf_client.observe()` to light up plans + tasks + drift + intervention history. | **Yes** — needs `uv sync --extra orchestration`. |

Verify the first two truly have zero goldfive references:

```
grep -Ei 'from goldfive|import goldfive' \
  examples/standalone_observability/spans_only.py \
  examples/standalone_observability/adk_telemetry.py
# (no output)
```

## Running

Start a harmonograf server in one terminal:

```
make server-run
```

And (optionally) the frontend:

```
make frontend-dev
```

Then run any example:

```
# No goldfive, no ADK, just spans:
uv run python examples/standalone_observability/spans_only.py

# ADK agent with span-emitting plugin (needs a model key):
export OPENAI_API_KEY=...
uv run --extra e2e python examples/standalone_observability/adk_telemetry.py

# Goldfive orchestration for comparison:
uv run --extra orchestration python examples/standalone_observability/with_orchestration.py
```

A convenience target runs server + frontend + `spans_only.py` end-to-end:

```
make demo-standalone
```

## What the frontend shows

| Panel | spans_only.py | adk_telemetry.py | with_orchestration.py |
| --- | --- | --- | --- |
| Sessions picker | populated (1 session) | populated (1 session) | populated (1 session) |
| Activity (Gantt, agent rows) | populated (1 row) | populated (N rows, one per ADK agent) | populated (N rows per goldfive.wrap tree) |
| Graph view | populated | populated | populated |
| Messages | populated | populated | populated |
| Tasks / plan banner | **empty** | **empty** | populated |
| Trajectory (interventions) | **empty** | **empty** | populated |
| Drift markers | **empty** | **empty** | populated |

Empty task panel / Trajectory is the expected standalone state — not an
error. The `adk_telemetry.py` path gets per-ADK-agent rows for free
because the plugin stacks per-agent ids even without `goldfive.wrap`
(harmonograf#74 / #80).
