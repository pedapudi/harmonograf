"""Tests for harmonograf reporting tools + ADK registration helper.

Covers:

* Each reporting function returns ``{"acknowledged": True}``.
* ``augment_instruction`` appends the appendix without clobbering, and
  is idempotent.
* The ADK-level ``_register_harmonograf_reporting_tools`` helper walks
  ``sub_agents``, ``inner_agent``, and ``AgentTool``-wrapped agents,
  skips the root, and is idempotent across repeat calls.
"""

from __future__ import annotations

import pytest

from harmonograf_client import tools as hg_tools
from harmonograf_client.tools import (
    REPORTING_TOOL_FUNCTIONS,
    REPORTING_TOOL_NAMES,
    SUB_AGENT_INSTRUCTION_APPENDIX,
    augment_instruction,
    report_new_work_discovered,
    report_plan_divergence,
    report_task_blocked,
    report_task_completed,
    report_task_failed,
    report_task_progress,
    report_task_started,
)


# ---------------------------------------------------------------------------
# Per-tool return contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: report_task_started("t1"),
        lambda: report_task_started("t1", detail="about to start"),
        lambda: report_task_progress("t1", fraction=0.5, detail="half"),
        lambda: report_task_completed("t1", summary="done"),
        lambda: report_task_completed("t1", summary="done", artifacts={"f": "x"}),
        lambda: report_task_failed("t1", reason="broke"),
        lambda: report_task_failed("t1", reason="broke", recoverable=False),
        lambda: report_task_blocked("t1", blocker="no key"),
        lambda: report_task_blocked("t1", blocker="no key", needed="API key"),
        lambda: report_new_work_discovered("t1", title="do x", description="d"),
        lambda: report_new_work_discovered(
            "t1", title="do x", description="d", assignee="research_agent"
        ),
        lambda: report_plan_divergence(note="plan stale"),
        lambda: report_plan_divergence(note="plan stale", suggested_action="replan"),
    ],
)
def test_tool_returns_ack(call):
    assert call() == {"acknowledged": True}


def test_tool_return_is_fresh_copy():
    a = report_task_started("t1")
    a["mutated"] = True
    b = report_task_started("t2")
    assert b == {"acknowledged": True}


def test_tool_function_list_and_names_match():
    assert len(REPORTING_TOOL_FUNCTIONS) == 7
    assert set(REPORTING_TOOL_NAMES) == {
        "report_task_started",
        "report_task_progress",
        "report_task_completed",
        "report_task_failed",
        "report_task_blocked",
        "report_new_work_discovered",
        "report_plan_divergence",
    }


# ---------------------------------------------------------------------------
# Instruction augmentation
# ---------------------------------------------------------------------------


def test_augment_instruction_appends():
    base = "You are a researcher. Gather facts."
    out = augment_instruction(base)
    assert out.startswith(base)
    assert "report_task_started(task_id)" in out
    assert "harmonograf.current_task_id" in out


def test_augment_instruction_idempotent():
    base = "You are a researcher."
    once = augment_instruction(base)
    twice = augment_instruction(once)
    assert once == twice
    # Only one copy of the appendix ever present.
    assert once.count("report_task_started(task_id)") == 1


def test_augment_instruction_handles_empty():
    out = augment_instruction("")
    assert out.strip().startswith("When you are working")
    assert "report_task_started(task_id)" in out


def test_appendix_does_not_clobber_original_structure():
    base = "Line 1.\nLine 2.\nLine 3."
    out = augment_instruction(base)
    assert "Line 1." in out and "Line 2." in out and "Line 3." in out
    # Appendix starts on its own block.
    assert SUB_AGENT_INSTRUCTION_APPENDIX.strip() in out


# ---------------------------------------------------------------------------
# ADK tool registration — requires google.adk
# ---------------------------------------------------------------------------

pytest.importorskip("google.adk.agents")

from google.adk.agents import Agent  # noqa: E402
from google.adk.tools import AgentTool, FunctionTool  # noqa: E402

from harmonograf_client.adk import _resolve_default_planner  # noqa: E402,F401


def _build_demo_tree():
    """Return a tree shaped like presentation_agent: coordinator with
    AgentTool-wrapped sub-agents.
    """
    research = Agent(
        name="research",
        model="gemini-2.5-flash",
        instruction="research things",
        description="researcher",
        tools=[],
    )

    def dummy_tool(x: str) -> str:
        """Dummy."""
        return x

    web_dev = Agent(
        name="web_dev",
        model="gemini-2.5-flash",
        instruction="build sites",
        description="web dev",
        tools=[FunctionTool(dummy_tool)],
    )
    coordinator = Agent(
        name="coordinator",
        model="gemini-2.5-flash",
        instruction="coordinate",
        description="coordinator",
        tools=[AgentTool(research), AgentTool(web_dev)],
    )
    return coordinator, research, web_dev


def _get_register_helper():
    """The registration helper is defined inside ``make_adk_plugin`` via
    a closure. Rather than reach in through the closure, reconstruct an
    equivalent helper here that reuses the same walker by building a
    plugin and accessing the helper via the plugin module's __closure__.
    """
    # Simplest: build a plugin and pluck the helper from the local
    # namespace via the plugin's __init__ closure cells. The function
    # lives on the plugin's ``register_reporting_tools`` attr we expose
    # below in the adk module for testability.
    from harmonograf_client import adk as adk_mod

    return adk_mod._register_harmonograf_reporting_tools_for_test


def test_register_walks_agenttool_tree():
    coordinator, research, web_dev = _build_demo_tree()
    register = _get_register_helper()

    touched = register(coordinator)
    # coordinator + research + web_dev all get reporting tools: bare
    # LlmAgent roots (e.g., test_dynamic_plans_real_adk.py) need them
    # too, so the walker no longer skips the root agent.
    assert touched == 3

    research_names = {
        getattr(t, "name", None)
        or getattr(getattr(t, "func", None), "__name__", None)
        for t in research.tools
    }
    for expected in REPORTING_TOOL_NAMES:
        assert expected in research_names

    web_names = {
        getattr(t, "name", None)
        or getattr(getattr(t, "func", None), "__name__", None)
        for t in web_dev.tools
    }
    # Existing dummy tool preserved.
    assert "dummy_tool" in web_names
    for expected in REPORTING_TOOL_NAMES:
        assert expected in web_names


def test_register_is_idempotent():
    coordinator, research, web_dev = _build_demo_tree()
    register = _get_register_helper()

    register(coordinator)
    first_count_research = len(research.tools)
    first_count_web = len(web_dev.tools)

    touched_second = register(coordinator)
    assert touched_second == 0
    assert len(research.tools) == first_count_research
    assert len(web_dev.tools) == first_count_web


def test_register_augments_root_agent():
    coordinator, research, web_dev = _build_demo_tree()
    # The root LlmAgent also needs reporting tools — real-ADK tests
    # (test_dynamic_plans_real_adk.py) invoke a bare LlmAgent as the
    # runner target, so a root-skip would leave those runs unable to
    # resolve ``report_plan_divergence`` et al.
    register = _get_register_helper()
    register(coordinator)

    root_func_names = {
        getattr(getattr(t, "func", None), "__name__", None)
        for t in coordinator.tools
    }
    for expected in REPORTING_TOOL_NAMES:
        assert expected in root_func_names


def test_register_follows_inner_agent():
    """A HarmonografAgent-shaped wrapper (``inner_agent`` attr) should
    still have its inner tree augmented."""
    coordinator, research, web_dev = _build_demo_tree()

    class _Wrapper:
        inner_agent = coordinator
        sub_agents = ()
        tools = ()
        name = "wrapper"

    register = _get_register_helper()
    touched = register(_Wrapper())
    # Wrapper itself skipped (root) + coordinator skipped (no tools
    # entry needing reporting) — only the two leaf agents should be
    # augmented. Coordinator is NOT root (root is _Wrapper) so it
    # *would* get tools appended too — which is acceptable and
    # matches the spec ("walk sub_agents tree and append to each").
    assert touched >= 2
    research_funcs = {
        getattr(getattr(t, "func", None), "__name__", None)
        for t in research.tools
    }
    assert "report_task_started" in research_funcs
