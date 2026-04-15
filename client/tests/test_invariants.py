"""Tests for the plan-state invariant validator (task #15).

The validator in :mod:`harmonograf_client.invariants` is pure and
read-only, so it's driven with lightweight duck-typed stand-ins for
``_AdkState`` + ``PlanState``. The spec requires every check to be
covered plus a performance smoke test (runs in <5ms for a 20-task
plan).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from harmonograf_client.invariants import (
    InvariantChecker,
    InvariantViolation,
    check_plan_state,
    enforce,
    reset_default_checker,
)


# ---------------------------------------------------------------------------
# Duck-typed fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str
    title: str = ""
    assignee_agent_id: str = ""
    status: str = "PENDING"


@dataclass
class FakeEdge:
    from_task_id: str
    to_task_id: str


@dataclass
class FakePlanState:
    plan_id: str
    tasks: dict[str, FakeTask]
    edges: list[FakeEdge] = field(default_factory=list)
    available_agents: list[str] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)


@dataclass
class FakeState:
    _active_plan_by_session: dict[str, FakePlanState] = field(default_factory=dict)
    _forced_current_task_id: str = ""
    _task_results: dict[str, str] = field(default_factory=dict)
    _span_to_task: dict[str, str] = field(default_factory=dict)


def _make_state(
    *,
    plan_id: str = "plan-1",
    tasks: list[FakeTask] | None = None,
    edges: list[FakeEdge] | None = None,
    known_agents: list[str] | None = None,
    hsession: str = "hs-1",
) -> FakeState:
    tasks_dict = {t.id: t for t in (tasks or [])}
    ps = FakePlanState(
        plan_id=plan_id,
        tasks=tasks_dict,
        edges=list(edges or []),
        available_agents=list(known_agents or []),
    )
    return FakeState(_active_plan_by_session={hsession: ps})


@pytest.fixture(autouse=True)
def _reset_default():
    reset_default_checker()
    yield
    reset_default_checker()


# ---------------------------------------------------------------------------
# Clean state -> no violations
# ---------------------------------------------------------------------------


class TestCleanSnapshot:
    def test_empty_plan_no_violations(self):
        state = _make_state(tasks=[])
        assert check_plan_state(state, "hs-1") == []

    def test_healthy_plan_no_violations(self):
        state = _make_state(
            tasks=[
                FakeTask(id="t1", status="COMPLETED", assignee_agent_id="writer"),
                FakeTask(id="t2", status="RUNNING", assignee_agent_id="writer"),
            ],
            edges=[FakeEdge("t1", "t2")],
            known_agents=["writer"],
        )
        assert check_plan_state(state, "hs-1") == []

    def test_missing_session_returns_empty(self):
        state = _make_state(tasks=[FakeTask(id="t1")])
        assert check_plan_state(state, "nonexistent") == []


# ---------------------------------------------------------------------------
# Individual invariants
# ---------------------------------------------------------------------------


class TestMonotonicState:
    def test_unknown_status_is_error(self):
        state = _make_state(tasks=[FakeTask(id="t1", status="WEIRD")])
        vs = check_plan_state(state, "hs-1")
        assert any(v.rule == "monotonic_state" and v.severity == "error" for v in vs)

    def test_illegal_transition_observed_across_calls(self):
        checker = InvariantChecker()
        task = FakeTask(id="t1", status="COMPLETED")
        state = _make_state(tasks=[task])
        assert checker.check(state, "hs-1") == []
        # Terminal -> RUNNING is disallowed.
        task.status = "RUNNING"
        vs = checker.check(state, "hs-1")
        assert any(
            v.rule == "monotonic_state" and "COMPLETED → RUNNING" in v.detail
            for v in vs
        )

    def test_legal_transition_does_not_fire(self):
        checker = InvariantChecker()
        task = FakeTask(id="t1", status="PENDING")
        state = _make_state(tasks=[task])
        checker.check(state, "hs-1")
        task.status = "RUNNING"
        assert checker.check(state, "hs-1") == []
        task.status = "COMPLETED"
        assert checker.check(state, "hs-1") == []


class TestDependencyConsistency:
    def test_completed_with_pending_dep_warns(self):
        state = _make_state(
            tasks=[
                FakeTask(id="t1", status="PENDING"),
                FakeTask(id="t2", status="COMPLETED"),
            ],
            edges=[FakeEdge("t1", "t2")],
        )
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "dependency_consistency"]
        assert len(matching) == 1
        assert matching[0].severity == "warning"
        assert "t2" in matching[0].detail and "t1" in matching[0].detail

    def test_completed_with_completed_dep_is_fine(self):
        state = _make_state(
            tasks=[
                FakeTask(id="t1", status="COMPLETED"),
                FakeTask(id="t2", status="COMPLETED"),
            ],
            edges=[FakeEdge("t1", "t2")],
        )
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "dependency_consistency" for v in vs)


class TestAssigneeValidity:
    def test_unknown_assignee_warns(self):
        state = _make_state(
            tasks=[FakeTask(id="t1", assignee_agent_id="ghost")],
            known_agents=["writer", "researcher"],
        )
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "assignee_validity"]
        assert len(matching) == 1
        assert "ghost" in matching[0].detail

    def test_empty_assignee_is_fine(self):
        state = _make_state(
            tasks=[FakeTask(id="t1", assignee_agent_id="")],
            known_agents=["writer"],
        )
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "assignee_validity" for v in vs)

    def test_no_known_agents_is_fine(self):
        state = _make_state(
            tasks=[FakeTask(id="t1", assignee_agent_id="whatever")],
            known_agents=[],
        )
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "assignee_validity" for v in vs)


class TestPlanIdUniqueness:
    def test_duplicate_plan_id_errors(self):
        ps1 = FakePlanState(plan_id="plan-X", tasks={"t1": FakeTask(id="t1")})
        ps2 = FakePlanState(plan_id="plan-X", tasks={"t2": FakeTask(id="t2")})
        state = FakeState(_active_plan_by_session={"hs-a": ps1, "hs-b": ps2})
        vs = check_plan_state(state, "hs-a")
        matching = [v for v in vs if v.rule == "plan_id_uniqueness"]
        assert len(matching) == 1
        assert matching[0].severity == "error"

    def test_distinct_plan_ids_ok(self):
        ps1 = FakePlanState(plan_id="plan-A", tasks={"t1": FakeTask(id="t1")})
        ps2 = FakePlanState(plan_id="plan-B", tasks={"t2": FakeTask(id="t2")})
        state = FakeState(_active_plan_by_session={"hs-a": ps1, "hs-b": ps2})
        vs = check_plan_state(state, "hs-a")
        assert not any(v.rule == "plan_id_uniqueness" for v in vs)


class TestForcedTaskConsistency:
    def test_forced_task_missing_errors(self):
        state = _make_state(tasks=[FakeTask(id="t1")])
        state._forced_current_task_id = "ghost"
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "forced_task_consistency"]
        assert len(matching) == 1
        assert matching[0].severity == "error"
        assert "ghost" in matching[0].detail

    def test_forced_task_terminal_errors(self):
        state = _make_state(
            tasks=[FakeTask(id="t1", status="COMPLETED")]
        )
        state._forced_current_task_id = "t1"
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "forced_task_consistency"]
        assert len(matching) == 1
        assert "terminal" in matching[0].detail

    def test_forced_task_running_is_fine(self):
        state = _make_state(
            tasks=[FakeTask(id="t1", status="RUNNING")]
        )
        state._forced_current_task_id = "t1"
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "forced_task_consistency" for v in vs)


class TestTaskResultsKeys:
    def test_stale_result_warns(self):
        state = _make_state(tasks=[FakeTask(id="t1")])
        state._task_results = {"t1": "ok", "gone": "stale"}
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "task_results_keys"]
        assert len(matching) == 1
        assert "gone" in matching[0].detail

    def test_matching_keys_ok(self):
        state = _make_state(tasks=[FakeTask(id="t1"), FakeTask(id="t2")])
        state._task_results = {"t1": "ok"}
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "task_results_keys" for v in vs)


class TestRevisionHistoryMonotone:
    def test_out_of_order_warns(self):
        ps = FakePlanState(
            plan_id="p",
            tasks={"t1": FakeTask(id="t1")},
            revisions=[
                {"revised_at": 200.0, "reason": "a"},
                {"revised_at": 100.0, "reason": "b"},
            ],
        )
        state = FakeState(_active_plan_by_session={"hs-1": ps})
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "revision_history_monotone"]
        assert len(matching) == 1

    def test_in_order_ok(self):
        ps = FakePlanState(
            plan_id="p",
            tasks={"t1": FakeTask(id="t1")},
            revisions=[
                {"revised_at": 100.0, "reason": "a"},
                {"revised_at": 200.0, "reason": "b"},
            ],
        )
        state = FakeState(_active_plan_by_session={"hs-1": ps})
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "revision_history_monotone" for v in vs)


class TestSpanBindings:
    def test_stale_binding_warns(self):
        state = _make_state(tasks=[FakeTask(id="t1")])
        state._span_to_task = {"span-1": "t1", "span-2": "ghost"}
        vs = check_plan_state(state, "hs-1")
        matching = [v for v in vs if v.rule == "span_bindings"]
        assert len(matching) == 1
        assert "ghost" in matching[0].detail

    def test_healthy_bindings_ok(self):
        state = _make_state(
            tasks=[FakeTask(id="t1"), FakeTask(id="t2")]
        )
        state._span_to_task = {"span-1": "t1", "span-2": "t2"}
        vs = check_plan_state(state, "hs-1")
        assert not any(v.rule == "span_bindings" for v in vs)


# ---------------------------------------------------------------------------
# enforce() behavior
# ---------------------------------------------------------------------------


class TestEnforce:
    def test_empty_list_is_noop(self):
        enforce([])  # must not raise

    def test_warnings_do_not_raise(self, caplog):
        vs = [
            InvariantViolation(
                rule="dependency_consistency",
                severity="warning",
                detail="harmless",
            )
        ]
        with caplog.at_level("WARNING", logger="harmonograf_client.invariants"):
            enforce(vs, context="test")
        assert any("harmless" in r.message for r in caplog.records)

    def test_error_raises_under_pytest(self):
        vs = [
            InvariantViolation(
                rule="monotonic_state",
                severity="error",
                detail="bad",
            )
        ]
        with pytest.raises(AssertionError, match="monotonic_state"):
            enforce(vs, context="test-ctx")


# ---------------------------------------------------------------------------
# Performance smoke test
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_twenty_task_plan_runs_under_5ms(self):
        tasks = [
            FakeTask(
                id=f"t{i}",
                title=f"Task {i}",
                assignee_agent_id="writer",
                status="PENDING",
            )
            for i in range(20)
        ]
        edges = [FakeEdge(f"t{i}", f"t{i+1}") for i in range(19)]
        state = _make_state(
            tasks=tasks, edges=edges, known_agents=["writer"]
        )
        # Warm up path caches.
        check_plan_state(state, "hs-1")
        # Measure best-of-5 to avoid import / gc noise.
        best = min(
            (
                (
                    (lambda: (time.perf_counter(), check_plan_state(state, "hs-1"), time.perf_counter()))()
                )
                for _ in range(5)
            ),
            key=lambda tup: tup[2] - tup[0],
        )
        elapsed_ms = (best[2] - best[0]) * 1000
        assert elapsed_ms < 5.0, f"invariant check took {elapsed_ms:.2f}ms"
