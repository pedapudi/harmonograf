"""Harmonograf reporting tools for ADK agents.

These are the tools harmonograf injects into each sub-agent so the agent
can communicate task state explicitly rather than relying on heuristic
parsing of its natural-language output. Each tool is a thin shim: it
returns ``{"acknowledged": True}`` immediately, and the real side effect
happens in harmonograf's ``before_tool_callback`` which intercepts these
calls and routes them into ``_AdkState`` / ``session.state``.

The session.state key constants are imported from
:mod:`harmonograf_client.state_protocol` so this module and the
harmonograf callbacks agree on the schema.

Tool catalogue
--------------

The seven tools split into two groups:

**Task lifecycle** — drive the plan's per-task state machine. Agents
should call exactly one "terminal" tool (``completed`` / ``failed`` /
``blocked``) per task they touch, plus any number of non-terminal
``started`` / ``progress`` calls on the way.

* :func:`report_task_started` — marks the task RUNNING in
  ``_AdkState``, updates ``harmonograf.current_task_*`` in
  ``session.state``, and records the agent's approach note in
  ``harmonograf.agent_note``.
* :func:`report_task_progress` — optional mid-task ping. Writes
  ``harmonograf.task_progress[task_id] = fraction`` and updates
  ``harmonograf.agent_note``. No state transition. Used by the
  frontend liveness indicator and the stuck-task detector.
* :func:`report_task_completed` — marks the task COMPLETED; stores the
  summary in ``harmonograf.task_outcome[task_id]`` and propagates it
  into ``harmonograf.completed_task_results`` so downstream tasks see
  it as context. In parallel mode, unblocks dependent tasks in the
  DAG walker.
* :func:`report_task_failed` — marks the task FAILED and records the
  reason. Fires a refine with drift kind ``task_failed_recoverable``
  or ``task_failed_fatal`` depending on ``recoverable``.
* :func:`report_task_blocked` — keeps the task RUNNING but records
  the external blocker in ``harmonograf.agent_note``; may fire a
  refine with drift kind ``blocked`` if the blocker is structural.

**Plan mutation** — signal that the plan itself needs to change. Both
tools fire a refine call back through the planner and produce a
revised plan that flows through ``TaskRegistry.upsertPlan`` on the
frontend with a computed diff.

* :func:`report_new_work_discovered` — adds a child task under an
  existing parent. Drift kind ``new_work_discovered``.
* :func:`report_plan_divergence` — the whole plan no longer matches
  what needs to happen. Sets ``harmonograf.divergence_flag = True``
  and fires drift kind ``plan_divergence``.

See ``docs/reporting-tools.md`` for an agent-facing quick reference
and ``AGENTS.md`` (Plan execution protocol) for how these tools fit
into the larger callback protocol.
"""

from __future__ import annotations

from typing import Any, Optional

from .state_protocol import (
    HARMONOGRAF_PREFIX,
    KEY_AGENT_NOTE,
    KEY_CURRENT_TASK_ID,
    KEY_DIVERGENCE_FLAG,
    KEY_TASK_OUTCOME,
    KEY_TASK_PROGRESS,
)


_ACK: dict[str, Any] = {"acknowledged": True}


def report_task_started(task_id: str, detail: str = "") -> dict:
    """Report that you are beginning work on a planned task.

    Call this BEFORE doing the actual work so harmonograf knows which
    task is currently in progress. ``task_id`` must match the id of a
    task in the current plan (available via session state under
    ``harmonograf.current_task_id``). ``detail`` is an optional
    free-form note describing how you plan to approach the task.
    """
    return dict(_ACK)


def report_task_progress(
    task_id: str, fraction: float = 0.0, detail: str = ""
) -> dict:
    """Report mid-task progress on a planned task.

    Optional — only call this if the task has meaningful sub-steps
    (e.g. ``"found 3 of 5 sources"``). ``fraction`` is a 0.0-1.0 hint
    of how far through the task you are. ``detail`` is a short human
    description of what you just finished.
    """
    return dict(_ACK)


def report_task_completed(
    task_id: str, summary: str, artifacts: Optional[dict] = None
) -> dict:
    """Report that you have completed a planned task successfully.

    Call this AFTER producing the final output for the task.
    ``summary`` must describe the result in one or two sentences so
    downstream agents can use it as context. ``artifacts`` is an
    optional dict of named outputs (e.g. ``{"file": "output.html"}``)
    the task produced.
    """
    return dict(_ACK)


def report_task_failed(
    task_id: str, reason: str, recoverable: bool = True
) -> dict:
    """Report that you were unable to complete a planned task.

    Include the ``reason`` so the planner / coordinator knows whether
    to retry, reroute, or stop. ``recoverable=True`` means the plan
    can route around this failure; ``recoverable=False`` means the
    whole workflow should probably stop.
    """
    return dict(_ACK)


def report_task_blocked(
    task_id: str, blocker: str, needed: str = ""
) -> dict:
    """Report that you cannot currently proceed with a task.

    Use this when an external blocker (missing information, waiting
    for another agent, a human-in-the-loop input, etc.) prevents you
    from making progress. ``blocker`` describes what is in the way;
    ``needed`` optionally describes what would unblock you.
    """
    return dict(_ACK)


def report_new_work_discovered(
    parent_task_id: str,
    title: str,
    description: str,
    assignee: str = "",
) -> dict:
    """Report that you've discovered additional work the plan doesn't know about.

    Harmonograf will ask the planner to add this task as a child of
    ``parent_task_id``. ``title`` is a short imperative name for the
    new task; ``description`` describes what needs to be done.
    ``assignee`` optionally names the sub-agent that should handle it.
    """
    return dict(_ACK)


def report_plan_divergence(note: str, suggested_action: str = "") -> dict:
    """Report that the current plan no longer matches what needs to happen.

    Harmonograf will trigger an explicit replan. ``note`` describes
    why the plan is stale; ``suggested_action`` optionally hints at
    what the new plan should do instead.
    """
    return dict(_ACK)


REPORTING_TOOL_FUNCTIONS: tuple = (
    report_task_started,
    report_task_progress,
    report_task_completed,
    report_task_failed,
    report_task_blocked,
    report_new_work_discovered,
    report_plan_divergence,
)

REPORTING_TOOL_NAMES: tuple[str, ...] = tuple(
    fn.__name__ for fn in REPORTING_TOOL_FUNCTIONS
)


def build_reporting_function_tools() -> list:
    """Wrap each reporting function as a ``google.adk.tools.FunctionTool``.

    Raises ``ImportError`` if ADK is not installed.
    """
    from google.adk.tools import FunctionTool  # type: ignore[import-not-found]

    return [FunctionTool(fn) for fn in REPORTING_TOOL_FUNCTIONS]


SUB_AGENT_INSTRUCTION_APPENDIX = """

When you are working on a planned task:
- Call `report_task_started(task_id)` before beginning work
- Call `report_task_completed(task_id, summary=...)` after finishing
- If you discover additional work, call `report_new_work_discovered(parent_task_id=<current>, title=..., description=...)`
- If you fail, call `report_task_failed(task_id, reason=...)`
- If you are stuck on an external blocker, call `report_task_blocked(task_id, blocker=...)`
- The task_id will be provided in the session state under `harmonograf.current_task_id`.
"""


def augment_instruction(existing: str) -> str:
    """Return ``existing`` plus the harmonograf reporting appendix.

    Idempotent: if the appendix is already present, ``existing`` is
    returned unchanged.
    """
    base = existing or ""
    marker = "report_task_started(task_id)"
    if marker in base:
        return base
    return base.rstrip() + SUB_AGENT_INSTRUCTION_APPENDIX
