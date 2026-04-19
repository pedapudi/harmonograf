# Standalone observability examples

**Use harmonograf as an observability console — without goldfive
orchestration.** Emit spans via `harmonograf_client.Client`, and the
frontend's Gantt / agent panels render them the same as any other
source. Plans, tasks, and drift (goldfive's signals) stay out of the
picture; the Tasks panel is simply empty.

See [`docs/standalone-observability.md`](../../docs/standalone-observability.md)
for the full story.

## What's in this directory

| File | What it does | Imports goldfive? |
| --- | --- | --- |
| [`spans_only.py`](spans_only.py) | Hand-authored synthetic spans via `Client.emit_span_*` — the "no-framework" path. | No. |
| [`adk_telemetry.py`](adk_telemetry.py) | A real ADK agent instrumented with `HarmonografTelemetryPlugin`. Telemetry only. | No. |
| [`with_orchestration.py`](with_orchestration.py) | Counterpoint: goldfive's `Runner` + `observe()` to light up plans + tasks + drift. | **Yes** — needs `uv sync --extra orchestration`. |

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
| Gantt (agent activity) | populated | populated | populated |
| Messages | populated | populated | populated |
| Tasks | **empty** | **empty** | populated |
| Plan view | **empty** | **empty** | populated |
| Drift indicators | **empty** | **empty** | populated |

Empty Tasks panel is the expected standalone state — not an error.
