"""Unit tests for harmonograf_client.planner."""

from __future__ import annotations

import json

import pytest

import importlib.util

from harmonograf_client.planner import (
    LLMPlanner,
    PassthroughPlanner,
    Plan,
    PlannerHelper,
    Task,
    TaskEdge,
    _strip_code_fences,
    make_default_adk_call_llm,
)


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None


def _valid_plan_json() -> str:
    return json.dumps(
        {
            "summary": "research and write",
            "tasks": [
                {
                    "id": "t1",
                    "title": "Research topic",
                    "description": "look up recent papers",
                    "assignee_agent_id": "researcher",
                },
                {
                    "id": "t2",
                    "title": "Draft report",
                    "assignee_agent_id": "writer",
                },
            ],
            "edges": [{"from_task_id": "t1", "to_task_id": "t2"}],
        }
    )


class TestPassthroughPlanner:
    def test_returns_none(self):
        p = PassthroughPlanner()
        assert p.generate(request="hello", available_agents=["a", "b"]) is None


class TestStripFences:
    def test_no_fence(self):
        assert _strip_code_fences("abc") == "abc"

    def test_json_fence(self):
        raw = "```json\n{\"a\":1}\n```"
        assert _strip_code_fences(raw).strip() == '{"a":1}'

    def test_plain_fence(self):
        raw = "```\n{\"a\":1}\n```"
        assert _strip_code_fences(raw).strip() == '{"a":1}'


class TestLLMPlanner:
    def test_parses_valid_json(self):
        calls: list[tuple[str, str, str]] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            calls.append((sys_p, user_p, model))
            return _valid_plan_json()

        p = LLMPlanner(call_llm=fake_llm, model="gpt-4o")
        plan = p.generate(
            request="research and write a report",
            available_agents=["researcher", "writer"],
        )
        assert plan is not None
        assert isinstance(plan, Plan)
        assert plan.summary == "research and write"
        assert [t.id for t in plan.tasks] == ["t1", "t2"]
        assert plan.tasks[0].title == "Research topic"
        assert plan.tasks[0].assignee_agent_id == "researcher"
        assert len(plan.edges) == 1
        assert plan.edges[0].from_task_id == "t1"
        assert plan.edges[0].to_task_id == "t2"
        # User prompt includes both the request and the agent list.
        assert "researcher" in calls[0][1]
        assert "research and write a report" in calls[0][1]
        assert calls[0][2] == "gpt-4o"

    def test_parses_fenced_json(self):
        def fake_llm(*args: str) -> str:
            return "```json\n" + _valid_plan_json() + "\n```"

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is not None
        assert len(plan.tasks) == 2

    def test_parses_plain_fenced_json(self):
        def fake_llm(*args: str) -> str:
            return "```\n" + _valid_plan_json() + "\n```"

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is not None

    def test_returns_none_on_invalid_json(self):
        def fake_llm(*args: str) -> str:
            return "not json at all {{"

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is None

    def test_returns_none_on_empty_string(self):
        def fake_llm(*args: str) -> str:
            return ""

        p = LLMPlanner(call_llm=fake_llm)
        assert p.generate(request="go", available_agents=["a"]) is None

    def test_returns_none_when_call_llm_raises(self):
        def fake_llm(*args: str) -> str:
            raise RuntimeError("network down")

        p = LLMPlanner(call_llm=fake_llm)
        # Should NOT propagate the exception.
        assert p.generate(request="go", available_agents=["a"]) is None

    def test_returns_none_when_json_has_no_tasks(self):
        def fake_llm(*args: str) -> str:
            return json.dumps({"summary": "x", "tasks": [], "edges": []})

        p = LLMPlanner(call_llm=fake_llm)
        assert p.generate(request="go", available_agents=["a"]) is None

    def test_tolerates_missing_edges(self):
        def fake_llm(*args: str) -> str:
            return json.dumps(
                {
                    "summary": "s",
                    "tasks": [
                        {"id": "t1", "title": "only", "assignee_agent_id": "a"}
                    ],
                }
            )

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is not None
        assert len(plan.tasks) == 1
        assert plan.edges == []

    def test_skips_malformed_task_entries(self):
        def fake_llm(*args: str) -> str:
            return json.dumps(
                {
                    "tasks": [
                        {"id": "", "title": "bad"},
                        {"id": "t1", "title": "ok"},
                    ]
                }
            )

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is not None
        assert [t.id for t in plan.tasks] == ["t1"]


class TestPlannerHelperIsAbstract:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            PlannerHelper()  # type: ignore[abstract]


class TestLLMPlannerRefine:
    def test_refine_happy_path(self):
        call_log: list[tuple[str, str, str]] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            call_log.append((sys_p, user_p, model))
            return json.dumps(
                {
                    "summary": "revised",
                    "tasks": [
                        {"id": "t1", "title": "Research", "assignee_agent_id": "researcher"},
                        {"id": "t3", "title": "Follow-up", "assignee_agent_id": "writer"},
                    ],
                    "edges": [{"from_task_id": "t1", "to_task_id": "t3"}],
                }
            )

        planner = LLMPlanner(call_llm=fake_llm, model="gpt-4o")
        original = Plan(
            tasks=[
                Task(id="t1", title="Research", assignee_agent_id="researcher"),
                Task(id="t2", title="Draft", assignee_agent_id="writer"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
            summary="original",
        )
        refined = planner.refine(
            original,
            {"kind": "tool_end", "tool_name": "search_web", "result_summary": "no results"},
        )
        assert refined is not None
        assert refined.summary == "revised"
        assert [t.id for t in refined.tasks] == ["t1", "t3"]
        # refine must have forwarded the event JSON into the user prompt
        assert call_log, "call_llm should have been invoked"
        assert "search_web" in call_log[0][1]
        assert "tool_end" in call_log[0][1]

    def test_refine_returns_none_on_invalid_json(self):
        def fake_llm(*args: str) -> str:
            return "not json"

        planner = LLMPlanner(call_llm=fake_llm)
        plan = Plan(tasks=[Task(id="t1", title="a")], edges=[])
        assert planner.refine(plan, {"kind": "noop"}) is None

    def test_refine_returns_none_when_call_llm_raises(self):
        def fake_llm(*args: str) -> str:
            raise RuntimeError("down")

        planner = LLMPlanner(call_llm=fake_llm)
        plan = Plan(tasks=[Task(id="t1", title="a")], edges=[])
        assert planner.refine(plan, {"kind": "x"}) is None

    def test_base_planner_refine_is_noop(self):
        class TrivialPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return None

        p = TrivialPlanner()
        plan = Plan(tasks=[Task(id="t1", title="a")], edges=[])
        assert p.refine(plan, {"kind": "tool_end"}) is None


class TestPlannerPrompts:
    def test_generate_prompt_instructs_full_end_to_end_plan(self):
        """The system prompt must steer the model toward a comprehensive
        upfront plan, not just a first step."""
        from harmonograf_client.planner import _DEFAULT_SYSTEM_PROMPT

        prompt = _DEFAULT_SYSTEM_PROMPT.lower()
        # Must mention end-to-end / full decomposition explicitly.
        assert "end-to-end" in prompt
        # Must explicitly signal a typical range (5-20).
        assert "5" in _DEFAULT_SYSTEM_PROMPT
        assert "20" in _DEFAULT_SYSTEM_PROMPT
        # Must mention the DAG / dependency concept.
        assert "dag" in prompt or "dependen" in prompt

    def test_refine_prompt_instructs_status_updates_and_additions(self):
        from harmonograf_client.planner import _REFINE_SYSTEM_PROMPT

        p = _REFINE_SYSTEM_PROMPT
        # Must mention every status value the adapter uses.
        for s in ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
            assert s in p, f"refine prompt missing status {s}"
        # Must explicitly say full plan (not delta).
        assert "complete" in p.lower() or "full" in p.lower()
        # Must tell the model it may add new tasks.
        assert "add" in p.lower()


class TestLLMPlannerComprehensivePlan:
    def test_parses_ten_task_plan(self):
        """A 10-task stubbed response should round-trip through
        LLMPlanner.generate without dropping tasks."""
        tasks = [
            {
                "id": f"t{i}",
                "title": f"task {i}",
                "assignee_agent_id": "worker",
            }
            for i in range(10)
        ]
        edges = [
            {"from_task_id": f"t{i}", "to_task_id": f"t{i + 1}"}
            for i in range(9)
        ]

        def fake_llm(*args: str) -> str:
            return json.dumps({"summary": "big", "tasks": tasks, "edges": edges})

        p = LLMPlanner(call_llm=fake_llm, model="stub")
        plan = p.generate(request="do a lot", available_agents=["worker"])
        assert plan is not None
        assert len(plan.tasks) == 10
        assert [t.id for t in plan.tasks] == [f"t{i}" for i in range(10)]
        assert len(plan.edges) == 9
        # Every task should default to PENDING when the model omits status.
        assert all(t.status == "PENDING" for t in plan.tasks)


class TestLLMPlannerRefineStatusAware:
    def test_refine_forwards_current_statuses_in_prompt(self):
        """The refine prompt must include each task's current status so
        the model can see what has run and what is still pending."""
        captured: list[str] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            captured.append(user_p)
            return json.dumps(
                {
                    "summary": "unchanged",
                    "tasks": [
                        {"id": "t1", "title": "a", "status": "COMPLETED"},
                        {"id": "t2", "title": "b", "status": "PENDING"},
                    ],
                    "edges": [],
                }
            )

        planner = LLMPlanner(call_llm=fake_llm)
        plan = Plan(
            tasks=[
                Task(id="t1", title="a", status="COMPLETED"),
                Task(id="t2", title="b", status="PENDING"),
            ],
            edges=[],
            summary="s",
        )
        refined = planner.refine(plan, {"kind": "tool_end", "tool_name": "x"})
        assert refined is not None
        assert captured, "call_llm should have fired"
        # Both statuses must appear in the serialised prompt.
        assert '"status": "COMPLETED"' in captured[0]
        assert '"status": "PENDING"' in captured[0]
        # Refined plan preserves statuses parsed from the response.
        by_id = {t.id: t.status for t in refined.tasks}
        assert by_id == {"t1": "COMPLETED", "t2": "PENDING"}

    def test_refine_parses_added_task(self):
        def fake_llm(*args: str) -> str:
            return json.dumps(
                {
                    "summary": "expanded",
                    "tasks": [
                        {"id": "t1", "title": "a", "status": "COMPLETED"},
                        {"id": "t2", "title": "b", "status": "PENDING"},
                        {"id": "t3", "title": "c", "status": "PENDING"},
                    ],
                    "edges": [{"from_task_id": "t2", "to_task_id": "t3"}],
                }
            )

        planner = LLMPlanner(call_llm=fake_llm)
        plan = Plan(
            tasks=[
                Task(id="t1", title="a", status="COMPLETED"),
                Task(id="t2", title="b", status="PENDING"),
            ],
            edges=[],
        )
        refined = planner.refine(plan, {"kind": "tool_end"})
        assert refined is not None
        assert [t.id for t in refined.tasks] == ["t1", "t2", "t3"]
        assert refined.tasks[0].status == "COMPLETED"
        assert refined.tasks[2].status == "PENDING"


class TestTaskAndEdgeDefaults:
    def test_task_defaults(self):
        t = Task(id="a", title="A")
        assert t.description == ""
        assert t.assignee_agent_id == ""
        assert t.predicted_start_ms == 0
        assert t.predicted_duration_ms == 0
        assert t.status == "PENDING"

    def test_plan_empty_defaults(self):
        p = Plan(tasks=[], edges=[])
        assert p.summary == ""
        assert p.revision_reason == ""

    def test_taskedge_fields(self):
        e = TaskEdge(from_task_id="a", to_task_id="b")
        assert e.from_task_id == "a"
        assert e.to_task_id == "b"


class TestLLMPlannerStatusParsing:
    def test_invalid_status_defaults_to_pending(self):
        def fake_llm(*args: str) -> str:
            return json.dumps(
                {
                    "tasks": [
                        {"id": "t1", "title": "A", "status": "BOGUS"},
                        {"id": "t2", "title": "B", "status": "running"},
                    ]
                }
            )

        p = LLMPlanner(call_llm=fake_llm)
        plan = p.generate(request="go", available_agents=["a"])
        assert plan is not None
        by_id = {t.id: t.status for t in plan.tasks}
        assert by_id == {"t1": "PENDING", "t2": "RUNNING"}

    def test_non_mapping_input_returns_none(self):
        def fake_llm(*args: str) -> str:
            return json.dumps([{"id": "t1", "title": "A"}])

        assert LLMPlanner(call_llm=fake_llm).generate(request="go", available_agents=[]) is None

    def test_non_list_tasks_returns_none(self):
        def fake_llm(*args: str) -> str:
            return json.dumps({"tasks": "not-a-list"})

        assert LLMPlanner(call_llm=fake_llm).generate(request="go", available_agents=[]) is None


class TestLLMPlannerPromptFidelity:
    def test_generate_user_prompt_lists_all_agents(self):
        captured: list[str] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            captured.append(user_p)
            return _valid_plan_json()

        p = LLMPlanner(call_llm=fake_llm, model="m")
        p.generate(request="do the thing", available_agents=["a1", "a2", "a3"])
        assert captured
        prompt = captured[0]
        for agent in ("a1", "a2", "a3"):
            assert agent in prompt
        assert "do the thing" in prompt
        assert "JSON" in prompt

    def test_generate_user_prompt_handles_empty_agent_list(self):
        captured: list[str] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            captured.append(user_p)
            return _valid_plan_json()

        p = LLMPlanner(call_llm=fake_llm)
        p.generate(request="go", available_agents=[])
        assert "(none listed)" in captured[0]

    def test_refine_user_prompt_contains_both_sections(self):
        captured: list[str] = []

        def fake_llm(sys_p: str, user_p: str, model: str) -> str:
            captured.append(user_p)
            return _valid_plan_json()

        planner = LLMPlanner(call_llm=fake_llm)
        plan = Plan(tasks=[Task(id="t1", title="A")], edges=[], summary="orig")
        planner.refine(plan, {"kind": "tool_end", "tool_name": "search"})
        prompt = captured[0]
        assert "Current plan" in prompt
        assert "Latest event" in prompt
        assert "search" in prompt
        assert "orig" in prompt

    def test_refine_returns_none_when_response_empty(self):
        def fake_llm(*args: str) -> str:
            return ""

        planner = LLMPlanner(call_llm=fake_llm)
        assert planner.refine(Plan(tasks=[Task(id="t1", title="a")], edges=[]), {"k": "v"}) is None


class TestCanonicalizePlanAssigneesFromPlanner:
    """Canonicalization lives in the adk module but operates on Plan
    objects produced by the planner, so we keep some cross-module
    coverage here."""

    def test_rewrites_assignee_variants(self):
        from harmonograf_client.adk import _canonicalize_plan_assignees

        plan = Plan(
            tasks=[
                Task(id="t1", title="A", assignee_agent_id="Research_Agent"),
                Task(id="t2", title="B", assignee_agent_id="writer"),
                Task(id="t3", title="C", assignee_agent_id=""),
            ],
            edges=[],
        )
        _canonicalize_plan_assignees(plan, ["research-agent", "writer"])
        assert plan.tasks[0].assignee_agent_id == "research-agent"
        assert plan.tasks[1].assignee_agent_id == "writer"
        # Empty backfills to first entry (no host provided).
        assert plan.tasks[2].assignee_agent_id == "research-agent"

    def test_preserves_unresolvable(self):
        from harmonograf_client.adk import _canonicalize_plan_assignees

        plan = Plan(
            tasks=[Task(id="t", title="A", assignee_agent_id="phantom")], edges=[]
        )
        _canonicalize_plan_assignees(plan, ["writer", "researcher"])
        assert plan.tasks[0].assignee_agent_id == "phantom"

    def test_nullable_assignees_tolerated(self):
        from harmonograf_client.adk import _canonicalize_plan_assignees

        # Task with None-ish fields shouldn't crash.
        plan = Plan(tasks=[Task(id="t", title="A")], edges=[])
        _canonicalize_plan_assignees(plan, ["writer"])
        assert plan.tasks[0].assignee_agent_id == "writer"


class TestDefaultAdkCallLlm:
    def test_returns_none_when_adk_unavailable(self):
        if _ADK_AVAILABLE:
            pytest.skip("ADK is importable; opposite branch tested elsewhere")
        assert make_default_adk_call_llm() is None

    def test_available_when_adk_importable(self):
        if not _ADK_AVAILABLE:
            pytest.skip("google.adk not installed")
        fn = make_default_adk_call_llm()
        assert fn is not None
        assert callable(fn)
        # Calling with empty model must raise a clear error (we don't
        # actually want to hit a live LLM in CI).
        with pytest.raises(ValueError):
            fn("sys", "user", "")
