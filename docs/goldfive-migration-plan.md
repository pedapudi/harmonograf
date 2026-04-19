# Harmonograf → Goldfive Migration Plan

> **STATUS: COMPLETED (Phase D landed 2026-04-18).**
> All four phases merged on `main`:
> - Phase A — proto migration + goldfive dependency ([#6](https://github.com/pedapudi/harmonograf/pull/6), closes [#2](https://github.com/pedapudi/harmonograf/issues/2))
> - Phase B — `HarmonografSink` + goldfive event ingest ([#7](https://github.com/pedapudi/harmonograf/pull/7), closes [#3](https://github.com/pedapudi/harmonograf/issues/3))
> - Phase C — delete legacy client orchestration, rewire demos on goldfive ([#9](https://github.com/pedapudi/harmonograf/pull/9), closes [#4](https://github.com/pedapudi/harmonograf/issues/4))
> - Phase D — e2e validation + cleanup + integration guide (this PR, closes [#5](https://github.com/pedapudi/harmonograf/issues/5) and meta [#1](https://github.com/pedapudi/harmonograf/issues/1))
>
> The current integration surface is documented in
> [goldfive-integration.md](goldfive-integration.md). The rest of this file is
> preserved as the design record.

---

*Author:* Architect pass (2026-04-18)
*Scope:* Refactor harmonograf to delegate orchestration to goldfive; retain observability (server, storage, frontend, spans, sessions, agents, annotations, control routing) as harmonograf's unique value.

---

## 0. Executive summary

**Before:** harmonograf owns two things — (a) observability of a running agent (spans, sessions, frontend), and (b) plan-driven orchestration (PlannerHelper, `_AdkState` task state machine, drift detection, reporting tools, re-invocation loops, HarmonografAgent wrapper). Roughly 50% of the client lives in (b); (b) leaks into proto/types.proto (Task, TaskPlan, TaskStatus, UpdatedTaskStatus) and into server storage (Task/TaskPlan tables, task-status deltas, hgraf.task_id span binding).

**After:** harmonograf owns only (a). Goldfive owns (b). Harmonograf becomes a `goldfive.EventSink` that translates goldfive `Event` envelopes into harmonograf `TelemetryUp` frames so the existing ingest → storage → bus → frontend pipeline keeps working for plan/task/drift signals. Spans + sessions + control routing + frontend stay in harmonograf unchanged.

**Key decision D7 (pinned from prior conversation):** harmonograf's `proto/harmonograf/v1/types.proto` **deletes** its duplicate `Plan`, `Task`, `TaskEdge`, `TaskPlan`, `UpdatedTaskStatus`, `TaskStatus`, `DriftKind`, and imports / references goldfive's equivalents from `goldfive/v1/types.proto`. Harmonograf's `telemetry.proto` gains an `Event goldfive_event = 11;` variant inside `TelemetryUp` that carries a `goldfive.v1.Event` verbatim. The pre-existing `task_plan` / `task_status_update` variants in TelemetryUp are **removed** — plan and task state now ride through `goldfive_event`.

**Non-goals:** we do not re-design harmonograf's span data model, the frontend Gantt, SessionBus fan-out, or the heartbeat / payload pipeline. These are value that harmonograf adds on top of goldfive and are staying exactly as they are.

**Out-of-scope but worth noting:** once this migration lands, harmonograf's client library becomes very thin — `Client` (the span transport), `HarmonografSink` (the goldfive adapter), and identity bookkeeping. Everything else moves, is deleted, or becomes demo-only.

---

## 1. File-level inventory

Legend:
- **DELETE** — functionality exists in goldfive; harmonograf removes its copy.
- **REPLACE** — keep the file but rewrite its contents against goldfive.
- **KEEP** — observability-specific; stays unchanged (or with minor cleanups).
- **EXTEND** — harmonograf-specific specialization on top of goldfive.

### 1.1 Client (`client/harmonograf_client/`)

| File | Verdict | Rationale |
|---|---|---|
| `__init__.py` | REPLACE | Public surface shrinks. Export `Client`, `HarmonografSink`, `Capability`, `SpanKind`, `SpanStatus`, `ControlAckSpec`, and convenience factories. Drop `HarmonografAgent`, `HarmonografRunner`, `LLMPlanner`, `PassthroughPlanner`, `Plan`, `PlannerHelper`, `Task`, `TaskEdge`, `attach_adk`, `make_adk_plugin`, `make_harmonograf_agent`, `make_harmonograf_runner`, `make_default_adk_call_llm`. These live in goldfive now. |
| `planner.py` (547 lines) | DELETE | `Plan`, `Task`, `TaskEdge`, `PlannerHelper`, `PassthroughPlanner`, `LLMPlanner`, `make_default_adk_call_llm` all have direct equivalents in `goldfive.planner` and `goldfive.types`. The default system prompts in harmonograf and goldfive overlap almost word-for-word; verify goldfive's prompt is at least as good before deleting harmonograf's (it is — `goldfive.planner.LLMPlanner` carries the richer prompt from harmonograf). |
| `tools.py` (215 lines) | DELETE (mostly) | The seven reporting functions + `REPORTING_TOOL_NAMES` + `build_reporting_function_tools` + `augment_instruction` + `SUB_AGENT_INSTRUCTION_APPENDIX` all live in goldfive (`BUILTIN_REPORTING_TOOLS` in `goldfive.reporting`, subtree augmentation inside `goldfive.adapters.adk`). **One leftover to keep:** `SUB_AGENT_INSTRUCTION_APPENDIX` / `augment_instruction` is imported by `tests/reference_agents/presentation_agent/agent.py`. Either (a) point that import at `goldfive.adapters.adk.instruction_appendix` (need to check the symbol is exported — it is not currently a public name in goldfive; file follow-up goldfive issue to expose it) or (b) keep a tiny compatibility shim in harmonograf that re-exports from goldfive. Recommendation: option (a) with a one-line goldfive PR making `augment_instruction` public. |
| `state_protocol.py` (385 lines) | DELETE | The `harmonograf.*` session-state schema is an ADK implementation detail of the old orchestrator. Goldfive owns orchestration now and uses `SessionContext` on ADK state (`goldfive.adapters._adk_plugin.SESSION_CONTEXT_STATE_KEY`) instead. The `KEY_*` constants, `read_*` helpers, `write_current_task`, `write_plan_context`, `write_tools_available`, and `extract_agent_writes` are all referenced from `adk.py` and `agent.py`, both of which are also deleted. No external consumers. |
| `adk.py` (5909 lines — `_AdkState`, `AdkAdapter`, `attach_adk`, `make_adk_plugin`) | DELETE | `_AdkState` is goldfive's `DefaultSteerer`. `AdkAdapter` + `attach_adk` + `make_adk_plugin` are goldfive's `ADKAdapter` (in `goldfive.adapters.adk`). The telemetry callbacks (span emission for BEFORE/AFTER model/tool/run, state_delta diffing, tool-error classification) are the part to preserve, but they belong in a new `HarmonografTelemetryPlugin` — a small ADK `BasePlugin` that emits spans via the surviving `Client`. **See §4.3 below** for how this is recovered as a separate, observability-only plugin. |
| `agent.py` (1903 lines — `HarmonografAgent`) | DELETE | Goldfive's Runner + ADKAdapter subsumes the entire orchestration loop. The re-invocation logic, parallel DAG walker, orchestrator/delegated/parallel modes — all live in `goldfive.executors` and `goldfive.runner`. Users construct a `goldfive.Runner` directly and install a `HarmonografSink` on it (see §4). |
| `runner.py` (217 lines — `HarmonografRunner`, `make_harmonograf_runner`) | DELETE | Replaced by `goldfive.Runner`. The demo scripts get a thin one-liner factory (see §4.4). |
| `client.py` (513 lines — `Client`, `submit_plan`, `submit_task_status_update`) | REPLACE (shrink) | `Client` keeps span emission, payload upload, control-handler registration, identity, heartbeat, shutdown. **Delete** `submit_plan` and `submit_task_status_update` methods — plan / task state now rides through `HarmonografSink → goldfive_event` instead of dedicated `TaskPlan` / `UpdatedTaskStatus` envelopes. Add a new low-level method `emit_goldfive_event(event_pb)` that pushes the event through the existing buffer/transport pipeline. The `planner.Plan` import dependency is removed. |
| `transport.py` (705 lines) | REPLACE (minor) | Keep all reconnect / breaker / heartbeat / control-stream logic. Remove the `TASK_PLAN` and `TASK_STATUS_UPDATE` `EnvelopeKind` cases; add a `GOLDFIVE_EVENT` envelope kind (see `buffer.py`). The serializer in `transport.py` that converts envelopes into `TelemetryUp` gains a case for `GOLDFIVE_EVENT → TelemetryUp(goldfive_event=...)`. Otherwise unchanged. |
| `buffer.py` (237 lines — `EventRingBuffer`, `PayloadBuffer`, `SpanEnvelope`, `EnvelopeKind`) | REPLACE (minor) | `EnvelopeKind` enum: delete `TASK_PLAN` and `TASK_STATUS_UPDATE` variants; add `GOLDFIVE_EVENT`. Eviction policy unchanged (goldfive events are low-volume and should not be evicted before span updates — add them to the "never evict" tier alongside SPAN_START / SPAN_END, at least for INFO/WARNING drift; CRITICAL drift should be preserved at all costs). |
| `heartbeat.py` | KEEP | Transport-level heartbeat unchanged. |
| `identity.py` | KEEP | Agent identity persisted to disk is observability-scoped. |
| `invariants.py` (426 lines) | DELETE | This validates invariants on `_AdkState`'s in-memory plan — goldfive has its own invariants. |
| `metrics.py` | KEEP, maybe DELETE | Only referenced by `adk.py` (`ProtocolMetrics`). If no other callers, delete with `adk.py`. Grep confirms it is only imported from `adk.py` → **DELETE**. |
| `enums.py` | KEEP | SpanKind / SpanStatus / Capability mirror harmonograf's proto enums; still used by surviving `Client`. |
| `sink.py` | **NEW** | See §3. |
| `telemetry_plugin.py` (or keep `adk.py` slot) | **NEW** | Optional. The span-emitting callbacks from old `adk.py` get extracted into a tiny ADK plugin whose only job is to emit spans via `Client`. Goldfive's ADKAdapter handles orchestration; this plugin handles observability. Users who want telemetry install it alongside goldfive. See §4.3. |

**Rough line-count impact on the client:**
- DELETE: `planner.py` 547 + `tools.py` 215 + `state_protocol.py` 385 + `adk.py` 5909 + `agent.py` 1903 + `runner.py` 217 + `invariants.py` 426 + `metrics.py` 55 = **9657 lines deleted**
- REPLACE (net delta small): `__init__.py`, `client.py` (~50 lines out), `transport.py` (~20 lines out), `buffer.py` (~10 lines delta)
- NEW: `sink.py` (~250 lines), `telemetry_plugin.py` (~600 lines, a distilled subset of old `adk.py`'s span-emission paths).

The client library ends up roughly 1500 LOC instead of 10000+.

### 1.2 Proto (`proto/harmonograf/v1/`)

| File | Verdict | Rationale |
|---|---|---|
| `types.proto` | REPLACE | Delete `TaskStatus`, `Task`, `TaskEdge`, `TaskPlan`, `UpdatedTaskStatus` messages. Optional: delete `revision_*` fields since they were only used inside `TaskPlan`. Keep: `Session`, `SessionStatus`, `Agent`, `AgentStatus`, `Framework`, `Capability`, `Span`, `SpanKind`, `SpanStatus`, `SpanLink`, `LinkRelation`, `AttributeValue`, `AttributeArray`, `PayloadRef`, `ErrorInfo`, `Annotation`, `AnnotationKind`, `AnnotationTarget`, `AgentTimePoint`, `ControlKind`, `ControlTarget`, `ControlEvent`, `ControlAck`, `ControlAckResult`. See §2 for the precise diff. |
| `telemetry.proto` | REPLACE | `TelemetryUp`: delete `task_plan = 9` and `task_status_update = 10`; add `goldfive.v1.Event goldfive_event = 11`. Add `import "goldfive/v1/events.proto";`. Hello / Welcome / SpanStart / SpanUpdate / SpanEnd / PayloadUpload / Heartbeat / Goodbye / ControlAck unchanged. |
| `control.proto` | KEEP | Control delivery (pause/resume/cancel/steer) is harmonograf-native. |
| `frontend.proto` | REPLACE (lightly) | References to harmonograf's `TaskPlan` / `Task` / `UpdatedTaskStatus` in SessionUpdate deltas must switch to `goldfive.v1.Plan` / `goldfive.v1.Task`. If there's a field like `repeated harmonograf.v1.TaskPlan plans`, it becomes `repeated goldfive.v1.Plan plans`. See §2 for specifics after reading the full file. |
| `service.proto` | KEEP | RPC methods unchanged. |
| `buf.yaml` | REPLACE | Add dependency declaration / path to goldfive proto tree. The simplest path: vendor goldfive's `proto/goldfive/v1/` into harmonograf's proto repo under `third_party/goldfive/v1/` (symlink or git-subtree) and add `--proto_path=third_party` to the Makefile's protoc invocation. A neater long-term path is buf modules, but that is not warranted for v1. |

### 1.3 Server (`server/harmonograf_server/`)

| File | Verdict | Rationale |
|---|---|---|
| `ingest.py` (773 lines) | REPLACE | Major surgery. (a) Delete `_handle_task_plan` and `_handle_task_status_update`. (b) Add `_handle_goldfive_event(ctx, event_pb)` that dispatches on `event.WhichOneof("payload")` and updates storage + publishes bus deltas. (c) `_bind_task_to_span` stays (reading `hgraf.task_id` from span attributes is still a useful cross-correlation — the server owns span→task binding independently of whether goldfive or a legacy client emits the plan). (d) The `_task_index` cache stays — now populated from `PlanSubmitted` / `PlanRevised` events rather than from `TaskPlan` messages. See §5. |
| `storage/base.py` (419 lines) | REPLACE (partially) | Keep all Session / Agent / Span / Annotation / Payload / ContextWindowSample abstractions. Replace local `Task`, `TaskEdge`, `TaskPlan`, `TaskStatus` dataclasses with re-exports from `goldfive.types`. The store interface methods `put_task_plan`, `get_task_plan`, `list_task_plans_for_session`, `update_task_status` stay (they're the server's persistence contract), but their signatures now use goldfive dataclasses. The storage field `TaskPlan.session_id` is harmonograf-specific — keep it by wrapping goldfive's `Plan` in a tiny harmonograf-side record that pairs a `goldfive.types.Plan` with `session_id`, `invocation_span_id`, `planner_agent_id`, `created_at`. Simpler alternative: extend goldfive's `Plan` at the storage layer by storing session_id alongside it in a separate column, since goldfive's `Plan.run_id` is the orthogonal identifier. **Recommendation:** keep a thin `StoredTaskPlan` wrapper dataclass in harmonograf that composes `goldfive.types.Plan` with `session_id`, `invocation_span_id`, `planner_agent_id`, `created_at`, `bound_span_ids: dict[task_id, span_id]`. This avoids polluting goldfive's types with harmonograf's session concept while keeping the storage schema intact. |
| `storage/memory.py` (463 lines) | REPLACE (light) | Mechanical rename: `Task`/`TaskEdge`/`TaskPlan` import sites move to `goldfive.types` + the new `StoredTaskPlan` wrapper. Logic unchanged. |
| `storage/sqlite.py` (1159 lines) | REPLACE (light) | Same as memory — import renames. **Schema migration:** current columns (`revision_reason`, `revision_kind`, `revision_severity`, `revision_index`) already exist on `task_plans`; goldfive's `Plan` uses the same. Adding a `BLOCKED` task status: update the check constraint and the default values (simplest: no check constraint, app-level validation). New column: `run_id TEXT NOT NULL DEFAULT ''` on `task_plans` to carry `goldfive.types.Plan.run_id`. See §9 for the schema migration section. |
| `storage/postgres.py` (216 lines) | REPLACE (light) | Same as sqlite. |
| `storage/factory.py` | KEEP | |
| `storage/__init__.py` | REPLACE | Re-export from `goldfive.types` for Task/TaskEdge/TaskStatus; keep the rest pointed at `harmonograf_server.storage.base`. |
| `convert.py` (454 lines) | REPLACE (significant) | Span / annotation / agent conversions stay. Task / TaskPlan / UpdatedTaskStatus conversions are deleted. Add new helpers `goldfive_plan_to_stored(event_pb) -> StoredTaskPlan`, `stored_plan_to_goldfive_pb(stored) -> goldfive.v1.Plan`, `apply_goldfive_task_transition(stored, task_id, status, ...)`. Use goldfive's existing `goldfive.conv` module for dataclass ↔ pb roundtrips so harmonograf only deals in goldfive dataclasses + its own wrapper. |
| `control_router.py` (349 lines) | KEEP | Control routing is harmonograf-specific. Goldfive's drift/steerer is internal to the agent; harmonograf's ControlRouter sends pause/resume/steer from the UI to the agent. These are orthogonal concerns. See §6. |
| `bus.py` (227 lines) | REPLACE (light) | The `DELTA_TASK_PLAN` and `DELTA_TASK_STATUS` delta kinds stay — that's the channel WatchSession uses to push plan updates to the frontend. The delta *payload* changes shape: instead of carrying harmonograf-proto `TaskPlan`, it carries the new `StoredTaskPlan` wrapper (which composes a goldfive Plan). Add one new delta kind: `DELTA_DRIFT = "drift"` for `DriftDetected` events — the frontend renders drift markers on the timeline. Add `DELTA_GOAL_DERIVED = "goal_derived"` optionally. |
| `rpc/frontend.py` (735 lines) | REPLACE (light) | Where `TaskPlan` / `Task` / `UpdatedTaskStatus` / `task_status_to_pb` are referenced in `WatchSession` / delta dispatch, they switch to goldfive types. Add delta handlers for drift + goal events in the WatchSession loop. |
| `rpc/telemetry.py` | KEEP | Stream lifecycle unchanged. |
| `rpc/control.py` | KEEP | |
| `main.py` / `cli.py` / `config.py` / `auth.py` / `health.py` / `metrics.py` / `retention.py` / `logging_setup.py` / `stress.py` / `_cors.py` / `_sonora_shim.py` | KEEP | Unaffected. |

### 1.4 Frontend (`frontend/src/`)

| Path | Verdict | Rationale |
|---|---|---|
| `src/pb/harmonograf/v1/*.ts` | REPLACE (regen) | Regenerate from the new harmonograf protos (which now reference goldfive protos). |
| `src/pb/goldfive/v1/*.ts` | **NEW** | Generate from goldfive's protos. buf.gen.yaml gets a new plugin invocation. |
| `src/gantt/driftKinds.ts` | REPLACE | Currently likely maps harmonograf's string `revision_kind` to icons/colors. After migration, the string values stay the same (goldfive's DriftKind enum is intentionally mirrored), but the enum values come from `goldfive.v1.DriftKind`. Adjust imports. |
| `src/gantt/*.ts` (renderer, layout, types, etc.) | KEEP (imports change) | Where the Gantt references `TaskPlan` / `Task` / task `status`, update imports to the goldfive pb. |
| `src/state/` | KEEP (imports change) | Redux / state slices referencing task types get import-only changes. |
| `src/rpc/` | KEEP | gRPC client calls the same RPCs. |
| `frontend/buf.gen.yaml` | REPLACE | Add goldfive proto tree to inputs. |

Full frontend migration is mechanical once the proto regen is done. No UX or rendering logic needs to change — the goldfive enums and shapes are deliberately parallel.

### 1.5 Tests

| Path | Verdict | Rationale |
|---|---|---|
| `tests/e2e/test_adk_hello.py` | KEEP (small edits) | Transport-level handshake. Unaffected by orchestration move. |
| `tests/e2e/test_planner_e2e.py` | REPLACE | Rewrite using `goldfive.Runner` + `HarmonografSink`; assert harmonograf server observes `PlanSubmitted` / `TaskStarted` / `TaskCompleted` via the bus. |
| `tests/e2e/test_presentation_agent.py` | REPLACE | Rewrite reference-agent test around `goldfive.Runner`. |
| `tests/e2e/test_scenarios.py` | REPLACE | Same. |
| `tests/integration/*` | AUDIT | Most are span-level; keep. Drop anything that exercises `HarmonografAgent` / `HarmonografRunner` directly. |
| `tests/storage_conformance_test.py` | KEEP (imports change) | Test the new `StoredTaskPlan` wrapper + goldfive dataclasses. |
| `tests/reference_agents/presentation_agent/agent.py` | REPLACE | Rewrite the bootstrap to use `goldfive.Runner(... , sinks=[HarmonografSink(client)])` instead of `HarmonografAgent` / `make_harmonograf_runner` / plugin install. |
| `client/tests/test_planner.py` | DELETE | Coverage lives in goldfive. |
| `client/tests/test_agent.py` | DELETE | |
| `client/tests/test_runner.py` | DELETE | |
| `client/tests/test_reporting_tools.py` | DELETE | |
| `client/tests/test_reporting_registration.py` | DELETE | |
| `client/tests/test_state_protocol.py` | DELETE | |
| `client/tests/test_drift_taxonomy.py` | DELETE | |
| `client/tests/test_task_lifecycle.py` | DELETE | |
| `client/tests/test_invariants.py` | DELETE | |
| `client/tests/test_tool_callbacks.py` | DELETE | |
| `client/tests/test_model_callbacks.py` | DELETE | (unless there are non-orchestration-specific span emissions to cover; audit) |
| `client/tests/test_protocol_callbacks.py` | DELETE | |
| `client/tests/test_event_observation.py` | DELETE | |
| `client/tests/test_orchestration_modes.py` | DELETE | |
| `client/tests/test_walker_*.py` | DELETE | |
| `client/tests/test_dynamic_plans_real_adk.py` | DELETE | |
| `client/tests/test_llm_agency_scenarios.py` | DELETE | |
| `client/tests/test_callback_perf.py` | AUDIT | Keep if it exercises the surviving span-emit plugin; delete otherwise. |
| `client/tests/test_client_plan.py` | DELETE | `submit_plan` is gone. |
| `client/tests/test_client_api.py` | KEEP (trim) | Keep the span-emission half; drop the plan half. |
| `client/tests/test_buffer.py` | KEEP | Buffer still holds envelopes. Update for the new `GOLDFIVE_EVENT` enum. |
| `client/tests/test_ring_buffer.py` | KEEP | |
| `client/tests/test_transport_*` | KEEP | Transport-level, unaffected. |
| `client/tests/test_heartbeat.py` | KEEP | |
| `client/tests/test_identity.py` | KEEP | |
| `client/tests/test_auth.py` | KEEP | |
| `client/tests/test_adk_adapter.py` | DELETE or KEEP | Delete the orchestration-related cases; keep the ones that exercise span emission via the surviving telemetry plugin. |
| `client/tests/test_adk_helpers.py` | AUDIT | |
| `client/tests/test_sink.py` | **NEW** | Round-trip a few Event kinds through `HarmonografSink`; assert the bytes pushed onto the buffer equal the expected `TelemetryUp(goldfive_event=...)`. |
| `server/tests/test_task_plans.py` | REPLACE | Exercise the new goldfive-event ingest path; assert the store ends up with the expected `StoredTaskPlan`. |
| `server/tests/test_telemetry_ingest.py` | REPLACE (light) | Update ingest tests that emit `TaskPlan` / `UpdatedTaskStatus` to emit `goldfive_event` instead. |
| `server/tests/test_ingest_extensive.py` | REPLACE (light) | Same. |
| `server/tests/storage_test.py` | REPLACE (light) | Imports. |
| `server/tests/test_storage_extensive.py` | REPLACE (light) | Imports. |
| `server/tests/test_rpc_frontend.py` | AUDIT | If it asserts WatchSession delta shapes for task plans, update. |
| `server/tests/test_control_*.py` | KEEP | Control routing is untouched. |
| `server/tests/test_bus_extensive.py` | KEEP | Update only the delta-payload shape assertions. |

**Tests to add (new):**
1. `client/tests/test_sink.py` — Exercises `HarmonografSink.emit` for each of the thirteen `Event.payload` variants. Asserts each yields the expected `TelemetryUp(goldfive_event=...)` push onto the client's buffer.
2. `server/tests/test_goldfive_ingest.py` — Ingest pipeline receives each event variant and produces the expected storage / bus side-effects.
3. `tests/e2e/test_goldfive_roundtrip.py` — Full loop: `goldfive.Runner` with a fake adapter runs → emits events → `HarmonografSink` ships them → harmonograf server ingests → storage reflects the plan → frontend WatchSession observes the deltas.

---

## 2. Proto migration

### 2.1 Messages to DELETE from `proto/harmonograf/v1/types.proto`

- `enum TaskStatus` (lines 225-232)
- `message TaskEdge` (lines 236-239)
- `message Task` (lines 241-256)
- `message TaskPlan` (lines 258-287)
- `message UpdatedTaskStatus` (lines 292-298)

Optionally delete the comment header block above these (lines 213-224) since the explanation moves to goldfive.

### 2.2 Unaffected messages in `types.proto`

Keep `Session`, `SessionStatus`, `Agent`, `AgentStatus`, `Framework`, `Capability`, `SpanKind`, `SpanStatus`, `LinkRelation`, `SpanLink`, `AttributeValue`, `AttributeArray`, `PayloadRef`, `ErrorInfo`, `Span`, `AnnotationKind`, `AnnotationTarget`, `AgentTimePoint`, `Annotation`, `ControlKind`, `ControlTarget`, `ControlEvent`, `ControlAck`, `ControlAckResult`.

### 2.3 `telemetry.proto` changes

Add import:
```
import "goldfive/v1/events.proto";
```

Replace the `TelemetryUp` oneof:
```
message TelemetryUp {
  oneof msg {
    Hello hello = 1;
    SpanStart span_start = 2;
    SpanUpdate span_update = 3;
    SpanEnd span_end = 4;
    PayloadUpload payload = 5;
    Heartbeat heartbeat = 6;
    ControlAck control_ack = 7;
    Goodbye goodbye = 8;
    // Reserved: were TaskPlan task_plan = 9 / UpdatedTaskStatus task_status_update = 10.
    // Plan + task state now ride inside goldfive_event.
    reserved 9, 10;
    // Goldfive orchestration event. Carries the full goldfive Event envelope
    // (RunStarted, GoalDerived, PlanSubmitted, PlanRevised, TaskStarted/
    // Progress/Completed/Failed/Blocked/Cancelled, DriftDetected,
    // RunCompleted, RunAborted). Server ingest dispatches on the event's
    // payload oneof; frontend WatchSession fans it out as typed deltas.
    goldfive.v1.Event goldfive_event = 11;
  }
}
```

Remove the `harmonograf/v1/types.proto` import since the deleted messages were the only consumer (Span, AttributeValue, PayloadRef, ErrorInfo, SpanStatus do continue to be used — keep the import).

### 2.4 `frontend.proto` changes

Search for references to `TaskPlan`, `Task`, `UpdatedTaskStatus`, `TaskStatus` in this file (must be done precisely on implementation; I saw hints in `rpc/frontend.py` of `SessionUpdate` variants for task plans and task status). Each becomes `goldfive.v1.Plan`, `goldfive.v1.Task`, `goldfive.v1.TaskStatus`. Also add an `Event goldfive_event = N;` variant to the `SessionUpdate` oneof so the frontend can consume goldfive events directly via WatchSession (this is what `bus.py` will be publishing).

### 2.5 `buf.yaml` / Makefile changes

`buf.yaml` stays; add a new dependency or vendor goldfive protos:

**Option A (vendor):** copy `goldfive/proto/goldfive/v1/` into `harmonograf/third_party/goldfive/v1/`. `Makefile` adds `--proto_path=third_party` to the protoc invocation.

**Option B (submodule):** add goldfive as a git submodule at `harmonograf/third_party/goldfive`. Cleaner for version pinning but adds submodule ceremony.

**Option C (Python package import):** goldfive's `goldfive/proto/goldfive/v1/*.proto` ships in the wheel. Resolve the path at make-time via `python -c "import goldfive; print(goldfive.__path__[0])"`. Works if we add the protos to the goldfive package data.

**Recommendation:** Option A for v1 — zero new tooling, zero network dependency during codegen, explicit version pinning via a pinned copy. Revisit when goldfive stabilizes a proto distribution.

### 2.6 Python pb package layout

Harmonograf's `client/harmonograf_client/pb/` and `server/harmonograf_server/pb/` end up containing:

```
pb/
  __init__.py          # sys.path insertion, re-exports types_pb2, telemetry_pb2, control_pb2, frontend_pb2, service_pb2
  harmonograf/v1/
    types_pb2.py       # regenerated (smaller — no Task/TaskPlan/etc.)
    telemetry_pb2.py   # regenerated (imports goldfive.v1.events_pb2)
    control_pb2.py     # unchanged shape
    frontend_pb2.py    # regenerated (imports goldfive.v1.types_pb2)
    service_pb2.py     # unchanged shape
  goldfive/v1/
    types_pb2.py       # generated from vendored proto
    events_pb2.py      # generated from vendored proto
```

Importantly, harmonograf must generate goldfive's pbs locally (into its own pb tree) rather than importing from `goldfive.pb.goldfive.v1` — otherwise the two pb2 modules are distinct Python modules wrapping the same proto, which causes isinstance() issues at runtime when a goldfive-side `Event` (built by goldfive) is shipped via a harmonograf-side `TelemetryUp`. **Fix:** at the sink boundary, harmonograf's `HarmonografSink.emit` serializes the incoming goldfive Event to bytes and parses those bytes into *harmonograf's local* `goldfive.v1.events_pb2.Event` before packing into `TelemetryUp`. This is one `event.SerializeToString()` + `local.ParseFromString(bytes)`. Alternatively — and this is cleaner — harmonograf depends on `goldfive` as a regular Python package and imports `goldfive.pb.goldfive.v1.events_pb2` directly. `TelemetryUp.goldfive_event` then references the same module through protoc's `import "goldfive/v1/events.proto";`, which works because grpc_tools resolves descriptors by proto path, not Python module identity — as long as harmonograf's protoc invocation resolves `goldfive/v1/events.proto` from the vendored source. The generated harmonograf `telemetry_pb2.py` *will* emit `import goldfive.v1.events_pb2 as _goldfive_v1_events_pb2` when its proto imports `goldfive/v1/events.proto`. Setting `--python_out` to write into a directory where `goldfive/v1/events_pb2.py` also lives (harmonograf's local pb tree) resolves this.

**Decision:** harmonograf generates goldfive's pbs into its own pb tree AND depends on the `goldfive` Python package. At runtime we use harmonograf's local copy (this is what the generated imports resolve to). Double-generation is harmless; the two trees' wire formats are identical. The `HarmonografSink` uses `SerializeToString → ParseFromString` to cross the module boundary. See §3 for sink code.

---

## 3. `HarmonografSink` design

New file: `client/harmonograf_client/sink.py`. ~250 LOC.

### 3.1 Public shape

```python
class HarmonografSink:
    """A goldfive.EventSink that forwards events to a harmonograf server.

    Each goldfive Event is wrapped in a TelemetryUp(goldfive_event=...) and
    pushed onto the Client's existing buffer/transport pipeline.  Uses the
    same backpressure and reconnect semantics as spans — nothing new on
    the wire except the goldfive_event variant.
    """

    def __init__(self, client: Client) -> None: ...
    async def emit(self, event_pb: Any) -> None: ...
    async def close(self) -> None: ...
```

### 3.2 Emit implementation (pseudocode)

```python
async def emit(self, event_pb):
    # event_pb may be the goldfive-package events_pb2.Event OR an already-local
    # harmonograf-package copy.  We detect by descriptor full_name and
    # serialize/parse if the module identity differs.
    local_cls = self._client._telemetry_pb2.Event  # harmonograf-local alias via pb.goldfive.v1.events_pb2
    if type(event_pb) is not local_cls:
        bs = event_pb.SerializeToString()
        local_event = local_cls()
        local_event.ParseFromString(bs)
    else:
        local_event = event_pb

    env = SpanEnvelope(
        kind=EnvelopeKind.GOLDFIVE_EVENT,
        span_id="",   # goldfive events don't bind to a single span
        payload=self._client._telemetry_pb2.TelemetryUp(goldfive_event=local_event),
        has_payload_ref=False,
    )
    self._client._events.push(env)
    self._client._transport.notify()
```

`emit` is sync under the hood but declared async to satisfy `EventSink`. It never awaits IO.

### 3.3 Close semantics

`close()` is a no-op — the sink does not own the Client's lifecycle. The caller calls `client.shutdown()` separately.

### 3.4 Event → TelemetryUp mapping

All thirteen goldfive Event payload oneofs ride through `TelemetryUp.goldfive_event` unchanged — no per-event dispatch on the client side. Server ingest dispatches on `event.WhichOneof("payload")` (see §5).

### 3.5 Buffer and eviction policy

`GOLDFIVE_EVENT` is a new `EnvelopeKind` in `buffer.py`. Recommended eviction priority (highest-to-lowest priority to keep):

1. `RunStarted`, `RunCompleted`, `RunAborted`, `PlanSubmitted`, `PlanRevised`, `GoalDerived`, `DriftDetected` (WARNING+ severity), `TaskFailed` (recoverable=false) — **never evict**.
2. `TaskStarted`, `TaskCompleted`, `TaskCancelled`, `TaskBlocked`, `DriftDetected` (INFO) — evict only if we're about to drop a whole span envelope.
3. `TaskProgress` — evict before span updates (it's a frequent, non-terminal signal).

The current `EventRingBuffer._evict_one_locked` has three tiers: drop oldest SPAN_UPDATE → strip oldest payload_ref → drop oldest SPAN_START/END. Extend this with:

- Tier 0.5 (before Tier 1): drop oldest `GOLDFIVE_EVENT` whose payload is `TaskProgress`.
- Tier 2.5 (before Tier 3): drop oldest `GOLDFIVE_EVENT` of an INFO drift / non-terminal task kind.

Implement this by having the sink tag envelopes with a subtype discriminator (a small string) that the buffer inspects — keeps the buffer free of proto knowledge.

### 3.6 Thread safety

`Client._events` and `Client._transport` are already thread-safe. `emit` is called from whatever event loop goldfive's executor is running on — the buffer push is thread-safe, and `transport.notify` is the existing cross-thread poke primitive.

---

## 4. `HarmonografAgent` replacement

### 4.1 Recommendation: **delete it; users construct `goldfive.Runner` directly**

HarmonografAgent's value was the re-invocation loop + orchestration-mode switching + plan enforcement. Goldfive's Runner + SequentialExecutor / ParallelDAGExecutor + DefaultSteerer provides all of it. Keeping a `HarmonografAgent` shim around goldfive.Runner would only obscure the fact that goldfive is in charge of orchestration.

### 4.2 User migration snippet

Old:
```python
from harmonograf_client import Client, HarmonografAgent, make_harmonograf_runner
client = Client(name="research", server_addr="localhost:7531")
wrapper = make_harmonograf_runner(agent=inner_agent, client=client)
async for ev in wrapper.run_async(...):
    ...
```

New:
```python
from harmonograf_client import Client, HarmonografSink
from goldfive import Runner, SequentialExecutor, LLMPlanner
from goldfive.adapters.adk import ADKAdapter

client = Client(name="research", server_addr="localhost:7531")
runner = Runner(
    agent=ADKAdapter(inner_agent),
    planner=LLMPlanner(...),
    executor=SequentialExecutor(),
    sinks=[HarmonografSink(client)],
)
outcome = await runner.run(user_request)
```

### 4.3 Surviving span emission — `HarmonografTelemetryPlugin`

Goldfive's ADKAdapter does not emit harmonograf spans. To preserve the current observability behavior (LLM call spans, tool call spans, invocation spans, transfer spans), harmonograf ships a small ADK plugin that lives alongside goldfive. This is roughly the `HarmonografAdkPlugin` body from the old `adk.py` minus everything orchestration-related.

```python
# client/harmonograf_client/telemetry_plugin.py
class HarmonografTelemetryPlugin(BasePlugin):
    """ADK plugin that emits harmonograf spans for every ADK lifecycle
    callback.  No orchestration — goldfive handles that."""
    def __init__(self, client: Client): ...
    async def before_run_callback(self, ...): ...   # INVOCATION span start
    async def after_run_callback(self, ...): ...    # INVOCATION span end
    async def before_model_callback(self, ...): ... # LLM_CALL start
    async def after_model_callback(self, ...): ...  # LLM_CALL end
    async def before_tool_callback(self, ...): ...  # TOOL_CALL start
    async def after_tool_callback(self, ...): ...   # TOOL_CALL end
    async def on_tool_error_callback(self, ...): ...# TOOL_CALL FAILED end
    async def on_event_callback(self, ...): ...     # TRANSFER spans
```

~600 LOC, extracted almost verbatim from the old `adk.py`'s span-emitting paths. Zero orchestration logic, no plan state reads/writes, no reporting-tool interception.

Users compose both:
```python
adk_adapter = ADKAdapter(inner_agent, plugins=[HarmonografTelemetryPlugin(client)])
runner = Runner(agent=adk_adapter, sinks=[HarmonografSink(client)], ...)
```

### 4.4 Factory convenience (optional)

> **Post-merge note:** this factory was *not* shipped in `harmonograf_client`.
> The public surface stayed minimal — `Client`, `HarmonografSink`,
> `HarmonografTelemetryPlugin`. The demo-agent convenience wrapper lives in
> the reference-agent module itself
> (`tests/reference_agents/presentation_agent/agent.py::build_goldfive_runner`)
> rather than in the library. The design sketch below is preserved for context.

If users find the two-step dance awkward, harmonograf can ship a one-liner:

```python
# client/harmonograf_client/__init__.py
def make_runner(inner_agent, *, server_addr, planner=None, ...):
    """Convenience: Client + HarmonografSink + HarmonografTelemetryPlugin
    + ADKAdapter + goldfive Runner, all wired up."""
    client = Client(name=inner_agent.name, server_addr=server_addr)
    plugin = HarmonografTelemetryPlugin(client)
    adapter = ADKAdapter(inner_agent, plugins=[plugin])
    return goldfive.Runner(
        agent=adapter,
        planner=planner or goldfive.PassthroughPlanner(),
        executor=goldfive.SequentialExecutor(),
        sinks=[HarmonografSink(client)],
    ), client
```

### 4.5 Demo-agent migration

`tests/reference_agents/presentation_agent/agent.py` currently constructs a `HarmonografRunner` / `HarmonografAgent` at module load and exports an ADK `app`. Rewrite as:

```python
from harmonograf_client import make_runner

runner, client = make_runner(
    inner_agent=root_agent,
    server_addr=os.environ.get("HARMONOGRAF_SERVER", "localhost:7531"),
    planner=goldfive.LLMPlanner(call_llm=..., model=root_agent.model),
)
# `app` is rebuilt to point at the goldfive Runner's underlying ADK runner.
```

If `app = google.adk.apps.App(root_agent=...)` is essential for `adk web` entry, expose `runner.agent.runner.app` (whatever path goldfive's ADKAdapter uses to produce the inner ADK runner). Verify goldfive's ADKAdapter surfaces an `app` property; if not, add a goldfive issue to expose it, or fall back to constructing the ADK App manually and passing it into ADKAdapter.

---

## 5. Server ingest changes

### 5.1 New entrypoint

```python
# server/harmonograf_server/ingest.py

async def _handle_goldfive_event(
    self, ctx: StreamContext, event_pb: goldfive_events_pb2.Event
) -> None:
    kind = event_pb.WhichOneof("payload")
    run_id = event_pb.run_id
    sequence = event_pb.sequence
    session_id = ctx.session_id  # goldfive has no session concept; reuse stream's session

    if kind == "run_started":
        await self._on_run_started(ctx, event_pb.run_started, run_id)
    elif kind == "goal_derived":
        await self._on_goal_derived(ctx, event_pb.goal_derived, run_id)
    elif kind == "plan_submitted":
        await self._on_plan_submitted(ctx, event_pb.plan_submitted, run_id)
    elif kind == "plan_revised":
        await self._on_plan_revised(ctx, event_pb.plan_revised, run_id)
    elif kind == "task_started":
        await self._on_task_started(ctx, event_pb.task_started, run_id)
    elif kind == "task_progress":
        await self._on_task_progress(ctx, event_pb.task_progress, run_id)
    elif kind == "task_completed":
        await self._on_task_completed(ctx, event_pb.task_completed, run_id)
    elif kind == "task_failed":
        await self._on_task_failed(ctx, event_pb.task_failed, run_id)
    elif kind == "task_blocked":
        await self._on_task_blocked(ctx, event_pb.task_blocked, run_id)
    elif kind == "task_cancelled":
        await self._on_task_cancelled(ctx, event_pb.task_cancelled, run_id)
    elif kind == "drift_detected":
        await self._on_drift_detected(ctx, event_pb.drift_detected, run_id)
    elif kind == "run_completed":
        await self._on_run_completed(ctx, event_pb.run_completed, run_id)
    elif kind == "run_aborted":
        await self._on_run_aborted(ctx, event_pb.run_aborted, run_id)
    else:
        logger.debug("ignoring unknown goldfive event payload: %s", kind)
```

### 5.2 Per-kind handlers

**`_on_plan_submitted`** — replaces `_handle_task_plan`:

```python
async def _on_plan_submitted(self, ctx, payload, run_id):
    gf_plan = goldfive.conv.from_pb_plan(payload.plan)   # -> goldfive.types.Plan
    stored = StoredTaskPlan(
        id=gf_plan.id,
        run_id=run_id,
        session_id=ctx.session_id,
        invocation_span_id="",  # Populated later if a span links back.
        planner_agent_id=ctx.agent_id,
        created_at=self._now(),
        plan=gf_plan,
        bound_span_ids={},
    )
    stored = await self._store.put_task_plan(stored)
    idx = self._task_index.setdefault(ctx.session_id, {})
    for task in stored.plan.tasks:
        idx[task.id] = stored.id
    self._bus.publish_task_plan(stored)
    logger.info("plan_submitted session=%s run=%s plan=%s tasks=%d",
                ctx.session_id, run_id, stored.id, len(stored.plan.tasks))
```

**`_on_plan_revised`** — upsert a revised plan with its drift metadata.

**`_on_task_started` / `_on_task_completed` / `_on_task_failed` / `_on_task_cancelled` / `_on_task_blocked`** — call `store.update_task_status(plan_id, task_id, goldfive-status, ...)`. Plan id is resolved from `_task_index[session_id][task_id]`.

**`_on_task_progress`** — optional: persist or just fan out on the bus without writing to storage. Recommendation: do not persist, just fan out; `task_progress` is a high-frequency, best-effort signal.

**`_on_drift_detected`** — publish a `DELTA_DRIFT` so the frontend can render a timeline marker. Optionally persist to a new `drift_events` table; v1 can skip persistence and keep drift ephemeral (the frontend only needs it for live display).

**`_on_run_started` / `_on_run_completed` / `_on_run_aborted`** — log and fan out; these are currently informational for the frontend.

### 5.3 Span-to-task binding

**Stays** — `_bind_task_to_span` continues to resolve `hgraf.task_id` attributes on leaf spans and transition the matching task to RUNNING. Goldfive does not know about spans, but harmonograf is still free to correlate its own span-level telemetry to tasks for visualization.

Important: the span-driven binding must not override an explicit goldfive-emitted status. Current code only fires on SpanStart (transition to RUNNING) and on terminal SpanEnd (transition to FAILED/CANCELLED). Keep that behavior — it only augments, never contradicts, goldfive's task_started / task_failed emissions.

The terminal-binding case becomes mildly redundant: goldfive will have already emitted `TaskFailed` or `TaskCancelled`, so the store's `update_task_status` will be a no-op on status but will populate `bound_span_id` (useful for the drawer's "what span ran this task?" link).

### 5.4 Store interface changes

`Store.put_task_plan(plan: StoredTaskPlan) -> StoredTaskPlan`  — same name, new dataclass.

`Store.get_task_plan(plan_id: str) -> Optional[StoredTaskPlan]`

`Store.update_task_status(plan_id, task_id, status: goldfive.TaskStatus, bound_span_id=None) -> Optional[goldfive.Task]`  — status enum changes.

`Store.list_task_plans_for_session(session_id: str) -> list[StoredTaskPlan]` — same.

Plus new:
`Store.list_task_plans_for_run(run_id: str) -> list[StoredTaskPlan]` — new. Useful for future multi-plan-per-run scenarios.

### 5.5 SQLite schema delta

Add one column:
```sql
ALTER TABLE task_plans ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_task_plans_run ON task_plans(run_id);
```

`revision_*` columns already match goldfive. `status` column on tasks gains a new value `BLOCKED`; if the schema has no CHECK constraint (confirmed from §1.3 SQL), this is free. If postgres has an enum constraint, add `ALTER TYPE task_status ADD VALUE 'BLOCKED'`.

### 5.6 Pb module wiring

`server/harmonograf_server/pb/__init__.py` re-exports generated goldfive events so ingest can do:
```python
from harmonograf_server.pb import goldfive_events_pb2, goldfive_types_pb2
```

The sink-side `SerializeToString`/`ParseFromString` detour is not needed server-side because the goldfive pb arrives already as harmonograf-local bytes (it was pushed as `TelemetryUp.goldfive_event` by the client, and we're reading it out of the same wire format).

---

## 6. Control events + drift — the seam

### 6.1 Keep the existing observer-frontend-to-agent channel

Harmonograf's ControlRouter + `ControlEvent` + `SubscribeControl` RPC stays. This is the "human watching the UI clicks pause" channel, orthogonal to goldfive's drift machinery.

### 6.2 How frontend steering reaches goldfive's drift pipeline

When a user clicks "Steer" in the frontend:

1. Frontend → `SendControl(agent_id, CONTROL_KIND_STEER, payload=<note>)` via the frontend RPC.
2. Server routes to the agent's `SubscribeControl` stream.
3. Agent-side Client receives the ControlEvent on its control-stream coroutine, invokes the registered handler.
4. The registered handler calls `steerer.report_user_steer(...)` directly OR — equivalently — invokes a reporting tool whose handler goes through goldfive's steerer.

To wire step 4: `HarmonografSink` also installs a control-ack callback on the `Client`, but more importantly — and this is the cleaner seam — the user registers a goldfive-aware control handler directly:

```python
def on_steer(event):
    # fire a goldfive drift event
    asyncio.create_task(
        runner.steerer.observe(
            {"kind": "user_steer", "detail": event.payload.decode()},
            session,
        )
    )
    return ControlAckSpec(result="success")

client.on_control("STEER", on_steer)
```

Goldfive's `DefaultSteerer.observe` already handles `user_steer` drift and triggers a plan refine. The corresponding `DriftDetected` event flows back through the same `HarmonografSink`, so the frontend sees a drift marker and (eventually) a `PlanRevised`.

For `PAUSE` / `RESUME` / `CANCEL` — these are more about the *transport* / ADK runner than about goldfive drift. Keep them as direct handlers on `Client`. Goldfive does not need to know about pause/resume; ADK handles the actual pause.

### 6.3 `report_plan_divergence` as a two-way street

When an agent calls `report_plan_divergence(note)`, goldfive's steerer emits `DriftDetected(kind=PLAN_DIVERGENCE)` then `PlanRevised` — both flow through `HarmonografSink`. The frontend displays a drift marker + the new plan. No code change needed beyond the sink.

### 6.4 STATUS_QUERY

Harmonograf's `CONTROL_KIND_STATUS_QUERY` asks the agent to describe what it is doing. This is orthogonal to drift — keep as-is, served by a handler on `Client` that introspects `goldfive.Session.current_task_id` via a closure shared with the running `Runner`.

---

## 7. Test migration plan

### 7.1 Deletion tranches (from §1.5)

Combined: roughly 20 client test files delete wholesale (covered by goldfive's 239 tests). Server tests are almost all keep-with-imports-changed.

### 7.2 New tests

**A. `client/tests/test_sink.py` (new, ~200 LOC)**
- For each event kind (13 variants), build the pb via `goldfive.events.*_event(...)`.
- Construct a `Client` with a mock transport.
- `await sink.emit(pb)`.
- Inspect `client._events._dq`: one `SpanEnvelope(kind=GOLDFIVE_EVENT)` whose `payload` is a `TelemetryUp` with the expected `goldfive_event.WhichOneof() == ...`.
- Assert sequence/run_id round-trip intact.
- Cover the module-boundary case: pass a goldfive-built Event, assert sink re-parses into harmonograf-local Event type.

**B. `server/tests/test_goldfive_ingest.py` (new, ~400 LOC)**
- `IngestPipeline` with `InMemoryStore` + `SessionBus`.
- Fabricate a `TelemetryUp(goldfive_event=...)` for each event kind.
- Call `pipeline.handle_message(ctx, msg)`.
- Assert storage side-effects: plan inserted, task status updated, bus delta published with the right kind / payload.
- Edge cases: `PlanRevised` for unknown plan_id → log + upsert; `TaskStarted` for unknown task_id → log-and-ignore; events arriving out of sequence → still handled (goldfive guarantees per-run monotonic sequence, but ingest should be tolerant since streams reconnect).

**C. `tests/e2e/test_goldfive_roundtrip.py` (new, ~300 LOC)**
- Full stack: `harmonograf_server` fixture + `Client` + `HarmonografSink` + `goldfive.Runner` with a stub adapter.
- Stub adapter returns deterministic InvocationResults (pretend the agent ran).
- Assert the store ends up with the expected plan + task statuses and the bus emits the expected delta sequence.

**D. `client/tests/test_client_api.py` (trim, ~-200 LOC)**
- Delete `test_submit_plan_*`, `test_submit_task_status_update_*`.
- Keep span emission, shutdown, metadata, identity, control callbacks.

**E. `server/tests/test_task_plans.py` (replace, ~500 LOC)**
- Rewrite around `goldfive_event`. Test plan persistence, revision lineage, status deltas.

**F. `tests/storage_conformance_test.py` (light edit)**
- Replace `TaskPlan` / `Task` construction with `StoredTaskPlan` + goldfive.

### 7.3 Testing order

Inside each PR in the phased plan (§8), local tests pass before moving on. The full e2e suite runs green after Phase C lands.

---

## 8. Phased execution plan

Each phase is a separate PR. Acceptance is "green CI + reviewer approval on the scope of that phase".

### Phase A — Proto migration (foundation)

**Scope:** vendor goldfive protos, regenerate harmonograf pb stubs (Python + TS), update `types.proto` / `telemetry.proto` / `frontend.proto`, add goldfive dependency to `pyproject.toml`. No functional code changes — just proto/codegen plumbing. Existing orchestration code temporarily continues to build against the deleted messages via a transitional `backport.py` that aliases the gone messages to simple dataclass stand-ins — or, simpler, delete the old orchestration wiring in Phase B without breaking tests in Phase A by making Phase A proto-only and gating compilation with `# type: ignore` / `try/except` on the now-missing imports.

**Cleaner alternative:** Phase A also removes the now-invalid imports from `client.py` and deletes `submit_plan` / `submit_task_status_update` and `client/tests/test_client_plan.py`. That keeps each file buildable.

**Files changed:**
- `proto/harmonograf/v1/types.proto` (delete 5 messages + 1 enum)
- `proto/harmonograf/v1/telemetry.proto` (replace oneof 9/10 with 11)
- `proto/harmonograf/v1/frontend.proto` (replace task type references)
- `Makefile` (add goldfive proto path; add proto-ts step for goldfive tree)
- `third_party/goldfive/v1/*.proto` (vendor)
- `pyproject.toml` (add `goldfive>=0.0.1` dep; add `goldfive[proto]` for codegen)
- `client/harmonograf_client/pb/` + `server/harmonograf_server/pb/` + `frontend/src/pb/` (regen)
- `client/harmonograf_client/buffer.py` (rename `TASK_PLAN` / `TASK_STATUS_UPDATE` → `GOLDFIVE_EVENT`)
- `client/harmonograf_client/transport.py` (serializer dispatch on new envelope)
- `client/harmonograf_client/client.py` (delete `submit_plan` and `submit_task_status_update`; `add emit_goldfive_event`)
- `client/tests/test_client_plan.py` (delete)
- `client/harmonograf_client/planner.py` (NOT yet deleted — still importable for `presentation_agent`; scheduled for Phase C)
- `client/harmonograf_client/__init__.py` (trim exports that are about to go)

**Acceptance criteria:**
- `make proto` regenerates both pb trees without errors.
- `uv run pytest client/tests/test_buffer.py client/tests/test_ring_buffer.py` pass.
- `uv run pytest client/tests/test_client_api.py` passes (after the plan-method removal).
- Frontend `pnpm build` succeeds after proto regen.
- Existing e2e tests that touched `submit_plan` are temporarily skipped with a TODO pointing at Phase B.
- No functional behaviour change yet — orchestration is still owned by harmonograf's `_AdkState` / `HarmonografAgent`, but those emit via direct `submit_plan` → *which is now gone* → so those tests are either skipped or rewritten in Phase B.

**Estimated diff:** +500 / -1500 lines, mostly codegen regen.

### Phase B — `HarmonografSink` + ingest

**Scope:** Land the sink, the server-side goldfive event ingest, and the storage-layer `StoredTaskPlan` wrapper. Orchestration code is still in-tree and functional via goldfive; the old harmonograf orchestrators no longer emit plans (they would if left alone, but Phase A deleted `submit_plan`, so this is enforced by the compiler).

**Files changed:**
- `client/harmonograf_client/sink.py` (new, ~250 lines)
- `client/harmonograf_client/telemetry_plugin.py` (new, ~600 lines — distilled span-emitting subset of old `adk.py`)
- `client/harmonograf_client/__init__.py` (export `HarmonografSink`, `HarmonografTelemetryPlugin`, `make_runner`)
- `server/harmonograf_server/storage/base.py` (add `StoredTaskPlan`; re-export goldfive types)
- `server/harmonograf_server/storage/memory.py` (mechanical rename)
- `server/harmonograf_server/storage/sqlite.py` (add `run_id` column + migration; mechanical rename)
- `server/harmonograf_server/storage/postgres.py` (same)
- `server/harmonograf_server/ingest.py` (add `_handle_goldfive_event` + subhandlers; remove `_handle_task_plan`, `_handle_task_status_update`)
- `server/harmonograf_server/convert.py` (delete task plan conversions; add goldfive pb ↔ stored conversions)
- `server/harmonograf_server/bus.py` (add `DELTA_DRIFT`, `DELTA_GOAL_DERIVED`; update `DELTA_TASK_PLAN` / `DELTA_TASK_STATUS` payload shapes)
- `server/harmonograf_server/rpc/frontend.py` (WatchSession dispatch on new deltas)
- `client/tests/test_sink.py` (new)
- `server/tests/test_goldfive_ingest.py` (new)
- `tests/e2e/test_goldfive_roundtrip.py` (new)
- `server/tests/test_task_plans.py` (rewrite)
- `server/tests/test_telemetry_ingest.py` / `test_ingest_extensive.py` (update)

**Acceptance criteria:**
- New sink + ingest tests pass.
- Existing span-level / control-router tests still pass.
- `tests/e2e/test_goldfive_roundtrip.py` is green.
- Schema migration is idempotent (run twice, no errors).

**Estimated diff:** +2500 / -1000 lines.

### Phase C — Remove legacy client orchestration

**Scope:** Delete the old harmonograf-owned orchestration code from the client library. Migrate demo agents to goldfive.

**Files changed:**
- DELETE `client/harmonograf_client/planner.py` (547 lines)
- DELETE `client/harmonograf_client/tools.py` (215 lines) — after confirming `augment_instruction` is re-exported from goldfive, or kept as a 5-line shim
- DELETE `client/harmonograf_client/state_protocol.py` (385 lines)
- DELETE `client/harmonograf_client/adk.py` (5909 lines) — after Phase B's `telemetry_plugin.py` has absorbed the span-emitting portions
- DELETE `client/harmonograf_client/agent.py` (1903 lines)
- DELETE `client/harmonograf_client/runner.py` (217 lines)
- DELETE `client/harmonograf_client/invariants.py` (426 lines)
- DELETE `client/harmonograf_client/metrics.py` (55 lines)
- DELETE all `client/tests/` orchestration tests (listed in §1.5)
- REWRITE `tests/reference_agents/presentation_agent/agent.py` around `make_runner` / goldfive Runner
- REWRITE `.demo-agents/presentation_agent/...` similarly (same content — this is just a staged copy)
- REWRITE `tests/e2e/test_presentation_agent.py`, `test_planner_e2e.py`, `test_scenarios.py`
- UPDATE `client/harmonograf_client/__init__.py` to final public surface

**Acceptance criteria:**
- Full test suite green.
- `make demo` / `make demo-presentation` succeed end-to-end.
- `grep -r HarmonografAgent` / `grep -r HarmonografRunner` / `grep -r PlannerHelper` in harmonograf returns zero hits (except CHANGELOG / docs).

**Estimated diff:** -10000 / +800 lines.

### Phase D — Cleanup and documentation

**Scope:** Update docs, kill dead third-party deps, tighten lints, final polish.

**Files changed:**
- `README.md` — update the client-side walkthrough, remove `HarmonografAgent` references.
- `docs/reporting-tools.md` — redirect to goldfive's reporting-tool docs (or inline copy the content with a "kept in sync from goldfive" header).
- `docs/adr/` — add an ADR recording decision D7 + the migration rationale.
- `docs/design/01-data-model-and-rpc.md` — update the §2 data-model section to note goldfive ownership of Plan / Task.
- `docs/quickstart.md` / `docs/operator-quickstart.md` — update snippets.
- `AGENTS.md` — update "Plan execution protocol" section to point at goldfive.
- `pyproject.toml` — tighten `goldfive` version pin to `>=0.0.1,<0.1`.
- `.agents/` conventions updates.
- Remove any dev dependencies that were only used by the deleted code.

**Acceptance criteria:**
- Docs rendered via mkdocs (if applicable) look right.
- No dead code (ruff `F401` clean on the touched files).
- Release notes drafted for v0.1.0.

**Estimated diff:** +400 / -600 lines (mostly doc updates).

---

## 9. Risk register

### R1 — Proto ABI break: old clients can't talk to new server

**Risk:** the moment the server removes `TelemetryUp.task_plan = 9` / `task_status_update = 10`, any still-running old client that tries to `submit_plan` will fail to serialize (field number retained by the `reserved` directive; server ignores). Worse, any still-running new client talking to an old server will have its `goldfive_event = 11` silently dropped on the floor.

**Mitigation:**
- The `reserved 9, 10;` in the new proto preserves wire compat (new server ignores old field without complaining; field numbers are never re-used).
- Document the server↔client version requirement in the release notes: v0.1 client ↔ v0.1 server. Refuse-to-connect logic can live in the `Welcome` flags map — the server includes `"goldfive_events": "true"` in `Welcome.flags`, and the client's sink logs a loud warning if this flag is absent (meaning: server is pre-migration and will silently drop goldfive events).
- Ship a server-side "compatibility adapter" optional flag that, if set, accepts the old `task_plan` / `task_status_update` envelopes and silently re-interprets them as equivalent goldfive `PlanSubmitted` / `TaskStarted` / etc. events. This is extra work and probably not worth it — just document the hard cutover.

### R2 — Schema migration for persistent stores

**Risk:** users upgrading harmonograf with a populated sqlite / postgres store need their existing `task_plans` table to gain a `run_id` column and their `tasks.status` column to accept the new `BLOCKED` enum value.

**Mitigation:**
- `SqliteStore.start()` already backfills columns idempotently via `ALTER TABLE`. Add:
  ```sql
  ALTER TABLE task_plans ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
  ```
  in the same style as the existing revision_* backfills.
- Postgres path: add `ALTER TABLE task_plans ADD COLUMN IF NOT EXISTS run_id TEXT NOT NULL DEFAULT ''`. If a CHECK constraint on `tasks.status` exists, drop-and-recreate it to include `BLOCKED`.
- No data loss — existing rows get `run_id=''` which is fine (old plans are pre-run-id).
- Release notes mention: "on first start, the server runs a small schema migration. Back up your sqlite file before upgrading."

### R3 — Frontend proto regen coordination

**Risk:** TypeScript bindings must regenerate before the frontend will type-check against the new goldfive types.

**Mitigation:**
- The Makefile's `proto-ts` target handles this automatically when `frontend/buf.gen.yaml` is present. Verify buf.gen.yaml exists; if it's still the placeholder mentioned in Makefile comments, add it in Phase A.
- Phase A includes a small frontend PR for imports; Phase B and beyond don't touch the frontend further.

### R4 — Module identity mismatch between goldfive-built and harmonograf-local event pbs

**Risk:** goldfive's Runner builds Event messages using its own `goldfive.pb.goldfive.v1.events_pb2`. Harmonograf's generated `telemetry_pb2.TelemetryUp.goldfive_event` field expects harmonograf's local copy of `goldfive.v1.events_pb2.Event`. Proto descriptors match, but Python isinstance checks / `CopyFrom` calls between differently-imported modules can fail.

**Mitigation:**
- The sink's `SerializeToString → ParseFromString` round-trip described in §3 handles this reliably. One serialization per event, negligible overhead.
- Alternative (better): harmonograf does not regenerate goldfive's pbs — instead it reuses `goldfive.pb.goldfive.v1.events_pb2` at runtime. This requires that the protoc invocation for harmonograf's telemetry.proto resolve `goldfive/v1/events.proto` via a proto_path that points at goldfive's installed location. Then the generated harmonograf `telemetry_pb2.py` does `from goldfive.pb.goldfive.v1 import events_pb2 as _goldfive_events_pb2` — same module as goldfive uses. Zero round-trip, zero identity mismatch.
- **Recommendation:** try the zero-copy path first (proto_path pointing at goldfive's install). Fall back to the serialize-roundtrip approach if the generated imports don't resolve cleanly. A test that exercises the sink end-to-end will catch any issue at CI time.

### R5 — Delta payload shape change in `WatchSession`

**Risk:** frontend components subscribed to `SessionUpdate.task_plan` / `SessionUpdate.task_status` deltas get a new proto shape. Rendering may break.

**Mitigation:**
- Goldfive's `Task` and `Plan` fields overlap 1:1 with harmonograf's removed `Task` and `TaskPlan`. Renderer changes are import-only.
- Add a dedicated frontend test: stub a `SessionUpdate` stream with all delta kinds, assert the Gantt renders without errors.
- The `bound_span_id` field changes from `string` (harmonograf) to `optional string` (goldfive). Frontend code that reads `task.bound_span_id === ""` needs to switch to `task.bound_span_id || ""` — already safe given JS coercion, but worth a grep.

### R6 — Loss of harmonograf's `TaskPlan.invocation_span_id` and `planner_agent_id`

**Risk:** goldfive's `Plan` has no equivalent fields. These were harmonograf-specific — they correlate a plan to the span that triggered planning and the agent that produced it.

**Mitigation:**
- `StoredTaskPlan` wraps `goldfive.types.Plan` with these fields at the storage layer. Ingest populates them from the StreamContext / from the emitting agent.
- Wire format: the ControlAck / StreamTelemetry already carries `agent_id`, and a `hgraf.invocation_span_id` attribute on the goldfive Event (optional — use `Plan.summary` field metadata if we want to ship it via goldfive without adding a field) or on the enclosing `TelemetryUp` frame via a side channel. Simpler: server derives `planner_agent_id` from `StreamContext.agent_id` at ingest time, and leaves `invocation_span_id` empty unless the agent explicitly emits a span with a matching attribute.

### R7 — `HarmonografTelemetryPlugin` drift from old `adk.py`

**Risk:** the new telemetry-only plugin is a new codepath, derived from but not identical to the old `adk.py`. Subtle span-emission regressions are possible.

**Mitigation:**
- Port span-emission logic function-by-function, diff-reviewing against the old file.
- Keep the existing `client/tests/test_adk_adapter.py` alive — retarget it at the new plugin so the span-level behavior is re-asserted.
- E2E test that records every span a sample agent emits and diff-checks against a golden trace; a trace mismatch is a regression.

### R8 — Timelines with in-flight agents during a server upgrade

**Risk:** if an agent is mid-run and the server restarts with new code, the new server might not understand in-flight events (or vice versa).

**Mitigation:**
- Transport-level reconnect already handles this. Goldfive Events carry monotonic sequence, so the new server can resume gracefully.
- Document: "upgrade agents and server in the same deploy; do not do rolling upgrades across this boundary."

### R9 — Docs and AGENTS.md pointing to stale APIs

**Risk:** Phase C removes names that appear in docs, demos, and LLM-facing `AGENTS.md`. If an LLM copilot copy-pastes a snippet from stale docs, it fails to import.

**Mitigation:** Phase D is explicitly a docs-only pass. Include a `docs/migration-guide.md` with a cookbook of old → new snippets.

### R10 — Goldfive v0.1 instability

**Risk:** goldfive itself is pre-1.0. Breaking changes upstream cascade into harmonograf.

**Mitigation:**
- Pin `goldfive>=0.0.1,<0.1` initially.
- Vendor the protos (Option A in §2.5) so proto codegen is deterministic even if goldfive's tree layout shifts.
- Track goldfive releases in `docs/milestones.md`; lock step-ups to explicit harmonograf releases.

---

## 10. Summary table: file-level migration matrix

| Path | Verdict | Phase |
|---|---|---|
| `proto/harmonograf/v1/types.proto` | REPLACE | A |
| `proto/harmonograf/v1/telemetry.proto` | REPLACE | A |
| `proto/harmonograf/v1/frontend.proto` | REPLACE (light) | A |
| `proto/harmonograf/v1/control.proto` | KEEP | — |
| `proto/harmonograf/v1/service.proto` | KEEP | — |
| `third_party/goldfive/v1/*.proto` | NEW (vendor) | A |
| `client/harmonograf_client/__init__.py` | REPLACE | A (shrink) then C (final) |
| `client/harmonograf_client/client.py` | REPLACE (shrink) | A |
| `client/harmonograf_client/buffer.py` | REPLACE (light) | A |
| `client/harmonograf_client/transport.py` | REPLACE (light) | A |
| `client/harmonograf_client/heartbeat.py` | KEEP | — |
| `client/harmonograf_client/identity.py` | KEEP | — |
| `client/harmonograf_client/enums.py` | KEEP | — |
| `client/harmonograf_client/planner.py` | DELETE | C |
| `client/harmonograf_client/tools.py` | DELETE | C |
| `client/harmonograf_client/state_protocol.py` | DELETE | C |
| `client/harmonograf_client/adk.py` | DELETE | C (after B extracts telemetry plugin) |
| `client/harmonograf_client/agent.py` | DELETE | C |
| `client/harmonograf_client/runner.py` | DELETE | C |
| `client/harmonograf_client/invariants.py` | DELETE | C |
| `client/harmonograf_client/metrics.py` | DELETE | C |
| `client/harmonograf_client/sink.py` | NEW | B |
| `client/harmonograf_client/telemetry_plugin.py` | NEW | B |
| `server/harmonograf_server/ingest.py` | REPLACE | B |
| `server/harmonograf_server/convert.py` | REPLACE | B |
| `server/harmonograf_server/bus.py` | REPLACE (light) | B |
| `server/harmonograf_server/control_router.py` | KEEP | — |
| `server/harmonograf_server/storage/base.py` | REPLACE | B |
| `server/harmonograf_server/storage/memory.py` | REPLACE (light) | B |
| `server/harmonograf_server/storage/sqlite.py` | REPLACE (light) + MIGRATION | B |
| `server/harmonograf_server/storage/postgres.py` | REPLACE (light) + MIGRATION | B |
| `server/harmonograf_server/storage/__init__.py` | REPLACE (re-exports) | B |
| `server/harmonograf_server/rpc/telemetry.py` | KEEP | — |
| `server/harmonograf_server/rpc/control.py` | KEEP | — |
| `server/harmonograf_server/rpc/frontend.py` | REPLACE (light) | B |
| `server/harmonograf_server/main.py` | KEEP | — |
| `frontend/src/pb/harmonograf/v1/*.ts` | REGEN | A |
| `frontend/src/pb/goldfive/v1/*.ts` | NEW (regen) | A |
| `frontend/src/gantt/*.ts` | KEEP (imports) | A |
| `frontend/src/state/*.ts` | KEEP (imports) | A |
| `tests/e2e/*` | REPLACE | B/C |
| `tests/reference_agents/presentation_agent/agent.py` | REPLACE | C |
| `client/tests/test_buffer.py` | KEEP (imports) | A |
| `client/tests/test_client_api.py` | REPLACE (trim) | A |
| `client/tests/test_client_plan.py` | DELETE | A |
| `client/tests/test_sink.py` | NEW | B |
| `client/tests/test_transport_*.py` | KEEP | — |
| `client/tests/test_planner.py` + ~15 others | DELETE | C |
| `server/tests/test_task_plans.py` | REPLACE | B |
| `server/tests/test_goldfive_ingest.py` | NEW | B |
| `server/tests/test_telemetry_ingest.py` + ingest_extensive | REPLACE (light) | B |
| `server/tests/test_control_*` | KEEP | — |
| `tests/e2e/test_goldfive_roundtrip.py` | NEW | B |
| `docs/*` | UPDATE | D |
| `pyproject.toml` | UPDATE | A (add dep), D (tighten pin) |
| `Makefile` | UPDATE | A |

---

## 11. Sanity checks and open questions

**Q1. Does `SUB_AGENT_INSTRUCTION_APPENDIX` / `augment_instruction` survive?**
Check goldfive. Grep reveals that goldfive's ADK adapter does its own instruction augmentation in `_augment_subtree_with_reporting`. The harmonograf-specific appendix string is embedded there. The `augment_instruction` function in harmonograf's `tools.py` is called from `presentation_agent/agent.py` only, for a somewhat redundant defensive augmentation. Safe to delete in Phase C and adjust the demo agent to not pre-augment — goldfive's adapter will do it anyway.

**Q2. How does harmonograf's ControlAck interact with goldfive's steerer when a user steers?**
As sketched in §6.2, the user's control handler manually invokes `runner.steerer.observe(...)`. An open follow-up: should goldfive expose a first-class "external drift source" API so harmonograf can plug into it more ergonomically? File as a goldfive issue. For v0.1 of the migration, the manual call is fine.

**Q3. Does goldfive's `DefaultSteerer` emit events with `current_agent_id` populated correctly?**
Yes, it copies from the session. Harmonograf's ingest populates the `Agent` row from the StreamContext's own `agent_id`, which may differ from `current_agent_id` inside the goldfive session. Server-side reconciliation: when a goldfive event carries a non-empty `current_agent_id`, ingest should treat it as an alias for the StreamContext's agent (similar to the existing `register_alias` in `control_router.py` for ADK sub-agent names). Low priority.

**Q4. Goldfive's `Goal` has no harmonograf analog. What does the frontend do with it?**
New UI affordance: goal chips in the session header. Out of scope for this migration — just persist and fan out; frontend can ignore initially.

**Q5. Will the drift taxonomy in goldfive (`DRIFT_KIND_*`) need a server-side enum table?**
No — the drift kind is a string/enum carried per-event. The server persists the drift marker (if at all) as a string column on a future `drift_events` table or keeps it ephemeral.

**Q6. Can harmonograf's existing `invocation_span_id` on `TaskPlan` still be populated?**
Yes — wrap it in `StoredTaskPlan`. Goldfive Events don't carry it, but the harmonograf client can emit a span with `hgraf.invocation_span_id` attribute and the server correlates. Not strictly necessary for v0.1.

**Q7. What about agents that talk to harmonograf *without* goldfive?**
Post-migration, harmonograf's Client still allows `emit_span_start` / `emit_span_end` / `emit_span_update` with no requirement to use goldfive. Users who want plain span observability without orchestration get exactly that. Users who want plan-driven orchestration pull in goldfive too. Clean separation.

---

## 12. Rollback plan

Each phase's PR is revertable. If Phase B lands and the frontend regresses:
- Revert Phase B's server ingest → harmonograf stops understanding goldfive events but still has working span ingest.
- Users on the new client library that ships Phase A's `emit_goldfive_event` will silently drop plan updates until the server is rolled forward again.

There is no on-wire fallback to the old `task_plan = 9` / `task_status_update = 10` message numbers because Phase A reserves them. If we need reversibility, the reservation should be temporarily removed — but this complicates the story. Recommendation: ship Phase A + B in a coupled release and do not try to support a mixed-version fleet across that line.

---

## 13. Ownership and sequencing

**Parallelizable work inside each phase:**
- Phase A: proto + Python codegen (1 worker), proto + TS codegen (1 worker).
- Phase B: client sink (1 worker), telemetry plugin (1 worker), server ingest + storage (1 worker), tests (1 worker). The sink and the server-side ingest both need the pb wiring from Phase A; otherwise independent.
- Phase C: deletion waves are independent. One worker per subsystem (client, tests, demo agents).
- Phase D: docs-only, one worker.

**Serialized dependencies:**
- Phase A → Phase B: B imports A's regenerated pb.
- Phase B → Phase C: C removes code that B's tests no longer rely on (the new goldfive-event codepath is proven working before the old path is deleted).
- Phase D runs after C's demo-agent rewrite is in.

---

## 14. Acceptance checklist (global)

After Phase D merges:

- [ ] `uv run pytest` — all tests pass (both harmonograf-local and e2e).
- [ ] `make demo-presentation` — runs a goldfive-orchestrated demo agent, harmonograf server records the plan, the Gantt renders it.
- [ ] `grep -rE "HarmonografAgent|HarmonografRunner|PlannerHelper|_AdkState" client/ server/` returns zero hits.
- [ ] Harmonograf's client library fits in <2000 lines (down from ~10000).
- [ ] `goldfive.Runner` + `HarmonografSink` is documented as the canonical integration path.
- [ ] Proto wire compat: old v0.0.x wire messages 9 / 10 are reserved; new message 11 is live.
- [ ] Schema migration tested on a populated pre-migration sqlite file.
- [ ] CHANGELOG entry written.
- [ ] ADR-xxx filed documenting decision D7.

---

## Critical Files for Implementation

- /home/sunil/git/harmonograf/proto/harmonograf/v1/telemetry.proto
- /home/sunil/git/harmonograf/client/harmonograf_client/sink.py (new)
- /home/sunil/git/harmonograf/server/harmonograf_server/ingest.py
- /home/sunil/git/harmonograf/server/harmonograf_server/storage/base.py
- /home/sunil/git/harmonograf/client/harmonograf_client/client.py