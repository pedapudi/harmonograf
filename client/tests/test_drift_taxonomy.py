"""Tests for the expanded drift taxonomy (task #6).

Covers the new drift kinds, severity handling, throttling, unrecoverable
cascading, hint propagation, revision tracking, and the
``apply_drift_from_control`` helper.

These tests avoid ADK entirely — they drive ``_AdkState`` directly with
duck-typed fakes so the unit suite stays green without google.adk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from harmonograf_client.adk import (
    DRIFT_KIND_CONTEXT_PRESSURE,
    DRIFT_KIND_LLM_MERGED_TASKS,
    DRIFT_KIND_LLM_REFUSED,
    DRIFT_KIND_LLM_REORDERED_WORK,
    DRIFT_KIND_LLM_SPLIT_TASK,
    DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES,
    DRIFT_KIND_USER_CANCEL,
    DRIFT_KIND_USER_STEER,
    DriftReason,
    PlanState,
    _AdkState,
    _STAMP_MISMATCH_THRESHOLD,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counter = 0
        self._current_activity = ""

    def emit_span_start(self, **kwargs) -> str:
        self._counter += 1
        sid = f"span-{self._counter}"
        self.calls.append(("start", sid, kwargs))
        return sid

    def emit_span_update(self, span_id: str, **kwargs) -> None:
        self.calls.append(("update", span_id, kwargs))

    def emit_span_end(self, span_id: str, **kwargs) -> None:
        self.calls.append(("end", span_id, kwargs))

    def on_control(self, kind: str, cb) -> None:
        self.calls.append(("on_control", kind, cb))

    def set_current_activity(self, text: str) -> None:
        self._current_activity = text

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = kwargs.get("plan_id") or f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid

    def submit_task_status_update(
        self, plan_id: str, task_id: str, status: str, **kwargs
    ) -> None:
        self.calls.append(
            (
                "submit_task_status_update",
                task_id,
                {"plan_id": plan_id, "status": status, **kwargs},
            )
        )


class RecordingPlanner(PlannerHelper):
    def __init__(self, refine_response: Optional[Plan] = None) -> None:
        self.refine_calls: list[dict[str, Any]] = []
        self._refine_response = refine_response

    def generate(self, **kwargs):  # type: ignore[override]
        return Plan(
            tasks=[Task(id="t1", title="one", assignee_agent_id="worker")],
            edges=[],
        )

    def refine(self, plan: Plan, event):  # type: ignore[override]
        self.refine_calls.append(dict(event))
        return self._refine_response


@dataclass
class FakeFunctionCall:
    name: str = ""


@dataclass
class FakePart:
    text: Optional[str] = None
    thought: bool = False
    function_call: Any = None


@dataclass
class FakeContent:
    parts: list = field(default_factory=list)
    role: str = "model"


@dataclass
class FakeEvent:
    id: str = ""
    author: str = ""
    content: Any = None
    actions: Any = None
    status: str = ""
    task_id: str = ""
    completed_task_id: str = ""
    finish_reason: str = ""


def _mk_plan_state(state: _AdkState, *, hsession_id: str = "hs") -> PlanState:
    t1 = Task(id="t1", title="a", assignee_agent_id="worker", status="PENDING")
    t2 = Task(id="t2", title="b", assignee_agent_id="worker", status="PENDING")
    t3 = Task(id="t3", title="c", assignee_agent_id="worker", status="PENDING")
    plan = Plan(
        tasks=[t1, t2, t3],
        edges=[
            TaskEdge(from_task_id="t1", to_task_id="t2"),
            TaskEdge(from_task_id="t2", to_task_id="t3"),
        ],
        summary="three",
    )
    ps = PlanState(
        plan=plan,
        plan_id=f"plan-{hsession_id}",
        tasks={"t1": t1, "t2": t2, "t3": t3},
        available_agents=["worker"],
        generating_invocation_id="inv",
        remaining_for_fallback=[t1, t2, t3],
    )
    with state._lock:
        state._active_plan_by_session[hsession_id] = ps
    return ps


# ---------------------------------------------------------------------------
# DriftReason dataclass
# ---------------------------------------------------------------------------


class TestDriftReasonDataclass:
    def test_defaults(self):
        dr = DriftReason(kind="x", detail="y")
        assert dr.severity == "info"
        assert dr.recoverable is True
        assert dr.hint == {}

    def test_all_fields(self):
        dr = DriftReason(
            kind="user_cancel",
            detail="bye",
            severity="critical",
            recoverable=False,
            hint={"why": "user"},
        )
        assert dr.severity == "critical"
        assert dr.recoverable is False
        assert dr.hint == {"why": "user"}


# ---------------------------------------------------------------------------
# detect_drift — new signals
# ---------------------------------------------------------------------------


class TestDetectDriftNewSignals:
    def _state(self) -> tuple[_AdkState, PlanState]:
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        return state, _mk_plan_state(state)

    def test_context_pressure_from_finish_reason_max_tokens(self):
        state, ps = self._state()
        ev = FakeEvent(id="e1", finish_reason="MAX_TOKENS")
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_CONTEXT_PRESSURE
        assert drift.severity == "warning"
        assert drift.hint.get("finish_reason") == "MAX_TOKENS"

    def test_context_pressure_from_finish_reason_length(self):
        state, ps = self._state()
        ev = FakeEvent(id="e1", finish_reason="LENGTH")
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_CONTEXT_PRESSURE

    def test_llm_refused_from_text_part(self):
        state, ps = self._state()
        content = FakeContent(
            parts=[FakePart(text="I cannot help with that request.")]
        )
        ev = FakeEvent(id="e1", author="worker", content=content)
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REFUSED
        assert drift.severity == "warning"
        assert "I cannot" in drift.hint.get("text", "")

    def test_llm_merged_from_text_part(self):
        state, ps = self._state()
        content = FakeContent(
            parts=[FakePart(text="I'm merging tasks t1 and t2 into one step.")]
        )
        ev = FakeEvent(id="e1", author="worker", content=content)
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_MERGED_TASKS

    def test_llm_split_from_text_part(self):
        state, ps = self._state()
        content = FakeContent(
            parts=[FakePart(text="Splitting this task into three subtasks.")]
        )
        ev = FakeEvent(id="e1", author="worker", content=content)
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_SPLIT_TASK

    def test_llm_reordered_from_text_part(self):
        state, ps = self._state()
        content = FakeContent(
            parts=[FakePart(text="I'll be doing this task first before t1.")]
        )
        ev = FakeEvent(id="e1", author="worker", content=content)
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REORDERED_WORK

    def test_thought_parts_are_ignored(self):
        state, ps = self._state()
        content = FakeContent(
            parts=[FakePart(text="I cannot do that", thought=True)]
        )
        ev = FakeEvent(id="e1", author="worker", content=content)
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        # Refusal markers in thought=True parts should be skipped.
        assert drift is None or drift.kind != DRIFT_KIND_LLM_REFUSED

    def test_multiple_stamp_mismatches_fires_at_threshold(self):
        state, ps = self._state()
        for _ in range(_STAMP_MISMATCH_THRESHOLD):
            state.note_stamp_mismatch()
        drift = state.detect_drift([], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES
        assert drift.hint.get("count") == _STAMP_MISMATCH_THRESHOLD

    def test_stamp_mismatches_below_threshold_no_drift(self):
        state, ps = self._state()
        state.note_stamp_mismatch()
        drift = state.detect_drift([], current_task=None, plan_state=ps)
        assert drift is None


# ---------------------------------------------------------------------------
# detect_semantic_drift — new signals
# ---------------------------------------------------------------------------


class TestDetectSemanticDriftNewSignals:
    def _state(self) -> _AdkState:
        return _AdkState(client=FakeClient())  # type: ignore[arg-type]

    def test_llm_refused_in_result(self):
        state = self._state()
        task = Task(id="t1", title="x")
        drift = state.detect_semantic_drift(
            task,
            "I cannot assist with this as it violates policy. "
            "Please reconsider your request.",
            events=[],
        )
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REFUSED
        assert drift.severity == "warning"

    def test_llm_merged_in_result(self):
        state = self._state()
        task = Task(id="t1", title="x")
        drift = state.detect_semantic_drift(
            task,
            "I'm combining tasks t1 and t2 since they overlap significantly.",
            events=[],
        )
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_MERGED_TASKS


# ---------------------------------------------------------------------------
# refine_plan_on_drift — severity handling
# ---------------------------------------------------------------------------


class TestRefineSeverity:
    def _setup(
        self,
    ) -> tuple[_AdkState, RecordingPlanner, PlanState]:
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        return state, planner, ps

    def test_info_severity_logs_debug(self, caplog):
        state, _planner, _ps = self._setup()
        with caplog.at_level(logging.DEBUG, logger="harmonograf_client.adk"):
            state.refine_plan_on_drift(
                "hs", DriftReason(kind="x", detail="y", severity="info")
            )
        debug_lines = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "drift observed" in r.message
        ]
        assert debug_lines, "expected DEBUG detail log for info severity"

    def test_warning_severity_logs_info(self, caplog):
        state, _p, _ps = self._setup()
        with caplog.at_level(logging.INFO, logger="harmonograf_client.adk"):
            state.refine_plan_on_drift(
                "hs", DriftReason(kind="y", detail="z", severity="warning")
            )
        info_lines = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "drift observed" in r.message
        ]
        assert info_lines, "expected INFO detail log for warning severity"

    def test_critical_severity_logs_warning_and_surfaces_span(self, caplog):
        state, _p, _ps = self._setup()
        # Seed an active invocation so surfaced span updates are visible.
        with state._lock:
            state._invocations["inv1"] = "span-inv1"
        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            state.refine_plan_on_drift(
                "hs",
                DriftReason(
                    kind="critical_x",
                    detail="bad thing",
                    severity="critical",
                ),
            )
        warn_lines = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "drift observed" in r.message
        ]
        assert warn_lines, "expected WARNING detail log for critical severity"
        client = state._client  # type: ignore[attr-defined]
        updates = [c for c in client.calls if c[0] == "update"]
        assert updates, "critical drift should surface span attributes"
        attrs = updates[-1][2].get("attributes", {})
        assert attrs.get("drift_kind") == "critical_x"
        assert attrs.get("drift_severity") == "critical"
        assert "error" in attrs


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


class TestRefineThrottling:
    def test_same_kind_within_window_is_suppressed(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        _mk_plan_state(state)
        for _ in range(5):
            state.refine_plan_on_drift(
                "hs",
                DriftReason(
                    kind="tool_returned_error", detail="err", severity="info"
                ),
            )
        assert len(planner.refine_calls) == 1, (
            f"throttle should have collapsed to 1 refine, got {len(planner.refine_calls)}"
        )

    def test_different_kinds_not_throttled_together(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        _mk_plan_state(state)
        state.refine_plan_on_drift(
            "hs", DriftReason(kind="k1", detail="a")
        )
        state.refine_plan_on_drift(
            "hs", DriftReason(kind="k2", detail="b")
        )
        assert len(planner.refine_calls) == 2

    def test_critical_bypasses_throttle(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        _mk_plan_state(state)
        for _ in range(3):
            state.refine_plan_on_drift(
                "hs",
                DriftReason(
                    kind="k_crit", detail="c", severity="critical"
                ),
            )
        assert len(planner.refine_calls) == 3


# ---------------------------------------------------------------------------
# Unrecoverable drift cascading
# ---------------------------------------------------------------------------


class TestUnrecoverableDrift:
    def test_cascades_downstream_cancelled(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ps.tasks["t1"].status = "RUNNING"

        state.refine_plan_on_drift(
            "hs",
            DriftReason(
                kind=DRIFT_KIND_USER_CANCEL,
                detail="user pressed stop",
                severity="critical",
                recoverable=False,
            ),
            current_task=ps.tasks["t1"],
        )

        assert ps.tasks["t1"].status == "FAILED"
        assert ps.tasks["t2"].status == "CANCELLED"
        assert ps.tasks["t3"].status == "CANCELLED"
        # Planner was NOT called for unrecoverable drift.
        assert planner.refine_calls == []

        client = state._client  # type: ignore[attr-defined]
        status_updates = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        statuses = {c[1]: c[2]["status"] for c in status_updates}
        assert statuses.get("t1") == "FAILED"
        assert statuses.get("t2") == "CANCELLED"
        assert statuses.get("t3") == "CANCELLED"

    def test_unrecoverable_records_revision(self):
        state = _AdkState(client=FakeClient(), planner=RecordingPlanner())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        state.refine_plan_on_drift(
            "hs",
            DriftReason(
                kind="agent_escalated",
                detail="sub-agent escalated",
                severity="critical",
                recoverable=False,
            ),
            current_task=ps.tasks["t1"],
        )
        assert ps.revisions
        assert ps.revisions[-1]["kind"] == "agent_escalated"
        assert ps.revisions[-1]["severity"] == "critical"
        assert ps.plan.revision_reason.startswith("agent_escalated:")


# ---------------------------------------------------------------------------
# Hint propagation
# ---------------------------------------------------------------------------


class TestHintPropagation:
    def test_hint_reaches_planner_refine(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        _mk_plan_state(state)
        state.refine_plan_on_drift(
            "hs",
            DriftReason(
                kind="llm_merged_tasks",
                detail="merge",
                hint={"merged_ids": ["t1", "t2"]},
            ),
        )
        assert planner.refine_calls
        event = planner.refine_calls[0]
        assert event["hint"] == {"merged_ids": ["t1", "t2"]}
        assert event["severity"] == "info"
        assert event["recoverable"] is True


# ---------------------------------------------------------------------------
# Revision tracking
# ---------------------------------------------------------------------------


class TestRevisionTracking:
    def test_revision_entry_shape(self):
        state = _AdkState(client=FakeClient(), planner=RecordingPlanner())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        state.refine_plan_on_drift(
            "hs",
            DriftReason(kind="task_failed", detail="boom", severity="warning"),
        )
        assert ps.revisions
        entry = ps.revisions[-1]
        for key in ("revised_at", "kind", "detail", "severity", "reason", "drift_kind"):
            assert key in entry, f"missing {key}"
        assert entry["kind"] == "task_failed"
        assert entry["severity"] == "warning"

    def test_revision_reason_prefixed_with_kind(self):
        state = _AdkState(client=FakeClient(), planner=RecordingPlanner())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        state.refine_plan_on_drift(
            "hs",
            DriftReason(kind="plan_divergence", detail="A" * 300),
        )
        assert ps.plan.revision_reason.startswith("plan_divergence: ")
        # Detail is truncated to 200 chars.
        assert len(ps.plan.revision_reason) <= len("plan_divergence: ") + 200


# ---------------------------------------------------------------------------
# apply_drift_from_control helper
# ---------------------------------------------------------------------------


class TestApplyDriftFromControl:
    def test_fans_out_to_every_active_session(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        _mk_plan_state(state, hsession_id="hs_a")
        _mk_plan_state(state, hsession_id="hs_b")
        state.apply_drift_from_control(
            DriftReason(
                kind=DRIFT_KIND_USER_STEER,
                detail="steer",
                severity="warning",
                hint={"user_text": "refocus"},
            )
        )
        assert len(planner.refine_calls) == 2
        assert all(
            c["hint"] == {"user_text": "refocus"} for c in planner.refine_calls
        )

    def test_no_sessions_is_noop(self):
        state = _AdkState(client=FakeClient(), planner=RecordingPlanner())  # type: ignore[arg-type]
        state.apply_drift_from_control(
            DriftReason(kind="user_steer", detail="x")
        )  # must not raise

    def test_user_cancel_drift_cascades(self):
        planner = RecordingPlanner()
        state = _AdkState(client=FakeClient(), planner=planner)  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ps.tasks["t1"].status = "RUNNING"
        state.apply_drift_from_control(
            DriftReason(
                kind=DRIFT_KIND_USER_CANCEL,
                detail="cancel",
                severity="critical",
                recoverable=False,
            )
        )
        assert ps.tasks["t1"].status == "FAILED"
        assert ps.tasks["t2"].status == "CANCELLED"
        assert ps.tasks["t3"].status == "CANCELLED"
        # Unrecoverable → no planner.refine call.
        assert planner.refine_calls == []


# ---------------------------------------------------------------------------
# Monotonic guard still holds after cascade
# ---------------------------------------------------------------------------


class TestMonotonicAfterCascade:
    def test_terminal_tasks_are_not_recancelled(self):
        state = _AdkState(client=FakeClient(), planner=RecordingPlanner())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ps.tasks["t2"].status = "COMPLETED"
        ps.tasks["t1"].status = "RUNNING"
        state.refine_plan_on_drift(
            "hs",
            DriftReason(
                kind="user_cancel",
                detail="x",
                severity="critical",
                recoverable=False,
            ),
            current_task=ps.tasks["t1"],
        )
        assert ps.tasks["t1"].status == "FAILED"
        # Already-completed t2 stays COMPLETED, cascade moves past it.
        assert ps.tasks["t2"].status == "COMPLETED"
        assert ps.tasks["t3"].status == "CANCELLED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
