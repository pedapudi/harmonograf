# Runbook: Drift not firing

A tool failed, the agent refused, the user sent a steer — and nothing
happened. No drift marker appeared on the InterventionsTimeline, no
plan revision fired, the planner looks idle.

All drift detection is in [goldfive](https://github.com/pedapudi/goldfive)
now. This runbook is about diagnosing which detector didn't fire and
why.

## Symptoms

- **Intervention timeline strip**: empty or missing the event you
  expected (`TOOL_ERROR`, `AGENT_REFUSAL`, `USER_STEER`,
  `LOOPING_REASONING`, `PLAN_DIVERGENCE`, `CONFABULATION_RISK`).
- **Drawer → Task → Plan revisions**: no new revision followed the
  event.
- **Goldfive log**: no line of the form
  `goldfive.drift: DriftDetected kind=<K> severity=<S> detail=<D>`.
- **Harmonograf server log**: nothing unusual. The server only sees
  drift indirectly via `goldfive_event.drift_detected` ingested from
  the agent.

## Detector catalogue

Goldfive runs a handful of detectors, each wired into a specific ADK
callback. If the callback doesn't fire for the event kind you care
about, the detector is blind.

| Drift kind | Detector location | Callback hook |
|---|---|---|
| `USER_STEER`, `USER_CANCEL` | `goldfive.steerer` | Synthesised from incoming `STEER` / `CANCEL` control events. |
| `TOOL_ERROR` | `goldfive.adapters.adk.plugin` | `on_tool_error_callback`. |
| `AGENT_REFUSAL` | `goldfive.adapters.adk.plugin` | `after_model_callback` parses refusal patterns. |
| `LOOPING_REASONING` | `goldfive.detectors.tool_loop_tracker` (#181/#186) | `after_tool_callback` — exact, name, and alternating modes. |
| `PLAN_DIVERGENCE` (goldfive#178 stage 2) | `goldfive.detectors.function_call_gate` | `before_tool_callback` gate. |
| `CONFABULATION_RISK` (goldfive#178 stage 3) | same gate | Hallucinated tool name. |
| `GOAL_DRIFT` | `goldfive.planner.goal_aware` | Goal-aware refiner observation. |
| `HUMAN_INTERVENTION_REQUIRED` | `goldfive.steerer` intervention ladder | Escalated when other drifts don't resolve. |
| `REFINE_VALIDATION_FAILED` | `goldfive.planner` | Planner's own refine result validation. |

## Immediate checks

```bash
# Did any detector run?
grep -E 'DriftDetected|drift observed' /path/to/agent.log | tail -30

# Did the relevant callback fire?
grep -E 'after_tool_callback|on_tool_error_callback|after_model_callback|before_tool_callback' /path/to/agent.log | tail -30

# Did the steerer even see a control?
grep -E 'STEER received|CANCEL received|control_ack' /path/to/agent.log | tail -20

# Check harmonograf's intervention list via the server RPC:
uv run --with grpcurl grpcurl -plaintext \
  -d '{"session_id":"<SID>"}' \
  localhost:7531 harmonograf.v1.Harmonograf/ListInterventions
```

## Root-cause candidates (ranked)

1. **Tool swallowed the exception** — a tool that does
   `try: ... except: return {"error": "..."}` never trips
   `on_tool_error_callback`. The tool returns "successfully" so
   `TOOL_ERROR` doesn't fire. Same symptom: the agent clearly
   failed, but harmonograf and goldfive see nothing.
2. **Callback never fired** — the ADK version skipped the callback
   for this event type. `after_tool_callback` coverage is the most
   common gap.
3. **Detector exception swallowed** — a detector raised and goldfive
   logged at DEBUG. Run with `LOG_LEVEL=DEBUG` to see.
4. **STEER annotation body was rejected** — empty body, body > 8 KiB,
   or ASCII control characters. The client-side bridge rejects it
   before goldfive's steerer sees it. Check the frontend for an
   inline error.
5. **STEER bypassed goldfive** — some clients don't install the
   goldfive control bridge. Controls deliver but drift never synthesises.
6. **Plan was None when drift was evaluated** — goldfive refuses to
   refine against an empty plan. Check that `PlanSubmitted` fired
   before the drift.
7. **Duplicate `HarmonografTelemetryPlugin`** (#68) — if the plugin
   was installed twice, the later instance silently disabled itself.
   Span visibility may be intact but any drift inference that depended
   on span-attribute hooks from the disabled instance is lost. Look
   for `duplicate HarmonografTelemetryPlugin instance detected` at INFO.
8. **Tool-loop threshold not reached** — `LOOPING_REASONING` needs a
   minimum repetition count (configurable in goldfive). A two-step
   loop won't trip it.

## Diagnostic steps

### 1. Goldfive drift log

Run with goldfive at INFO:

```bash
GOLDFIVE_LOG_LEVEL=INFO ...
grep -E 'DriftDetected' /path/to/agent.log
```

If you see drifts firing on unrelated events but not yours, the
detector for your event class is blind.

### 2. Intervention RPC

```bash
grpcurl -plaintext \
  -d '{"session_id":"sess_2026-04-21_0001"}' \
  localhost:7531 harmonograf.v1.Harmonograf/ListInterventions | jq .
```

Empty `interventions` list for a session where you triggered a STEER
means the annotation didn't make it to goldfive.

### 3. STEER annotation diagnostics

```bash
sqlite3 data/harmonograf.db \
  "SELECT id, kind, body, author, created_at, delivered_at
   FROM annotations
   WHERE session_id='<SID>' AND kind='STEERING'
   ORDER BY created_at DESC LIMIT 5;"
```

- `delivered_at IS NULL` → the STEER never made it to the agent. The
  server dispatched a `STEER` ControlEvent but no ack came back.
  Check `control_router` logs.
- Body looks empty or malformed → validation rejected it on the
  client bridge.

### 4. Duplicate plugin

```bash
grep 'duplicate HarmonografTelemetryPlugin' /path/to/agent.log
```

If present, remove one of the installation points. The plugin is
usually installed via `App(plugins=[HarmonografTelemetryPlugin(c)])`
— make sure a downstream `observe()` or `add_plugin` isn't also
installing it.

### 5. Tool swallowing

Audit the tool source. If it has broad `except`, let the exception
propagate instead.

## Fixes

1. Stop swallowing tool exceptions.
2. Upgrade ADK if a callback isn't firing.
3. Fix the detector's exception, add a unit test.
4. Post STEER annotations with non-empty, < 8 KiB bodies.
5. Ensure only one `HarmonografTelemetryPlugin` lands on the
   `PluginManager`.
6. For loops, tune goldfive's `ToolLoopTracker` threshold or
   instrument the tool to surface the ambiguity.

## Cross-links

- [user-guide/control-actions.md](../user-guide/control-actions.md)
  — STEER body constraints.
- [user-guide/trajectory-view.md](../user-guide/trajectory-view.md) —
  how drifts surface on the intervention timeline.
- [dev-guide/debugging.md](../dev-guide/debugging.md) — server-side
  SQL snippets for inspecting plans, annotations, agents.
