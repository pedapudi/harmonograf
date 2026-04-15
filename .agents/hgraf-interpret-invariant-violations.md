---
name: hgraf-interpret-invariant-violations
description: Read InvariantViolation log lines, map them to the specific rule in invariants.py, and decide whether to fix the bug or silence the check.
---

# hgraf-interpret-invariant-violations

## When to use

You see log lines like `invariant monotonic_state: task draft illegal transition COMPLETED → PENDING` or pytest is failing on `_run_invariants` output and you need to understand what is going wrong and what to do about it.

For general adk.py safety, see `hgraf-safely-modify-adk-py.md` in batch 1. This skill specializes on reading violation records and mapping them to root causes.

## Prerequisites

1. Read `client/harmonograf_client/invariants.py:58-76 InvariantViolation` — fields `rule`, `severity`, `detail`, and `log_level()`.
2. Read `invariants.py:41-55` — the `_VALID_STATUSES`, `_TERMINAL_STATUSES`, and `_ALLOWED_TRANSITIONS` tables.
3. Read `invariants.py:93-118 InvariantChecker.check` — the top-level dispatcher that runs every individual check and returns the list in a stable order.
4. Know where violations are surfaced: `_AdkState._run_invariants` in `adk.py`, called at the end of each walker turn. Errors assert in tests; warnings log and continue.

## The eight invariants and what they mean

### 1. `monotonic_state` (error)

Source: `invariants.py:124 _check_monotonic`.

A task's status went backwards through an illegal transition, or is not one of `PENDING | RUNNING | COMPLETED | FAILED | CANCELLED`.

Allowed transitions (`invariants.py:49-55`):
- `PENDING → PENDING | RUNNING | CANCELLED | FAILED`
- `RUNNING → RUNNING | COMPLETED | FAILED | CANCELLED`
- `COMPLETED`, `FAILED`, `CANCELLED` — terminal, no outgoing edges.

Typical root causes:
- A callback directly wrote `task.status = "PENDING"` after a COMPLETED. The guard in `_set_task_status` prevents this; bypassing the guard (mutating `task.status` on a raw object) is the usual culprit.
- A refine path rebuilt tasks from the LLM response without preserving terminal statuses (see `_apply_refined_plan` preservation loop at `adk.py:3494`).
- A test fixture with a hand-crafted plan used an invalid status string.

Action: fix the offending write path. **Never silence this error** — it means the state machine is corrupt.

### 2. `dependency_consistency` (warning)

Source: `invariants.py:158 _check_dependency_consistency`.

`dst` is COMPLETED but `src` (its dependency) is still PENDING.

Typical root causes:
- A parallel walker completed a task before its dependency started (walker bug — very rare).
- The LLM in refine-mode reordered tasks without rewiring edges.
- `dropout` scenarios where the LLM omitted the dependency from the refined plan entirely.

Action: usually fix in the plan-building code. For edge cases, a warning is acceptable — the UI shows it but execution proceeds.

### 3. `assignee_validity` (warning)

Source: `invariants.py:187 _check_assignee_validity`.

A task's `assignee_agent_id` is not in `plan_state.available_agents`.

Typical root causes:
- LLM hallucinated an agent id.
- Plan refresh changed `available_agents` but preserved old tasks with stale assignees.
- Initial plan from `PlannerHelper.generate` bypassed `_canonicalize_plan_assignees`.

Action: ensure `_canonicalize_plan_assignees` runs on every plan insertion (`adk.py:3478` is the canonical site for refine). For initial plans, confirm the call also happens in the generate path.

### 4. `plan_id_uniqueness` (error likely)

Source: `invariants.py:210 _check_plan_id_uniqueness`.

Two different `hsession_id`s share the same `plan_id` in `state._active_plan_by_session`.

Typical root causes:
- Using a hardcoded plan_id in tests that run in parallel.
- A UUID generator misfiring (collision probability is negligible — check for a default value like `"plan_0"`).

Action: always generate plan_ids with `uuid.uuid4().hex` or equivalent. Fix the source.

### 5. `forced_task` (error)

Source: `invariants.py :: _check_forced_task`.

The forced `task_id` ContextVar (parallel walker) points at a task that isn't in the current plan_state, or at a task in a terminal status.

Typical root causes:
- Walker logic left a stale ContextVar value after a refine superseded the old plan.
- A test manually set the ContextVar without restoring it.

Action: check the walker's ContextVar lifecycle. `reset()` on every task boundary.

### 6. `task_results_keys` (warning)

Source: `invariants.py :: _check_task_results_keys`.

`session.state['harmonograf.completed_task_results']` has keys that don't correspond to any task in the current plan.

Typical root causes:
- Stale entries from a superseded plan that weren't garbage-collected.
- Testing harnesses that pre-populate the state without syncing with the plan.

Action: usually fine to silence in tests (pre-populate results matching a real plan). In production, clean up on plan supersession.

### 7. `revision_history_monotone` (error)

Source: `invariants.py :: _check_revision_history_monotone`.

The `revision_history` list inside `PlanState` has non-monotonic `revision_index` values.

Typical root causes:
- Concurrent refine paths racing without the lock (very rare — the lock at `adk.py:3493` exists for this).
- A test that manually appended a revision without bumping the index.

Action: always go through `_apply_refined_plan` — never mutate `plan_state.plan.revision_index` by hand.

### 8. `span_bindings` (warning)

Source: `invariants.py :: _check_span_bindings`.

A task's `bound_span_id` points at a span that doesn't exist in the client's local span tracker, OR the span's `hgraf.task_id` attribute doesn't match.

Typical root causes:
- A forced `task_id` ContextVar drove a binding that the client didn't actually stamp.
- A span was emitted and discarded before the binding was written.
- A test emits spans out-of-order relative to task reports.

Action: confirm the order of operations is `emit_span_start → (bind task) → emit_span_update` in the adapter path. Read `adk.py :: _bind_span_to_task`.

## Step-by-step diagnosis

### 1. Capture the full violation list

Violations arrive as a `list[InvariantViolation]` — **read all of them**, not just the first. The checker returns them in stable order and a single bug often triggers two or three different rules.

```python
violations = check_plan_state(state, hsession_id)
for v in violations:
    log.log(v.log_level(), "invariant %s (%s): %s", v.rule, v.severity, v.detail)
```

The same `rule` appearing N times usually means N tasks caught by the same bug, not N different bugs.

### 2. Reproduce deterministically

Convert the violation into a failing unit test. Pattern:

```python
def test_reproduces_monotonic_regression():
    state = _AdkState(...)
    plan_state = _build_plan_state(...)
    state._active_plan_by_session["s1"] = plan_state
    # Simulate the exact mutation path
    plan_state.tasks["draft"].status = "COMPLETED"
    plan_state.tasks["draft"].status = "PENDING"  # illegal
    violations = check_plan_state(state, "s1")
    assert any(v.rule == "monotonic_state" for v in violations)
```

Then fix the underlying write path and assert the violation disappears.

### 3. Decide fix-or-silence

| Severity | Default action |
|---|---|
| `error` | Always fix. Silencing an error means the state machine is lying. |
| `warning` | Fix if cheap; silence if the check is incorrect for a specific legitimate case. |

To silence a warning for a specific known-safe path, add a guard *inside the check*, not a try/except around it. Silencing by catching exceptions hides real bugs.

### 4. When the check itself is wrong

If the check is producing false positives on a legitimate plan shape:

1. Add a test case in `client/tests/test_invariants.py` that demonstrates the false positive.
2. Refine the check. Prefer narrowing (adding a precondition) over widening the allowed set.
3. Document the new precondition in the check's docstring.

### 5. Performance note

`_run_invariants` runs on every walker turn. If the plan is large (100+ tasks), the check budget matters — each rule is O(n) or O(n²) for edges. Profile with the pattern in `hgraf-profile-callback-perf.md`.

## Verification

```bash
uv run pytest client/tests/test_invariants.py -x -q
uv run pytest client/tests/test_protocol_callbacks.py -x -q -k invariant
uv run pytest client/tests -x -q -k "invariant or monotonic"
```

## Common pitfalls

- **Silencing in tests with an assert filter**: `assert not any(v.severity == "error")` in tests is the right call — errors are bugs. `assert not violations` is too strict because warnings are informational.
- **Testing after the bug is gone**: if your fix removes the violation, also add a regression test that re-triggers it so a future rewrite can't reintroduce the bug.
- **Reading `_last_status` across tests**: the module-level `InvariantChecker` carries state across calls. Construct a fresh `InvariantChecker()` per test to avoid cross-test pollution.
- **Confusing rule name with detail**: the `rule` field is a stable tag (e.g. `"monotonic_state"`); the `detail` is a human-readable string. Aggregations and filters should key on `rule`, never on the detail.
- **Firing invariants during refine**: `_run_invariants` is called *after* the walker turn settles. If you add a mid-refine check, you'll see transient violations that aren't real — don't.
- **Assuming log-level matches severity**: `log_level()` returns `ERROR` for `"error"` and `WARNING` for anything else (`invariants.py:74`). "Silence" = filter at the logger, not at the severity label.
