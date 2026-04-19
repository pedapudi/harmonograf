# ADR 0019 — `HarmonografAgent` and `HarmonografAdkPlugin` are separate

> **SUPERSEDED (goldfive migration).** `HarmonografAgent` and the old
> `HarmonografAdkPlugin` were deleted in the goldfive migration. The
> modern split is `goldfive.adapters.adk.ADKAdapter` (orchestration —
> owned by goldfive) vs. `HarmonografTelemetryPlugin` (observability —
> owned by harmonograf), composed in the ADK App. The principle — two
> extension points, one App — still holds; the code is just two repos
> wide. See [../goldfive-integration.md](../goldfive-integration.md).

## Status

Superseded by the goldfive migration.

## Context

Harmonograf's ADK integration has two logically separable
responsibilities:

1. **Orchestration.** Wrap a user-written coordinator agent and drive
   the plan: read the plan at run start, decide whether to run it
   sequential / parallel / delegated ([ADR 0012](0012-three-orchestration-modes.md)), inject reporting
   tools, enforce the task state machine, fire refines on drift.
2. **Telemetry and state tracking.** Translate every ADK lifecycle
   callback into a harmonograf span, maintain `_AdkState` (task
   state, plan revisions, history), intercept reporting-tool calls,
   persist to the client transport, and thread session-state
   protocol reads/writes around every model call.

Both need to live inside ADK. Both need to see every callback. The
question is whether they are one class or two.

Option A (one class): merge the orchestrator and the plugin into a
single `HarmonografAgent` that subclasses `BaseAgent` and also
implements the plugin hooks. Simpler topology, one thing to install.

Option B (two classes): `HarmonografAgent` is the orchestrator
(subclass of `BaseAgent`) and `HarmonografAdkPlugin` is the
telemetry + state plugin (subclass of ADK's `BasePlugin`). The App
is expected to include both.

## Decision

**Two classes, composed by the App.** From the class docstring of
`HarmonografAgent` at `client/harmonograf_client/agent.py`:

> Plan state is owned by the HarmonografAdkPlugin (see
> make_adk_plugin). HarmonografAgent discovers the plugin via
> ctx.plugin_manager at invocation time, so the two can be composed
> independently: an App is expected to include *both* the
> HarmonografAgent as root and the HarmonografAdkPlugin in its plugin
> list. The plugin provides telemetry + plan-state tracking; the
> agent provides the orchestration loop.

The split falls out of how ADK structures its extension points:

- `BaseAgent._run_async_impl` is where you hook to control the
  *execution shape* of an agent run. This is where orchestration
  lives.
- `BasePlugin` hooks (`before_model_callback`, `before_tool_callback`,
  `on_event_callback`, etc.) are where you hook to observe and
  augment the *events* of an agent run. This is where telemetry and
  state tracking live.

Merging them would mean subclassing `BaseAgent` and *also*
implementing `BasePlugin` in the same class, which is possible but
couples two ADK extension surfaces that ADK treats as
independent-by-design. Specifically: a user who wants observability
without orchestration (delegated mode, or just watching a
third-party agent run) wants the plugin without the agent. Forcing
them to take both is the same failure as forcing a single
orchestration mode ([ADR 0012](0012-three-orchestration-modes.md)).

**Two ADK extension points, one App** — `HarmonografAgent` subclasses
`BaseAgent` (orchestration), `HarmonografAdkPlugin` subclasses `BasePlugin`
(telemetry + state). The agent discovers the plugin at runtime via
`ctx.plugin_manager`.

```mermaid
flowchart TB
    App[ADK App] --> Root[HarmonografAgent<br/>(BaseAgent subclass)<br/>orchestration loop]
    App --> Plug[HarmonografAdkPlugin<br/>(BasePlugin subclass)<br/>telemetry + _AdkState]
    Root -. ctx.plugin_manager<br/>discovers .-> Plug
    Plug --> State[_AdkState<br/>plan + task transitions]
    Root --> State
    Plug --> Wire[client transport<br/>spans / heartbeats / acks]
    Note["attach_adk() helper<br/>installs both for InMemoryRunner"]:::note
    App --- Note

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    classDef note fill:#fef3c7,stroke:#b45309,color:#000
    class Root,Plug good
```

## Consequences

**Good.**
- **Observability without orchestration is possible.** Install the
  plugin, point the App at an existing agent tree, done. No
  requirement to wrap the root in `HarmonografAgent`.
- **Orchestration without telemetry is also possible**, though
  uncommon — a test harness that wants to exercise the walker
  without the wire protocol can instantiate `HarmonografAgent`
  against an in-memory client.
- **Each class has one responsibility.** `agent.py` is about plan
  execution; `adk.py` is about callback translation and state.
  Bugs localize.
- **Independent evolution.** We can change the orchestration
  strategy (add a fourth mode, change the walker algorithm)
  without touching telemetry, and we can add new spans or new
  state-protocol keys without touching the walker.

**Bad.**
- **Setup is two steps, not one.** Users have to include the
  `HarmonografAgent` as the root agent *and* the plugin in the App's
  plugin list. Forgetting either half produces a confusing failure:
  no orchestration if the agent is missing, no spans and no state
  if the plugin is missing. We partially mitigate with the
  `attach_adk` helper that does both for `InMemoryRunner`, but the
  raw API has the two-step friction.
- **Cross-class wiring.** `HarmonografAgent._run_async_impl` looks
  up the plugin via `ctx.plugin_manager` at invocation time. If the
  plugin is not installed, the agent logs and runs in a
  reduced-functionality mode. This discovery dance is runtime, not
  compile-time; a test that forgets to install the plugin gets a
  warning log rather than a typing error.
- **Shared state lives in the plugin.** `_AdkState` is owned by the
  plugin; the agent reads from it. This means the orchestration loop
  is partly mutating state that another class owns, which is the
  kind of split-brain that makes refactoring tricky. The plugin is
  designed to expose a narrow read/write interface, but the discipline
  is convention, not compiler-enforced.
- **Two docstrings, two mental models.** A new contributor has to
  learn "what is the agent" and "what is the plugin" and "how do
  they find each other" before they can change anything. The
  single-class alternative would be simpler to onboard to even if it
  were architecturally worse.

The split is worth the friction because it matches ADK's own
extension-point split. A single-class design would fight the
framework, and the framework is the thing we are trying not to fight
([ADR 0003](0003-adk-first.md)).

## Implemented in

- [Design 02 — Client library](../design/02-client-library.md)
- [Design 12 — Client library + ADK integration](../design/12-client-library-and-adk.md)
