# Client library internals

The client library (`client/harmonograf_client/`) is the biggest and
trickiest component in the repo. `adk.py` alone is close to 5,800 lines, and
the plan-execution state machine interacts with ADK internals in ways that
are easy to get wrong. Read this chapter before touching anything under
`client/`.

## Anatomy

| File | Lines | Purpose |
|---|---|---|
| `adk.py` | ~5,779 | ADK plugin adapter. Hosts `AdkAdapter`, `_AdkState`, callback dispatch, reporting-tool interception, drift detection, the rigid DAG walker. Everything plan-related that touches ADK. |
| `agent.py` | ~1,919 | `HarmonografAgent`: `BaseAgent` subclass wrapping a user agent. Owns the orchestration loop (sequential/parallel/delegated). |
| `state_protocol.py` | ~385 | Schema for `session.state` `harmonograf.*` keys + defensive readers/writers + diff helper. |
| `planner.py` | ~547 | `Plan`, `Task`, `TaskEdge` dataclasses + `PlannerHelper` interface + `PassthroughPlanner` + `LLMPlanner`. |
| `tools.py` | ~215 | Reporting tool definitions (`report_task_*`), registry, `augment_instruction` helper. |
| `invariants.py` | ~426 | `InvariantChecker` — stateful validator for the plan state machine. |
| `metrics.py` | ~55 | `ProtocolMetrics` counters. |
| `buffer.py` | ~237 | `EventRingBuffer`, `PayloadBuffer`, `SpanEnvelope`. |
| `transport.py` | ~698 | gRPC transport, reconnect, Hello/Welcome handshake, resume tokens. |
| `client.py` | ~493 | `Client` — non-blocking handle used by everything else. |
| `heartbeat.py` | ~86 | `Heartbeat` dataclass (emitted periodically by the transport). |
| `identity.py` | ~125 | `AgentIdentity` — persisted agent ID. |
| `runner.py` | ~217 | `HarmonografRunner` factory (convenience over raw `Runner`). |
| `enums.py` | ~41 | `SpanKind`, `SpanStatus`, `Capability` — mirrors of proto enums. |

Public API surface lives in `client/harmonograf_client/__init__.py:29-49`:
`AdkAdapter`, `Capability`, `Client`, `ControlAckSpec`, `HarmonografAgent`,
`HarmonografRunner`, `LLMPlanner`, `PassthroughPlanner`, `Plan`,
`PlannerHelper`, `SpanKind`, `SpanStatus`, `Task`, `TaskEdge`, `attach_adk`,
`make_adk_plugin`, `make_default_adk_call_llm`, `make_harmonograf_agent`,
`make_harmonograf_runner`. Anything not in that list is internal — feel free
to rename or restructure it.

## Plugin vs agent: why both?

`HarmonografAgent` (in `agent.py`) and `AdkAdapter` (in `adk.py`) look like
they overlap, but they're layered. Understand this before you get lost.

| Layer | What it does | When to use |
|---|---|---|
| `AdkAdapter` (plugin) | Installs ADK lifecycle callbacks on a `Runner`. Emits spans, updates `_AdkState`, intercepts reporting tools. Does *not* wrap the agent tree. | When you want harmonograf *observation* without changing how the agent is structured. Rarely used directly — `attach_adk()` / `make_adk_plugin()` are the usual entry points. |
| `HarmonografAgent` (wrapper) | A `BaseAgent` that wraps the user's sub-agent tree and runs the orchestration loop. Injects reporting tools into every sub-agent. Drives the plan walker in parallel mode. | The default. Built by `make_harmonograf_agent()` and used inside `HarmonografRunner`. |

The plugin is always there — `HarmonografAgent` also installs the plugin on the
surrounding `Runner` so both layers cooperate. The wrapper layer is the one
that *enforces* the plan; the plugin layer is the one that *observes* it.

A rough rule: if it touches `session.state`, the inner agent tree, or the
orchestration loop, it's in `agent.py`. If it touches ADK callbacks, span
emission, or drift detection, it's in `adk.py`. There's overlap — `_AdkState`
lives in `adk.py:1591` and is used from both places — but this is the
general split.

## The plan-execution state machine

### Why spans do not drive state

In earlier iterations the plan state was inferred from span lifecycles — a
`TOOL_CALL` span ending with status `COMPLETED` would mark a task as done.
That design failed under real LLM behavior: models skipped tools, merged
tasks, described completion in prose, hallucinated span boundaries, and the
inference logic grew into a pile of heuristics.

The current design splits the two concerns cleanly:

- **Spans are telemetry only.** Every ADK callback emits a span for
  observability, and that's all they do.
- **Task state is driven by three explicit channels** (below). No channel
  touches spans.

This separation is the most important design decision in the client library.
Do not re-couple span lifecycle to task state. Any PR that tries to "simplify"
by inferring task state from spans should be rejected.

### The three channels

Plan state moves through three coordinated paths:

#### 1. `session.state` (ADK's shared mutable dict)

Keys are defined in `client/harmonograf_client/state_protocol.py`. Prefix
constant `HARMONOGRAF_PREFIX = "harmonograf."` at line 86.

| Key | Direction | Written by | Read by |
|---|---|---|---|
| `harmonograf.current_task_id` | harmonograf → agent | `write_current_task()` | Agent instruction template |
| `harmonograf.current_task_title` | harmonograf → agent | `write_current_task()` | Agent instruction template |
| `harmonograf.current_task_description` | harmonograf → agent | `write_current_task()` | Agent instruction template |
| `harmonograf.current_task_assignee` | harmonograf → agent | `write_current_task()` | Agent instruction template |
| `harmonograf.plan_id` | harmonograf → agent | `write_plan_context()` | Agent (optional) |
| `harmonograf.plan_summary` | harmonograf → agent | `write_plan_context()` | Agent instruction template |
| `harmonograf.available_tasks` | harmonograf → agent | `write_plan_context()` | Agent instruction template |
| `harmonograf.completed_task_results` | harmonograf → agent | `write_plan_context()` | Agent instruction template |
| `harmonograf.tools_available` | harmonograf → agent | `write_tools_available()` | Agent (optional) |
| `harmonograf.task_progress` | agent → harmonograf | Agent (via reporting tool or direct state delta) | `extract_agent_writes()` |
| `harmonograf.task_outcome` | agent → harmonograf | Agent | `extract_agent_writes()` |
| `harmonograf.agent_note` | agent → harmonograf | Agent | `extract_agent_writes()` |
| `harmonograf.divergence_flag` | agent → harmonograf | Agent | `extract_agent_writes()` |

All keys are defined as `KEY_*` module constants in `state_protocol.py`
(lines 88-99+). Import those constants; do not hardcode the strings.

The diff helper `extract_agent_writes()` compares two state snapshots and
returns the subset of `harmonograf.*` keys the agent wrote. Harmonograf calls
it after every model turn to capture any direct state-delta writes the model
made.

**Pitfall:** `session.state` is a live dict. Mutations are visible to the
agent *and* to harmonograf. If you forget to clear `current_task_id` between
tasks in parallel mode, the agent will think it's still on the old one.
`clear_current_task()` exists for this reason — use it.

#### 2. Reporting tools

Agents call these functions like any other tool. Harmonograf intercepts them
in `before_tool_callback` and applies the state transition directly; the
tool bodies themselves return only `{"acknowledged": true}`.

Defined in `client/harmonograf_client/tools.py`:

| Tool | Line | Transition |
|---|---|---|
| `report_task_started` | 77 | `PENDING` → `IN_PROGRESS` |
| `report_task_progress` | 89 | updates progress %; stays in `IN_PROGRESS` |
| `report_task_completed` | 102 | `IN_PROGRESS` → `COMPLETED`; captures outcome |
| `report_task_failed` | 116 | `IN_PROGRESS` → `FAILED` |
| `report_task_blocked` | 129 | `IN_PROGRESS` → `BLOCKED` |
| `report_new_work_discovered` | 142 | triggers replan (drift kind: `NEW_WORK_DISCOVERED`) |
| `report_plan_divergence` | 158 | triggers replan (drift kind: `AGENT_REPORTED_DIVERGENCE`) |

Registry constants: `REPORTING_TOOL_FUNCTIONS` (line 168) — the tuple of
function objects — and `REPORTING_TOOL_NAMES` (line 178) — the tuple of
string names used by the interception code.

`build_reporting_function_tools()` (line 183) wraps each function in an ADK
`FunctionTool` so it can be attached to an agent. `augment_instruction()`
(line 205) appends a standard instruction appendix so the LLM knows when to
call which tool.

`HarmonografAgent._augment_subtree_with_reporting()` (`agent.py:100`) walks
the agent tree and injects the reporting tools into every sub-agent's toolset.
This happens at construction time so you don't have to remember.

Interception happens in `AdkAdapter.before_tool_callback`
(`adk.py:1299`) — it checks whether the tool name is in
`REPORTING_TOOL_NAMES` and, if so, applies the transition on `_AdkState`
before returning the stub ack.

**Pitfall:** agents sometimes call reporting tools with stale task IDs
(e.g., because the model repeated itself). `_AdkState` uses the
`InvariantChecker` to reject illegal transitions; the tool still returns
success to the agent, but the state does not advance. Grep for
`_STAMP_MISMATCH_THRESHOLD` (default 3, `adk.py:373`) — if an agent triggers
three stamp mismatches in a row, harmonograf fires a replan with drift kind
`DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES`.

#### 3. ADK callback inspection

Belt-and-suspenders for models that describe their work in prose instead of
calling tools. These live in `AdkAdapter` in `adk.py`:

| Callback | Line | What it inspects |
|---|---|---|
| `after_model_callback` | 1227 | Parses response content for structured signals: `function_calls`, markers like "Task complete:", and `state_delta` writes. `ResponseSignals` dataclass at `adk.py:474`. |
| `before_tool_callback` | 1299 | Starts `TOOL_CALL` span, intercepts reporting tools, updates `_AdkState`. |
| `on_event_callback` | 1391 | Watches for `transfer`, `escalate`, and `state_delta` events. Emits `TRANSFER` span. Attributes state deltas. |

### `_AdkState`

The centerpiece: `_AdkState` at `client/harmonograf_client/adk.py:1591`. It
owns the per-session plan state machine. All three channels above funnel into
it. Key facts:

- **Monotonic.** `PENDING → IN_PROGRESS → {COMPLETED, FAILED, BLOCKED}` with no
  going back. Enforced by `InvariantChecker._check_monotonic`
  (`invariants.py:124`).
- **Walker-owned in parallel mode.** In parallel mode, the rigid DAG walker
  is the only caller that advances tasks forward. Reporting tools still fire
  but their transitions are applied through the walker's path for
  consistency.
- **Callback-driven in sequential and delegated modes.** The coordinator LLM
  (sequential) or the single inner agent (delegated) drives state via
  callbacks.

`_AdkState` lives on the plugin instance so it persists across a single
`Runner.run_async` invocation. It is reset per invocation — don't rely on
state surviving across runs.

## The three orchestration modes

Selected by constructor flags on `HarmonografAgent` (`agent.py:207`):

```python
HarmonografAgent(
    agent=my_llm_agent,
    orchestrator_mode=True,       # False → delegated
    parallel_mode=False,           # True → parallel; requires orchestrator_mode
    planner=my_planner,            # optional; defaults to PassthroughPlanner
)
```

### Sequential mode (default)

`orchestrator_mode=True, parallel_mode=False`.

The whole plan is fed as one user turn to the coordinator LLM. The LLM
decides execution order and calls sub-agents via `AgentTool` as it sees fit.
Per-task lifecycle comes from reporting tools — the LLM is instructed to
call `report_task_started` / `report_task_completed` at task boundaries.

This is the default because it works with any ADK agent tree out of the box
and requires no knowledge of plan structure inside the sub-agents. Use it
unless you specifically need parallelism or delegation.

### Parallel mode

`orchestrator_mode=True, parallel_mode=True`.

A rigid DAG batch walker drives sub-agents directly per task, respecting
plan edges as dependencies. The walker:

1. Computes topological stages via `Plan.topological_stages()`
   (`planner.py:80`).
2. For each stage, forces the `task_id` on the sub-agent via
   `_forced_task_id_var` (`ContextVar` at `adk.py:320-321`).
3. Invokes the sub-agent as a self-contained run.
4. Collects the outcome and advances `_AdkState`.

The `ContextVar` is the key to binding spans to tasks: any callback in the
sub-agent's run reads `_forced_task_id_var.get()` and stamps spans with it.
Without the ContextVar, you can't tell which of N parallel sub-agents
produced a given span.

Safety caps to know about:

| Constant | Value | File |
|---|---|---|
| `_ORCHESTRATOR_WALKER_SAFETY_CAP` | `20` | `agent.py:69` |
| `_DEFAULT_MAX_PLAN_REINVOCATIONS` | `3` | `agent.py:67` |
| `_PARTIAL_REINVOCATION_BUDGET` | `3` | `agent.py:70-74` |

The safety cap bounds walker iterations in case of a pathological plan.
Re-invocation budgets bound how many times harmonograf will re-run the
coordinator LLM after a replan.

### Delegated mode

`orchestrator_mode=False`.

A single delegation: harmonograf hands the whole plan to the inner agent and
lets it run. The `on_event_callback` observer
(`adk.py:1391`) scans for drift after the fact. Use this when the inner
agent is itself an orchestrator and harmonograf should just watch.

### Picking a mode

| You want | Use |
|---|---|
| Standard ADK agent tree, harmonograf observes but LLM orchestrates | Sequential (default) |
| Plan with known dependencies; you want actual parallelism | Parallel |
| The inner agent is itself a multi-agent orchestrator | Delegated |

If in doubt, start with sequential — it has the fewest ways to surprise you.

## Dynamic replan and drift taxonomy

The drift kinds drive replans. Full list at
`client/harmonograf_client/adk.py:352-368`:

| Constant | Meaning |
|---|---|
| `DRIFT_KIND_LLM_REFUSED` | Model declined to act on the plan. |
| `DRIFT_KIND_LLM_MERGED_TASKS` | Model described multiple tasks as one. |
| `DRIFT_KIND_LLM_SPLIT_TASK` | Model split one task into several. |
| `DRIFT_KIND_LLM_REORDERED_WORK` | Model executed tasks out of planned order. |
| `DRIFT_KIND_CONTEXT_PRESSURE` | Context window is nearly full (see `Heartbeat.context_window_tokens`). |
| `DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES` | ≥3 illegal stamps in a row (see `_STAMP_MISMATCH_THRESHOLD` at `adk.py:373`). |
| `DRIFT_KIND_USER_STEER` | Human sent a steering annotation. |
| `DRIFT_KIND_USER_CANCEL` | Human cancelled the plan. |
| `DRIFT_KIND_TOOL_ERROR` | A tool raised. |
| `DRIFT_KIND_AGENT_ESCALATED` | ADK `escalate` event. |
| `DRIFT_KIND_AGENT_REPORTED_DIVERGENCE` | Agent called `report_plan_divergence`. |
| `DRIFT_KIND_UNEXPECTED_TRANSFER` | Agent transferred outside the plan. |
| `DRIFT_KIND_EXTERNAL_SIGNAL` | Server pushed a refine via control event. |
| `DRIFT_KIND_COORDINATOR_EARLY_STOP` | Coordinator LLM bailed before the plan completed. |

Each drift signal is wrapped in a `DriftReason` dataclass
(`adk.py:326`) with kind, severity, recoverable flag, and detail string.

The replan path is deferential: when drift fires, harmonograf calls
`PlannerHelper.refine()` (`planner.py:138`) with the current plan plus the
drift reason. The planner decides whether to change anything. A throttle
prevents replan storms: `_DRIFT_REFINE_THROTTLE_SECONDS = 2.0` at
`adk.py:378`.

The new plan is upserted through the server's `TaskRegistry`, which
re-computes the diff via `computePlanDiff` (`frontend/src/gantt/index.ts:130`)
and the UI banner lights up with the changes.

**Adding a new drift kind:** edit the constant list in `adk.py:352-368`,
update the `_classify_drift()` helper (grep for it), add a corresponding
entry in `frontend/src/gantt/driftKinds.ts` for the UI color/icon, and add
a test under `client/tests/test_drift_taxonomy.py`. Proto has no drift enum
— drift reasons ride as span attributes, not as their own message type.

## The invariant checker

`InvariantChecker` at `client/harmonograf_client/invariants.py:78` runs
in-process and catches plan-state violations before they ship to the server.
It is stateful — it remembers transition history per plan.

Checks it enforces (method names on `InvariantChecker`):

| Check | Purpose |
|---|---|
| `_check_monotonic` | No going backwards through task states. |
| `_check_dependency_consistency` | Task graph is a DAG; no cycles; edge endpoints exist. |
| `_check_assignee_validity` | `assignee_agent_id` must be in the registered agents. |
| `_check_plan_id_uniqueness` | No duplicate plan IDs within a session. |
| `_check_forced_task` | In parallel mode, forced `task_id` must correspond to a real task. |
| `_check_task_results_keys` | `task_outcome` keys must match real task IDs. |
| `_check_revision_history_monotone` | Plan `revision_index` strictly increasing across refines. |
| `_check_span_bindings` | Spans with `hgraf.task_id` attribute must bind to real tasks. |

Entry points:

- `check_plan_state()` (`invariants.py:369`) — free function; uses the
  module-level default checker. Use this in tests and ad-hoc validation.
- `enforce()` (`invariants.py:402`) — context manager / decorator that wraps a
  block of plan mutations and raises `InvariantViolation` on failure.

`InvariantViolation` dataclass at `invariants.py:59`: `rule`, `severity`,
`detail`.

**When a violation fires in production** it is logged and counted on
`ProtocolMetrics.invariant_violations`. It does **not** crash the agent
process — an invariant bug is a harmonograf bug, not an agent bug. See
`debugging.md` for how to surface violation logs.

## Metrics

`ProtocolMetrics` at `client/harmonograf_client/metrics.py:17` is a dataclass
of counters updated on the ADK callback hot path. Shape:

| Field | Type | Incremented when |
|---|---|---|
| `callbacks_fired` | `dict[str, int]` | Any ADK callback runs |
| `task_transitions` | `dict[str, int]` | `_AdkState` applies a transition |
| `refine_fires` | `dict[str, int]` | `PlannerHelper.refine()` is invoked (key = drift kind) |
| `state_reads` / `state_writes` | `int` | `state_protocol` helpers read or write `harmonograf.*` keys |
| `reporting_tools_invoked` | `dict[str, int]` | A reporting tool is intercepted |
| `invariant_violations` | `int` | `InvariantChecker` raises |
| `walker_iterations` | `int` | Parallel walker completes a stage |

`format_protocol_metrics()` (`metrics.py:36`) dumps a human-readable string,
handy from a REPL or debugger. Metrics are also shipped as span attributes
on the invocation span so the UI can display them.

## Buffering and transport

### `EventRingBuffer`

`client/harmonograf_client/buffer.py:83`. Despite the name it's a
deque-backed bounded buffer, not a classic fixed-array ring. The tiered
drop policy is the interesting part:

1. **Critical spans never drop.** `INVOCATION`, `LLM_CALL`, and `TOOL_CALL`
   span starts and ends are marked critical and occupy a reserved slice of
   the buffer.
2. **Non-critical spans drop first** under pressure (`USER_MESSAGE`,
   `AGENT_MESSAGE`, `CUSTOM`, etc.).
3. **Payloads drop before spans.** `PayloadBuffer` (line 173) has its own
   bounded pool and eviction counter. Evicted payloads are reported via
   heartbeat so the server can backfill or mark them as lost.

The buffer reports `BufferStats` (`buffer.py:70`): `buffered_events`,
`dropped_events`, `buffered_payload_bytes`, `payloads_evicted`. Those fields
are included in every heartbeat.

### `Transport`

`client/harmonograf_client/transport.py:88`. gRPC bidi stream with
Hello/Welcome handshake, exponential-backoff reconnect
(`RECONNECT_INITIAL_MS`, `RECONNECT_MAX_MS`), and resume tokens.

Flow:

1. On start: open `StreamTelemetry`, send `Hello` (includes `resume_token`
   if the agent restarted).
2. Server replies `Welcome` with `assigned_session_id` and `assigned_stream_id`.
3. Transport drains the event buffer and sends `TelemetryUp` frames. Each
   acknowledged span updates the resume token.
4. On disconnect: backoff (`min(backoff * 2, RECONNECT_MAX_MS)` — see
   `transport.py:306`), then retry. Unacked events stay in the buffer and
   are replayed on reconnect.
5. Separate `SubscribeControl` server-stream delivers control events from
   the server. Acks ride back on `TelemetryUp.control_ack`.

Heartbeats are sent every `heartbeat_interval_s` (`TransportConfig` at
`transport.py:65`). A heartbeat carries `BufferStats`, `progress_counter`,
`current_activity`, and context-window measurements — the server's
stuckness detector reads `progress_counter` to decide if an agent has frozen.

**Pitfall:** if you add a new field to `Hello`, regenerate proto stubs
*and* teach the server's Hello handler in `server/harmonograf_server/ingest.py`
to respect it, or the field is silently ignored.

## `HarmonografRunner`

`client/harmonograf_client/runner.py:41`. Convenience factory for the common
case:

```python
runner = make_harmonograf_runner(agent=my_agent, server_addr="127.0.0.1:7531")
await runner.run_async(user_id="alice", session_id=..., new_message=...)
```

It constructs `HarmonografAgent`, wraps it in ADK's `InMemoryRunner`, and
installs the harmonograf plugin. Two modes exist (see `runner.py:46-58`):

| Mode | When to use |
|---|---|
| Agent mode (default; pass `agent=`) | `HarmonografAgent` + `InMemoryRunner` + plugin auto-wired. |
| Composition mode (pass `runner=`) | Adopt a pre-built runner; plugin attached but no re-invocation loop. Used by tests and by code that wants a custom Runner subclass. |

## Writing client-side code: pitfalls

1. **Don't touch `session.state` outside `state_protocol.py`.** The
   defensive readers/writers exist so a missing or wrong-typed value can't
   crash the agent. Adding a new key means adding a new helper.
2. **Don't infer task state from spans.** Ever. See
   "Why spans do not drive state" above.
3. **Respect the `ContextVar`.** In parallel mode, every callback must read
   `_forced_task_id_var.get()` to stamp spans. Any code path that forgets it
   will silently produce unbindable spans.
4. **Never call `await` inside a sync ADK callback.** ADK's callback
   protocol is sync-first. If you need async work, schedule it on the event
   loop or queue it. `adk.py` has several helpers for this — look at how
   existing callbacks do it and copy the pattern.
5. **Reporting tool stubs must return `{"acknowledged": True}`** and nothing
   else. If you add state to the return value, the LLM will start riffing
   on it and you'll lose the invariant that these tools are side-effect-free
   from the model's perspective.
6. **Drift replans are throttled.** Don't add a drift fire that you expect
   to go off dozens of times per second; the throttle will eat most of them
   and you'll get confusing test failures. Fire once per logical event.
7. **Don't swap `PassthroughPlanner` for `LLMPlanner` without checking the
   prompt.** `LLMPlanner` expects a specific JSON shape back; if your model
   returns something else, you get silent planner failures.

## Testing the client library

See [`testing.md`](testing.md) for the full testing strategy. The
client-side test files that give the best starter examples:

| Test | What it demonstrates |
|---|---|
| `client/tests/test_orchestration_modes.py` | How to drive all three modes with a scripted `FakeLlm`. `FakeClient` duck-types `Client` — copy this when you need to test transport-free. |
| `client/tests/test_dynamic_plans_real_adk.py` | Dynamic replan with a real ADK runner. Heavier harness; requires `google-adk`. |
| `client/tests/test_reporting_tools.py` | Reporting-tool interception and state machine transitions. |
| `client/tests/test_invariants.py` | Every invariant check in isolation. |
| `client/tests/test_drift_taxonomy.py` | Drift classification edge cases. |
| `client/tests/test_state_protocol.py` | `session.state` schema and diff helper. |
| `client/tests/test_walker_completion_timing.py` | Parallel walker dispatch correctness. |
| `client/tests/test_callback_perf.py` | Overhead budget for the callback hot path. |

## Next

[`server.md`](server.md) walks through the other end of the gRPC stream.
