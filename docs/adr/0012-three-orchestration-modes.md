# ADR 0012 — Three orchestration modes (sequential, parallel, delegated)

## Status

Accepted.

## Context

`HarmonografAgent` is the ADK parent agent that wraps a user-written coordinator.
Its job is to turn a plan (DAG of tasks) into actual agent execution. There is
not one right way to do this because real agent patterns differ:

1. **Some plans are linear.** A coordinator LLM wants to read the plan as one
   big user turn and execute it itself, calling sub-agents or tools step by
   step. Harmonograf should *not* take the wheel in this case — the
   coordinator is already doing the orchestration and we only need to observe
   and inject reporting tools.

2. **Some plans are parallel DAGs.** Tasks have explicit dependencies and
   independent branches can run concurrently. Leaving a coordinator LLM to
   manage parallelism is slower, more expensive, and more likely to go wrong
   than running a deterministic DAG walker over the plan and dispatching
   sub-agents directly.

3. **Some agents don't want a plan.** A delegate agent receives a task and
   figures out its own task sequencing. Harmonograf watches the run, picks
   up drift signals, and does not try to enforce a plan from the outside.
   Useful for third-party agents being observed inside a harmonograf session
   without being taken over.

Picking one mode and forcing everyone into it would over-constrain the
product. Auto-detecting the mode per agent is tempting but brittle — the
signals we'd use (tool list, plan shape, sub-agent count) are not reliable.

## Decision

`HarmonografAgent` accepts two orthogonal flags that select one of **three
orchestration modes**. Documented in `AGENTS.md`'s "Plan execution protocol"
section and in the `HarmonografAgent` class docstring in
`client/harmonograf_client/agent.py`.

- **Sequential** (`orchestrator_mode=True, parallel_mode=False`) — default.
  The whole plan is formatted and fed to the coordinator LLM as one user
  turn. The coordinator executes it; reporting tools drive per-task state.
  Harmonograf is orchestrator in name only — it seeds context and watches.
  Good for: small-to-medium plans where the coordinator LLM is capable
  enough to drive execution.

- **Parallel** (`orchestrator_mode=True, parallel_mode=True`) — the rigid
  DAG batch walker. Harmonograf directly dispatches sub-agents per task,
  respecting plan edges as dependencies, using a forced `task_id`
  ContextVar so each sub-agent knows which task it is executing. The LLM
  is not in the orchestration loop. Good for: large plans with explicit
  independent branches.

- **Delegated** (`orchestrator_mode=False`) — a single delegation to the
  inner agent; the event observer watches for drift afterward. The inner
  agent owns its own task sequencing entirely. Good for: wrapping an
  existing agent without taking over its control flow.

**Mode selection by flag pair** — two orthogonal `HarmonografAgent` flags
pick one of three execution shapes; auto-detection was rejected as brittle.

```mermaid
flowchart TD
    Start([HarmonografAgent ctor]) --> Q1{orchestrator_mode?}
    Q1 -- "False" --> M3[Delegated mode<br/>single delegation +<br/>event observer for drift]:::mode
    Q1 -- "True" --> Q2{parallel_mode?}
    Q2 -- "False" --> M1[Sequential mode (default)<br/>plan → coordinator LLM<br/>re-invoke up to 3×]:::mode
    Q2 -- "True" --> M2[Parallel mode<br/>DAG batch walker<br/>forced task_id ContextVar<br/>cap = 20]:::mode

    classDef mode fill:#d4edda,stroke:#27ae60,color:#000
```

## Consequences

**Good.**
- Each mode is appropriate for a different execution pattern, and users
  pick what matches their agent. No single mode has to be "the compromise."
- The parallel walker is deterministic — it respects the plan DAG, which
  means its failure modes are traceable without needing to reason about
  an LLM's internal orchestration choices.
- Sequential mode is cheap to adopt. It requires no user code changes
  beyond wiring `HarmonografAgent` as the root agent.
- Delegated mode means existing ADK agents can be observed without
  rewriting them. An operator who just wants to watch can get a view
  without refactoring their agent code.

**Bad.**
- **Three code paths is three times the test surface.** The walker in
  parallel mode, the re-invocation loop in sequential mode, and the
  event observer in delegated mode each have their own ordering, failure,
  and cancel semantics. Integration tests have to cover all three.
- **Picking a mode is a user decision.** New users have to read docs and
  figure out which mode fits their agent. We do not auto-pick, and the
  decision is not always obvious.
- **The parallel walker has a safety cap**
  (`_ORCHESTRATOR_WALKER_SAFETY_CAP = 20` in `agent.py`). Plans that exceed
  the cap are truncated with a warning. This is a cheap guard against
  runaway loops but it limits the DAG sizes parallel mode can handle.
- **Sequential mode's re-invocation logic is subtle.** If a task is still
  PENDING with satisfied dependencies after the inner generator exhausts,
  the orchestrator appends a synthetic user turn and re-invokes, up to
  `max_plan_reinvocations = 3` by default. This is a polite poke at the
  coordinator, but it can mask bugs where the LLM simply never calls
  `report_task_started` for a task it is silently executing.
- **Delegated mode cannot drive plan state.** By design. Users who choose
  it get partial task-state fidelity, because the inner agent is not
  guaranteed to call reporting tools.

The complexity is load-bearing. Collapsing to a single mode would force
each of the three use cases to bend to it, and each would bend badly.

## Implemented in

- [Design 12 — Client library + ADK integration](../design/12-client-library-and-adk.md)
