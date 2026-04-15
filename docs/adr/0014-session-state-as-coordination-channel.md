# ADR 0014 — `session.state` is the coordination channel

## Status

Accepted.

## Context

Harmonograf needs a bidirectional coordination channel between the
orchestrator (harmonograf code running inside the client library) and the
agent (user-written ADK agent, possibly a sub-agent, possibly running in
its own model turn). The channel has to convey:

- **Downstream:** which task is currently active, what the plan looks like,
  what tools are available for reporting, what other tasks exist and their
  statuses, what results previously-completed tasks produced.
- **Upstream:** task progress hints, final outcomes, free-form notes, and
  explicit divergence flags.

The options, given that harmonograf must operate inside ADK's sandbox
without monkey-patching (ADR 0003):

1. **Inject context into the system prompt each turn.** Works, but the
   system prompt is one-way (prompt → model, not model → prompt), so the
   upstream channel would still need a different mechanism, and we'd end
   up with two channels.
2. **Use tool calls for both directions.** Reporting tools cover the
   upstream path but can't replace "give the agent the current task
   context" — the agent has to *already have* that context before it
   decides whether to call a tool.
3. **Use `session.state`** — ADK's shared mutable dict that persists
   across turns within a session. The orchestrator writes before each
   model call (`before_model_callback`), and agents write via ADK's
   `state_delta` events which harmonograf reads in `on_event_callback`
   and `after_model_callback`.

## Decision

Use **ADK's `session.state`** with a dedicated `harmonograf.` key prefix
and a schema enforced by `client/harmonograf_client/state_protocol.py`.

Harmonograf writes (downstream):

| Key | Meaning |
|-----|---------|
| `harmonograf.current_task_id` | Active task id |
| `harmonograf.current_task_title` | Human-readable title |
| `harmonograf.current_task_description` | Full description |
| `harmonograf.current_task_assignee` | Sub-agent name |
| `harmonograf.plan_id` | Plan the task belongs to |
| `harmonograf.plan_summary` | One-line plan goal |
| `harmonograf.available_tasks` | List of `{id, title, assignee, status, deps}` |
| `harmonograf.completed_task_results` | `task_id -> summary` for completed tasks |
| `harmonograf.tools_available` | Reporting tool names wired up |

Agent writes (upstream), via `state_delta` events or reporting-tool
interception:

| Key | Meaning |
|-----|---------|
| `harmonograf.task_progress` | `task_id -> float` in [0,1] |
| `harmonograf.task_outcome` | `task_id -> summary` for terminal outcomes |
| `harmonograf.agent_note` | Free-form latest note |
| `harmonograf.divergence_flag` | True iff the whole plan is stale |

The schema, readers, writers, and a `extract_agent_writes` diff helper
live in `state_protocol.py`. All keys are under `HARMONOGRAF_PREFIX` so
the diff helper can pull out just the harmonograf-owned writes without
touching user-owned state.

**Bidirectional flow over one ADK dict** — harmonograf writes the active
task before each model call; the agent writes back via `state_delta`, and
`extract_agent_writes` filters for the `harmonograf.*` prefix.

```mermaid
sequenceDiagram
    autonumber
    participant Orch as Harmonograf orchestrator
    participant State as session.state (ADK)
    participant Agent as Sub-agent (LLM turn)
    Orch->>State: write harmonograf.current_task_*<br/>plan_id, available_tasks, completed_task_results
    State-->>Agent: rendered into prompt<br/>(before_model_callback)
    Agent->>State: state_delta { harmonograf.task_progress, .agent_note, .divergence_flag }
    State-->>Orch: extract_agent_writes(pre, post)<br/>filtered by HARMONOGRAF_PREFIX
    Note over Orch,State: schema lives in state_protocol.py;<br/>readers fall back to typed defaults
```

## Consequences

**Good.**
- **Bidirectional with one mechanism.** We did not invent a channel; we
  reused ADK's. Agents read state naturally in their prompts, and
  write via a mechanism ADK already supports.
- **Persistent across turns.** Because `session.state` survives turn
  boundaries, an agent that crashes mid-turn and resumes picks up the
  same task context without harmonograf re-seeding.
- **Schema is explicit.** The `state_protocol.py` module is pure data
  with no adk.py dependency. The schema lives in one place; readers
  never raise on missing or malformed keys (they return typed
  defaults).
- **Diffable.** `extract_agent_writes` compares pre- and post-turn state
  and returns only the `harmonograf.*` keys the agent touched. This is
  what makes `after_model_callback` able to pick up agent-side writes
  as a belt-and-suspenders path alongside reporting tools.

**Bad.**
- **ADK-specific.** `session.state` is an ADK concept. A future Strands
  or OpenAI Agents adapter will need to map these keys onto whatever
  equivalent (or surrogate) that framework offers, and some may not
  have one at all.
- **Collision-prone.** We trust the `harmonograf.` prefix to keep our
  keys out of user-owned state. If a user writes to `harmonograf.*`
  keys directly (accidentally or otherwise), behavior is undefined.
  The readers are defensive but we cannot stop a user from overwriting
  `harmonograf.current_task_id`.
- **Not strongly typed on the wire.** `session.state` is a plain dict;
  values are whatever the writer put there. The `state_protocol.py`
  readers do type coercion and fall back to defaults, but a buggy
  agent write can ship garbage into state and the reader's only
  response is "fall back to default."
- **Size is unchecked.** `available_tasks` can grow proportional to the
  plan size. For very large plans this bloats every model call's
  prompt. We mitigate by capping the list and letting the agent ask
  for more via an explicit tool call, but the cap is a tuning knob,
  not an invariant.
- **Prompt injection vector.** Everything harmonograf writes into state
  ends up rendered into the agent's prompt somehow (either because
  the agent is instructed to read state, or because ADK surfaces it).
  Untrusted task descriptions end up in LLM prompts. Treat state
  content as untrusted input.

The channel exists because ADK gave it to us. If ADK didn't have
`session.state`, we would have invented something clumsier. This
decision is a cheerful use of a feature that happens to exist.
