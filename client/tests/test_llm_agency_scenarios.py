"""Real-ADK LLM agency regression tests (iter15 task #20).

These tests exercise the expanded drift taxonomy from task #6 against
**real** ``google.adk.events.Event`` + ``google.genai.types`` payloads,
so the detector is hit with the same object shapes a live
``InMemoryRunner`` would produce. They deliberately target the
``detect_drift`` / ``detect_semantic_drift`` / ``refine_plan_on_drift``
seam rather than the full plugin runner path — task #10 owns the
end-to-end InMemoryRunner scenarios in ``test_dynamic_plans_real_adk.py``
and these tests are the complementary unit-level regression net for the
agency-specific drift kinds.

Drift kinds covered (all from task #6):
    llm_refused, llm_merged_tasks, llm_split_task, llm_reordered_work,
    context_pressure, multiple_stamp_mismatches, user_steer, user_cancel,
    external_signal

Hermetic: no network, no real LLM. Skipped if google.adk / google.genai
are not installed.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Optional

import pytest

_ADK_AVAILABLE = (
    importlib.util.find_spec("google.adk") is not None
    and importlib.util.find_spec("google.genai") is not None
)

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk / google.genai not installed — run `make install`",
)

from harmonograf_client.adk import (  # noqa: E402
    DRIFT_KIND_CONTEXT_PRESSURE,
    DRIFT_KIND_COORDINATOR_EARLY_STOP,
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
from harmonograf_client.planner import (  # noqa: E402
    Plan,
    PlannerHelper,
    Task,
    TaskEdge,
)


# ---------------------------------------------------------------------------
# Minimal FakeClient (same surface as test_drift_taxonomy) — tests drive
# _AdkState directly, no real harmonograf server.
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counter = 0

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
        pass

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


# ---------------------------------------------------------------------------
# Real google.adk.events.Event builder
# ---------------------------------------------------------------------------


def _mk_event(
    *,
    author: str = "researcher",
    text: Optional[str] = None,
    finish_reason: Optional[str] = None,
    invocation_id: str = "inv-1",
    thought: bool = False,
) -> Any:
    """Construct a real ``google.adk.events.Event`` with optional text
    part and finish_reason. Uses real ``google.genai.types.Content`` /
    ``Part`` so the object shape matches what ``InMemoryRunner`` yields.
    """
    from google.adk.events import Event
    from google.genai import types

    content: Optional[Any] = None
    if text is not None:
        content = types.Content(
            role="model",
            parts=[types.Part(text=text, thought=thought)],
        )
    kwargs: dict[str, Any] = {
        "invocation_id": invocation_id,
        "author": author,
    }
    if content is not None:
        kwargs["content"] = content
    if finish_reason is not None:
        kwargs["finish_reason"] = finish_reason
    return Event(**kwargs)


def _mk_plan_state(
    state: _AdkState,
    *,
    hsession_id: str = "hs",
    task_ids: tuple[str, ...] = ("t1", "t2", "t3"),
    linear: bool = True,
) -> PlanState:
    tasks = [
        Task(id=tid, title=tid, assignee_agent_id="researcher", status="PENDING")
        for tid in task_ids
    ]
    edges: list[TaskEdge] = []
    if linear:
        for a, b in zip(task_ids, task_ids[1:]):
            edges.append(TaskEdge(from_task_id=a, to_task_id=b))
    plan = Plan(tasks=list(tasks), edges=edges, summary="plan")
    ps = PlanState(
        plan=plan,
        plan_id=f"plan-{hsession_id}",
        tasks={t.id: t for t in tasks},
        available_agents=["researcher"],
        generating_invocation_id="inv-1",
        remaining_for_fallback=list(tasks),
    )
    with state._lock:
        state._active_plan_by_session[hsession_id] = ps
    return ps


# ===========================================================================
# detect_drift seam — real google.adk Event objects
# ===========================================================================


class TestDetectDriftOnRealEvents:
    def test_llm_refused_marker_on_real_event(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(
            text=(
                "I'm unable to help with this request. "
                "It falls outside my capabilities."
            )
        )
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REFUSED
        assert drift.severity == "warning"
        assert "marker" in drift.hint
        assert "text" in drift.hint

    def test_llm_merged_tasks_marker_on_real_event(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(
            text=(
                "I'll combine the research and outline steps by "
                "consolidating tasks into a single response."
            )
        )
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_MERGED_TASKS
        assert drift.severity == "info"

    def test_llm_split_task_marker_on_real_event(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(
            text=(
                "This task needs to be broken into a structure design "
                "step and a styling step — splitting this task now."
            )
        )
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_SPLIT_TASK

    def test_llm_reordered_work_marker_on_real_event(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(
            text="Let me tackle t2 first — switching the order so t1 comes later."
        )
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REORDERED_WORK

    def test_thought_text_parts_are_ignored(self):
        """Refusal text on a ``thought=True`` part must NOT fire drift —
        those are the model's private thinking lane, not its answer.
        """
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(
            text="I'm unable to help with this privately thinking...",
            thought=True,
        )
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is None

    def test_context_pressure_from_max_tokens_enum(self):
        """``google.adk.events.Event.finish_reason`` is an enum
        (``FinishReason.MAX_TOKENS``) — the detector must match it,
        not the str form ``"FinishReason.MAX_TOKENS"``.
        """
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(finish_reason="MAX_TOKENS")
        # Sanity: the real Event stores an enum, not a bare string.
        assert hasattr(ev.finish_reason, "name")
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_CONTEXT_PRESSURE
        assert drift.severity == "warning"
        assert drift.hint.get("finish_reason") == "MAX_TOKENS"

    def test_context_pressure_non_gemini_string_finish_reason(self):
        """Non-Gemini providers may surface ``finish_reason`` as a bare
        string (e.g. OpenAI-style ``"length"``) rather than the genai
        enum — the detector must still normalize and match.
        """
        class _StrEvent:
            def __init__(self, fr: str) -> None:
                self.finish_reason = fr
                self.content = None
                self.actions = None
                self.status = ""
                self.id = "str-ev"
                self.author = "researcher"

        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        drift = state.detect_drift(
            [_StrEvent("LENGTH")], current_task=None, plan_state=ps
        )
        assert drift is not None
        assert drift.kind == DRIFT_KIND_CONTEXT_PRESSURE
        assert drift.hint.get("finish_reason") == "LENGTH"

    def test_clean_event_no_drift(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        ev = _mk_event(text="Here are three research sources I found.")
        drift = state.detect_drift([ev], current_task=None, plan_state=ps)
        assert drift is None


# ===========================================================================
# detect_semantic_drift — refusal / merge in end-of-turn summary
# ===========================================================================


class TestDetectSemanticDriftOnSummary:
    def test_llm_refused_in_result_summary(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        task = Task(id="t1", title="research", assignee_agent_id="researcher")
        drift = state.detect_semantic_drift(
            task,
            "I'm unable to help with this — it's outside my scope.",
            [],
        )
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_REFUSED

    def test_llm_merged_in_result_summary(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        task = Task(id="t1", title="research", assignee_agent_id="researcher")
        drift = state.detect_semantic_drift(
            task,
            "I'm combining the research and outline steps by consolidating tasks.",
            [],
        )
        assert drift is not None
        assert drift.kind == DRIFT_KIND_LLM_MERGED_TASKS


# ===========================================================================
# Multiple stamp mismatches — stateful counter threshold
# ===========================================================================


class TestMultipleStampMismatches:
    def test_below_threshold_no_drift(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        for _ in range(_STAMP_MISMATCH_THRESHOLD - 1):
            state.note_stamp_mismatch()
        drift = state.detect_drift([], current_task=None, plan_state=ps)
        assert drift is None

    def test_at_threshold_fires_drift(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        for _ in range(_STAMP_MISMATCH_THRESHOLD):
            state.note_stamp_mismatch()
        drift = state.detect_drift([], current_task=None, plan_state=ps)
        assert drift is not None
        assert drift.kind == DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES
        assert drift.severity == "warning"
        assert drift.hint.get("count") == _STAMP_MISMATCH_THRESHOLD

    def test_reset_clears_counter(self):
        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        ps = _mk_plan_state(state)
        for _ in range(_STAMP_MISMATCH_THRESHOLD):
            state.note_stamp_mismatch()
        state.reset_stamp_mismatches()
        drift = state.detect_drift([], current_task=None, plan_state=ps)
        assert drift is None


# ===========================================================================
# refine_plan_on_drift — hint propagation + severity surfacing
# ===========================================================================


class TestContextPressureRefine:
    def test_context_pressure_hint_reaches_planner(self):
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        _mk_plan_state(state, hsession_id="hs")
        drift = DriftReason(
            kind=DRIFT_KIND_CONTEXT_PRESSURE,
            detail="response truncated (MAX_TOKENS)",
            severity="warning",
            hint={"finish_reason": "MAX_TOKENS"},
        )
        state.refine_plan_on_drift("hs", drift)
        assert len(planner.refine_calls) == 1
        ev = planner.refine_calls[0]
        assert ev.get("hint", {}).get("finish_reason") == "MAX_TOKENS"
        assert ev.get("severity") == "warning"


# ===========================================================================
# apply_drift_from_control — external signal / user steer / user cancel
# ===========================================================================


class TestApplyDriftFromControl:
    def test_external_signal_fans_out_to_planner(self):
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        _mk_plan_state(state, hsession_id="hs")
        drift = DriftReason(
            kind="external_signal",
            detail="synthetic external trigger",
            severity="info",
            hint={"origin": "test"},
        )
        state.apply_drift_from_control(drift)
        assert len(planner.refine_calls) == 1
        assert planner.refine_calls[0]["kind"] == "external_signal"
        assert planner.refine_calls[0]["hint"].get("origin") == "test"

    def test_user_steer_is_warning_with_user_text_hint(self):
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        _mk_plan_state(state, hsession_id="hs")
        drift = DriftReason(
            kind=DRIFT_KIND_USER_STEER,
            detail="user steered",
            severity="warning",
            hint={"user_text": "focus on follow-ups"},
        )
        state.apply_drift_from_control(drift)
        assert len(planner.refine_calls) == 1
        ev = planner.refine_calls[0]
        assert ev["severity"] == "warning"
        assert ev["hint"]["user_text"] == "focus on follow-ups"
        # Revision stamped on the plan with the kind-prefixed reason.
        with state._lock:
            ps = state._active_plan_by_session["hs"]
        assert ps.plan.revision_reason.startswith("user_steer:")

    def test_user_cancel_is_critical_unrecoverable_and_cascades(self):
        """user_cancel is critical + unrecoverable — the current RUNNING
        task is FAILED and every downstream PENDING task cascades to
        CANCELLED. The planner is NOT called (unrecoverable skips refine).
        """
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        ps = _mk_plan_state(state, hsession_id="hs")
        ps.tasks["t1"].status = "RUNNING"
        drift = DriftReason(
            kind=DRIFT_KIND_USER_CANCEL,
            detail="user cancelled",
            severity="critical",
            recoverable=False,
        )
        state.apply_drift_from_control(drift)
        # Unrecoverable drift bypasses planner.refine entirely.
        assert planner.refine_calls == []
        assert ps.tasks["t1"].status == "FAILED"
        assert ps.tasks["t2"].status == "CANCELLED"
        assert ps.tasks["t3"].status == "CANCELLED"
        # Revision is still recorded with severity=critical.
        assert ps.revisions
        last = ps.revisions[-1]
        assert last["kind"] == DRIFT_KIND_USER_CANCEL
        assert last["severity"] == "critical"

    def test_no_active_sessions_noop(self):
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        # No plan registered — apply_drift_from_control must not raise.
        state.apply_drift_from_control(
            DriftReason(kind="external_signal", detail="none")
        )
        assert planner.refine_calls == []


# ===========================================================================
# Throttling regression — multiple_stamp_mismatches must not re-refine on
# every subsequent detect_drift call inside the throttle window.
# ===========================================================================


class TestStampMismatchRefineThrottled:
    def test_repeated_stamp_mismatch_refine_throttled(self):
        planner = RecordingPlanner()
        state = _AdkState(
            client=FakeClient(), planner=planner  # type: ignore[arg-type]
        )
        _mk_plan_state(state, hsession_id="hs")
        drift = DriftReason(
            kind=DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES,
            detail="3 rejections",
            severity="warning",
            hint={"count": 3},
        )
        state.refine_plan_on_drift("hs", drift)
        state.refine_plan_on_drift("hs", drift)
        state.refine_plan_on_drift("hs", drift)
        # Same kind + same session within throttle window → only the
        # first call reaches the planner.
        assert len(planner.refine_calls) == 1


# ===========================================================================
# Plugin after_model_callback drift wiring — the bare-LlmAgent path. Without
# this wiring, a refusal only fires a refine when HarmonografAgent's walker
# runs detect_drift at end-of-turn; runners that use a plain LlmAgent see
# no refine at all. The callback must (a) scan llm_response with
# detect_drift, (b) call refine_plan_on_drift on fire, and (c) be a no-op
# when no plan is registered for the session.
# ===========================================================================


class _FakeCallbackContext:
    """Minimal CallbackContext stand-in that ``_invocation_id_from_callback``
    and ``_route_from_callback_or_invocation`` can resolve. Carries a bare
    ``invocation_id`` and no ``_invocation_context``, so routing falls
    through to the pre-seeded ``state._invocation_route`` entry.
    """

    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id


class TestAfterModelCallbackDriftScan:
    def _build_plugin_with_plan(
        self, *, hsession_id: str = "hs-plugin", inv_id: str = "inv-plugin"
    ):
        pytest.importorskip("google.adk.plugins.base_plugin")
        from harmonograf_client.adk import make_adk_plugin

        planner = RecordingPlanner()
        client = FakeClient()
        plugin = make_adk_plugin(client, planner=planner)  # type: ignore[arg-type]
        state = plugin._hg_state
        ps = _mk_plan_state(state, hsession_id=hsession_id)
        with state._lock:
            state._invocation_route[inv_id] = ("researcher", hsession_id)
        return plugin, state, planner, ps, inv_id

    @pytest.mark.asyncio
    async def test_refusal_text_from_plugin_path_fires_refine(self):
        """A refusal response passed through ``after_model_callback`` must
        invoke ``refine_plan_on_drift`` even though no HarmonografAgent
        walker is in the stack — this is the regression net for bare
        ``LlmAgent`` users.
        """
        plugin, _state, planner, _ps, inv_id = self._build_plugin_with_plan()
        cc = _FakeCallbackContext(inv_id)
        resp = _mk_event(
            text=(
                "I'm unable to help with this request. "
                "It falls outside my capabilities."
            ),
            invocation_id=inv_id,
        )

        await plugin.after_model_callback(
            callback_context=cc, llm_response=resp
        )

        kinds = [c.get("kind") for c in planner.refine_calls]
        assert DRIFT_KIND_LLM_REFUSED in kinds, (
            f"expected llm_refused in {kinds}"
        )

    @pytest.mark.asyncio
    async def test_context_pressure_from_plugin_path_fires_refine(self):
        """A ``finish_reason=MAX_TOKENS`` response on the plugin path must
        also flow into ``refine_plan_on_drift`` — regression for the
        enum-name normalization inside ``detect_drift``.
        """
        plugin, _state, planner, _ps, inv_id = self._build_plugin_with_plan(
            hsession_id="hs-plugin-ctx", inv_id="inv-plugin-ctx"
        )
        cc = _FakeCallbackContext(inv_id)
        resp = _mk_event(
            text="partial answer and then truncate",
            finish_reason="MAX_TOKENS",
            invocation_id=inv_id,
        )

        await plugin.after_model_callback(
            callback_context=cc, llm_response=resp
        )

        kinds = [c.get("kind") for c in planner.refine_calls]
        assert DRIFT_KIND_CONTEXT_PRESSURE in kinds, (
            f"expected context_pressure in {kinds}"
        )

    @pytest.mark.asyncio
    async def test_no_plan_state_is_noop(self):
        """Bare-LlmAgent users without an active plan must not crash —
        the drift scan is guarded on plan_state existing for the session.
        """
        pytest.importorskip("google.adk.plugins.base_plugin")
        from harmonograf_client.adk import make_adk_plugin

        planner = RecordingPlanner()
        client = FakeClient()
        plugin = make_adk_plugin(client, planner=planner)  # type: ignore[arg-type]
        state = plugin._hg_state
        with state._lock:
            state._invocation_route["inv-noplan"] = ("researcher", "hs-noplan")
        cc = _FakeCallbackContext("inv-noplan")
        resp = _mk_event(
            text="I'm unable to help with this request.",
            invocation_id="inv-noplan",
        )

        await plugin.after_model_callback(
            callback_context=cc, llm_response=resp
        )

        assert planner.refine_calls == []


# ===========================================================================
# task #26: coordinator_early_stop mitigation on the sequential walker.
#
# Reproduces the iter15 demo bug where the LLM closes its turn after
# finishing 2 of 4 planned tasks (no drift, no error, no task_failed).
# The sequential walker now detects "progress made AND tasks still
# PENDING", fires a ``coordinator_early_stop`` DriftReason, and the
# retry loop re-prompts the inner agent until the remaining tasks run.
# ===========================================================================


class TestCoordinatorEarlyStopSequential:
    @pytest.mark.asyncio
    async def test_sequential_walker_fires_early_stop_and_completes_plan(self):
        """Inner agent finishes t1+t2 on the first pass then returns
        without touching t3+t4. The walker must fire
        ``coordinator_early_stop`` drift once and re-invoke the inner
        agent, at which point the second pass clears the remaining
        tasks.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")

        from typing import AsyncGenerator

        from google.adk.agents.base_agent import BaseAgent

        from harmonograf_client.agent import HarmonografAgent
        from harmonograf_client.planner import Task, TaskEdge

        from .test_agent import (
            FakeClient as WalkerFakeClient,
            FakeEvent,
            FakePlugin,
            _make_ctx,
            _seed_plan,
        )

        planner = RecordingPlanner()
        client = WalkerFakeClient()
        hg_state = _AdkState(client=client, planner=planner)  # type: ignore[arg-type]
        plugin = FakePlugin(hg_state)

        hsession_id = "hsess-inv-1"
        task_ids = ("t1", "t2", "t3", "t4")

        class ProgressingInner(BaseAgent):
            """Scripted inner agent that marks two tasks COMPLETED on
            each ``run_async`` call, simulating a coordinator that makes
            partial progress before closing its turn."""

            model_config = {"arbitrary_types_allowed": True}
            _state: Any
            _hsession_id: str
            _batches: list
            _call_log: list

            def __init__(self) -> None:
                super().__init__(name="coordinator")
                object.__setattr__(self, "_state", hg_state)
                object.__setattr__(self, "_hsession_id", hsession_id)
                object.__setattr__(self, "_batches", [["t1", "t2"], ["t3", "t4"]])
                object.__setattr__(self, "_call_log", [])

            @property
            def call_log(self) -> list:
                return self._call_log

            async def _run_async_impl(
                self, ctx: Any
            ) -> AsyncGenerator[Any, None]:
                self._call_log.append(ctx)
                idx = len(self._call_log) - 1
                batch = self._batches[idx] if idx < len(self._batches) else []
                with self._state._lock:
                    ps = self._state._active_plan_by_session.get(
                        self._hsession_id
                    )
                    if ps is not None:
                        for tid in batch:
                            tracked = ps.tasks.get(tid)
                            if tracked is not None:
                                tracked.status = "COMPLETED"
                yield FakeEvent("inv-1", f"pass-{idx}")

        inner = ProgressingInner()
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=client,
            planner=planner,
            enforce_plan=True,
            parallel_mode=False,
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)

        _seed_plan(
            hg_state,
            inv_id="inv-1",
            tasks=[
                Task(id=tid, title=tid, assignee_agent_id="coordinator")
                for tid in task_ids
            ],
            edges=[
                TaskEdge(from_task_id=a, to_task_id=b)
                for a, b in zip(task_ids, task_ids[1:])
            ],
            statuses={tid: "PENDING" for tid in task_ids},
        )

        # Force classifier sweep to be a no-op — the inner agent drives
        # task transitions directly, mirroring the real reporting-tool
        # callback path. Budget >= 1 so the retry loop can fire.
        hg_state.classify_and_sweep_running_tasks = (  # type: ignore[assignment]
            lambda hsession_id="", *, result_summary="", exclude=None: {}
        )
        hg_state.reinvocation_budget = lambda: 3  # type: ignore[assignment]

        _ = [ev async for ev in agent._run_async_impl(ctx)]

        # Inner ran at least twice — first pass + early-stop retry.
        assert len(inner.call_log) >= 2, (
            f"expected retry after early-stop, got {len(inner.call_log)} calls"
        )
        # coordinator_early_stop drift reached the planner exactly once.
        kinds = [c.get("kind") for c in planner.refine_calls]
        assert kinds.count(DRIFT_KIND_COORDINATOR_EARLY_STOP) == 1, (
            f"expected one coordinator_early_stop refine, got {kinds}"
        )
        # Pending-task hint was propagated on that refine call.
        early = next(
            c for c in planner.refine_calls
            if c.get("kind") == DRIFT_KIND_COORDINATOR_EARLY_STOP
        )
        pending = set(early.get("hint", {}).get("pending_task_ids") or [])
        assert pending == {"t3", "t4"}, f"unexpected pending hint: {pending}"
        # All four tasks ended COMPLETED.
        with hg_state._lock:
            ps = hg_state._active_plan_by_session[hsession_id]
        assert [ps.tasks[tid].status for tid in task_ids] == [
            "COMPLETED"
        ] * 4

    @pytest.mark.asyncio
    async def test_sequential_walker_skips_early_stop_without_progress(self):
        """If the inner agent returns without moving any task to a
        terminal state, early-stop must NOT fire — that's the zero-
        progress path, handled by the existing partial retry logic.
        Regression for the walker simplification tests that asserted a
        single inner call with a stub that never advances the protocol.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")

        from harmonograf_client.agent import HarmonografAgent
        from harmonograf_client.planner import Task

        from .test_agent import (
            FakeClient as WalkerFakeClient,
            FakeEvent,
            FakePlugin,
            StubInnerAgent,
            _make_ctx,
            _seed_plan,
        )

        planner = RecordingPlanner()
        client = WalkerFakeClient()
        hg_state = _AdkState(client=client, planner=planner)  # type: ignore[arg-type]
        plugin = FakePlugin(hg_state)

        inner = StubInnerAgent(
            name="coordinator",
            passes=[[FakeEvent("inv-1", "first-pass")]],
        )
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=client,
            planner=planner,
            enforce_plan=True,
            parallel_mode=False,
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)
        _seed_plan(
            hg_state,
            inv_id="inv-1",
            tasks=[
                Task(id="t1", title="a", assignee_agent_id="coordinator"),
                Task(id="t2", title="b", assignee_agent_id="coordinator"),
            ],
            edges=[],
            statuses={"t1": "PENDING", "t2": "PENDING"},
        )
        # Sweep is a no-op; no task ever reaches terminal state.
        hg_state.classify_and_sweep_running_tasks = (  # type: ignore[assignment]
            lambda hsession_id="", *, result_summary="", exclude=None: {}
        )
        hg_state.reinvocation_budget = lambda: 3  # type: ignore[assignment]

        _ = [ev async for ev in agent._run_async_impl(ctx)]

        # Inner called exactly once: zero-progress path must not retry.
        assert len(inner.call_log) == 1
        kinds = [c.get("kind") for c in planner.refine_calls]
        assert DRIFT_KIND_COORDINATOR_EARLY_STOP not in kinds
