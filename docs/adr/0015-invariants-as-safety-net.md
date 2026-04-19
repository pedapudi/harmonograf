> **DEPRECATED (goldfive migration).** The invariant validator described here
> (`client/harmonograf_client/invariants.py`) has been deleted; invariant
> checking is a goldfive concern now. See
> [../goldfive-integration.md](../goldfive-integration.md) and
> [../goldfive-migration-plan.md](../goldfive-migration-plan.md).

# ADR 0015 — Ship the invariant validator to production

## Status

Accepted.

## Context

Plan state in harmonograf is a state machine with several invariants that
must hold at all times:

- Every task status is one of {PENDING, RUNNING, COMPLETED, FAILED,
  CANCELLED}.
- Terminal statuses (COMPLETED, FAILED, CANCELLED) have no outgoing
  transitions — once terminal, always terminal (see [ADR 0017](0017-monotonic-task-state.md)).
- A task can only transition RUNNING → anything; a task cannot go from
  COMPLETED back to RUNNING.
- A task's `bound_span_id` should refer to a span that exists and
  belongs to the assignee agent (or be empty).
- Dependencies must be acyclic.
- A task whose dependencies are unsatisfied cannot be RUNNING.
- Reinvocation budgets for tasks the classifier flagged as "partial"
  are bounded (see `_PARTIAL_REINVOCATION_BUDGET = 3`).

The per-write guards in `adk.py` — the monotonic status machine, the
refine preservation logic, the walker's single-writer discipline — make
illegal *single* transitions structurally impossible. The question is
whether that is enough, or whether we should also validate the
*aggregate* state at the end of each walker turn.

Arguments for not shipping an invariant validator:
- It's runtime cost on the hot path.
- If the per-write guards are correct, the validator should never fire.
- Shipping a validator is admitting the per-write guards are not
  trusted.

Arguments for shipping one:
- The per-write guards are *almost* correct. "Almost" is exactly the
  window where compounding bugs slip through. A validator catches them.
- The alternative is finding these bugs in the field via user reports.
- The validator is read-only and cheap (it walks the plan once per
  walker turn).

## Decision

Ship the invariant validator (`client/harmonograf_client/invariants.py`)
and run it **on every walker turn in production**, not just in tests.

Behavior:
- `check_plan_state(state, hsession_id)` returns a list of
  `InvariantViolation` dataclasses.
- Violations are logged at the violation's own log level (`.log_level()`),
  which is WARN for most and ERROR for monotonicity breaks.
- Tests assert no `severity == "error"` violations; production logs them
  and moves on. See the module docstring:

  ```python
  violations = check_plan_state(state, hsession_id)
  for v in violations:
      log.log(v.log_level(), "invariant %s: %s", v.rule, v.detail)
  assert _not_in_tests() or not any(v.severity == "error" for v in violations)
  ```

The validator is pure and has no runtime dependency on `adk.py`. It
reads state via duck typing, mutates nothing, and can be called on any
plan-shaped object. A stateful `InvariantChecker` variant is offered
for callers that want cross-turn history (the monotonic-state check
needs to see transitions, not just current state).

**Validator topology** — per-write guards block illegal *single*
transitions; `check_plan_state` runs at end of every walker turn to catch
*aggregate* incoherence the per-write guards missed.

```mermaid
flowchart LR
    Wr[reporting tool /<br/>walker write] --> G1[per-write guard<br/>_set_task_status]
    G1 -- legal --> S[(plan state)]
    G1 -- illegal --> Drop[silently dropped<br/>(monotonic)]
    S --> Turn[walker turn end]
    Turn --> Val[check_plan_state<br/>invariants.py]
    Val --> Vio[InvariantViolation list<br/>severity=warn|error]
    Vio --> Log[log at v.log_level()]
    Vio --> Test{tests?}
    Test -- yes --> Assert[assert no error]
    Test -- no --> Continue[production: log + continue]

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class Val,Vio good
```

## Consequences

**Good.**
- Several real bugs have been caught by the validator in CI before
  shipping — cases where a combination of per-write guards allowed a
  globally incoherent state even though each individual write was
  legal.
- The validator serves as executable documentation of what "a valid
  plan state" means. A new contributor reads `invariants.py` to learn
  the invariants; changes to invariants are localized there.
- Production logs carry the violation records, so a user report that
  "the Gantt shows a weird state" can be cross-referenced to
  validator warnings from the same session.
- Validator is read-only and single-threaded; it can't itself corrupt
  state.

**Bad.**
- **Runtime cost.** The validator walks the whole plan on every walker
  turn. For very large plans this is non-trivial — roughly O(tasks +
  edges) per turn. In practice plans are small enough that we don't
  notice, but there is no hard bound.
- **Log noise.** In failure cases, the validator can emit multiple
  violations for what is morally one bug (e.g., a monotonicity break
  that invalidates every downstream dependency). Operators reading
  logs can see cascades that look alarming but are one root cause.
- **Production code relies on tests.** The assert statement uses
  `_not_in_tests()` to decide whether to hard-fail. If that detection
  is wrong (e.g., a new test harness that does not set the env var),
  tests silently miss violations or production hard-asserts in a hot
  path. This is a fragile coupling the module owns.
- **Invariants can become stale.** If the state model changes but
  `invariants.py` is not updated, the validator either (a) stops
  catching new classes of bug or (b) fires false positives on legal
  transitions. Reviewers have to remember to update both.
- **"Runs in production" is a euphemism.** The validator currently
  runs only in the walker — the sequential and delegated modes don't
  invoke it as of today. Bugs that only manifest in sequential mode
  are not caught. Extending coverage is a known gap.

The validator pays for itself every time it catches a compounding bug
the per-write guards missed. The runtime cost is the price we pay to
sleep at night.

## Implemented in

- [Design 03 — Server](../design/03-server.md)
- [Design 11 — Server architecture deep-dive](../design/11-server-architecture.md)
