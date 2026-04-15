"""Harmonograf session.state protocol schema and helpers.

This module defines the namespaced keys harmonograf uses inside ADK's
``session.state`` dict to exchange context with agents, and provides
defensive read/write helpers around them. It is a pure data module with
no runtime dependency on adk.py, agent.py, or planner.py — everything
uses duck-typing and ``TYPE_CHECKING`` imports.

Two directions share the state channel:

* **Harmonograf -> Agents** — the orchestrator writes the active task,
  plan context, and reporting tool metadata before the agent's turn.
* **Agents -> Harmonograf** — the agent writes progress, outcomes, and
  optional notes via ADK ``state_delta`` events, which harmonograf reads
  after the turn.

Schema
------

All keys live under the ``harmonograf.`` prefix so the module can
round-trip a diff safely (see :func:`extract_agent_writes`) without
interfering with user-owned state keys. Any non-harmonograf key in the
state dict is ignored by this module.

**Harmonograf -> Agents** (written in ``before_model_callback``)

============================================  ==========  =============================================
Key                                           Type        Meaning
============================================  ==========  =============================================
``harmonograf.current_task_id``               ``str``     Active task id. Empty when no task is active.
``harmonograf.current_task_title``            ``str``     Human-readable title for the active task.
``harmonograf.current_task_description``      ``str``     Full description of the active task.
``harmonograf.current_task_assignee``         ``str``     Name of the sub-agent the task is assigned to.
``harmonograf.plan_id``                       ``str``     Id of the current plan the task belongs to.
``harmonograf.plan_summary``                  ``str``     Planner's short summary of the plan goal.
``harmonograf.available_tasks``               ``list``    List of ``{id, title, assignee, status, deps}``
                                                          dicts — one per task in the current plan.
``harmonograf.completed_task_results``        ``dict``    ``task_id -> summary`` from every completed
                                                          task, written via reporting tools.
``harmonograf.tools_available``               ``list``    Reporting tool names that are wired up for
                                                          this agent.
============================================  ==========  =============================================

**Agents -> Harmonograf** (written by the agent via ``state_delta``
events or via the reporting-tool interception in
``before_tool_callback``)

============================================  ==========  =============================================
Key                                           Type        Meaning
============================================  ==========  =============================================
``harmonograf.task_progress``                 ``dict``    ``task_id -> float`` — 0.0-1.0 progress hint.
                                                          Non-monotonic; last-write-wins.
``harmonograf.task_outcome``                  ``dict``    ``task_id -> summary`` — terminal outcomes
                                                          (completed / failed). Written by the reporting
                                                          tool interception path.
``harmonograf.agent_note``                    ``str``     Free-form latest note from the agent (approach,
                                                          blocker, reasoning). Surfaced in the Drawer.
``harmonograf.divergence_flag``               ``bool``    Agent set this True to say the whole plan is
                                                          stale. Paired with a ``report_plan_divergence``
                                                          call that fires the refine.
============================================  ==========  =============================================

Helper layout
-------------

* ``read_*`` — defensive readers for each key. Never raise on missing
  or malformed state; each returns a typed default (empty string,
  empty dict, ``False``, etc.) so callers don't need to None-check.
* ``write_current_task`` / ``clear_current_task`` / ``write_plan_context``
  / ``write_tools_available`` — structured writers the orchestrator
  uses to seed state before each model call. Each refuses to write
  a key that isn't under :data:`HARMONOGRAF_PREFIX`.
* :func:`extract_agent_writes` — diff two state snapshots and return
  only the ``harmonograf.*`` keys the agent added, changed, or
  removed. Used by ``on_event_callback`` and ``after_model_callback``
  to pick up agent-side state writes after the turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping, MutableMapping

if TYPE_CHECKING:
    from .planner import Plan, Task

HARMONOGRAF_PREFIX = "harmonograf."

# Harmonograf -> Agents
KEY_CURRENT_TASK_ID = "harmonograf.current_task_id"
KEY_CURRENT_TASK_TITLE = "harmonograf.current_task_title"
KEY_CURRENT_TASK_DESCRIPTION = "harmonograf.current_task_description"
KEY_CURRENT_TASK_ASSIGNEE = "harmonograf.current_task_assignee"
KEY_PLAN_ID = "harmonograf.plan_id"
KEY_PLAN_SUMMARY = "harmonograf.plan_summary"
KEY_COMPLETED_TASK_RESULTS = "harmonograf.completed_task_results"
KEY_AVAILABLE_TASKS = "harmonograf.available_tasks"
KEY_TOOLS_AVAILABLE = "harmonograf.tools_available"

# Agents -> Harmonograf
KEY_TASK_PROGRESS = "harmonograf.task_progress"
KEY_TASK_OUTCOME = "harmonograf.task_outcome"
KEY_AGENT_NOTE = "harmonograf.agent_note"
KEY_DIVERGENCE_FLAG = "harmonograf.divergence_flag"

_CURRENT_TASK_KEYS = (
    KEY_CURRENT_TASK_ID,
    KEY_CURRENT_TASK_TITLE,
    KEY_CURRENT_TASK_DESCRIPTION,
    KEY_CURRENT_TASK_ASSIGNEE,
)

ALL_KEYS: tuple[str, ...] = (
    KEY_CURRENT_TASK_ID,
    KEY_CURRENT_TASK_TITLE,
    KEY_CURRENT_TASK_DESCRIPTION,
    KEY_CURRENT_TASK_ASSIGNEE,
    KEY_PLAN_ID,
    KEY_PLAN_SUMMARY,
    KEY_COMPLETED_TASK_RESULTS,
    KEY_AVAILABLE_TASKS,
    KEY_TOOLS_AVAILABLE,
    KEY_TASK_PROGRESS,
    KEY_TASK_OUTCOME,
    KEY_AGENT_NOTE,
    KEY_DIVERGENCE_FLAG,
)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _safe_get(state: Any, key: str, default: Any = None) -> Any:
    if not isinstance(state, Mapping):
        return default
    try:
        value = state.get(key, default)
    except Exception:
        return default
    return value if value is not None else default


def _assert_harmonograf_key(key: str) -> None:
    if not key.startswith(HARMONOGRAF_PREFIX):
        raise ValueError(
            f"state_protocol refuses to write non-harmonograf key: {key!r}"
        )


def _set(state: MutableMapping[str, Any], key: str, value: Any) -> None:
    _assert_harmonograf_key(key)
    state[key] = value


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def read_current_task(state: Any) -> dict:
    """Return ``{id, title, description, assignee}`` for the active task.

    Missing or malformed state yields a dict with empty-string values so
    callers can render without None-checks.
    """
    return {
        "id": _safe_str(_safe_get(state, KEY_CURRENT_TASK_ID, "")),
        "title": _safe_str(_safe_get(state, KEY_CURRENT_TASK_TITLE, "")),
        "description": _safe_str(
            _safe_get(state, KEY_CURRENT_TASK_DESCRIPTION, "")
        ),
        "assignee": _safe_str(_safe_get(state, KEY_CURRENT_TASK_ASSIGNEE, "")),
    }


def read_plan_id(state: Any) -> str:
    return _safe_str(_safe_get(state, KEY_PLAN_ID, ""))


def read_plan_summary(state: Any) -> str:
    return _safe_str(_safe_get(state, KEY_PLAN_SUMMARY, ""))


def read_completed_results(state: Any) -> dict:
    """Return the map of ``task_id -> result summary`` for predecessors."""
    value = _safe_get(state, KEY_COMPLETED_TASK_RESULTS, None)
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if isinstance(k, str):
            out[k] = _safe_str(v)
    return out


def read_available_tasks(state: Any) -> list[dict]:
    value = _safe_get(state, KEY_AVAILABLE_TASKS, None)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def read_tools_available(state: Any) -> list[str]:
    value = _safe_get(state, KEY_TOOLS_AVAILABLE, None)
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def read_agent_outcome(state: Any, task_id: str) -> str:
    value = _safe_get(state, KEY_TASK_OUTCOME, None)
    if not isinstance(value, Mapping):
        return ""
    return _safe_str(value.get(task_id, ""))


def read_agent_progress(state: Any, task_id: str) -> float:
    value = _safe_get(state, KEY_TASK_PROGRESS, None)
    if not isinstance(value, Mapping):
        return 0.0
    raw = value.get(task_id, 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def read_agent_note(state: Any) -> str:
    return _safe_str(_safe_get(state, KEY_AGENT_NOTE, ""))


def read_divergence_flag(state: Any) -> bool:
    return bool(_safe_get(state, KEY_DIVERGENCE_FLAG, False))


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_current_task(state: MutableMapping[str, Any], task: Any) -> None:
    """Mutate state with current_task_* fields from a Task-like object.

    Accepts anything that duck-types to planner.Task (``.id``, ``.title``,
    ``.description``, ``.assignee_agent_id``) or a mapping with equivalent
    keys. ``None`` clears the current task.
    """
    if task is None:
        clear_current_task(state)
        return

    if isinstance(task, Mapping):
        tid = task.get("id", "")
        title = task.get("title", "")
        description = task.get("description", "")
        assignee = task.get("assignee") or task.get("assignee_agent_id", "")
    else:
        tid = getattr(task, "id", "")
        title = getattr(task, "title", "")
        description = getattr(task, "description", "")
        assignee = getattr(task, "assignee_agent_id", "") or getattr(
            task, "assignee", ""
        )

    _set(state, KEY_CURRENT_TASK_ID, _safe_str(tid))
    _set(state, KEY_CURRENT_TASK_TITLE, _safe_str(title))
    _set(state, KEY_CURRENT_TASK_DESCRIPTION, _safe_str(description))
    _set(state, KEY_CURRENT_TASK_ASSIGNEE, _safe_str(assignee))


def clear_current_task(state: MutableMapping[str, Any]) -> None:
    for key in _CURRENT_TASK_KEYS:
        if key in state:
            state.pop(key, None)


def write_plan_context(
    state: MutableMapping[str, Any],
    plan_state: Any,
    completed_results: Mapping[str, Any] | None,
    host_agent: str,
) -> None:
    """Write plan_id, summary, available_tasks, and completed_results.

    ``plan_state`` duck-types to planner.Plan — anything with ``.summary``
    and ``.tasks`` (plus optional ``.edges``) works. ``host_agent`` is the
    fallback assignee rendered into available_tasks when a task has none.
    """
    plan_id = ""
    summary = ""
    tasks_iter: Iterable[Any] = ()
    edges_iter: Iterable[Any] = ()

    if plan_state is not None:
        plan_id = _safe_str(
            getattr(plan_state, "id", None) or getattr(plan_state, "plan_id", "")
        )
        summary = _safe_str(getattr(plan_state, "summary", ""))
        tasks_iter = getattr(plan_state, "tasks", ()) or ()
        edges_iter = getattr(plan_state, "edges", ()) or ()

    deps_by_task: dict[str, list[str]] = {}
    for edge in edges_iter:
        src = _safe_str(getattr(edge, "from_task_id", ""))
        dst = _safe_str(getattr(edge, "to_task_id", ""))
        if not src or not dst:
            continue
        deps_by_task.setdefault(dst, []).append(src)

    available: list[dict] = []
    for task in tasks_iter:
        tid = _safe_str(getattr(task, "id", ""))
        available.append(
            {
                "id": tid,
                "title": _safe_str(getattr(task, "title", "")),
                "assignee": _safe_str(
                    getattr(task, "assignee_agent_id", "") or host_agent
                ),
                "status": _safe_str(getattr(task, "status", "PENDING")),
                "deps": list(deps_by_task.get(tid, [])),
            }
        )

    _set(state, KEY_PLAN_ID, plan_id)
    _set(state, KEY_PLAN_SUMMARY, summary)
    _set(state, KEY_AVAILABLE_TASKS, available)

    results_out: dict[str, str] = {}
    if isinstance(completed_results, Mapping):
        for k, v in completed_results.items():
            if isinstance(k, str):
                results_out[k] = _safe_str(v)
    _set(state, KEY_COMPLETED_TASK_RESULTS, results_out)


def write_tools_available(
    state: MutableMapping[str, Any], tool_names: Iterable[str]
) -> None:
    _set(
        state,
        KEY_TOOLS_AVAILABLE,
        [name for name in tool_names if isinstance(name, str)],
    )


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def extract_agent_writes(before: Any, after: Any) -> dict:
    """Return harmonograf.* keys the agent added, changed, or removed.

    Only keys under :data:`HARMONOGRAF_PREFIX` are considered. A removed
    key is represented as ``{key: None}`` so callers can distinguish it
    from an unset key. ``before``/``after`` may be any mapping; non-
    mapping inputs are treated as empty.
    """
    before_map: Mapping[str, Any] = before if isinstance(before, Mapping) else {}
    after_map: Mapping[str, Any] = after if isinstance(after, Mapping) else {}

    before_h = {
        k: v for k, v in before_map.items()
        if isinstance(k, str) and k.startswith(HARMONOGRAF_PREFIX)
    }
    after_h = {
        k: v for k, v in after_map.items()
        if isinstance(k, str) and k.startswith(HARMONOGRAF_PREFIX)
    }

    changes: dict[str, Any] = {}
    for key, value in after_h.items():
        if key not in before_h:
            changes[key] = value
        elif before_h[key] != value:
            changes[key] = value
    for key in before_h:
        if key not in after_h:
            changes[key] = None
    return changes
