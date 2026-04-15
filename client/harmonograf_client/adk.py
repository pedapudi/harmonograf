"""ADK adapter — one-line integration for google.adk Runner.

Usage::

    from harmonograf_client import Client, attach_adk
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(agent=...)
    client = Client(name="research-agent")
    handle = attach_adk(runner, client)

The adapter installs an ADK ``BasePlugin`` on the runner's
``plugin_manager`` and threads two responsibilities into each ADK
lifecycle callback:

1. **Telemetry** — translate the callback into harmonograf spans. This
   is the legacy observability path and still runs on every callback.
2. **Plan execution protocol** — read and write the ``harmonograf.*``
   keys in ``session.state`` via :mod:`harmonograf_client.state_protocol`
   and intercept reporting-tool calls from
   :mod:`harmonograf_client.tools` to drive task state. This is the
   primary source of truth for task progression — spans are *not* used
   to infer task state.

Callback layout
---------------

=====================================  ===================================================
ADK callback                            harmonograf behaviour
=====================================  ===================================================
``before_run_callback``                 start ``INVOCATION`` span; snapshot state
``after_run_callback``                  end the ``INVOCATION`` span
``before_model_callback``               start ``LLM_CALL`` span; write current task + plan
                                        context into ``session.state`` (see
                                        :func:`state_protocol.write_current_task` /
                                        :func:`state_protocol.write_plan_context`)
``after_model_callback``                end the ``LLM_CALL`` span; parse structured
                                        signals (function_calls, explicit markers,
                                        embedded state_delta writes) as a belt-and-
                                        suspenders state transition path for agents
                                        that don't call the reporting tools
``before_tool_callback``                start ``TOOL_CALL`` span; **intercept reporting
                                        tools** — if the tool name is in
                                        :data:`tools.REPORTING_TOOL_NAMES`, apply the
                                        resulting state transition into ``_AdkState``
                                        directly (the tool body itself returns an ack)
``after_tool_callback``                 end the ``TOOL_CALL`` span with result
``on_tool_error_callback``              end the ``TOOL_CALL`` span FAILED; fire a
                                        refine with drift kind ``tool_error``
``on_event_callback`` w/ transfer       emit ``TRANSFER`` span with INVOKED link;
                                        treat as a drift signal if the transfer
                                        target is not in the current plan
``on_event_callback`` w/ state_delta    attribute the span; diff harmonograf.* keys
                                        via :func:`state_protocol.extract_agent_writes`
                                        to pick up any agent-side state writes
=====================================  ===================================================

Long-running tools (``event.long_running_tool_ids``) mark the in-flight
TOOL_CALL as ``AWAITING_HUMAN`` — the subsequent ``after_tool_callback``
closes it once a response arrives.

Relationship to HarmonografAgent
--------------------------------

The adapter is split from :class:`harmonograf_client.agent.HarmonografAgent`
on purpose. The *plugin* installed here owns the plan-state and all of
the callback-driven protocol; the *agent* owns orchestration (which
sub-agent runs next, when to re-invoke, when to call the planner). An
App is expected to register both — the plugin in the runner's plugin
list and a ``HarmonografAgent`` as the root agent. They discover each
other through ``ctx.plugin_manager`` at invocation time, so callers
that only want telemetry can install the plugin without the agent and
callers that want to test orchestration in isolation can install the
agent without the plugin.

Capabilities advertised by ADK itself: ``HUMAN_IN_LOOP`` (long-running
tools) and ``STEERING`` (via injecting into session state). Callers who
want ``PAUSE_RESUME`` / ``REWIND`` need their own runner wrapper and
must advertise those flags on the ``Client`` themselves.

The adapter never imports ``google.adk`` at module load — only when
``attach_adk`` is called. That keeps the main ``harmonograf_client``
import free of the ADK dependency.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any, Mapping, MutableMapping, Optional

import os

from .client import Client
from .tools import (
    REPORTING_TOOL_NAMES,
    build_reporting_function_tools,
)
from .planner import (
    LLMPlanner,
    Plan,
    PlannerHelper,
    Task,
    TaskEdge,
    make_default_adk_call_llm,
)
from .transport import ControlAckSpec
from . import state_protocol as _sp
from .metrics import ProtocolMetrics, format_protocol_metrics

log = logging.getLogger("harmonograf_client.adk")


_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED"}
)

# Re-invocation budget: how many partial-progress turns we tolerate for a
# single task before declaring it FAILED. The walker increments a counter
# each time it re-runs the same task with a "continue" nudge; when the
# counter hits this cap the next sweep flips the task to FAILED with
# reason "reinvocation budget exhausted".
_REINVOCATION_BUDGET = 3

# Explicit completion / failure markers the prompt asks the LLM to emit.
# These are the PRIMARY signal — the heuristic markers below are fallback
# only. The regex tolerates "Task complete:", "Task complete -", "TASK
# FAILED.", etc.
_TASK_COMPLETE_MARKER = re.compile(
    r"task\s+complete\b[\s:.\-]*", re.IGNORECASE
)
_TASK_FAILED_MARKER = re.compile(
    r"task\s+failed\b[\s:.\-]*", re.IGNORECASE
)

# Heuristic substrings indicating the LLM is reporting failure. Matched
# case-insensitively over the result summary.
_FAILURE_HEURISTIC_MARKERS: tuple[str, ...] = (
    "i couldn't",
    "i could not",
    "i was unable",
    "unable to complete",
    "failed to ",
    "could not complete",
    "encountered an error",
    "error:",
    "exception:",
    "traceback",
)

# Heuristic substrings indicating partial / in-progress output.
_PARTIAL_HEURISTIC_MARKERS: tuple[str, ...] = (
    "in progress",
    "still working",
    "more to do",
    "to be continued",
    "continuing on",
    "next, i will",
    "i will continue",
)


def _register_harmonograf_reporting_tools_for_test(agent: Any) -> int:
    """Walk ``agent``'s sub-tree and append harmonograf reporting tools
    to each non-root agent's tool list (idempotent).

    The traversal follows three edges: ``sub_agents`` (native ADK tree),
    ``inner_agent`` (HarmonografAgent wrapper), and ``AgentTool.agent``
    (agents exposed to a parent as a tool — the shape used by the
    presentation_agent demo). Non-destructive: copies tuple tool lists
    before appending, and skips agents that already carry the
    reporting tools.

    Name ends in ``_for_test`` so tests can import the plain function
    without constructing a plugin closure — the production callsite
    lives in the plugin-scoped wrapper inside ``make_adk_plugin``.
    """
    if agent is None:
        return 0
    try:
        reporting_tools = build_reporting_function_tools()
    except Exception as exc:  # noqa: BLE001 — no ADK means nothing to do
        log.debug("reporting tool registration skipped: %s", exc)
        return 0

    touched = 0
    seen: set[int] = set()
    stack: list[Any] = [agent]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        children = list(getattr(cur, "sub_agents", None) or ())
        inner = getattr(cur, "inner_agent", None)
        if inner is not None:
            children.append(inner)
        for t in getattr(cur, "tools", None) or ():
            nested = getattr(t, "agent", None)
            if nested is not None:
                children.append(nested)
        for child in children:
            stack.append(child)
        existing = getattr(cur, "tools", None)
        if existing is None:
            continue
        existing_names = set()
        for t in existing:
            n = getattr(t, "name", None) or getattr(
                getattr(t, "func", None), "__name__", None
            )
            if n:
                existing_names.add(n)
        if any(n in existing_names for n in REPORTING_TOOL_NAMES):
            continue
        new_list = list(existing) + list(reporting_tools)
        try:
            cur.tools = new_list
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "could not augment tools on %s: %s",
                getattr(cur, "name", "?"), exc,
            )
            continue
        touched += 1
    if touched:
        log.info(
            "harmonograf: registered reporting tools on %d sub-agents", touched
        )
    return touched


_TRANSITION_COUNTER: Optional[dict[str, int]] = None


def _set_task_status(task: Any, new_status: str) -> bool:
    """Monotonic state-machine guard for plan task status writes.

    Allowed transitions: PENDING → RUNNING → (COMPLETED | FAILED | CANCELLED).
    Terminal states are absorbing — any attempt to transition out of one is
    rejected and logged at WARNING. Every code path that writes
    ``task.status`` MUST go through this helper so the cycle bug
    (COMPLETED → RUNNING) becomes structurally impossible.

    Side effect: on a successful (real) transition, bumps
    ``_TRANSITION_COUNTER[new_status]`` if one is registered via
    :func:`_install_transition_counter`. Same-status and rejected
    writes do NOT bump. This lets ``_AdkState`` account for every
    call site uniformly without threading metrics through signatures.
    """
    if task is None or not new_status:
        return False
    cur = getattr(task, "status", "") or ""
    if cur == new_status:
        return True
    if cur in _TERMINAL_TASK_STATUSES:
        log.warning(
            "REJECTED status transition %s → %s for task %s (already terminal)",
            cur, new_status, getattr(task, "id", "?"),
        )
        return False
    try:
        task.status = new_status
    except Exception as exc:  # noqa: BLE001
        log.debug("status write failed for task %s: %s", getattr(task, "id", "?"), exc)
        return False
    log.debug(
        "task %s status %s → %s",
        getattr(task, "id", "?"), cur or "PENDING", new_status,
    )
    counter = _TRANSITION_COUNTER
    if counter is not None:
        counter[new_status] = counter.get(new_status, 0) + 1
    return True


def _install_transition_counter(counter: Optional[dict[str, int]]) -> None:
    """Register (or clear) a dict that :func:`_set_task_status` will
    bump on every successful transition. Process-global — the last
    installer wins. ``_AdkState`` calls this with its metrics
    ``task_transitions`` defaultdict during ``__init__``.
    """
    global _TRANSITION_COUNTER
    _TRANSITION_COUNTER = counter


@dataclasses.dataclass
class PlanState:
    """Per-session plan state. One PlanState lives in
    ``_AdkState._active_plan_by_session`` under the harmonograf session id
    of the run that submitted the plan. All plan-related lookups — span
    stamping, next-task selection, guidance injection, refine — hit this
    dict rather than a tangle of invocation-id-keyed maps.
    """

    plan: Plan
    plan_id: str
    tasks: dict[str, Any]                  # task_id -> Task (mutable status)
    available_agents: list[str]
    generating_invocation_id: str          # the inv that submitted the plan
    remaining_for_fallback: list[Any]      # assignee-match queue (popped as stamps bind)
    revisions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    host_agent_name: str = ""              # the coordinator/host agent — never the default backfill

    @property
    def edges(self) -> list:
        return list(getattr(self.plan, "edges", []) or [])


# Task-local forced task id. Writes to this var are scoped to the
# current asyncio Task (so parallel within-stage runs don't clobber
# each other). Reads fall back to the shared attr on _AdkState when
# unset, preserving iter4 semantics for non-parallel callers.
_forced_task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hgraf_forced_task_id", default=None
)


@dataclasses.dataclass
class DriftReason:
    """Structured drift signal returned by :meth:`_AdkState.detect_drift`.

    ``kind`` is a short tag (e.g. ``"tool_call_wrong_agent"``),
    ``detail`` is a human-readable one-liner, ``severity`` gates log
    level + UI surfacing, ``recoverable=False`` tells
    :meth:`_AdkState.refine_plan_on_drift` to fail the current task and
    cascade CANCELLED to downstream tasks instead of calling the
    planner, and ``hint`` carries structured context the LLM refiner
    may use (e.g. ``{"user_text": "..."}`` for STEER).
    """

    kind: str
    detail: str
    event_id: str = ""
    severity: str = "info"  # one of: info, warning, critical
    recoverable: bool = True
    hint: dict = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# Drift taxonomy — helpers owned by the drift path.
# ---------------------------------------------------------------------------

# Canonical drift kind catalog — new kinds introduced in the expanded
# taxonomy live here so callers (tests, UI) can import the set.
DRIFT_KIND_LLM_REFUSED = "llm_refused"
DRIFT_KIND_LLM_MERGED_TASKS = "llm_merged_tasks"
DRIFT_KIND_LLM_SPLIT_TASK = "llm_split_task"
DRIFT_KIND_LLM_REORDERED_WORK = "llm_reordered_work"
DRIFT_KIND_CONTEXT_PRESSURE = "context_pressure"
DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES = "multiple_stamp_mismatches"
DRIFT_KIND_USER_STEER = "user_steer"
DRIFT_KIND_USER_CANCEL = "user_cancel"
DRIFT_KIND_TOOL_ERROR = "tool_error"
DRIFT_KIND_AGENT_ESCALATED = "agent_escalated"
DRIFT_KIND_AGENT_REPORTED_DIVERGENCE = "agent_reported_divergence"
DRIFT_KIND_UNEXPECTED_TRANSFER = "unexpected_transfer"
DRIFT_KIND_EXTERNAL_SIGNAL = "external_signal"
# Coordinator closed its turn while PENDING tasks remain. The sequential
# walker's partial-retry loop can't catch this because the classifier
# only scans RUNNING tasks — this drift fires instead.
DRIFT_KIND_COORDINATOR_EARLY_STOP = "coordinator_early_stop"

# Stamp-mismatch threshold: when the count of forced-task-id rejections
# (attempts to re-bind already-terminal tasks) crosses this threshold we
# raise a ``multiple_stamp_mismatches`` drift.
_STAMP_MISMATCH_THRESHOLD = 3

# Refine throttle (seconds) per drift kind. Recoverable drifts of the
# same kind fired within this window collapse into a single refine.
# Critical / unrecoverable drifts bypass the throttle.
_DRIFT_REFINE_THROTTLE_SECONDS = 2.0


# --- Drift marker substrings (refusal / merge / split / reorder) ------
#
# These run against LLM response text parts (detect_drift) and task
# result_summary strings (detect_semantic_drift). Matching is
# case-insensitive and anchored loosely — false positives are
# acceptable because ``refine_plan_on_drift`` is deferential (the
# planner may no-op).

_LLM_REFUSAL_MARKERS: tuple[str, ...] = (
    "i cannot",
    "i can't",
    "i won't",
    "i will not",
    "i'm unable",
    "i am unable",
    "i refuse",
    "i must decline",
    "i'm not able to",
    "i am not able to",
    "cannot assist",
    "can't help with",
)

_LLM_MERGE_MARKERS: tuple[str, ...] = (
    "merging tasks",
    "merged tasks",
    "combining task",
    "combining these task",
    "combined task",
    "fold into one task",
    "folding these task",
    "consolidating task",
    "consolidate task",
)

_LLM_SPLIT_MARKERS: tuple[str, ...] = (
    "splitting task",
    "splitting this task",
    "split this task",
    "breaking this task into",
    "break this task into",
    "divide this task",
    "subdividing task",
    "decompose this task",
)

_LLM_REORDER_MARKERS: tuple[str, ...] = (
    "reordering the plan",
    "out of order",
    "doing this first",
    "doing this task first",
    "switching the order",
    "tackling this before",
    "swap the order",
)

# Finish-reason values that indicate the response was clipped and the
# agent may not have produced a complete answer.
_CONTEXT_PRESSURE_FINISH_REASONS: frozenset[str] = frozenset(
    {
        "MAX_TOKENS",
        "LENGTH",
        "MAX_OUTPUT_TOKENS",
        "TRUNCATED",
        "CONTENT_FILTER",
    }
)


def _first_text_marker(lowered: str, markers: tuple[str, ...]) -> Optional[str]:
    """Return the first marker substring found in ``lowered`` (which
    must already be lowercased), or None.
    """
    for m in markers:
        if m in lowered:
            return m
    return None


# ---------------------------------------------------------------------------
# End drift taxonomy helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Model-callback helpers: state protocol + response signals.
# Owned by the model-callback path. Do not reuse from tool/event callbacks
# without coordinating.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ResponseSignals:
    """Structured view of an llm_response extracted in after_model_callback.

    ``function_calls`` lists the ``{name, args}`` function_call parts on
    the response; ``text_parts`` collects the non-thought text parts. The
    marker fields are set when a text part contains an explicit
    ``Task complete:`` / ``Task failed:`` line so the callback can route
    status transitions without re-parsing.
    """

    function_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    text_parts: list[str] = dataclasses.field(default_factory=list)
    has_task_complete_marker: bool = False
    has_task_failed_marker: bool = False
    marker_task_id: Optional[str] = None
    marker_reason: Optional[str] = None
    finish_reason: Optional[str] = None


_MODEL_TASK_COMPLETE_RE = re.compile(
    r"task\s+complete\s*[:\-]\s*([A-Za-z0-9_\-.]+)(?:\s*[-—–:]\s*(.*))?",
    re.IGNORECASE,
)
_MODEL_TASK_FAILED_RE = re.compile(
    r"task\s+failed\s*[:\-]\s*([A-Za-z0-9_\-.]+)(?:\s*[-—–:]\s*(.*))?",
    re.IGNORECASE,
)


def _observe_response_signals(llm_response: Any) -> ResponseSignals:
    """Extract a :class:`ResponseSignals` from an ADK ``LlmResponse``.

    Defensive against missing ``content`` / ``parts`` — returns an empty
    :class:`ResponseSignals` if either is absent. ``thought=True`` parts
    are skipped (they belong to the thinking-text lane).
    """
    sig = ResponseSignals()
    if llm_response is None:
        return sig
    finish_reason = _safe_attr(llm_response, "finish_reason", None)
    if finish_reason is not None:
        try:
            sig.finish_reason = str(finish_reason)
        except Exception:
            pass
    content = _safe_attr(llm_response, "content", None)
    if content is None:
        return sig
    parts = _safe_attr(content, "parts", None) or []
    for part in parts:
        fc = _safe_attr(part, "function_call", None)
        if fc is not None:
            name = _safe_attr(fc, "name", "") or ""
            args = _safe_attr(fc, "args", None)
            sig.function_calls.append({"name": str(name), "args": args})
            continue
        if _safe_attr(part, "thought", False):
            continue
        text = _safe_attr(part, "text", "") or ""
        if not text:
            continue
        sig.text_parts.append(text)
        m = _MODEL_TASK_COMPLETE_RE.search(text)
        if m:
            sig.has_task_complete_marker = True
            sig.marker_task_id = (m.group(1) or "").strip(".,;:")
            reason = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            sig.marker_reason = reason.strip() if reason else None
            continue
        m = _MODEL_TASK_FAILED_RE.search(text)
        if m:
            sig.has_task_failed_marker = True
            sig.marker_task_id = (m.group(1) or "").strip(".,;:")
            reason = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            sig.marker_reason = reason.strip() if reason else None
    return sig


def _snapshot_harmonograf_state(session_state: Any) -> dict[str, Any]:
    """Copy every ``harmonograf.*`` key out of ``session_state``.

    Non-mapping inputs return an empty dict so callers don't need a
    type check.
    """
    if not isinstance(session_state, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in session_state.items():
        if isinstance(k, str) and k.startswith(_sp.HARMONOGRAF_PREFIX):
            out[k] = v
    return out


def _completed_results_map(plan_state: Any) -> dict[str, str]:
    """Build the ``task_id -> summary`` map for predecessors of the
    currently active tasks. ``plan_state`` may be ``None``.
    """
    if plan_state is None:
        return {}
    tasks_by_id: dict[str, Any] = getattr(plan_state, "tasks", {}) or {}
    out: dict[str, str] = {}
    for tid, tracked in tasks_by_id.items():
        if (getattr(tracked, "status", "") or "") != "COMPLETED":
            continue
        title = getattr(tracked, "title", "") or ""
        out[str(tid)] = str(title)
    return out


def _pick_current_task_for_agent(
    plan_state: Any, agent_id: str, forced_task_id: str
) -> Optional[Any]:
    """Return the task the agent is about to work on, or ``None``.

    Priority:

    1. If ``forced_task_id`` is set AND that task's assignee matches
       ``agent_id`` (the walker's parallel-stage path), use it.
    2. Else, walk the plan and return the first PENDING or RUNNING task
       whose assignee matches and whose dependencies are satisfied.
    3. Otherwise ``None`` — the agent may be freewheeling, or the plan
       has no work for them.
    """
    if plan_state is None:
        return None
    tasks_by_id: dict[str, Any] = getattr(plan_state, "tasks", {}) or {}
    norm_agent = _normalize_agent_id(agent_id) if agent_id else ""

    if forced_task_id and forced_task_id in tasks_by_id:
        forced = tasks_by_id[forced_task_id]
        assignee = getattr(forced, "assignee_agent_id", "") or ""
        if not agent_id or assignee == agent_id or (
            norm_agent and _normalize_agent_id(assignee) == norm_agent
        ):
            return forced

    plan = getattr(plan_state, "plan", None)
    if plan is None:
        return None
    edges = getattr(plan_state, "edges", []) or []
    for t in getattr(plan, "tasks", []) or []:
        tid = getattr(t, "id", "") or ""
        if not tid:
            continue
        tracked = tasks_by_id.get(tid, t)
        status = (getattr(tracked, "status", "") or "PENDING")
        if status not in ("PENDING", "RUNNING"):
            continue
        assignee = getattr(tracked, "assignee_agent_id", "") or ""
        if agent_id and assignee != agent_id and (
            not norm_agent or _normalize_agent_id(assignee) != norm_agent
        ):
            continue
        # Deps satisfied?
        blocked = False
        for e in edges:
            if getattr(e, "to_task_id", "") == tid:
                dep = tasks_by_id.get(getattr(e, "from_task_id", ""))
                if dep is None:
                    continue
                if (getattr(dep, "status", "") or "") != "COMPLETED":
                    blocked = True
                    break
        if blocked:
            continue
        return tracked
    return None


def _expected_next_assignee(plan_state: Any) -> str:
    """Return the assignee of the plan's next dispatchable task.

    Walks the plan's task list in declaration order, skipping tasks in
    terminal states, and returns the first task whose dependencies are
    all COMPLETED. Used by the on_event transfer handler to compare a
    sub-agent transfer against the plan's intent.
    """
    if plan_state is None:
        return ""
    tasks_by_id: dict[str, Any] = getattr(plan_state, "tasks", {}) or {}
    edges = getattr(plan_state, "edges", []) or []
    plan = getattr(plan_state, "plan", None)
    ordered = list(getattr(plan, "tasks", None) or tasks_by_id.values())
    for t in ordered:
        tid = getattr(t, "id", "") or ""
        if not tid:
            continue
        tracked = tasks_by_id.get(tid, t)
        status = getattr(tracked, "status", "") or "PENDING"
        if status not in ("PENDING", "RUNNING"):
            continue
        blocked = False
        for e in edges:
            if getattr(e, "to_task_id", "") == tid:
                dep = tasks_by_id.get(getattr(e, "from_task_id", ""))
                if dep is None:
                    continue
                if (getattr(dep, "status", "") or "") != "COMPLETED":
                    blocked = True
                    break
        if blocked:
            continue
        return getattr(tracked, "assignee_agent_id", "") or ""
    return ""


def _hydrate_plan_state_from_session_state(
    state: "_AdkState",
    session_state: Mapping[str, Any],
    hsession_id: str,
    inv_id: str,
) -> Optional["PlanState"]:
    """Reconstruct a ``PlanState`` from ``harmonograf.*`` keys already
    present in ``session.state`` and install it into
    ``state._active_plan_by_session[hsession_id]``.

    This is the fallback path for runs where a plan was never produced
    by ``maybe_run_planner`` — e.g. tests that inject plan context
    directly into ``session.state`` via the state protocol, or a
    harmonograf session whose planner-generated PlanState was lost
    across process boundaries. Without hydration, every downstream
    callback-driven lookup (``_write_plan_context_to_session_state``,
    drift detection, state_delta outcome routing) would no-op.

    Returns the hydrated ``PlanState`` (already stored under the
    session lock) or ``None`` if session.state doesn't carry enough
    information to rebuild one.
    """
    if not isinstance(session_state, Mapping) or not hsession_id:
        return None
    plan_id = _sp.read_plan_id(session_state)
    available = _sp.read_available_tasks(session_state)
    if not plan_id or not available:
        return None

    tasks: list[Task] = []
    for item in available:
        tid = str(item.get("id", "") or "")
        if not tid:
            continue
        tasks.append(
            Task(
                id=tid,
                title=str(item.get("title", "") or ""),
                description=str(item.get("description", "") or ""),
                assignee_agent_id=str(item.get("assignee", "") or ""),
                status=str(item.get("status", "PENDING") or "PENDING"),
            )
        )
    if not tasks:
        return None

    tasks_by_id = {t.id: t for t in tasks}
    edges: list[TaskEdge] = []
    for item in available:
        tid = str(item.get("id", "") or "")
        if not tid:
            continue
        for dep in item.get("deps", []) or []:
            dep_id = str(dep or "")
            if dep_id and dep_id in tasks_by_id:
                edges.append(TaskEdge(from_task_id=dep_id, to_task_id=tid))

    plan = Plan(
        tasks=tasks,
        edges=edges,
        summary=_sp.read_plan_summary(session_state),
    )
    plan_state = PlanState(
        plan=plan,
        plan_id=plan_id,
        tasks=tasks_by_id,
        available_agents=sorted(
            {t.assignee_agent_id for t in tasks if t.assignee_agent_id}
        ),
        generating_invocation_id=inv_id or "",
        remaining_for_fallback=list(tasks),
    )
    with state._lock:
        state._active_plan_by_session[hsession_id] = plan_state
    log.info(
        "planner: hydrated PlanState from session.state plan_id=%s hsession=%s tasks=%d",
        plan_id, hsession_id, len(tasks),
    )
    return plan_state


def _write_plan_context_to_session_state(
    state: "_AdkState", cc: Any
) -> None:
    """Write plan + current-task context into ADK ``session.state`` and
    snapshot the harmonograf keys for later diffing.

    No-ops when the invocation has no callback context, no session, or
    no active plan. The snapshot is always recorded (even if empty) so
    :func:`_route_after_model_signals` has a stable before-picture.
    """
    inv_id = _invocation_id_from_callback(cc)
    ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(
        cc, "invocation_context", None
    )
    if ic is None:
        return
    session = _safe_attr(ic, "session", None)
    session_state = _safe_attr(session, "state", None)
    if not isinstance(session_state, MutableMapping):
        return
    agent_id, hsession_id = state._route_from_callback_or_invocation(cc, inv_id)
    with state._lock:
        plan_state = state._active_plan_by_session.get(hsession_id)
    # Hydration fallback: if we don't know this plan but session.state
    # carries a harmonograf.plan_id + available_tasks, reconstruct a
    # PlanState so downstream routing has something to bind to.
    if plan_state is None:
        plan_state = _hydrate_plan_state_from_session_state(
            state, session_state, hsession_id, inv_id
        )
    # Forced task id: prefer task-local var (walker parallel path), else
    # the shared _AdkState attribute.
    forced = _forced_task_id_var.get() or getattr(
        state, "_forced_current_task_id", ""
    ) or ""
    host_agent = (
        getattr(plan_state, "host_agent_name", "") if plan_state is not None
        else ""
    ) or agent_id or ""
    completed = _completed_results_map(plan_state)

    if plan_state is not None:
        # ``state_protocol.write_plan_context`` reads ``.id``/``.plan_id``,
        # ``.summary``, ``.tasks``, ``.edges`` off a single arg. Plan
        # carries task/edge/summary but no plan id, PlanState carries the
        # id but its ``.tasks`` is a dict. Build a minimal view so the
        # writer sees a uniform shape.
        plan_obj = getattr(plan_state, "plan", None)
        plan_view = SimpleNamespace(
            id=getattr(plan_state, "plan_id", ""),
            plan_id=getattr(plan_state, "plan_id", ""),
            summary=getattr(plan_obj, "summary", "") or "",
            tasks=list(getattr(plan_obj, "tasks", []) or []),
            edges=list(getattr(plan_obj, "edges", []) or []),
        )
        try:
            _sp.write_plan_context(
                session_state,
                plan_view,
                completed,
                host_agent,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("state_protocol.write_plan_context failed: %s", exc)

    current_task = _pick_current_task_for_agent(plan_state, agent_id, forced)
    try:
        _sp.write_current_task(session_state, current_task)
    except Exception as exc:  # noqa: BLE001
        log.debug("state_protocol.write_current_task failed: %s", exc)

    snapshot = _snapshot_harmonograf_state(session_state)
    state._metrics.state_state_reads += 1
    state._metrics.state_state_writes += 1
    with state._lock:
        state._state_snapshot_before[inv_id] = snapshot


def _route_after_model_signals(
    state: "_AdkState",
    cc: Any,
    signals: ResponseSignals,
) -> None:
    """Diff session.state against the pre-model snapshot and route any
    agent-reported outcome / divergence signals (plus text markers) to
    the task state machine and drift refine hooks.

    Harmless when there is no plan for the session, no snapshot, or no
    session.state on the callback — structural checks early-out quietly.
    """
    inv_id = _invocation_id_from_callback(cc)
    ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(
        cc, "invocation_context", None
    )
    if ic is None:
        return
    agent_id, hsession_id = state._route_from_callback_or_invocation(cc, inv_id)
    session = _safe_attr(ic, "session", None)
    session_state = _safe_attr(session, "state", None)
    with state._lock:
        before = state._state_snapshot_before.get(inv_id, {})
        plan_state = state._active_plan_by_session.get(hsession_id)

    writes: dict[str, Any] = {}
    if isinstance(session_state, Mapping):
        state._metrics.state_state_reads += 1
        try:
            writes = _sp.extract_agent_writes(before, session_state)
        except Exception as exc:  # noqa: BLE001
            log.debug("state_protocol.extract_agent_writes failed: %s", exc)
            writes = {}

    outcome_map = writes.get(_sp.KEY_TASK_OUTCOME)
    if isinstance(outcome_map, Mapping) and plan_state is not None:
        for tid, outcome in outcome_map.items():
            if not isinstance(tid, str):
                continue
            tracked = plan_state.tasks.get(tid)
            if tracked is None:
                continue
            prev = _sp._safe_get(before.get(_sp.KEY_TASK_OUTCOME, {}), tid, "")
            if prev == outcome:
                continue
            val = str(outcome or "").upper()
            if val == "COMPLETED":
                _set_task_status(tracked, "COMPLETED")
                log.info(
                    "model_callbacks: agent reported task %s COMPLETED via state_delta",
                    tid,
                )
            elif val == "FAILED":
                _set_task_status(tracked, "FAILED")
                log.info(
                    "model_callbacks: agent reported task %s FAILED via state_delta",
                    tid,
                )
                try:
                    state.refine_plan_on_drift(
                        hsession_id,
                        DriftReason(
                            kind="task_failed_by_agent",
                            detail=f"agent reported task {tid} failed via state_delta",
                        ),
                        current_task=tracked,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "refine_plan_on_drift (task_failed_by_agent) raised: %s",
                        exc,
                    )

    if bool(writes.get(_sp.KEY_DIVERGENCE_FLAG)):
        log.info(
            "model_callbacks: agent set harmonograf.divergence_flag — firing refine"
        )
        try:
            state.refine_plan_on_drift(
                hsession_id,
                DriftReason(
                    kind="agent_reported_divergence",
                    detail=_sp._safe_str(
                        _sp._safe_get(
                            session_state, _sp.KEY_AGENT_NOTE, ""
                        )
                    ) or "agent set harmonograf.divergence_flag",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("refine_plan_on_drift (divergence) raised: %s", exc)

    # Text markers — parsed from the response content. These are
    # complementary to state_delta writes: an agent that can't emit
    # state_delta events (e.g. pure-text turns) can still report
    # completion by including "Task complete: tN" in its final answer.
    if plan_state is not None and signals.marker_task_id:
        tracked = plan_state.tasks.get(signals.marker_task_id)
        if tracked is not None:
            if signals.has_task_complete_marker:
                if _set_task_status(tracked, "COMPLETED"):
                    log.info(
                        "model_callbacks: agent emitted 'Task complete: %s' text marker",
                        signals.marker_task_id,
                    )
            elif signals.has_task_failed_marker:
                if _set_task_status(tracked, "FAILED"):
                    log.info(
                        "model_callbacks: agent emitted 'Task failed: %s' text marker",
                        signals.marker_task_id,
                    )
                    try:
                        state.refine_plan_on_drift(
                            hsession_id,
                            DriftReason(
                                kind="task_failed_by_agent",
                                detail=(
                                    signals.marker_reason
                                    or f"Task failed: {signals.marker_task_id}"
                                ),
                            ),
                            current_task=tracked,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "refine_plan_on_drift (text marker) raised: %s", exc
                        )


# ---------------------------------------------------------------------------
# End model-callback helpers
# ---------------------------------------------------------------------------


class AdkAdapter:
    """Handle returned by :func:`attach_adk`. Holds the installed plugin
    and exposes :meth:`detach` for clean removal in tests.
    """

    def __init__(self, runner: Any, client: Client, plugin: Any) -> None:
        self._runner = runner
        self._client = client
        self._plugin = plugin

    @property
    def plugin(self) -> Any:
        return self._plugin

    def detach(self) -> None:
        try:
            plugins = self._runner.plugin_manager.plugins
            if self._plugin in plugins:
                plugins.remove(self._plugin)
        except Exception:
            pass

    async def run_async(self, **kwargs: Any):
        """Cancellable async-generator proxy for ``runner.run_async()``.

        Callers should use ``adapter.run_async(...)`` instead of
        ``runner.run_async(...)`` directly to get CANCEL / PAUSE / RESUME
        support from the Harmonograf UI.

        On CANCEL: raises ``asyncio.CancelledError`` after ending all
        in-flight spans as CANCELLED and repairing ADK session state.
        The caller receives the CancelledError and can decide to abort or
        re-run (with steering applied if desired).

        Example::

            handle = attach_adk(runner, client)
            async for event in handle.run_async(
                user_id=..., session_id=..., new_message=msg
            ):
                process(event)
        """
        loop = asyncio.get_event_loop()
        task = asyncio.current_task()

        _state: Optional["_AdkState"] = getattr(self._plugin, "_hg_state", None)
        if _state is not None:
            _state.register_running_task(task, loop)

        try:
            async for event in self._runner.run_async(**kwargs):
                yield event
        except asyncio.CancelledError:
            if _state is not None:
                _state._cleanup_cancelled_spans()
                _last_ic = getattr(_state, "_last_ic", None)
                if _last_ic is not None:
                    _repair_adk_session_after_cancel(_last_ic)
                # If cancelled due to STEER, immediately re-run the same invocation
                # so the steering instruction is injected at the very first
                # before_model_callback of the fresh run — guaranteed delivery.
                if _state.has_pending_steer():
                    if _last_ic is not None:
                        _repair_session_for_steer_rerun(_last_ic)
                    _state.register_running_task(asyncio.current_task(), loop)
                    try:
                        async for event in self._runner.run_async(**kwargs):
                            yield event
                        return  # Steer re-run completed normally — don't re-raise.
                    except asyncio.CancelledError:
                        _state._cleanup_cancelled_spans()
                        raise
                    finally:
                        _state.clear_running_task()
            raise
        finally:
            if _state is not None:
                _state.clear_running_task()


def _resolve_default_planner(
    planner: Any,
    planner_model: str,
) -> tuple[Optional[PlannerHelper], str]:
    """Apply opt-out / auto-wire semantics.

    - ``planner=False``: disable the planner entirely (returns ``None``).
    - ``planner`` is a :class:`PlannerHelper` instance: use it as-is.
    - ``planner is None``: construct an :class:`LLMPlanner` backed by
      ADK's default LLM client, if ADK is importable.  Otherwise
      returns ``None`` so the adapter runs without a planner.

    The model is resolved from (in order): explicit ``planner_model``
    kwarg, ``HARMONOGRAF_PLANNER_MODEL`` env var, or falls through empty
    so :meth:`_AdkState.maybe_run_planner` can inherit the host agent's
    model at invocation time.
    """
    if planner is False:
        return None, planner_model
    if isinstance(planner, PlannerHelper):
        return planner, planner_model
    resolved_model = planner_model or os.environ.get("HARMONOGRAF_PLANNER_MODEL", "")
    call_llm = make_default_adk_call_llm()
    if call_llm is None:
        log.debug(
            "make_adk_plugin: ADK not importable; running without a planner"
        )
        return None, resolved_model
    log.info(
        "make_adk_plugin: auto-wiring default LLMPlanner (model=%s)",
        resolved_model or "<host-agent default>",
    )
    return LLMPlanner(call_llm=call_llm, model=resolved_model), resolved_model


def make_adk_plugin(
    client: Client,
    *,
    planner: Any = None,
    planner_model: str = "",
    refine_on_events: bool = True,
) -> Any:
    """Build a harmonograf ADK ``BasePlugin`` bound to ``client``.

    This is the same plugin that :func:`attach_adk` installs, but
    returned standalone so callers who own an ADK ``App`` — for example
    the ``adk web`` CLI, which constructs its own ``Runner`` — can pass
    the plugin to the ``App(plugins=...)`` constructor and have it
    attached automatically. Also wires the STEER / INJECT_MESSAGE
    control handlers on ``client``.

    If ``planner`` is provided, the plugin will invoke it in
    ``before_run_callback`` with the user's request and the list of
    available agents, and forward any returned :class:`Plan` to
    :meth:`Client.submit_plan`. The planner's LLM model is resolved by
    the following fallback chain: explicit ``planner_model=`` kwarg →
    ``planner.model`` attribute (if an :class:`LLMPlanner`) → host
    agent's ``agent.model`` (discovered at invocation time) → whatever
    default the planner's own ``call_llm`` implements.
    """
    from google.adk.plugins.base_plugin import BasePlugin

    planner, planner_model = _resolve_default_planner(planner, planner_model)

    state: "_AdkState" = _AdkState(
        client=client,
        planner=planner,
        planner_model=planner_model,
        refine_on_events=refine_on_events,
    )

    # BEGIN_TOOLS_REGISTRATION
    def _register_harmonograf_reporting_tools(state: "_AdkState", agent: Any) -> int:
        """Plugin-scoped wrapper that delegates to the module-level walker.

        Kept here (inside ``make_adk_plugin``) and between the
        BEGIN_/END_TOOLS_REGISTRATION markers so other agents editing this
        module can locate the registration site. The actual implementation
        lives at module scope in ``_register_harmonograf_reporting_tools_for_test``
        so tests can import it without building a plugin closure.
        """
        return _register_harmonograf_reporting_tools_for_test(agent)
    # END_TOOLS_REGISTRATION

    class HarmonografAdkPlugin(BasePlugin):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(name="harmonograf")

        async def before_run_callback(self, *, invocation_context):
            # HarmonografAgent is a transparent orchestration wrapper. To
            # keep it invisible to telemetry without losing the invocation
            # span, substitute the inner agent so routing, session setup,
            # and the INVOCATION span land on the real coordinator row —
            # NOT a phantom "harmonograf" row. Plan submission is
            # deliberately skipped here: HarmonografAgent._run_async_impl
            # calls maybe_run_planner explicitly with host_agent=inner_agent,
            # which keeps plan submission at exactly-once per invocation.
            _root = _safe_attr(invocation_context, "agent", None)
            try:
                _register_harmonograf_reporting_tools(state, _root)
            except Exception as exc:  # noqa: BLE001
                log.debug("reporting-tool registration raised: %s", exc)
            if _is_harmonograf_agent_context(invocation_context):
                inner = _safe_attr(
                    _safe_attr(invocation_context, "agent", None),
                    "inner_agent",
                    None,
                )
                if inner is None:
                    return None
                state.on_invocation_start(
                    _IcWithSubstitutedAgent(invocation_context, inner)
                )
                return None
            state.on_invocation_start(invocation_context)
            # Planner hook: generate and submit a TaskPlan, if configured.
            try:
                state.maybe_run_planner(invocation_context)
            except Exception as exc:  # noqa: BLE001
                log.warning("planner hook raised; ignoring: %s", exc)
            return None

        async def after_run_callback(self, *, invocation_context):
            # on_invocation_end keys off invocation_id only, so passing
            # the raw IC is correct for both wrapped and unwrapped roots
            # — the state dicts were populated under the same inv_id.
            state.on_invocation_end(invocation_context)
            return None

        async def before_model_callback(self, *, callback_context, llm_request):
            if _is_harmonograf_agent_context(callback_context):
                return None
            # Self-register the current asyncio task so STEER(cancel) / CANCEL /
            # PAUSE controls can interrupt this LLM call even when the caller
            # drives runner.run_async() directly instead of adapter.run_async().
            # Re-registers at every model boundary so the handle stays fresh.
            _cur_task = asyncio.current_task()
            if _cur_task is not None:
                with state._task_lock:
                    if state._running_task is None or state._running_task.done():
                        state._running_task = _cur_task
                        state._adk_loop = asyncio.get_event_loop()
            # Honour PAUSE — block here until RESUME is received.
            evt = state._pause_event
            if evt is not None:
                await evt.wait()
            # Inject any pending STEER instruction as a user turn.
            steer_text = state.consume_pending_steer()
            if steer_text:
                _inject_steer_into_request(llm_request, steer_text)
                # Emit task_report so the UI confirms the steer was applied.
                inv_id = _invocation_id_from_callback(callback_context)
                state._emit_task_report(inv_id, f"Steering applied: {steer_text[:80]}")
            # Inject plan guidance so the model executes the plan the
            # planner produced instead of going off-script. Prefer a
            # one-shot pending guidance blob seeded at plan submit; else
            # compute fresh each call so task completions update the
            # "next task" hint in real time.
            state.inject_plan_guidance_if_any(callback_context, llm_request)
            # Write plan + current-task context into ADK session.state
            # via the harmonograf state protocol, and snapshot the
            # harmonograf.* keys so after_model_callback can diff the
            # agent's state_delta writes. Errors here are swallowed to
            # keep the LLM_CALL span lifecycle intact.
            try:
                _write_plan_context_to_session_state(state, callback_context)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "before_model_callback: state protocol write failed: %s",
                    exc,
                )
            state.on_model_start(callback_context, llm_request)
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            if _is_harmonograf_agent_context(callback_context):
                return None
            # Extract structured signals from the response (function
            # calls, text, markers, finish_reason) and route outcome /
            # divergence signals from session.state writes to the plan
            # state machine. Do this BEFORE ``state.on_model_end`` so
            # the LLM_CALL span close sees any status transitions the
            # agent implied.
            signals: Optional[ResponseSignals] = None
            try:
                signals = _observe_response_signals(llm_response)
                _route_after_model_signals(state, callback_context, signals)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "after_model_callback: signal routing failed: %s", exc
                )
            # Broader drift scan: state_protocol routing above only
            # fires refines for task_failed_by_agent and
            # agent_reported_divergence. For bare-LlmAgent runs that
            # aren't wrapped in HarmonografAgent (which calls
            # detect_drift itself), the plugin is the only place that
            # gets to see LLM agency markers — refusal / merge / split
            # / reorder — so scan llm_response here and fire a
            # deferential refine if one fires. Guarded on plan_state
            # existing: no plan → no drift.
            try:
                inv_id_for_drift = _invocation_id_from_callback(callback_context)
                _, hsession_for_drift = state._route_from_callback_or_invocation(
                    callback_context, inv_id_for_drift
                )
                with state._lock:
                    plan_state_for_drift = state._active_plan_by_session.get(
                        hsession_for_drift
                    )
                if plan_state_for_drift is not None:
                    current_task_for_drift = _pick_current_task_for_agent(
                        plan_state_for_drift,
                        state._route_from_callback_or_invocation(
                            callback_context, inv_id_for_drift
                        )[0],
                        _forced_task_id_var.get()
                        or getattr(state, "_forced_current_task_id", "")
                        or "",
                    )
                    drift = state.detect_drift(
                        [llm_response],
                        current_task=current_task_for_drift,
                        plan_state=plan_state_for_drift,
                    )
                    if drift is not None:
                        try:
                            state.refine_plan_on_drift(
                                hsession_for_drift,
                                drift,
                                current_task=current_task_for_drift,
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug(
                                "after_model_callback: refine_plan_on_drift(%s) raised: %s",
                                drift.kind, exc,
                            )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "after_model_callback: detect_drift scan failed: %s", exc
                )
            state.on_model_end(callback_context, llm_response)
            inv_id = _invocation_id_from_callback(callback_context)
            with state._lock:
                state._state_snapshot_before.pop(inv_id, None)
            return None

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            if _is_harmonograf_agent_context(tool_context):
                return None
            # Telemetry first: open the TOOL_CALL span via the existing
            # path so the reporting tool still appears in the timeline.
            state.on_tool_start(tool, tool_args, tool_context)
            tool_name = _safe_attr(tool, "name", "") or ""
            if tool_name in REPORTING_TOOL_NAMES:
                # Apply the reporting tool's side effect directly and
                # short-circuit ADK execution by returning a stub ACK.
                inv_id = _invocation_id_from_callback(tool_context)
                _agent_id, hsession_id = state._route_from_callback_or_invocation(
                    tool_context, inv_id
                )
                return state._dispatch_reporting_tool(
                    tool_name, tool_args or {}, hsession_id
                )
            return None

        async def after_tool_callback(
            self, *, tool, tool_args, tool_context, result
        ):
            if _is_harmonograf_agent_context(tool_context):
                return None
            tool_name = _safe_attr(tool, "name", "") or ""
            is_reporting = tool_name in REPORTING_TOOL_NAMES
            # Always close the TOOL_CALL span so telemetry stays balanced.
            state.on_tool_end(tool, tool_context, result=result, error=None)
            if is_reporting:
                # Side effect already applied in before_tool_callback.
                return None
            if _is_agent_tool(tool):
                # AgentTool result is a sub-agent's final text — capture
                # it as a result summary against whatever task is forced.
                summary = _stringify(result)[:600]
                if summary:
                    forced = (
                        _forced_task_id_var.get()
                        or state._forced_current_task_id
                        or ""
                    )
                    if forced:
                        with state._lock:
                            state._task_results.setdefault(forced, summary)
                return None
            # Regular tool: inspect the response for failure / unexpected
            # result shapes and fire a deferential refine if anything
            # looks off. The planner gets the final say.
            inv_id = _invocation_id_from_callback(tool_context)
            _agent_id, hsession_id = state._route_from_callback_or_invocation(
                tool_context, inv_id
            )
            drift = _classify_tool_response(tool_name, result)
            if drift is not None and hsession_id:
                try:
                    state.refine_plan_on_drift(hsession_id, drift)
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "refine_plan_on_drift(%s) raised: %s", drift.kind, exc
                    )
            return None

        async def on_tool_error_callback(
            self, *, tool, tool_args, tool_context, error
        ):
            if _is_harmonograf_agent_context(tool_context):
                return None
            tool_name = _safe_attr(tool, "name", "") or ""
            # Close the TOOL_CALL span FAILED via the existing telemetry
            # path; on_tool_end also marks the bound task FAILED.
            state.on_tool_end(tool, tool_context, result=None, error=error)
            if tool_name in REPORTING_TOOL_NAMES:
                return None
            inv_id = _invocation_id_from_callback(tool_context)
            _agent_id, hsession_id = state._route_from_callback_or_invocation(
                tool_context, inv_id
            )
            if hsession_id:
                try:
                    state.refine_plan_on_drift(
                        hsession_id,
                        DriftReason(
                            kind="tool_error",
                            detail=f"{tool_name}: {error}",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "refine_plan_on_drift(tool_error) raised: %s", exc
                    )
            return None

        async def on_event_callback(self, *, invocation_context, event):
            # Runner top-level IC always carries the root agent; when the
            # root is HarmonografAgent, substitute the inner agent so
            # TRANSFER spans and state-delta updates route to the real
            # coordinator row instead of a phantom "harmonograf" row.
            if _is_harmonograf_agent_context(invocation_context):
                inner = _safe_attr(
                    _safe_attr(invocation_context, "agent", None),
                    "inner_agent",
                    None,
                )
                if inner is None:
                    return None
                state.on_event(
                    _IcWithSubstitutedAgent(invocation_context, inner), event
                )
                return None
            state.on_event(invocation_context, event)
            return None

    plugin = HarmonografAdkPlugin()
    plugin._hg_state = state

    def _handle_steer(event: Any) -> ControlAckSpec:
        raw = event.payload.decode("utf-8", errors="replace") if event.payload else ""
        if not raw:
            return ControlAckSpec(result="success", detail="empty steer payload ignored")

        # Parse payload — try JSON (new format), fall back to plain text (legacy).
        try:
            parsed = json.loads(raw)
            body = parsed.get("text", raw) if isinstance(parsed, dict) else raw
            mode = parsed.get("mode", "cancel") if isinstance(parsed, dict) else "cancel"
        except (json.JSONDecodeError, ValueError):
            body = raw
            mode = "cancel"

        if not body:
            return ControlAckSpec(result="success", detail="empty steer text ignored")

        state.set_pending_steer(body)

        # Route the steer through the drift pipeline so planner.refine
        # can reshape the plan in light of the user's instruction
        # (deferential — the planner may no-op).
        try:
            state.apply_drift_from_control(
                DriftReason(
                    kind=DRIFT_KIND_USER_STEER,
                    detail=f"user steer: {body[:200]}",
                    severity="warning",
                    hint={"user_text": body, "mode": mode},
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("STEER apply_drift_from_control raised: %s", exc)

        if mode == "cancel":
            # Cancel current run so steer takes effect at very first model boundary of re-run.
            cancelled = state.cancel_running_task()
            if cancelled:
                log.info("STEER(cancel) queued, run cancelled for immediate re-run: %.80s", body)
                return ControlAckSpec(result="success", detail=f"steering queued, re-running: {body[:60]}")
            log.info("STEER(cancel) queued, no active run: %.80s", body)
            return ControlAckSpec(result="success", detail=f"steering queued: {body[:60]}")
        else:
            # Append mode: queue without cancelling — injected at next natural model boundary.
            log.info("STEER(append) queued for next model boundary: %.80s", body)
            return ControlAckSpec(result="success", detail=f"steering queued (append): {body[:60]}")

    def _handle_inject(event: Any) -> ControlAckSpec:
        # INJECT_MESSAGE injects a free-form message as the next user turn.
        # Not yet implemented for ADK; ack with failure so callers know.
        log.info("INJECT_MESSAGE received but not implemented for ADK adapter")
        return ControlAckSpec(result="failure", detail="INJECT_MESSAGE not implemented for ADK")

    def _handle_rewind_to(event: Any) -> ControlAckSpec:
        log.info("REWIND_TO received but not implemented for ADK adapter")
        return ControlAckSpec(result="failure", detail="REWIND_TO not implemented for ADK")

    def _handle_status_query(event: Any) -> ControlAckSpec:
        """Respond to STATUS_QUERY with a detailed description of current activity.

        The report combines: current activity (from the last heartbeat), in-flight
        tool calls (with their key arguments), any LLM streaming text, and the last
        user message for full context.
        """
        with state._lock:
            streaming_texts = dict(state._llm_streaming_text)
            thinking_texts = dict(state._llm_thinking_text)
            tool_labels = list(state._tool_labels.values())
            last_user_msg = state._last_user_message

        parts: list[str] = []

        # Current activity (updated at each lifecycle event and on LLM stream ticks).
        activity = client._current_activity
        if activity:
            parts.append(activity)

        # In-flight tool calls with their arguments.
        if tool_labels:
            calls = ", ".join(tool_labels[:3])
            suffix = f" (+{len(tool_labels) - 3} more)" if len(tool_labels) > 3 else ""
            parts.append(f"Tools in flight: {calls}{suffix}")

        # In-flight thinking text (most informative when model is reasoning).
        if thinking_texts:
            latest_thinking = max(thinking_texts.values(), key=len)
            if len(latest_thinking) > 10:
                snippet = latest_thinking[-150:].replace("\n", " ").strip()
                parts.append(f"Thinking: \u2026{snippet}")

        # In-flight response text (when model is generating output).
        if streaming_texts and not thinking_texts:
            latest_resp = max(streaming_texts.values(), key=len)
            if len(latest_resp) > 10:
                snippet = latest_resp[-120:].replace("\n", " ").strip()
                parts.append(f"Responding: \u2026{snippet}")

        # Last user message — gives full task context.
        if last_user_msg:
            truncated = last_user_msg[:120] + ("\u2026" if len(last_user_msg) > 120 else "")
            parts.append(f'Working on: \u201c{truncated}\u201d')

        report = " | ".join(parts) if parts else "No active task."

        # Emit as task_report on every active invocation span so the UI updates
        # reactively via the stream (correct agent_id, no polling needed).
        with state._lock:
            active_span_ids = list(state._invocations.values())
        for span_id in active_span_ids:
            client.emit_span_update(span_id, attributes={"task_report": report})

        return ControlAckSpec(result="success", detail=report)

    def _handle_cancel(event: Any) -> ControlAckSpec:
        cancelled = state.cancel_running_task()
        detail = "cancellation scheduled" if cancelled else "no active task"
        # CANCEL is unrecoverable — route through the drift pipeline
        # so the current task fails and downstream tasks cascade to
        # CANCELLED before the task returns its ack.
        try:
            state.apply_drift_from_control(
                DriftReason(
                    kind=DRIFT_KIND_USER_CANCEL,
                    detail=f"user cancel: {detail}",
                    severity="critical",
                    recoverable=False,
                    hint={"cancelled_running": bool(cancelled)},
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("CANCEL apply_drift_from_control raised: %s", exc)
        return ControlAckSpec(result="success", detail=detail)

    def _handle_pause(event: Any) -> ControlAckSpec:
        state.set_paused(True)
        return ControlAckSpec(result="success", detail="paused")

    def _handle_resume(event: Any) -> ControlAckSpec:
        state.set_paused(False)
        return ControlAckSpec(result="success", detail="resumed")

    client.on_control("STEER", _handle_steer)
    client.on_control("INJECT_MESSAGE", _handle_inject)
    client.on_control("REWIND_TO", _handle_rewind_to)
    client.on_control("STATUS_QUERY", _handle_status_query)
    client.on_control("CANCEL", _handle_cancel)
    client.on_control("PAUSE", _handle_pause)
    client.on_control("RESUME", _handle_resume)

    return plugin


def attach_adk(
    runner: Any,
    client: Client,
    *,
    planner: Any = None,
    planner_model: str = "",
    refine_on_events: bool = True,
) -> AdkAdapter:
    """Install a harmonograf plugin on the ADK runner and return the
    adapter handle. Equivalent to :func:`make_adk_plugin` followed by
    appending the plugin to ``runner.plugin_manager.plugins``.
    """
    plugin = make_adk_plugin(
        client,
        planner=planner,
        planner_model=planner_model,
        refine_on_events=refine_on_events,
    )
    try:
        runner.plugin_manager.plugins.append(plugin)
    except Exception as e:
        raise RuntimeError(f"attach_adk: could not install plugin on runner: {e}") from e
    return AdkAdapter(runner=runner, client=client, plugin=plugin)


class _AdkState:
    """Tracks in-flight spans so callbacks can close them at the right
    parent. All access is lock-guarded since ADK callbacks may fire from
    different asyncio tasks within one runner invocation.
    """

    def __init__(
        self,
        client: Client,
        *,
        planner: Optional[PlannerHelper] = None,
        planner_model: str = "",
        refine_on_events: bool = True,
    ) -> None:
        self._client = client
        self._planner = planner
        self._planner_model = planner_model
        self._refine_on_events = refine_on_events
        # Session-keyed plan storage. Populated by maybe_run_planner
        # after a successful submit_plan; cleared by on_invocation_end
        # when the generating invocation ends. All stamp / next-task /
        # guidance / refine paths read from this single source of truth
        # so sub-invocations launched via AgentTool — which have their
        # own invocation_ids but share the parent's harmonograf session
        # via the ContextVar — still find the plan.
        self._active_plan_by_session: dict[str, PlanState] = {}
        # span_id → task_id, so span-end callbacks can update task state
        # without re-parsing span attributes.
        self._span_to_task: dict[str, str] = {}
        self._lock = threading.Lock()
        # invocation_id → span_id (INVOCATION)
        self._invocations: dict[str, str] = {}
        # invocation_id → current LLM_CALL span_id
        self._llm_by_invocation: dict[str, str] = {}
        # tool call id → tool span id
        self._tools: dict[str, str] = {}
        # tool call id → human-readable label ("search_web(query='...')")
        self._tool_labels: dict[str, str] = {}
        # tool call id → long-running flag
        self._long_running: set[str] = set()
        # Last non-empty user message text (for STATUS_QUERY context).
        self._last_user_message: str = ""
        # invocation_id → (agent_id, session_id) the spans were emitted under,
        # so SpanEnd can re-route to the same row even if context routing fails.
        self._invocation_route: dict[str, tuple[str, str]] = {}
        # Session mutations queued by control handlers — surfaced via
        # pending_session_mutations() for agent code to apply.
        self._pending_mutations: list[tuple[str, str]] = []
        # Multi-session routing: ADK sub-runners (AgentTool) create fresh
        # ADK session ids per sub-invocation, but those should land in the
        # SAME harmonograf session as the enclosing root run. A PER-INSTANCE
        # ContextVar gives us two guarantees at once:
        #
        #   * Concurrent top-level /run calls live in independent asyncio
        #     Tasks whose context copies are independent — each sees an
        #     empty CV and mints its own harmonograf session.
        #   * AgentTool sub-runners execute inline (``await`` on the parent
        #     task) and naturally inherit the CV — their fresh ADK
        #     session id aliases back to the parent's harmonograf session.
        #
        # Per-instance (rather than module-level) means two different
        # ``_AdkState`` objects can't leak state into each other, which
        # matters both for tests that share a process and for future
        # callers who attach multiple plugins on one Client.
        self._adk_to_h_session: dict[str, str] = {}
        self._current_root_hsession_var: contextvars.ContextVar[str] = (
            contextvars.ContextVar(
                f"_harmonograf_current_root_hsession_{id(self)}", default=""
            )
        )
        # invocation_id → token from ContextVar.set(), so on_invocation_end
        # can reset the var in LIFO order.
        self._route_tokens: dict[str, Any] = {}
        # LLM span_id → cumulative streaming text length. Partial events bump
        # this so the frontend can render thinking tick marks on the in-flight
        # LLM block. Task #12 (B4 liveness).
        self._llm_stream_len: dict[str, int] = {}
        # LLM span_id → partial-event counter, also used as a monotonic
        # progress pulse so renderers can pulse/tick even when the partial
        # text has no natural length (e.g. tool-call streaming).
        self._llm_stream_ticks: dict[str, int] = {}
        # LLM span_id → accumulated streaming response text (thought=False parts).
        self._llm_streaming_text: dict[str, str] = {}
        # LLM span_id → accumulated streaming thinking text (thought=True parts).
        self._llm_thinking_text: dict[str, str] = {}
        # LLM span_id → parent invocation span_id (for live task_report during thinking).
        self._llm_to_invocation: dict[str, str] = {}
        # LLM span_id → length of thinking text at last task_report emit (rate-limits flooding).
        self._last_thinking_emit_len: dict[str, int] = {}
        # agent_id → cumulative invocation count for that agent (iteration attribute).
        self._invocation_count: dict[str, int] = {}
        # Interrupt infrastructure — task tracking for cancel/pause/resume
        self._running_task: Optional[asyncio.Task] = None
        self._adk_loop: Optional[asyncio.AbstractEventLoop] = None
        self._task_lock = threading.Lock()
        # Pause gate: None = not paused; set to an asyncio.Event on the ADK loop
        # when paused. before_model_callback awaits it. Cleared on RESUME.
        self._pause_event: Optional[asyncio.Event] = None
        # Last known invocation context — used for session repair after cancel.
        self._last_ic: Any = None
        # Pending STEER text: injected as a user turn at the next model call boundary.
        self._pending_steer: Optional[str] = None
        # One-shot pending plan-guidance slot: seeded in maybe_run_planner right
        # after submit_plan so the very first before_model_callback sees the
        # plan before the general _compute_plan_guidance_text path kicks in.
        self._pending_plan_guidance: Optional[str] = None
        # Last plan-guidance text injected into an LLM request, per agent.
        # Purely a de-dup key so repeat injections across rapid-fire model
        # calls don't emit duplicate task_reports to the UI.
        self._last_injected_plan_guidance: dict[str, str] = {}
        # Plan snapshot preserved after on_invocation_end. HarmonografRunner
        # reads this post-run to decide whether to re-invoke with a nudge
        # when the agent stopped while tasks remain. Keyed by invocation_id.
        self._plan_snapshot_for_inv: dict[str, tuple[Any, dict[str, Any]]] = {}
        # Most-recently RUNNING task on the active plan, tracked so the
        # frontend has a single "what is the agent working on right now?"
        # view that outlives any particular span lifetime. Updated by
        # _stamp_attrs_with_task the moment a span binds a task.
        self._current_task_id: str = ""
        self._current_task_title: str = ""
        self._current_task_description: str = ""
        self._current_task_agent_id: str = ""
        # Forced task id path: owned by HarmonografAgent. When set, every
        # span stamped by _stamp_attrs_with_task binds to this task id
        # regardless of the assignee heuristic. Cleared by the owner
        # (HarmonografAgent) when its inner-agent step completes, via
        # mark_forced_task_completed(). This makes the agent's plan
        # bookkeeping the authoritative source of "what task is this
        # span for?" — the old heuristic (matching span.agent_id to
        # task.assignee_agent_id) is preserved as a fallback for
        # invocations that don't route through HarmonografAgent.
        self._forced_current_task_id: str = ""
        # LLM span_id → accumulated llm.thought text. Kept in-memory so we
        # can concatenate streaming thought chunks into a single attribute
        # update without re-reading from the wire.
        self._llm_thought_emit: dict[str, str] = {}
        # Re-invocation tracking: task_id → number of partial-progress
        # re-invocations the walker has scheduled so far. Reset to 0 on
        # any terminal transition. The classifier flips a task to FAILED
        # once the counter exceeds ``_REINVOCATION_BUDGET``. Iter13 #7.
        self._task_reinvocation_count: dict[str, int] = {}
        # Task ids that have had a tool error (FAILED span) bound to them
        # since the last classify-and-sweep. Populated by ``on_tool_end``
        # when ``error`` is non-None and the span was bound to a task.
        # Drained per-task by the classifier so a turn-end sweep can
        # tell "this RUNNING task hit a tool error during this turn".
        self._recent_error_task_ids: set[str] = set()
        # Reporting-tool side-effect storage. Populated by
        # before_tool_callback when sub-agents call the harmonograf
        # report_task_* tools; read by status_query / UI surfaces.
        self._task_progress: dict[str, float] = {}
        self._task_results: dict[str, str] = {}
        self._task_blockers: dict[str, str] = {}
        self._task_artifacts: dict[str, dict[str, Any]] = {}
        # Drift refine throttling: (hsession_id, kind) -> time.monotonic() at
        # last successful refine. Recoverable drifts within the throttle
        # window are collapsed. Critical drifts bypass.
        self._last_refine_by_kind: dict[tuple[str, str], float] = {}
        # Count of forced-task stamp rejections (re-bind attempts on
        # already-terminal tasks). Crossing _STAMP_MISMATCH_THRESHOLD
        # raises a ``multiple_stamp_mismatches`` drift.
        self._stamp_mismatch_count: int = 0
        # invocation_id → snapshot of harmonograf.* session.state keys
        # taken in before_model_callback. after_model_callback reads this
        # to diff the agent's state_delta writes via state_protocol.
        # Owned by the model-callback path.
        self._state_snapshot_before: dict[str, dict[str, Any]] = {}
        # Lightweight protocol counters — see metrics.ProtocolMetrics.
        # Hot-path increments only; reads via get_protocol_metrics().
        self._metrics: ProtocolMetrics = ProtocolMetrics()
        _install_transition_counter(self._metrics.task_transitions)
        # Plan singleton guarantee is now provided implicitly by
        # _active_plan_by_session (one PlanState per hsession_id); the
        # old _plan_generated_for_session / _root_invocation_by_session
        # dicts are no longer needed.

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_protocol_metrics(self) -> ProtocolMetrics:
        return self._metrics

    def format_protocol_metrics(self) -> str:
        return format_protocol_metrics(self._metrics)

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def on_invocation_start(self, ic: Any) -> None:
        self._metrics.callbacks_fired["on_invocation_start"] += 1
        self._last_ic = ic
        inv_id = _safe_attr(ic, "invocation_id", "")
        # Detect top-level vs. nested: if the root-hsession ContextVar
        # is already set in this asyncio Task, we're nested under an
        # outer invocation (AgentTool sub-run) — preserve its plan.
        parent_hsession_in_ctx = self._current_root_hsession_var.get()
        is_top_level = not parent_hsession_in_ctx
        agent_id, hsession_id = self._route_from_context(ic, opening_root=True)
        log.debug(
            "on_invocation_start inv_id=%s agent=%s adk_session=%s resolved_hsession=%s top_level=%s",
            inv_id, agent_id or "",
            _safe_attr(getattr(ic, "session", None), "id", ""),
            hsession_id, is_top_level,
        )
        if hsession_id:
            # Supersession: a new TOP-LEVEL invocation landing on a
            # harmonograf session that still carries a PlanState from a
            # prior, already-ended invocation clears it so the planner
            # can submit a fresh plan for this new turn. on_invocation_end
            # intentionally does NOT pop (so post-drive assertions and
            # STEER / CANCEL handlers still see the plan); the pop lives
            # here instead, gated on "is this a fresh top-level turn and
            # is the stored plan stale (from a different, non-current
            # invocation)?". Nested sub-runs hit is_top_level=False and
            # leave the plan intact.
            if is_top_level:
                with self._lock:
                    stored = self._active_plan_by_session.get(hsession_id)
                    if (
                        stored is not None
                        and stored.generating_invocation_id
                        and stored.generating_invocation_id != inv_id
                        and stored.generating_invocation_id not in self._invocations
                    ):
                        self._active_plan_by_session.pop(hsession_id, None)
                        self._span_to_task.clear()
                        self._pending_plan_guidance = None
                        self._last_injected_plan_guidance.clear()
                        log.info(
                            "planner: superseded stale PlanState for session %s (new top-level inv %s replaces generating inv %s)",
                            hsession_id, inv_id, stored.generating_invocation_id,
                        )
            token = self._current_root_hsession_var.set(hsession_id)
            with self._lock:
                self._route_tokens[inv_id] = token
        name = agent_id or "agent"
        # Track per-agent invocation count for the "iteration" attribute.
        with self._lock:
            iteration = self._invocation_count.get(name, 0) + 1
            self._invocation_count[name] = iteration
        attrs: dict[str, Any] = {
            "invocation_id": inv_id,
            "user_id": _safe_attr(getattr(ic, "user_id", None), "__str__", "") or str(_safe_attr(ic, "user_id", "")),
            "iteration": iteration,
        }
        adk_session_id = _safe_attr(getattr(ic, "session", None), "id", "")
        if adk_session_id:
            attrs["adk_session_id"] = adk_session_id
        # Emit agent description and class if available from the ADK Agent object.
        agent = _safe_attr(ic, "agent", None)
        agent_desc = _safe_attr(agent, "description", "") if agent is not None else ""
        if agent_desc:
            attrs["agent_description"] = str(agent_desc)
        if agent is not None:
            agent_class = type(agent).__name__
            if agent_class and agent_class not in ("NoneType", "object"):
                attrs["agent_class"] = agent_class
        self._client.set_current_activity(f"Starting invocation of {name}")
        attrs["task_report"] = f"Waiting for model — {name}"
        span_id = self._client.emit_span_start(
            kind="INVOCATION",
            name=name,
            attributes=attrs,
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        with self._lock:
            self._invocations[inv_id] = span_id
            self._invocation_route[inv_id] = (agent_id, hsession_id)

    def maybe_run_planner(self, ic: Any, host_agent: Any = None) -> None:
        """If a :class:`PlannerHelper` is configured, extract the user's
        request from the invocation context, gather available agents,
        call ``planner.generate``, and submit the returned plan.

        ``host_agent`` defaults to ``ic.agent`` but may be overridden —
        :class:`HarmonografAgent` passes its ``inner_agent`` so the
        planner sees the *wrapped* agent's sub_agents / tools as the
        available-agent set rather than the HarmonografAgent wrapper
        itself (which only has the inner agent as a sub_agent).

        Never raises into ADK: all errors are logged and swallowed.
        """
        # When the outer agent is a HarmonografAgent and the caller did
        # NOT pass an override, skip: HarmonografAgent._run_async_impl is
        # responsible for calling back into this method with host_agent
        # set to its inner_agent. This prevents double-planning.
        if host_agent is None and getattr(
            _safe_attr(ic, "agent", None), "_is_harmonograf_agent", False
        ):
            log.debug(
                "maybe_run_planner: SKIP reason=is_harmonograf_agent_without_host inv_id=%s",
                _safe_attr(ic, "invocation_id", ""),
            )
            return
        planner = self._planner
        if planner is None:
            log.debug(
                "maybe_run_planner: SKIP reason=no_planner inv_id=%s",
                _safe_attr(ic, "invocation_id", ""),
            )
            return
        inv_id = _safe_attr(ic, "invocation_id", "")
        if not inv_id:
            return
        # One plan per harmonograf session: sub-agent invocations
        # (AgentTool / transfer) fire their own before_run_callback under
        # a nested invocation_id, but share the parent's hsession via
        # _current_root_hsession_var — so any plan already submitted
        # under that hsession short-circuits the planner call here.
        with self._lock:
            _, _hsession_for_guard = self._invocation_route.get(inv_id, ("", ""))
            if (
                _hsession_for_guard
                and _hsession_for_guard in self._active_plan_by_session
            ):
                log.debug(
                    "planner: skipping — plan already submitted for session %s (inv %s)",
                    _hsession_for_guard,
                    inv_id,
                )
                return
        request = _extract_user_request_from_ic(ic)
        if not request:
            log.debug("planner: no user request found on invocation %s", inv_id)
            return
        effective_host = host_agent if host_agent is not None else _safe_attr(ic, "agent", None)
        available = _collect_available_agents_for(effective_host)

        # If the planner is an LLMPlanner with no model set, try to
        # inherit from the host agent. We mutate a copy of the attr —
        # the planner is shared across invocations, so we pass the
        # resolved model only indirectly via the context dict (the
        # planner may also honour its own default).
        context: dict[str, Any] = {}
        resolved_model = self._planner_model
        if not resolved_model:
            # If the planner exposes .model, fall back to that.
            resolved_model = getattr(planner, "model", "") or ""
        if not resolved_model:
            host_model = _safe_attr(effective_host, "model", "")
            if host_model:
                resolved_model = str(host_model)
        if resolved_model:
            context["model"] = resolved_model
            # Push the resolved model onto the LLMPlanner if it has one
            # and it was empty at construction time — so subsequent
            # invocations don't have to re-resolve. Best-effort only.
            try:
                if getattr(planner, "_model", None) == "":
                    planner._model = resolved_model  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            plan = planner.generate(
                request=request,
                available_agents=available,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("planner.generate raised; skipping plan: %s", exc)
            return
        if plan is None or not getattr(plan, "tasks", None):
            log.debug("planner: no plan produced for invocation %s", inv_id)
            return

        # Canonicalize assignees against the known available_agents list
        # before submit: LLM planners routinely hallucinate formatting
        # variations ("research-agent", "Research_Agent", "research")
        # that don't exactly match ic.agent.name, which silently breaks
        # the assignee-fallback heuristic in _stamp_attrs_with_task.
        host_name_for_plan = str(_safe_attr(effective_host, "name", "") or "")
        _canonicalize_plan_assignees(plan, available, host_name_for_plan)

        invocation_span_id = self._get_invocation_span(inv_id) or ""
        with self._lock:
            _, hsession_id = self._invocation_route.get(inv_id, ("", ""))
        try:
            plan_id = self._client.submit_plan(
                plan,
                invocation_span_id=invocation_span_id,
                session_id=hsession_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("client.submit_plan raised; ignoring: %s", exc)
            return
        tasks_by_id: dict[str, Any] = {}
        for _t in plan.tasks:
            _tid = getattr(_t, "id", "") or ""
            if not _tid:
                continue
            # Initial status seeding for a fresh plan: PENDING is
            # always allowed because no prior status exists. Bypass the
            # monotonic guard here only — once tracked, all writes go
            # through _set_task_status.
            _t.status = "PENDING"
            tasks_by_id[_tid] = _t
        plan_state = PlanState(
            plan=plan,
            plan_id=plan_id,
            tasks=tasks_by_id,
            available_agents=list(available),
            generating_invocation_id=inv_id,
            remaining_for_fallback=list(plan.tasks),
            host_agent_name=host_name_for_plan,
        )
        with self._lock:
            if hsession_id:
                self._active_plan_by_session[hsession_id] = plan_state
        log.info(
            "planner: submitted plan %s for invocation %s session %s (%d task(s))",
            plan_id,
            inv_id,
            hsession_id,
            len(plan.tasks),
        )
        # Seed one-shot guidance for the host agent so the very first
        # before_model_callback sees the plan.
        try:
            seed_agent_id = str(_safe_attr(effective_host, "name", "") or "")
            if seed_agent_id and hsession_id:
                guidance = self._compute_plan_guidance_text(
                    hsession_id, seed_agent_id
                )
                if guidance:
                    with self._lock:
                        self._pending_plan_guidance = guidance
        except Exception as exc:  # noqa: BLE001
            log.debug("planner: failed to seed pending plan guidance: %s", exc)

    def _maybe_refine_plan(self, inv_id: str, event: dict[str, Any]) -> None:
        """If a plan is active on the hsession containing ``inv_id``,
        ask the planner to refine it in response to ``event`` and
        forward any updated plan under the same ``plan_id`` so the
        server upserts.
        """
        if not self._refine_on_events:
            return
        planner = self._planner
        if planner is None:
            return
        with self._lock:
            _, hsession_id = self._invocation_route.get(inv_id, ("", ""))
            plan_state = (
                self._active_plan_by_session.get(hsession_id)
                if hsession_id
                else None
            )
            invocation_span_id = self._invocations.get(inv_id, "")
        if plan_state is None:
            return
        plan_id = plan_state.plan_id
        plan = plan_state.plan
        plan_for_refine = self._snapshot_plan_with_current_statuses(plan_state)
        import time as _time
        t0 = _time.monotonic()
        log.info(
            "planner.refine: invoking for plan %s on event kind=%s",
            plan_id,
            event.get("kind", "?") if isinstance(event, dict) else "?",
        )
        try:
            new_plan = planner.refine(plan_for_refine, event)
        except Exception as exc:  # noqa: BLE001
            log.warning("planner.refine raised; ignoring: %s", exc)
            return
        elapsed = _time.monotonic() - t0
        if elapsed > 2.0:
            log.warning(
                "planner.refine blocked the event loop for %.1fs — "
                "consider disabling refine_on_events", elapsed
            )
        if new_plan is None or not getattr(new_plan, "tasks", None):
            return
        # Canonicalize assignees on the refined plan too — refine
        # hallucinates formatting variations just like generate does.
        _canonicalize_plan_assignees(
            new_plan,
            list(plan_state.available_agents),
            plan_state.host_agent_name,
        )
        try:
            self._client.submit_plan(
                new_plan,
                plan_id=plan_id,
                invocation_span_id=invocation_span_id,
                session_id=hsession_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("client.submit_plan (refine) raised; ignoring: %s", exc)
            return
        # Merge the refined plan into plan_state in place: preserve existing
        # RUNNING/COMPLETED statuses for tasks that carry over (so work we
        # already did doesn't get re-run), and adopt any new tasks the
        # refine introduced as PENDING. CRITICAL: terminal statuses
        # (COMPLETED/FAILED/CANCELLED) ALWAYS win over whatever the
        # refined plan claims — refine is deferential to the LLM but the
        # client's monotonic ground truth overrides refine for
        # already-resolved work, otherwise the cycle returns.
        with self._lock:
            preserved_statuses: dict[str, str] = {}
            for tid, existing in plan_state.tasks.items():
                status = getattr(existing, "status", "") or "PENDING"
                if status in ("RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
                    preserved_statuses[tid] = status
            new_tasks_by_id: dict[str, Any] = {}
            for t in new_plan.tasks:
                tid = getattr(t, "id", "") or ""
                if not tid:
                    continue
                incoming_status = getattr(t, "status", "") or "PENDING"
                preserved = preserved_statuses.get(tid)
                if preserved is not None:
                    if (
                        preserved in _TERMINAL_TASK_STATUSES
                        and incoming_status != preserved
                    ):
                        log.info(
                            "refine: task %s came back as %s but client "
                            "ground truth is %s — preserving terminal status",
                            tid, incoming_status, preserved,
                        )
                    # Bypass guard: this is initial seeding of a fresh
                    # task object on the new plan.
                    t.status = preserved
                else:
                    t.status = incoming_status
                new_tasks_by_id[tid] = t
            plan_state.plan = new_plan
            plan_state.tasks = new_tasks_by_id
            plan_state.remaining_for_fallback = [
                t for t in new_plan.tasks
                if (getattr(t, "status", "PENDING") or "PENDING") == "PENDING"
            ]
        log.info(
            "planner: refined plan %s for session %s (%d task(s)) — submitted live upsert",
            plan_id,
            hsession_id,
            len(new_plan.tasks),
        )

    def _bind_span_to_task(
        self, span_id: str, attrs: Optional[Mapping[str, Any]]
    ) -> None:
        """If ``attrs`` carries an ``hgraf.task_id`` (i.e. the span was
        just stamped by :meth:`_stamp_attrs_with_task`), record the
        span→task mapping so the matching span-end callback can update
        the tracked task status.
        """
        if not span_id or not attrs:
            return
        tid = attrs.get("hgraf.task_id")
        if not tid:
            return
        with self._lock:
            self._span_to_task[span_id] = str(tid)

    def _mark_task_for_span(
        self, span_id: str, status: str
    ) -> None:
        """Propagate a terminal FAILED/CANCELLED span-end to the plan
        task bound to ``span_id``. No-op if the span was never bound or
        the task is no longer in any active plan.

        **COMPLETED is intentionally NOT supported here.** Task
        completion is a semantic event that only the walker knows how
        to emit — a single LLM_CALL span ending is NOT "task done"; it
        is one of N calls the agent makes while executing the task.
        See ``mark_forced_task_completed``. Span lifecycle
        is a telemetry signal, not a task-state signal; the two are
        deliberately decoupled. FAILED still propagates because an
        errored span is a real signal that the task itself failed.
        """
        if not span_id:
            return
        if status == "COMPLETED":
            # Still consume the span→task mapping so the map doesn't
            # grow unboundedly, but do not touch task status.
            with self._lock:
                self._span_to_task.pop(span_id, "")
            return
        with self._lock:
            tid = self._span_to_task.pop(span_id, "")
            if not tid:
                return
            for plan_state in self._active_plan_by_session.values():
                task = plan_state.tasks.get(tid)
                if task is not None:
                    prev = getattr(task, "status", "") or ""
                    _applied = _set_task_status(task, status)
                    if _applied and prev != status:
                        log.info(
                            "planner: task %s %s → %s (via span %s)",
                            tid, prev or "PENDING", status, span_id,
                        )
                    return

    def _snapshot_plan_with_current_statuses(self, plan_state: PlanState) -> Plan:
        """Return a deep copy of ``plan_state.plan`` in which each task's
        ``status`` reflects the latest tracked state from
        ``plan_state.tasks``.
        """
        import copy
        snap = copy.deepcopy(plan_state.plan)
        with self._lock:
            for t in snap.tasks:
                tid = getattr(t, "id", "") or ""
                tracked = plan_state.tasks.get(tid)
                if tracked is not None:
                    t.status = getattr(tracked, "status", "PENDING") or "PENDING"
        return snap

    def _deps_satisfied(
        self, task: Any, edges: list, tasks_by_id: dict[str, Any]
    ) -> bool:
        """Return True iff every edge pointing TO ``task`` has a source
        task present in ``tasks_by_id`` with status COMPLETED.

        A dep referenced by an edge but missing from ``tasks_by_id``
        counts as NOT satisfied — a missing dep indicates either a
        dangling edge or a bookkeeping bug, and the safe behaviour is
        to wait.

        Caller must hold ``self._lock``.
        """
        tid = getattr(task, "id", "") or ""
        if not tid:
            return False
        for e in edges:
            if getattr(e, "to_task_id", "") != tid:
                continue
            dep_id = getattr(e, "from_task_id", "") or ""
            dep_task = tasks_by_id.get(dep_id)
            if dep_task is None:
                return False
            if (getattr(dep_task, "status", "") or "") != "COMPLETED":
                return False
        return True

    def _next_task_for_agent(
        self, hsession_id: str, agent_id: str
    ) -> Optional[Any]:
        """Return the first PENDING task assigned to ``agent_id`` whose
        dependencies are ALL COMPLETED, within the plan owned by the
        harmonograf session ``hsession_id``. Returns ``None`` if no
        active plan or no unblocked task.
        """
        if not agent_id or not hsession_id:
            return None
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return None
            edges = plan_state.edges
            norm_agent_id = _normalize_agent_id(agent_id)
            for t in plan_state.plan.tasks:
                tid = getattr(t, "id", "") or ""
                if not tid:
                    continue
                tracked = plan_state.tasks.get(tid)
                if tracked is None:
                    continue
                if (getattr(tracked, "status", "") or "") != "PENDING":
                    continue
                assignee = getattr(tracked, "assignee_agent_id", "") or ""
                if assignee != agent_id and (
                    not norm_agent_id
                    or _normalize_agent_id(assignee) != norm_agent_id
                ):
                    continue
                if not self._deps_satisfied(tracked, edges, plan_state.tasks):
                    continue
                return tracked
        return None

    def _compute_plan_guidance_text(
        self, hsession_id: str, agent_id: str
    ) -> Optional[str]:
        """Build the multi-line plan guidance blob injected into the LLM
        request. Returns ``None`` if there is no active plan on this
        session, or if the plan has no task for the current agent.
        """
        if not agent_id or not hsession_id:
            return None
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None or not getattr(plan_state.plan, "tasks", None):
                return None
            plan = plan_state.plan
            has_any = any(
                (getattr(t, "assignee_agent_id", "") or "") == agent_id
                for t in plan.tasks
            )
            if not has_any:
                return None
            task_snapshot: list[tuple[str, str, str, str, list[str]]] = []
            deps_by_task: dict[str, list[str]] = {}
            for e in plan.edges:
                deps_by_task.setdefault(e.to_task_id, []).append(e.from_task_id)
            for t in plan.tasks:
                tid = getattr(t, "id", "") or ""
                if not tid:
                    continue
                tracked = plan_state.tasks.get(tid, t)
                status = (getattr(tracked, "status", "") or "PENDING")
                task_snapshot.append(
                    (
                        tid,
                        str(getattr(t, "title", "") or ""),
                        status,
                        str(getattr(t, "assignee_agent_id", "") or ""),
                        list(deps_by_task.get(tid, [])),
                    )
                )
            summary = str(getattr(plan, "summary", "") or "")

        next_task = self._next_task_for_agent(hsession_id, agent_id)

        lines: list[str] = ["[Plan guidance]"]
        if summary:
            lines.append(f"Summary: {summary}")
        lines.append("Tasks:")
        for tid, title, status, assignee, deps in task_snapshot:
            dep_str = ", ".join(deps) if deps else ""
            lines.append(
                f"  - {tid} [{status}] {title} (assignee={assignee}) — deps: [{dep_str}]"
            )
        if next_task is not None:
            nt_id = getattr(next_task, "id", "") or ""
            nt_title = getattr(next_task, "title", "") or ""
            lines.append(
                f'Your current assigned task is: {nt_id} "{nt_title}"'
            )
            lines.append(
                "Execute this task now. When it is done, move on to the next "
                "PENDING task assigned to you whose dependencies are all "
                "COMPLETED. Do not run tasks assigned to other agents and do "
                "not run tasks whose dependencies have not finished."
            )
        else:
            lines.append(
                f"No task is currently unblocked for agent {agent_id}. "
                "Wait for upstream tasks to complete before proceeding."
            )
        return "\n".join(lines)

    def next_task_from_snapshot(
        self, inv_id: str, agent_id: str
    ) -> Optional[Any]:
        """Inspect the plan snapshot preserved at on_invocation_end and
        return the first PENDING task for ``agent_id`` whose dependencies
        are all COMPLETED (or whose dep tasks dropped out of the plan).

        Returns ``None`` if no snapshot exists, no plan exists, or no
        task is currently unblocked for the agent. Used by
        :class:`HarmonografRunner` to decide whether to re-invoke the
        agent with a follow-up nudge after a run completes.
        """
        if not inv_id or not agent_id:
            return None
        with self._lock:
            snap = self._plan_snapshot_for_inv.get(inv_id)
            if snap is None:
                return None
            plan, tracked = snap
            edges = list(getattr(plan, "edges", []) or [])
            tasks = list(getattr(plan, "tasks", []) or [])
            deps_by_task: dict[str, list[str]] = {}
            for e in edges:
                deps_by_task.setdefault(
                    e.to_task_id, []
                ).append(e.from_task_id)
            for t in tasks:
                tid = getattr(t, "id", "") or ""
                if not tid:
                    continue
                live = tracked.get(tid, t)
                if (getattr(live, "status", "") or "") != "PENDING":
                    continue
                if (getattr(live, "assignee_agent_id", "") or "") != agent_id:
                    continue
                deps = deps_by_task.get(tid, [])
                blocked = False
                for dep_id in deps:
                    dep = tracked.get(dep_id)
                    if dep is None:
                        blocked = True
                        break
                    if (getattr(dep, "status", "") or "") != "COMPLETED":
                        blocked = True
                        break
                if blocked:
                    continue
                return live
        return None

    def clear_plan_snapshot(self, inv_id: str) -> None:
        """Drop the preserved plan snapshot for ``inv_id`` (called by
        :class:`HarmonografRunner` once it's done consuming it)."""
        with self._lock:
            self._plan_snapshot_for_inv.pop(inv_id, None)

    def inject_plan_guidance_if_any(self, cc: Any, llm_request: Any) -> Optional[str]:
        """Inject plan guidance into ``llm_request.contents`` if an active
        plan exists AND the current agent has any matching task.

        Strategy:
          1. If ``_pending_plan_guidance`` is set (seeded at plan submit),
             consume it and inject. This guarantees the very first model
             call after plan submit sees the plan even if resolve order
             leaves computing-from-state unable to find the agent.
          2. Otherwise, compute guidance fresh from the current tracked
             state. This picks up task-completion updates automatically.

        Returns the guidance string that was injected, or ``None``.
        De-duplicates by comparing against the last-injected guidance
        per agent — an identical string suppresses re-injection so rapid
        back-to-back model calls don't accumulate duplicate context.
        """
        inv_id = _invocation_id_from_callback(cc)
        agent_id, hsession_id = self._route_from_callback_or_invocation(cc, inv_id)

        guidance: Optional[str] = None
        with self._lock:
            pending = self._pending_plan_guidance
            if pending:
                self._pending_plan_guidance = None
                guidance = pending
        if guidance is None:
            guidance = self._compute_plan_guidance_text(hsession_id, agent_id)
        if not guidance:
            return None

        with self._lock:
            last = self._last_injected_plan_guidance.get(agent_id, "")
            if last == guidance:
                return None
            self._last_injected_plan_guidance[agent_id] = guidance

        _inject_plan_guidance_into_request(llm_request, guidance)
        return guidance

    def set_forced_task_id(self, task_id: str) -> bool:
        """Declare the task every subsequent span should bind to, until
        cleared. Called by :class:`HarmonografAgent` right before each
        inner-agent step so the plan bookkeeping — not the fragile
        assignee-string heuristic — owns span→task binding.

        Writes the shared attr only. Parallel within-stage execution in
        HarmonografAgent uses the task-local ``_forced_task_id_var``
        ContextVar directly (with explicit token reset) instead of this
        method, so sibling parallel tasks don't clobber each other.

        Returns ``True`` on success and ``False`` when the call was
        REFUSED because the targeted task is already in a terminal
        state. Refusing terminal tasks is the structural fix for the
        cycle bug — if the walker ever asks to bind spans to a finished
        task, that's a walker bug, and we WILL NOT let the request
        re-bind spans (which would cascade into a forced
        COMPLETED→RUNNING transition via ``_stamp_attrs_with_task``).
        """
        tid = str(task_id or "")
        if not tid:
            with self._lock:
                self._forced_current_task_id = ""
            return True
        with self._lock:
            for plan_state in self._active_plan_by_session.values():
                tracked = plan_state.tasks.get(tid)
                if tracked is None:
                    continue
                cur = getattr(tracked, "status", "") or ""
                if cur in _TERMINAL_TASK_STATUSES:
                    log.warning(
                        "REFUSING set_forced_task_id(%s): task is already %s",
                        tid, cur,
                    )
                    return False
                break
            self._forced_current_task_id = tid
        return True

    def forced_task_id(self) -> str:
        ctx_tid = _forced_task_id_var.get()
        if ctx_tid:
            return ctx_tid
        with self._lock:
            return self._forced_current_task_id

    def sweep_running_tasks_to_completed(
        self,
        hsession_id: str = "",
        *,
        exclude: Optional[set[str]] = None,
    ) -> list[str]:
        """Walker turn-end sweep: transition every task currently
        RUNNING on the active plan(s) to COMPLETED, and emit an
        explicit ``submit_task_status_update`` to the server for each
        so the server store + frontend see the transition.

        Invariant: a task is RUNNING only while a walker turn is active. When the walker's inner
        ``run_async`` generator exhausts, every remaining RUNNING task
        is by definition "done" — either it's the forced task, or it
        was observably stamped during the same turn via the assignee-
        fallback or via another forced-task-id the coordinator fed
        into a nested AgentTool dispatch.

        This is the fix for the multi-task-per-turn bug: a coordinator
        dispatching to two sub-agents via AgentTool in a single turn
        stamps both tasks RUNNING (one via forced id, the other via
        fallback or a nested forced id). Previously only the forced
        task was marked complete; now both are.

        Parallel-safe note: the parallel isolated path
        (:meth:`HarmonografAgent._run_single_task_isolated`) does NOT
        use this sweep because sibling tasks run concurrently and
        their RUNNING state must NOT be clobbered when one sibling's
        turn ends. The serial / in-place / delegated paths are the
        only callers — in those paths, no sibling walker turn is in
        flight so sweeping is safe.

        Returns the list of task ids that transitioned to COMPLETED.
        """
        transitioned: list[tuple[str, str]] = []  # (plan_id, task_id)
        with self._lock:
            sessions = (
                [hsession_id] if hsession_id
                else list(self._active_plan_by_session.keys())
            )
            for sid in sessions:
                plan_state = self._active_plan_by_session.get(sid)
                if plan_state is None:
                    continue
                for tid, tracked in plan_state.tasks.items():
                    if exclude is not None and tid in exclude:
                        continue
                    cur = getattr(tracked, "status", "") or ""
                    if cur != "RUNNING":
                        continue
                    if _set_task_status(tracked, "COMPLETED"):
                        log.info(
                            "walker: sweep task %s RUNNING → COMPLETED",
                            tid,
                        )
                        transitioned.append((plan_state.plan_id, tid))
        # Emit server updates OUTSIDE the adapter lock — the transport
        # has its own locks and we don't want to nest.
        swept_ids: list[str] = []
        for plan_id, tid in transitioned:
            swept_ids.append(tid)
            try:
                self._client.submit_task_status_update(
                    plan_id, tid, "COMPLETED"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "sweep: submit_task_status_update(%s COMPLETED) failed: %s",
                    tid, exc,
                )
        # Any sweep clears the forced-task bookkeeping too — the turn
        # is done, there is no current task.
        with self._lock:
            self._forced_current_task_id = ""
        _forced_task_id_var.set(None)
        return swept_ids

    # ------------------------------------------------------------------
    # Lifecycle outcome classifier
    # ------------------------------------------------------------------

    def _classify_task_outcome(
        self,
        tid: str,
        result_summary: str,
        *,
        had_error_span: bool,
    ) -> str:
        """Decide what outcome a just-finished walker turn implies for
        ``tid``. Returns one of ``"completed"`` / ``"failed"`` /
        ``"partial"``. Pure function of inputs — does NOT mutate state.

        Order of checks (first-match wins):
          1. Explicit ``Task failed:`` marker in the result_summary →
             failed (LLM was instructed to use this; trust it).
          2. Explicit ``Task complete:`` marker → completed (also
             instructed; trust it even over heuristics).
          3. ``had_error_span`` (a tool errored during this turn and was
             bound to ``tid``) → failed.
          4. Failure heuristic substrings ("i couldn't", "failed to",
             …) → failed.
          5. Empty result (no text after strip) → partial.
          6. Partial heuristic substrings ("in progress", "more to
             do", …) → partial.
          7. Default → completed.
        """
        text = (result_summary or "").strip()
        lowered = text.lower()
        if _TASK_FAILED_MARKER.search(text):
            return "failed"
        if _TASK_COMPLETE_MARKER.search(text):
            return "completed"
        if had_error_span:
            return "failed"
        for marker in _FAILURE_HEURISTIC_MARKERS:
            if marker in lowered:
                return "failed"
        if not text:
            return "partial"
        for marker in _PARTIAL_HEURISTIC_MARKERS:
            if marker in lowered:
                return "partial"
        return "completed"

    def classify_and_sweep_running_tasks(
        self,
        hsession_id: str = "",
        *,
        result_summary: str = "",
        exclude: Optional[set[str]] = None,
    ) -> dict[str, str]:
        """Classifier-driven replacement for
        :meth:`sweep_running_tasks_to_completed`. For every RUNNING task
        on the active plan(s), classify the turn outcome and apply the
        appropriate transition:

          * ``completed`` → COMPLETED + ``submit_task_status_update``
          * ``failed`` → FAILED + submit + refine ``task_failed`` +
            propagate downstream via ``upstream_failed`` refine
          * ``partial`` → leave RUNNING; the caller (walker) decides
            whether to re-invoke. The classifier does NOT bump the
            re-invocation counter — that's the walker's job, since it
            knows whether it actually re-ran the inner agent.

        Returns a dict ``{task_id: outcome}`` where outcome is one of
        ``"completed"``, ``"failed"``, ``"partial"``. Ignored tasks
        (excluded or non-RUNNING) are not in the dict.
        """
        outcomes: dict[str, str] = {}
        # (plan_id, task_id, new_status) tuples to submit + refine
        # outside the lock.
        to_submit: list[tuple[str, str, str]] = []
        failed_for_refine: list[tuple[str, str, str]] = []  # (plan_id, tid, detail)
        with self._lock:
            sessions = (
                [hsession_id] if hsession_id
                else list(self._active_plan_by_session.keys())
            )
            # Pull the forced task id so PENDING-but-forced tasks (e.g.
            # the inner agent yielded only text and never stamped a leaf
            # span RUNNING) still get classified at turn end.
            forced_id = (
                _forced_task_id_var.get() or self._forced_current_task_id or ""
            )
            for sid in sessions:
                plan_state = self._active_plan_by_session.get(sid)
                if plan_state is None:
                    continue
                # Promote a PENDING forced task to RUNNING so the
                # classifier loop below picks it up. Without this, a stub
                # inner agent (or one that yielded only text) leaves the
                # task PENDING forever — the same bug
                # ``mark_forced_task_completed`` used to paper over.
                if forced_id and forced_id in plan_state.tasks:
                    forced_tracked = plan_state.tasks[forced_id]
                    fcur = getattr(forced_tracked, "status", "") or ""
                    if fcur == "PENDING":
                        _set_task_status(forced_tracked, "RUNNING")
                for tid, tracked in list(plan_state.tasks.items()):
                    if exclude is not None and tid in exclude:
                        continue
                    cur = getattr(tracked, "status", "") or ""
                    if cur != "RUNNING":
                        continue
                    had_err = tid in self._recent_error_task_ids
                    outcome = self._classify_task_outcome(
                        tid, result_summary, had_error_span=had_err
                    )
                    outcomes[tid] = outcome
                    if outcome == "completed":
                        if _set_task_status(tracked, "COMPLETED"):
                            log.info(
                                "walker classify: task %s RUNNING → COMPLETED",
                                tid,
                            )
                            to_submit.append(
                                (plan_state.plan_id, tid, "COMPLETED")
                            )
                            self._task_reinvocation_count.pop(tid, None)
                            self._recent_error_task_ids.discard(tid)
                    elif outcome == "failed":
                        if _set_task_status(tracked, "FAILED"):
                            detail = (
                                f"task {tid} classified FAILED "
                                f"(tool_error={had_err}, "
                                f"summary={result_summary[:100]!r})"
                            )
                            log.info(
                                "walker classify: task %s RUNNING → FAILED (%s)",
                                tid,
                                "tool_error" if had_err else "result_marker",
                            )
                            to_submit.append(
                                (plan_state.plan_id, tid, "FAILED")
                            )
                            failed_for_refine.append(
                                (plan_state.plan_id, tid, detail)
                            )
                            self._task_reinvocation_count.pop(tid, None)
                            self._recent_error_task_ids.discard(tid)
                    else:  # partial
                        log.info(
                            "walker classify: task %s partial — keeping RUNNING",
                            tid,
                        )
            # Clear the forced-task slot regardless — the turn is over.
            self._forced_current_task_id = ""
        _forced_task_id_var.set(None)

        # Outside lock: submit status updates + fire refines.
        for plan_id, tid, status in to_submit:
            try:
                self._client.submit_task_status_update(plan_id, tid, status)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "classify_and_sweep: submit %s %s failed: %s",
                    tid, status, exc,
                )
        for plan_id, tid, detail in failed_for_refine:
            self._refine_after_task_failure(hsession_id, tid, detail)
        return outcomes

    def _refine_after_task_failure(
        self, hsession_id: str, failed_tid: str, detail: str
    ) -> None:
        """Fire a refine with ``task_failed`` and, if the failed task has
        any downstream PENDING tasks, also fire ``upstream_failed`` so the
        planner gets a chance to re-route the plan. Both refines are
        deferential — ``refine_plan_on_drift`` may choose not to revise.
        """
        if not hsession_id:
            return
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
        failed_task = None
        downstream_pending: list[str] = []
        if plan_state is not None:
            failed_task = plan_state.tasks.get(failed_tid)
            for e in getattr(plan_state, "edges", []) or []:
                if getattr(e, "from_task_id", "") != failed_tid:
                    continue
                child_id = getattr(e, "to_task_id", "")
                child = plan_state.tasks.get(child_id)
                if child is None:
                    continue
                if (getattr(child, "status", "") or "") == "PENDING":
                    downstream_pending.append(child_id)
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(kind="task_failed", detail=detail),
                current_task=failed_task,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("refine_plan_on_drift(task_failed) raised: %s", exc)
        if downstream_pending:
            try:
                self.refine_plan_on_drift(
                    hsession_id,
                    DriftReason(
                        kind="upstream_failed",
                        detail=(
                            f"task {failed_tid} failed; "
                            f"{len(downstream_pending)} downstream pending: "
                            f"{','.join(downstream_pending)}"
                        ),
                    ),
                    current_task=failed_task,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "refine_plan_on_drift(upstream_failed) raised: %s", exc
                )

    def note_task_reinvocation(self, tid: str) -> int:
        """Increment the re-invocation counter for ``tid`` and return the
        new value. The walker calls this *before* re-running the inner
        agent on a partial-outcome task, then compares against
        :data:`_REINVOCATION_BUDGET`.
        """
        if not tid:
            return 0
        with self._lock:
            n = self._task_reinvocation_count.get(tid, 0) + 1
            self._task_reinvocation_count[tid] = n
        return n

    def reinvocation_budget(self) -> int:
        """Expose the budget cap so the walker doesn't import the
        private constant.
        """
        return _REINVOCATION_BUDGET

    def mark_task_failed(
        self, hsession_id: str, tid: str, reason: str
    ) -> bool:
        """Force a RUNNING task to FAILED with an explicit ``reason``.
        Used by the walker when the re-invocation budget is exhausted.
        Submits a server status update and fires ``task_failed`` +
        downstream refine, mirroring the classifier's failure path.
        Returns True iff the transition actually happened.
        """
        if not hsession_id or not tid:
            return False
        plan_id = ""
        ok = False
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return False
            tracked = plan_state.tasks.get(tid)
            if tracked is None:
                return False
            cur = getattr(tracked, "status", "") or ""
            if cur in _TERMINAL_TASK_STATUSES:
                return False
            if _set_task_status(tracked, "FAILED"):
                ok = True
                plan_id = plan_state.plan_id
                self._task_reinvocation_count.pop(tid, None)
                self._recent_error_task_ids.discard(tid)
                log.info(
                    "walker: task %s force-marked FAILED (%s)", tid, reason
                )
        if ok and plan_id:
            try:
                self._client.submit_task_status_update(plan_id, tid, "FAILED")
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "mark_task_failed: submit failed for %s: %s", tid, exc
                )
            self._refine_after_task_failure(
                hsession_id, tid, f"task {tid} failed: {reason}"
            )
        return ok

    def task_status(self, hsession_id: str, tid: str) -> str:
        """Return the tracked status of ``tid`` on session ``hsession_id``,
        or empty string if not found. Used by the walker to inspect
        whether a turn left the task in RUNNING (= partial) or some
        terminal state.
        """
        if not hsession_id or not tid:
            return ""
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return ""
            tracked = plan_state.tasks.get(tid)
            if tracked is None:
                return ""
            return getattr(tracked, "status", "") or ""

    def mark_forced_task_completed(self) -> str:
        """Mark the currently-forced task as COMPLETED in whichever
        active PlanState holds it, and clear the forced id. Returns the
        task id that was cleared (or empty string if none was set).
        Idempotent. Called by :class:`HarmonografAgent` after its
        inner-agent step yields the last event of the step.

        This is the EXCLUSIVE source of task completion in the client —
        span_end does NOT propagate to task status, so this method is
        also responsible for telling the server the task is done via
        :meth:`Client.submit_task_status_update`. The server must not
        derive completion from leaf-span ``hgraf.task_id`` scanning
        (an LLM_CALL ending is not "task done"; it is one of N calls
        the agent makes while executing the task).

        Prefers the ContextVar (parallel-safe, task-local) then falls
        back to the shared attr.
        """
        ctx_tid = _forced_task_id_var.get() or ""
        plan_id_for_update = ""
        submit_tid = ""
        with self._lock:
            tid = ctx_tid or self._forced_current_task_id
            if tid:
                for plan_state in self._active_plan_by_session.values():
                    tracked = plan_state.tasks.get(tid)
                    if tracked is None:
                        continue
                    status = getattr(tracked, "status", "") or ""
                    if _set_task_status(tracked, "COMPLETED"):
                        log.info(
                            "planner: forced task %s %s → COMPLETED",
                            tid, status or "PENDING",
                        )
                        plan_id_for_update = plan_state.plan_id
                        submit_tid = tid
                    break
            # Clear the shared attr unconditionally. Parallel siblings
            # read their own ContextVar first, so wiping the shared
            # attr here can't clobber another task's forced id.
            self._forced_current_task_id = ""
        _forced_task_id_var.set(None)
        # Tell the server OUTSIDE the lock — the transport may block
        # briefly on its own locks and we don't want to hold ours.
        if plan_id_for_update and submit_tid:
            try:
                self._client.submit_task_status_update(
                    plan_id_for_update, submit_tid, "COMPLETED"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "submit_task_status_update(COMPLETED) failed for task %s: %s",
                    submit_tid, exc,
                )
        return tid

    # ------------------------------------------------------------------
    # Observer: drift detection + explicit refine
    # ------------------------------------------------------------------

    def detect_drift(
        self,
        events: list,
        current_task: Any,
        plan_state: Optional["PlanState"],
    ) -> Optional[DriftReason]:
        """Scan ``events`` for signals that the agent has drifted from
        the active plan. Returns the first drift found, or None.

        Signals:
          1. ``tool_call_wrong_agent`` — a function_call event from an
             agent that is not the assignee of ``current_task`` (orch
             mode) or not the assignee of any eligible PENDING task
             (delegated mode).
          2. ``transfer_to_unplanned_agent`` — a transfer action targets
             an agent that isn't assigned any task in the plan.
          3. ``failed_span`` — an event reports span status FAILED for a
             task that was RUNNING.
          4. ``task_completion_out_of_order`` — an event marks a task
             COMPLETED whose deps aren't all COMPLETED.
        """
        if plan_state is None:
            return None
        tasks_by_id = plan_state.tasks
        assignees: set[str] = set()
        for t in tasks_by_id.values():
            a = getattr(t, "assignee_agent_id", "") or ""
            if a:
                assignees.add(a)
                na = _normalize_agent_id(a)
                if na:
                    assignees.add(na)
        current_assignee = ""
        if current_task is not None:
            current_assignee = getattr(current_task, "assignee_agent_id", "") or ""

        def _norm(s: str) -> str:
            return _normalize_agent_id(s) or s

        # Eligible (PENDING, deps satisfied) assignees — used for
        # delegated-mode wrong-agent detection.
        eligible_assignees: set[str] = set()
        edges = plan_state.edges
        for tid, tracked in tasks_by_id.items():
            if (getattr(tracked, "status", "") or "") != "PENDING":
                continue
            if not self._deps_satisfied(tracked, edges, tasks_by_id):
                continue
            a = getattr(tracked, "assignee_agent_id", "") or ""
            if a:
                eligible_assignees.add(a)
                na = _normalize_agent_id(a)
                if na:
                    eligible_assignees.add(na)

        for ev in events or []:
            ev_id = str(_safe_attr(ev, "id", "") or "")
            author = str(_safe_attr(ev, "author", "") or "")
            content = _safe_attr(ev, "content", None)
            parts = _safe_attr(content, "parts", None) or [] if content else []

            # Signal 1: tool call from wrong agent.
            for part in parts:
                fc = _safe_attr(part, "function_call", None)
                if fc is None:
                    continue
                fc_name = str(_safe_attr(fc, "name", "") or "")
                if current_task is not None and current_assignee:
                    if author and _norm(author) != _norm(current_assignee):
                        return DriftReason(
                            kind="tool_call_wrong_agent",
                            detail=(
                                f"tool {fc_name!r} called by {author!r} but "
                                f"current task assignee is {current_assignee!r}"
                            ),
                            event_id=ev_id,
                        )
                else:
                    # Delegated mode: wrong-agent when the author isn't
                    # assigned to ANY currently-eligible task.
                    if (
                        author
                        and eligible_assignees
                        and _norm(author) not in eligible_assignees
                        and author not in eligible_assignees
                    ):
                        return DriftReason(
                            kind="tool_call_wrong_agent",
                            detail=(
                                f"tool {fc_name!r} called by {author!r} — not "
                                f"an eligible task assignee"
                            ),
                            event_id=ev_id,
                        )

            actions = _safe_attr(ev, "actions", None)
            # Signal 2: transfer to unplanned agent.
            transfer_to = _safe_attr(actions, "transfer_to_agent", None) if actions else None
            if transfer_to:
                tgt = str(transfer_to)
                if (
                    assignees
                    and tgt not in assignees
                    and _norm(tgt) not in assignees
                ):
                    return DriftReason(
                        kind="transfer_to_unplanned_agent",
                        detail=f"transfer to {tgt!r} which is not assigned any plan task",
                        event_id=ev_id,
                    )

            # Signal 3: failed span. We look for a hgraf-ish marker on
            # the event or on any part attribute bag.
            ev_status = str(_safe_attr(ev, "status", "") or "").upper()
            if ev_status == "FAILED":
                bound_tid = str(_safe_attr(ev, "task_id", "") or "")
                if bound_tid:
                    tracked = tasks_by_id.get(bound_tid)
                    if tracked is not None and (getattr(tracked, "status", "") or "") == "RUNNING":
                        return DriftReason(
                            kind="failed_span",
                            detail=f"task {bound_tid} span ended FAILED while RUNNING",
                            event_id=ev_id,
                        )

            # Signal 4: out-of-order task completion.
            completed_tid = str(_safe_attr(ev, "completed_task_id", "") or "")
            if completed_tid and completed_tid in tasks_by_id:
                tracked = tasks_by_id[completed_tid]
                if not self._deps_satisfied(tracked, edges, tasks_by_id):
                    return DriftReason(
                        kind="task_completion_out_of_order",
                        detail=(
                            f"task {completed_tid} marked COMPLETED but "
                            f"dependencies are not all COMPLETED"
                        ),
                        event_id=ev_id,
                    )

            # Signal 5: context_pressure — response was truncated / hit
            # a token cap. Real google.adk.events.Event carries an enum
            # here (``FinishReason.MAX_TOKENS``), so prefer ``.name`` and
            # fall back to the trailing segment of the str form.
            fr_raw = _safe_attr(ev, "finish_reason", None)
            fr_name = getattr(fr_raw, "name", None) or str(fr_raw or "")
            finish_reason = fr_name.upper().rsplit(".", 1)[-1]
            if finish_reason and finish_reason in _CONTEXT_PRESSURE_FINISH_REASONS:
                return DriftReason(
                    kind=DRIFT_KIND_CONTEXT_PRESSURE,
                    detail=f"response truncated (finish_reason={finish_reason})",
                    event_id=ev_id,
                    severity="warning",
                    hint={"finish_reason": finish_reason},
                )

            # Signals 6-9: scan text parts for LLM agency markers
            # (refusal / merge / split / reorder). These live on text
            # parts of the event content. Each marker is deferential —
            # first match wins and hands a hint to the refiner.
            for part in parts:
                if _safe_attr(part, "thought", False):
                    continue
                text = str(_safe_attr(part, "text", "") or "")
                if not text:
                    continue
                lowered = text.lower()
                m = _first_text_marker(lowered, _LLM_REFUSAL_MARKERS)
                if m is not None:
                    return DriftReason(
                        kind=DRIFT_KIND_LLM_REFUSED,
                        detail=f"LLM refusal marker {m!r}: {text[:140]!r}",
                        event_id=ev_id,
                        severity="warning",
                        hint={"marker": m, "text": text[:500]},
                    )
                m = _first_text_marker(lowered, _LLM_MERGE_MARKERS)
                if m is not None:
                    return DriftReason(
                        kind=DRIFT_KIND_LLM_MERGED_TASKS,
                        detail=f"LLM merge marker {m!r}: {text[:140]!r}",
                        event_id=ev_id,
                        severity="info",
                        hint={"marker": m, "text": text[:500]},
                    )
                m = _first_text_marker(lowered, _LLM_SPLIT_MARKERS)
                if m is not None:
                    return DriftReason(
                        kind=DRIFT_KIND_LLM_SPLIT_TASK,
                        detail=f"LLM split marker {m!r}: {text[:140]!r}",
                        event_id=ev_id,
                        severity="info",
                        hint={"marker": m, "text": text[:500]},
                    )
                m = _first_text_marker(lowered, _LLM_REORDER_MARKERS)
                if m is not None:
                    return DriftReason(
                        kind=DRIFT_KIND_LLM_REORDERED_WORK,
                        detail=f"LLM reorder marker {m!r}: {text[:140]!r}",
                        event_id=ev_id,
                        severity="info",
                        hint={"marker": m, "text": text[:500]},
                    )

        # Signal 10: multiple stamp mismatches (stateful counter). The
        # counter is bumped by note_stamp_mismatch when the forced-task
        # stamping path rejects a re-bind on a terminal task. We check
        # it here so ``detect_drift`` callers don't need a special
        # path.
        with self._lock:
            mismatches = self._stamp_mismatch_count
        if mismatches >= _STAMP_MISMATCH_THRESHOLD:
            return DriftReason(
                kind=DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES,
                detail=(
                    f"{mismatches} forced-task stamp rejections — "
                    f"plan likely out of sync with execution"
                ),
                severity="warning",
                hint={"count": mismatches},
            )

        return None

    def detect_semantic_drift(
        self,
        task: Any,
        result_summary: str,
        events: list,
    ) -> Optional[DriftReason]:
        """Scan a task's OUTPUTS (not structural events) for signals that
        the plan needs to change. Lightweight textual heuristics only —
        never invokes an LLM. False positives are OK because refine is
        deferential: the planner decides whether to actually revise.

        Classification (first-match wins):
          1. ``task_failed`` — result contains error markers, or any
             event has status FAILED / ``error`` attr.
          2. ``task_empty_result`` — result_summary is empty or < 20 chars
             after strip().
          3. ``task_result_new_work`` — result mentions new work:
             "need to", "requires", "blocked by", "should also",
             "further investigation".
          4. ``task_result_contradicts_plan`` — result indicates a prior
             step was wrong: "was wrong", "incorrect", "mistake",
             "contradicts", "reconsider".
        """
        tid = str(getattr(task, "id", "") or "") if task is not None else ""
        text = (result_summary or "").strip()
        lowered = text.lower()

        # Signal 1: task_failed — event-level FAILED status OR error
        # markers in the text. Check events first (authoritative).
        for ev in events or []:
            status = str(_safe_attr(ev, "status", "") or "").upper()
            if status == "FAILED":
                return DriftReason(
                    kind="task_failed",
                    detail=f"task {tid} event reported status FAILED",
                    event_id=str(_safe_attr(ev, "id", "") or ""),
                )
            err = _safe_attr(ev, "error_message", "") or _safe_attr(ev, "error", "")
            if err:
                return DriftReason(
                    kind="task_failed",
                    detail=f"task {tid} event carried error: {str(err)[:120]}",
                    event_id=str(_safe_attr(ev, "id", "") or ""),
                )
        error_markers = (
            "error:", "exception:", "traceback", "failed to",
            "could not", "unable to complete",
        )
        if any(marker in lowered for marker in error_markers):
            return DriftReason(
                kind="task_failed",
                detail=f"task {tid} result contains error marker: {text[:120]!r}",
            )

        # Signal 1b: llm_refused — the agent declined the task rather
        # than failing it. Refusal and failure look similar to a human
        # but the planner may want to respond differently (e.g. route
        # the task to a different agent).
        m = _first_text_marker(lowered, _LLM_REFUSAL_MARKERS)
        if m is not None:
            return DriftReason(
                kind=DRIFT_KIND_LLM_REFUSED,
                detail=f"task {tid} refused by LLM ({m!r}): {text[:140]!r}",
                severity="warning",
                hint={"marker": m, "text": text[:500], "task_id": tid},
            )

        # Signal 1c: llm_merged_tasks — agent signalled it was folding
        # multiple tasks into one response.
        m = _first_text_marker(lowered, _LLM_MERGE_MARKERS)
        if m is not None:
            return DriftReason(
                kind=DRIFT_KIND_LLM_MERGED_TASKS,
                detail=f"task {tid} merge marker ({m!r}): {text[:140]!r}",
                severity="info",
                hint={"marker": m, "text": text[:500], "task_id": tid},
            )

        # Signal 2: empty / stub result.
        if len(text) < 20:
            return DriftReason(
                kind="task_empty_result",
                detail=(
                    f"task {tid} produced empty/stub result "
                    f"(len={len(text)}): {text!r}"
                ),
            )

        # Signal 3: new work keywords.
        new_work_markers = (
            "need to", "needs to", "requires ", "blocked by",
            "should also", "further investigation",
            "additional step", "more information",
        )
        for marker in new_work_markers:
            if marker in lowered:
                excerpt = text[:140]
                return DriftReason(
                    kind="task_result_new_work",
                    detail=f"task {tid} reveals new work ({marker!r}): {excerpt!r}",
                )

        # Signal 4: contradicts plan.
        contradict_markers = (
            "was wrong", "is incorrect", "was incorrect", "mistake",
            "contradicts", "reconsider", "different approach",
        )
        for marker in contradict_markers:
            if marker in lowered:
                return DriftReason(
                    kind="task_result_contradicts_plan",
                    detail=f"task {tid} contradicts plan ({marker!r}): {text[:140]!r}",
                )

        return None

    def refine_plan_on_drift(
        self,
        hsession_id: str,
        drift: DriftReason,
        current_task: Any = None,
    ) -> None:
        """Explicit refine triggered by the observer when
        :meth:`detect_drift` (or a reporting tool / control handler)
        produces a :class:`DriftReason`.

        Severity handling:
          * ``info`` → DEBUG detail log
          * ``warning`` → INFO detail log
          * ``critical`` → WARNING detail log + error attributes on
            every active invocation span

        An unconditional INFO ``plan refined: drift=...`` line is
        always emitted once the refine commits, so existing observers
        (and tests) still see it.

        Unrecoverable (``recoverable=False``): the plan is declared
        broken — the current task is flipped to FAILED, all RUNNING
        tasks are flipped to FAILED, and every downstream PENDING task
        cascades to CANCELLED. The planner is NOT called.

        Throttling: recoverable drifts of the same ``kind`` fired
        within ``_DRIFT_REFINE_THROTTLE_SECONDS`` collapse into a
        single refine. Critical / unrecoverable drifts bypass the
        throttle.

        Hint propagation: ``drift.hint`` is forwarded into the drift
        context dict passed to ``planner.refine`` so the LLM refiner
        can use structured context.

        Revision tracking: every fired drift (recoverable or not)
        appends an entry to ``plan_state.revisions`` with
        ``{revised_at, kind, detail, severity, reason, drift_kind}``
        and stamps ``plan.revision_reason = f"{drift.kind}: ..."``.
        """
        if not hsession_id or drift is None:
            return
        self._metrics.refine_fires[drift.kind] += 1
        import time as _time

        sev = (drift.severity or "info").lower()
        log.debug(
            "refine: entry hsession=%s drift_kind=%s severity=%s recoverable=%s drift_detail=%r current_task=%s",
            hsession_id, drift.kind, sev, drift.recoverable,
            drift.detail[:120] if drift.detail else "",
            getattr(current_task, "id", "") if current_task is not None else "",
        )

        # Severity-aware detail log (supplements the unconditional
        # "plan refined" INFO line below).
        sev_msg = "drift observed kind=%s severity=%s detail=%s"
        if sev == "critical":
            log.warning(sev_msg, drift.kind, sev, drift.detail)
        elif sev == "warning":
            log.info(sev_msg, drift.kind, sev, drift.detail)
        else:
            log.debug(sev_msg, drift.kind, sev, drift.detail)

        planner = self._planner
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
        if plan_state is None:
            return
        plan_id = plan_state.plan_id

        # Throttle recoverable, non-critical drifts by (session, kind) so
        # fan-out across sessions doesn't suppress siblings.
        now = _time.monotonic()
        throttle_key = (hsession_id, drift.kind)
        if drift.recoverable and sev != "critical":
            last = self._last_refine_by_kind.get(throttle_key, 0.0)
            if last and (now - last) < _DRIFT_REFINE_THROTTLE_SECONDS:
                log.debug(
                    "refine: throttled kind=%s (last=%.3fs ago)",
                    drift.kind, now - last,
                )
                return
        self._last_refine_by_kind[throttle_key] = now

        # For critical drifts, surface an error attribute on every
        # live INVOCATION span so the frontend can render it red.
        if sev == "critical":
            self._surface_drift_on_active_span(hsession_id, drift)

        revision_entry: dict[str, Any] = {
            "revised_at": _time.time(),
            "kind": drift.kind,
            "detail": drift.detail,
            "severity": sev,
            # Legacy keys preserved for pre-expansion UI code + tests.
            "reason": drift.detail,
            "drift_kind": drift.kind,
        }
        stamped_reason = f"{drift.kind}: {(drift.detail or '')[:200]}"

        # Unrecoverable path: do NOT call planner.refine — fail the
        # current task and cascade CANCELLED to downstream pending
        # tasks. Still record the revision so the UI shows it.
        if not drift.recoverable:
            log.warning(
                "unrecoverable drift kind=%s detail=%s — failing current "
                "task and cascading CANCELLED to downstream",
                drift.kind, drift.detail,
            )
            self._fail_and_cascade_unrecoverable(
                hsession_id, current_task, drift
            )
            with self._lock:
                plan_state.revisions.append(revision_entry)
                try:
                    plan_state.plan.revision_reason = stamped_reason
                    plan_state.plan.revision_kind = drift.kind or ""
                    plan_state.plan.revision_severity = sev
                    plan_state.plan.revision_index = int(
                        getattr(plan_state.plan, "revision_index", 0) or 0
                    ) + 1
                except Exception:
                    pass
            try:
                self._client.submit_plan(
                    plan_state.plan,
                    plan_id=plan_id,
                    session_id=hsession_id or None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "client.submit_plan (unrecoverable drift) raised; ignoring: %s",
                    exc,
                )
            return

        drift_context: dict[str, Any] = {
            "kind": drift.kind,
            "detail": drift.detail,
            "severity": sev,
            "recoverable": drift.recoverable,
            "hint": dict(drift.hint or {}),
            "current_task_id": (
                getattr(current_task, "id", "") if current_task is not None else None
            ),
        }
        log.info(
            "plan refined: drift=%s reason=%s plan_id=%s",
            drift.kind, drift.detail, plan_id,
        )
        new_plan: Optional[Plan] = None
        if planner is not None:
            plan_for_refine = self._snapshot_plan_with_current_statuses(plan_state)
            try:
                new_plan = planner.refine(plan_for_refine, drift_context)
            except Exception as exc:  # noqa: BLE001
                log.warning("planner.refine raised; ignoring: %s", exc)
                new_plan = None

        # Reset the stamp-mismatch counter once the drift it provokes
        # has been handled, so we don't keep re-firing the same signal.
        if drift.kind == DRIFT_KIND_MULTIPLE_STAMP_MISMATCHES:
            self.reset_stamp_mismatches()

        if new_plan is None or not getattr(new_plan, "tasks", None):
            log.debug(
                "refine: planner.refine returned None (no-op) plan_id=%s",
                plan_id,
            )
            with self._lock:
                plan_state.revisions.append(revision_entry)
                try:
                    plan_state.plan.revision_reason = stamped_reason
                    plan_state.plan.revision_kind = drift.kind or ""
                    plan_state.plan.revision_severity = sev
                    plan_state.plan.revision_index = int(
                        getattr(plan_state.plan, "revision_index", 0) or 0
                    ) + 1
                except Exception:
                    pass
            try:
                self._client.submit_plan(
                    plan_state.plan,
                    plan_id=plan_id,
                    session_id=hsession_id or None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "client.submit_plan (drift, no refine) raised; ignoring: %s",
                    exc,
                )
            return

        log.debug(
            "refine: planner.refine returned tasks=%d plan_id=%s",
            len(new_plan.tasks), plan_id,
        )
        self._apply_refined_plan(
            hsession_id=hsession_id,
            plan_state=plan_state,
            new_plan=new_plan,
            stamped_reason=stamped_reason,
            revision_entry=revision_entry,
            drift_kind=drift.kind or "",
            drift_severity=sev,
        )

    def _apply_refined_plan(
        self,
        *,
        hsession_id: str,
        plan_state: "PlanState",
        new_plan: Plan,
        stamped_reason: str,
        revision_entry: dict[str, Any],
        drift_kind: str = "",
        drift_severity: str = "",
    ) -> None:
        """Canonicalize + swap in a refined plan, preserving terminal
        task statuses from the client's ground truth. Extracted from
        :meth:`refine_plan_on_drift` so it can be reused and unit-tested
        in isolation.
        """
        plan_id = plan_state.plan_id
        _canonicalize_plan_assignees(
            new_plan,
            list(plan_state.available_agents),
            plan_state.host_agent_name,
        )
        new_plan.revision_reason = stamped_reason
        new_plan.revision_kind = drift_kind
        new_plan.revision_severity = drift_severity
        new_plan.revision_index = int(
            getattr(plan_state.plan, "revision_index", 0) or 0
        ) + 1
        log.debug(
            "apply_refined_plan: plan_id=%s incoming_tasks=%d",
            plan_id, len(new_plan.tasks),
        )
        with self._lock:
            preserved_statuses: dict[str, str] = {}
            for tid, existing in plan_state.tasks.items():
                status = getattr(existing, "status", "") or "PENDING"
                if status in ("RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
                    preserved_statuses[tid] = status
            new_tasks_by_id: dict[str, Any] = {}
            for t in new_plan.tasks:
                tid = getattr(t, "id", "") or ""
                if not tid:
                    continue
                incoming = getattr(t, "status", "") or "PENDING"
                preserved = preserved_statuses.get(tid)
                if preserved is not None:
                    if (
                        preserved in _TERMINAL_TASK_STATUSES
                        and incoming != preserved
                    ):
                        log.info(
                            "refine(drift): task %s came back as %s but "
                            "client ground truth is %s — preserving terminal",
                            tid, incoming, preserved,
                        )
                    t.status = preserved
                else:
                    t.status = incoming
                new_tasks_by_id[tid] = t
            plan_state.plan = new_plan
            plan_state.tasks = new_tasks_by_id
            plan_state.remaining_for_fallback = [
                t for t in new_plan.tasks
                if (getattr(t, "status", "PENDING") or "PENDING") == "PENDING"
            ]
            plan_state.revisions.append(revision_entry)
        try:
            self._client.submit_plan(
                new_plan,
                plan_id=plan_id,
                session_id=hsession_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("client.submit_plan (drift refine) raised; ignoring: %s", exc)

    def _fail_and_cascade_unrecoverable(
        self,
        hsession_id: str,
        current_task: Any,
        drift: DriftReason,
    ) -> None:
        """Mark ``current_task`` FAILED and cascade CANCELLED to every
        downstream PENDING task. Also fails any currently-RUNNING task
        (the user_cancel case takes them all down). Monotonic-guarded
        via :func:`_set_task_status`. Emits submit_task_status_update
        for each transition outside the adapter lock.
        """
        transitions: list[tuple[str, str]] = []  # (tid, new_status)
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return
            plan_id = plan_state.plan_id
            tasks_by_id = plan_state.tasks
            edges = plan_state.edges

            fail_ids: set[str] = set()
            if current_task is not None:
                ctid = str(getattr(current_task, "id", "") or "")
                if ctid:
                    fail_ids.add(ctid)
            for tid, tracked in tasks_by_id.items():
                if (getattr(tracked, "status", "") or "") == "RUNNING":
                    fail_ids.add(tid)
            for tid in list(fail_ids):
                tracked = tasks_by_id.get(tid)
                if tracked is None:
                    continue
                if _set_task_status(tracked, "FAILED"):
                    transitions.append((tid, "FAILED"))

            # BFS downstream of every failed id, cascading CANCELLED
            # to any non-terminal task.
            child_map: dict[str, list[str]] = {}
            for e in edges:
                frm = getattr(e, "from_task_id", "") or ""
                to = getattr(e, "to_task_id", "") or ""
                if frm and to:
                    child_map.setdefault(frm, []).append(to)
            frontier = list(fail_ids)
            seen: set[str] = set(fail_ids)
            while frontier:
                nxt: list[str] = []
                for parent in frontier:
                    for child_id in child_map.get(parent, []):
                        if child_id in seen:
                            continue
                        seen.add(child_id)
                        tracked_child = tasks_by_id.get(child_id)
                        if tracked_child is None:
                            nxt.append(child_id)
                            continue
                        cur = getattr(tracked_child, "status", "") or ""
                        if cur in _TERMINAL_TASK_STATUSES:
                            nxt.append(child_id)
                            continue
                        if _set_task_status(tracked_child, "CANCELLED"):
                            transitions.append((child_id, "CANCELLED"))
                        nxt.append(child_id)
                frontier = nxt
            self._forced_current_task_id = ""
        _forced_task_id_var.set(None)

        for tid, status in transitions:
            try:
                self._client.submit_task_status_update(plan_id, tid, status)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "unrecoverable cascade: submit %s %s failed: %s",
                    tid, status, exc,
                )

    def _surface_drift_on_active_span(
        self, hsession_id: str, drift: DriftReason
    ) -> None:
        """Stamp a critical drift as an error attribute on every live
        INVOCATION span so the UI can render it. Best-effort.
        """
        with self._lock:
            active_span_ids = list(self._invocations.values())
        attrs = {
            "drift_kind": drift.kind,
            "drift_severity": drift.severity or "critical",
            "drift_detail": (drift.detail or "")[:500],
            "error": drift.detail or drift.kind,
        }
        for span_id in active_span_ids:
            try:
                self._client.emit_span_update(span_id, attributes=attrs)
            except Exception as exc:  # noqa: BLE001
                log.debug("surface drift on span %s failed: %s", span_id, exc)

    def apply_drift_from_control(self, drift: DriftReason) -> None:
        """Helper the control router calls to route STEER / CANCEL
        through the drift pipeline.

        The control handler has no direct hsession_id handle — the
        control envelope addresses the client, not a specific
        invocation. So this method fans the drift out to every active
        session. No active sessions → no-op.
        """
        if drift is None:
            return
        with self._lock:
            sessions = list(self._active_plan_by_session.keys())
        if not sessions:
            log.info(
                "apply_drift_from_control: no active sessions — drift kind=%s dropped",
                drift.kind,
            )
            return
        for hsession_id in sessions:
            try:
                self.refine_plan_on_drift(hsession_id, drift)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "apply_drift_from_control: refine_plan_on_drift(%s) raised: %s",
                    drift.kind, exc,
                )

    def note_stamp_mismatch(self) -> int:
        """Bump the forced-task stamp mismatch counter. Called by
        :meth:`_stamp_attrs_with_task` when it rejects an attempt to
        re-bind a span to an already-terminal task. Crossing
        ``_STAMP_MISMATCH_THRESHOLD`` causes :meth:`detect_drift` to
        raise a ``multiple_stamp_mismatches`` drift.
        """
        with self._lock:
            self._stamp_mismatch_count += 1
            return self._stamp_mismatch_count

    def reset_stamp_mismatches(self) -> None:
        """Reset the stamp mismatch counter — called after a refine on
        ``multiple_stamp_mismatches`` so the next window starts fresh.
        """
        with self._lock:
            self._stamp_mismatch_count = 0

    def _stamp_attrs_with_task(
        self,
        attrs: Optional[dict[str, Any]],
        agent_id: str,
        hsession_id: str,
        span_kind: str = "",
    ) -> Optional[dict[str, Any]]:
        """Attach ``hgraf.task_id`` to ``attrs`` if this span belongs to
        a plan task on session ``hsession_id``. The forced-task-id path
        (set by :class:`HarmonografAgent`) wins: every span emitted
        while a forced id is set binds to that task authoritatively.
        The assignee-matching fallback is preserved for invocations
        that don't route through HarmonografAgent — it pops tasks from
        ``plan_state.remaining_for_fallback`` so two spans don't fight
        for the same binding.

        Only leaf execution spans (``LLM_CALL`` / ``TOOL_CALL``) are
        stamped. Wrapper spans like ``INVOCATION`` or ``TRANSFER`` are
        skipped — their lifecycles don't correspond to executing a task.
        """
        if not agent_id or not hsession_id:
            return attrs
        if span_kind and span_kind not in ("LLM_CALL", "TOOL_CALL"):
            return attrs
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return attrs
            # ContextVar wins (parallel-safe, task-local); fall back to
            # the shared attr for non-parallel callers.
            forced_id = _forced_task_id_var.get() or self._forced_current_task_id
            if forced_id and forced_id in plan_state.tasks:
                tracked = plan_state.tasks[forced_id]
                prev = getattr(tracked, "status", "") or ""
                # MONOTONIC GUARD: if the forced task is already terminal,
                # do not re-bind this span to it and do not transition it
                # back to RUNNING. This is the structural fix for the
                # cycle bug (COMPLETED → RUNNING via forced rebind).
                if prev in _TERMINAL_TASK_STATUSES:
                    log.warning(
                        "planner: REJECTED stamping forced task %s as RUNNING — "
                        "already %s (agent=%s, session=%s)",
                        forced_id, prev, agent_id, hsession_id,
                    )
                    # Bump the mismatch counter (inside the same lock
                    # we already hold — lock is re-entrant only if
                    # RLock, and self._lock is threading.Lock, so
                    # increment the field directly instead of calling
                    # note_stamp_mismatch).
                    self._stamp_mismatch_count += 1
                    return attrs
                if _set_task_status(tracked, "RUNNING") and prev != "RUNNING":
                    log.info(
                        "planner: forced task %s %s → RUNNING (agent=%s, session=%s)",
                        forced_id, prev or "PENDING", agent_id, hsession_id,
                    )
                self._current_task_id = forced_id
                self._current_task_title = str(getattr(tracked, "title", "") or "")
                self._current_task_description = str(
                    getattr(tracked, "description", "") or ""
                )
                self._current_task_agent_id = agent_id
                out_forced: dict[str, Any] = dict(attrs or {})
                out_forced["hgraf.task_id"] = forced_id
                return out_forced
            remaining = plan_state.remaining_for_fallback
            edges = plan_state.edges
            matched = None
            norm_agent_id = _normalize_agent_id(agent_id)
            for i, t in enumerate(remaining):
                assignee = getattr(t, "assignee_agent_id", "") or ""
                if assignee != agent_id and (
                    not norm_agent_id
                    or _normalize_agent_id(assignee) != norm_agent_id
                ):
                    continue
                cand_id = getattr(t, "id", "") or ""
                tracked_cand = plan_state.tasks.get(cand_id)
                if tracked_cand is None:
                    continue
                _cand_status = (getattr(tracked_cand, "status", "") or "")
                _cand_deps_ok = self._deps_satisfied(
                    tracked_cand, edges, plan_state.tasks
                )
                if _cand_status != "PENDING":
                    continue
                if not _cand_deps_ok:
                    continue
                matched = remaining.pop(i)
                break
            if matched is None:
                return attrs
            task_id = getattr(matched, "id", "") or ""
            if task_id and task_id in plan_state.tasks:
                tracked = plan_state.tasks[task_id]
                prev = getattr(tracked, "status", "") or ""
                if _set_task_status(tracked, "RUNNING") and prev != "RUNNING":
                    log.info(
                        "planner: task %s %s → RUNNING (agent=%s, session=%s)",
                        task_id, prev or "PENDING", agent_id, hsession_id,
                    )
                self._current_task_id = task_id
                self._current_task_title = str(getattr(tracked, "title", "") or "")
                self._current_task_description = str(
                    getattr(tracked, "description", "") or ""
                )
                self._current_task_agent_id = agent_id
        if not task_id:
            return attrs
        out: dict[str, Any] = dict(attrs or {})
        out["hgraf.task_id"] = task_id
        return out

    def on_invocation_end(self, ic: Any) -> None:
        self._metrics.callbacks_fired["on_invocation_end"] += 1
        inv_id = _safe_attr(ic, "invocation_id", "")
        with self._lock:
            span_id = self._invocations.pop(inv_id, None)
            self._llm_by_invocation.pop(inv_id, None)
            _, _hsession_for_end = self._invocation_route.get(inv_id, ("", ""))
            # Only clear the PlanState when the invocation that GENERATED
            # it finishes. Sub-invocations (AgentTool) share the session
            # but did not generate the plan, so their end must not drop
            # it — the plan stays alive for the outer run to consume.
            plan_state = (
                self._active_plan_by_session.get(_hsession_for_end)
                if _hsession_for_end
                else None
            )
            if (
                plan_state is not None
                and plan_state.generating_invocation_id == inv_id
            ):
                import copy as _copy
                self._plan_snapshot_for_inv[inv_id] = (
                    plan_state.plan,
                    {k: _copy.copy(v) for k, v in plan_state.tasks.items()},
                )
                # Do NOT pop _active_plan_by_session here. The plan must
                # stay alive on the harmonograf session across the
                # invocation boundary so post-drive assertions and
                # control handlers (STEER / CANCEL) can still find it.
                # A subsequent top-level invocation on the same session
                # supersedes it naturally when maybe_run_planner writes
                # a new PlanState into the same slot.
                log.info(
                    "planner: snapshotted PlanState for session %s (generating inv %s ended, plan retained for supersession)",
                    _hsession_for_end, inv_id,
                )
            self._invocation_route.pop(inv_id, None)
            token = self._route_tokens.pop(inv_id, None)
        if token is not None:
            try:
                self._current_root_hsession_var.reset(token)
            except (LookupError, ValueError):
                pass
        if span_id:
            # Clear the task_report before ending the span so the UI doesn't
            # show stale status (e.g. "Thinking: …") after the agent finishes.
            self._client.emit_span_update(span_id, attributes={"task_report": ""})
            self._client.emit_span_end(span_id, status="COMPLETED")

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def on_model_start(self, cc: Any, req: Any) -> None:
        self._metrics.callbacks_fired["on_model_start"] += 1
        inv_id = _invocation_id_from_callback(cc)
        parent = self._get_invocation_span(inv_id)
        agent_id, hsession_id = self._route_from_callback_or_invocation(cc, inv_id)
        model = _safe_attr(req, "model", "") or "llm"
        attrs: dict[str, Any] = {"model": model}
        if model and model != "llm":
            attrs["model_name"] = str(model)
        # Count messages and build request_preview for popover.
        contents = _safe_attr(req, "contents", None)
        msg_count = len(contents) if contents is not None else 0
        if msg_count:
            attrs["message_count"] = msg_count
        try:
            if contents:
                preview_parts = []
                for item in contents:
                    dump = getattr(item, "model_dump", None)
                    preview_parts.append(str(dump(mode="json") if callable(dump) else item))
                request_preview = " ".join(preview_parts)[:200]
                attrs["request_preview"] = request_preview
        except Exception:
            pass
        # Capture the last user-role message for STATUS_QUERY context.
        try:
            if contents:
                for item in reversed(list(contents)):
                    role = _safe_attr(item, "role", "") or ""
                    if str(role).lower() == "user":
                        parts_obj = _safe_attr(item, "parts", None) or []
                        text_parts = [
                            _safe_attr(p, "text", "") or ""
                            for p in parts_obj
                            if _safe_attr(p, "text", "")
                        ]
                        user_text = " ".join(text_parts).strip()
                        if user_text:
                            with self._lock:
                                self._last_user_message = user_text[:200]
                        break
        except Exception:
            pass
        model_label = f"Calling {model} ({msg_count} message{'s' if msg_count != 1 else ''})"
        self._client.set_current_activity(model_label)
        # Provisional context-window sample: the precise prompt token count
        # isn't known until on_model_end surfaces usage_metadata, but we'd
        # rather publish the char/4 estimate now than leave the gauge at
        # zero until the model responds. on_model_end will overwrite with
        # the authoritative count once the response arrives.
        try:
            ctx_tokens = _estimate_request_tokens(req)
            ctx_limit = _lookup_context_window_limit(str(model))
            if ctx_tokens or ctx_limit:
                self._client.set_context_window(ctx_tokens, ctx_limit)
        except Exception:
            pass
        # Also write as task_report on the LLM_CALL span so the correct agent
        # row is updated via the span-attribute path (bypasses heartbeat agent_id).
        attrs["task_report"] = model_label
        # Planner binding — see on_tool_start for details.
        attrs = self._stamp_attrs_with_task(
            attrs, agent_id, hsession_id, span_kind="LLM_CALL"
        ) or attrs
        payload = _safe_llm_request_payload(req)
        span_id = self._client.emit_span_start(
            kind="LLM_CALL",
            name=str(model),
            parent_span_id=parent,
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="input",
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        with self._lock:
            self._llm_by_invocation[inv_id] = span_id
            # Map llm_span_id → invocation span_id so thinking stream can emit
            # live task_report on the parent invocation span.
            inv_span_id = self._invocations.get(inv_id)
            if inv_span_id:
                self._llm_to_invocation[span_id] = inv_span_id
        self._bind_span_to_task(span_id, attrs)

    def on_model_end(self, cc: Any, resp: Any) -> None:
        self._metrics.callbacks_fired["on_model_end"] += 1
        inv_id = _invocation_id_from_callback(cc)
        with self._lock:
            span_id = self._llm_by_invocation.get(inv_id)
            streaming_text = self._llm_streaming_text.get(span_id, "") if span_id else ""
        if not span_id:
            return
        attrs = _safe_llm_response_attrs(resp)
        # Separate thinking text from response text in the content parts.
        try:
            content = _safe_attr(resp, "content", None)
            if content is not None:
                parts = _safe_attr(content, "parts", None) or []
                # Response text: non-thought parts only.
                response_text = _parts_text(parts, thought=False)
                if response_text:
                    attrs["response_preview"] = response_text[:300]
                # Thinking text: thought=True parts. Write both the truncated
                # preview (cheap to render) and the full text so the frontend
                # Trajectory tab / drawer can show the complete reasoning
                # trace without a payload fetch. task_role="thinking" is used
                # by the frontend thinking.ts extractor.
                thinking_text = _parts_text(parts, thought=True)
                if thinking_text:
                    attrs["thinking_preview"] = thinking_text[:300]
                    attrs["thinking_text"] = thinking_text
                    attrs["has_thinking"] = True
        except Exception:
            pass
        finish_reason = _safe_attr(resp, "finish_reason", None)
        if finish_reason is not None:
            try:
                attrs["finish_reason"] = str(finish_reason)
            except Exception:
                pass
        # Authoritative context-window update: ADK exposes the real
        # prompt_token_count on llm_response.usage_metadata. Overwrites
        # the on_model_start estimate for this tick and every later
        # heartbeat until the next model call.
        try:
            usage = _safe_attr(resp, "usage_metadata", None)
            if usage is not None:
                prompt_tokens = _safe_attr(usage, "prompt_token_count", None)
                if prompt_tokens is not None:
                    prompt_tokens_int = int(prompt_tokens)
                    model_ver = _safe_attr(resp, "model_version", "") or ""
                    limit = _lookup_context_window_limit(str(model_ver))
                    # Preserve an earlier limit if model_version doesn't resolve.
                    if not limit:
                        _, prev_limit = self._client._context_window_snapshot
                        limit = prev_limit
                    self._client.set_context_window(prompt_tokens_int, limit)
        except Exception:
            pass
        # current_activity: prefer accumulated streaming text, then response.
        display_text = streaming_text or attrs.get("response_preview", "")
        if display_text:
            snippet = display_text[:80].replace("\n", " ")
            self._client.set_current_activity(f"Received: {snippet}\u2026")
        else:
            self._client.set_current_activity("Processing model response")
        payload = _safe_llm_response_payload(resp)
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="output",
        )
        self._mark_task_for_span(span_id, "COMPLETED")
        # Emit task_report on the enclosing INVOCATION span: what the LLM
        # just did and what it's planning to do next (tool calls, if any).
        with self._lock:
            invocation_span_id = self._invocations.get(inv_id)
        if invocation_span_id:
            planned: list[str] = []
            try:
                candidates = _safe_attr(resp, "candidates", None)
                if candidates:
                    for candidate in list(candidates)[:1]:
                        cand_content = _safe_attr(candidate, "content", None)
                        if cand_content is not None:
                            cand_parts = _safe_attr(cand_content, "parts", None) or []
                            for part in cand_parts:
                                fc = _safe_attr(part, "function_call", None)
                                if fc is not None:
                                    fc_name = _safe_attr(fc, "name", None)
                                    if fc_name:
                                        planned.append(f"call {fc_name}")
            except Exception:
                pass
            if planned:
                description = f"Planning: {', '.join(planned)}"
            else:
                description = "Processing response"
            if streaming_text:
                snippet = streaming_text[:80].replace("\n", " ")
                if planned:
                    description = f"{snippet}\u2026 \u2192 {description}"
                else:
                    description = snippet
            self._client.emit_span_update(
                invocation_span_id,
                attributes={"task_report": description, "current_task": description},
            )
        # LLM span is done — the current-LLM pointer is cleared so
        # subsequent tool calls link to the INVOCATION again if no new
        # LLM_CALL is open.
        with self._lock:
            if self._llm_by_invocation.get(inv_id) == span_id:
                self._llm_by_invocation.pop(inv_id, None)
            self._llm_stream_len.pop(span_id, None)
            self._llm_stream_ticks.pop(span_id, None)
            self._llm_streaming_text.pop(span_id, None)
            self._llm_thinking_text.pop(span_id, None)
            self._llm_to_invocation.pop(span_id, None)
            self._last_thinking_emit_len.pop(span_id, None)
            self._llm_thought_emit.pop(span_id, None)
        # Auto-refine on model_end has been removed. Refine is now
        # driven by the observer in HarmonografAgent, which calls
        # :meth:`detect_drift` / :meth:`refine_plan_on_drift` after each
        # inner-agent turn, and only when drift is actually detected.

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    # ===== reporting-tool handlers ====================================
    # These methods are dispatched from before_tool_callback when a
    # sub-agent calls one of the harmonograf report_task_* tools. They
    # apply the side effect directly (transition task state, emit a
    # status update, fire refine on drift) so the tool body itself can
    # stay a no-op at the agent level. The dispatcher returns a stub ACK
    # which before_tool_callback hands back to ADK, short-circuiting the
    # actual tool execution.
    # ------------------------------------------------------------------

    def _dispatch_reporting_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        hsession_id: str,
    ) -> dict[str, Any]:
        """Apply the side effect of a harmonograf reporting tool call.

        Returns an ACK dict that the caller hands back to ADK as the
        substituted tool result. Any per-handler exception is swallowed
        and logged so a malformed agent call cannot abort the run.
        """
        args = tool_args or {}
        try:
            if tool_name == "report_task_started":
                self._handle_report_task_started(hsession_id, args)
            elif tool_name == "report_task_progress":
                self._handle_report_task_progress(hsession_id, args)
            elif tool_name == "report_task_completed":
                self._handle_report_task_completed(hsession_id, args)
            elif tool_name == "report_task_failed":
                self._handle_report_task_failed(hsession_id, args)
            elif tool_name == "report_task_blocked":
                self._handle_report_task_blocked(hsession_id, args)
            elif tool_name == "report_new_work_discovered":
                self._handle_report_new_work_discovered(hsession_id, args)
            elif tool_name == "report_plan_divergence":
                self._handle_report_plan_divergence(hsession_id, args)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "reporting-tool handler %s raised; ignoring: %s",
                tool_name, exc,
            )
        return {"acknowledged": True}

    def _resolve_plan_task(
        self, hsession_id: str, task_id: str
    ) -> tuple[Optional[PlanState], Any, str]:
        """Look up ``(plan_state, task, plan_id)`` for ``task_id`` on the
        active plan for ``hsession_id``. Returns ``(None, None, "")`` if
        nothing matches — every handler tolerates that.
        """
        if not hsession_id or not task_id:
            return None, None, ""
        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
            if plan_state is None:
                return None, None, ""
            tracked = plan_state.tasks.get(task_id)
            return plan_state, tracked, plan_state.plan_id

    def _handle_report_task_started(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        task_id = str(args.get("task_id", "") or "")
        detail = str(args.get("detail", "") or "")
        plan_state, tracked, plan_id = self._resolve_plan_task(
            hsession_id, task_id
        )
        if tracked is None:
            log.info(
                "report_task_started: unknown task_id=%r (hsession=%s)",
                task_id, hsession_id,
            )
            return
        transitioned = False
        with self._lock:
            cur = getattr(tracked, "status", "") or "PENDING"
            if cur == "PENDING":
                transitioned = _set_task_status(tracked, "RUNNING")
        log.info(
            "report_task_started: task=%s prev=%s -> RUNNING (%s) detail=%r",
            task_id, cur, transitioned, detail[:80],
        )
        if transitioned and plan_id:
            try:
                self._client.submit_task_status_update(
                    plan_id, task_id, "RUNNING"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "report_task_started: submit_task_status_update failed: %s",
                    exc,
                )

    def _handle_report_task_progress(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        task_id = str(args.get("task_id", "") or "")
        if not task_id:
            return
        try:
            fraction = float(args.get("fraction", 0.0) or 0.0)
        except (TypeError, ValueError):
            fraction = 0.0
        fraction = max(0.0, min(1.0, fraction))
        detail = str(args.get("detail", "") or "")
        with self._lock:
            self._task_progress[task_id] = fraction
        log.info(
            "report_task_progress: task=%s fraction=%.2f detail=%r",
            task_id, fraction, detail[:80],
        )

    def _handle_report_task_completed(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        task_id = str(args.get("task_id", "") or "")
        summary = str(args.get("summary", "") or "")
        artifacts = args.get("artifacts") if isinstance(args.get("artifacts"), dict) else None
        plan_state, tracked, plan_id = self._resolve_plan_task(
            hsession_id, task_id
        )
        transitioned = False
        if tracked is not None:
            with self._lock:
                cur = getattr(tracked, "status", "") or "PENDING"
                if cur not in _TERMINAL_TASK_STATUSES:
                    transitioned = _set_task_status(tracked, "COMPLETED")
                    if transitioned:
                        self._task_reinvocation_count.pop(task_id, None)
                        self._recent_error_task_ids.discard(task_id)
        with self._lock:
            if summary:
                self._task_results[task_id] = summary
            if artifacts:
                self._task_artifacts[task_id] = dict(artifacts)
        log.info(
            "report_task_completed: task=%s transitioned=%s summary=%r",
            task_id, transitioned, summary[:120],
        )
        if transitioned and plan_id:
            try:
                self._client.submit_task_status_update(
                    plan_id, task_id, "COMPLETED"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "report_task_completed: submit_task_status_update failed: %s",
                    exc,
                )

    def _handle_report_task_failed(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        task_id = str(args.get("task_id", "") or "")
        reason = str(args.get("reason", "") or "")
        recoverable = bool(args.get("recoverable", True))
        if not task_id:
            return
        detail = (
            f"{reason} (recoverable={recoverable})"
            if reason
            else f"recoverable={recoverable}"
        )
        # mark_task_failed transitions the task and fires
        # _refine_after_task_failure with kind=task_failed; we rely on
        # that to drive the planner refine path so we don't double-fire.
        ok = self.mark_task_failed(hsession_id, task_id, detail)
        log.info(
            "report_task_failed: task=%s ok=%s reason=%r recoverable=%s",
            task_id, ok, reason[:80], recoverable,
        )

    def _handle_report_task_blocked(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        task_id = str(args.get("task_id", "") or "")
        blocker = str(args.get("blocker", "") or "")
        needed = str(args.get("needed", "") or "")
        if not task_id:
            return
        with self._lock:
            self._task_blockers[task_id] = blocker or needed
        plan_state, tracked, _plan_id = self._resolve_plan_task(
            hsession_id, task_id
        )
        log.info(
            "report_task_blocked: task=%s blocker=%r needed=%r",
            task_id, blocker[:80], needed[:80],
        )
        detail = (
            f"task {task_id} blocked: {blocker}"
            + (f" (needed: {needed})" if needed else "")
        )
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(kind="task_blocked", detail=detail),
                current_task=tracked,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("refine_plan_on_drift(task_blocked) raised: %s", exc)

    def _handle_report_new_work_discovered(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        parent_id = str(args.get("parent_task_id", "") or "")
        title = str(args.get("title", "") or "")
        description = str(args.get("description", "") or "")
        assignee = str(args.get("assignee", "") or "")
        suffix = f" assignee={assignee}" if assignee else ""
        detail = (
            f"new work under {parent_id}: {title}: {description}{suffix}"
        )
        plan_state, parent_task, _plan_id = self._resolve_plan_task(
            hsession_id, parent_id
        )
        log.info(
            "report_new_work_discovered: parent=%s title=%r assignee=%s",
            parent_id, title[:80], assignee,
        )
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(kind="new_work_discovered", detail=detail),
                current_task=parent_task,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "refine_plan_on_drift(new_work_discovered) raised: %s", exc
            )

    def _handle_report_plan_divergence(
        self, hsession_id: str, args: dict[str, Any]
    ) -> None:
        note = str(args.get("note", "") or "")
        suggested = str(args.get("suggested_action", "") or "")
        detail = (
            f"{note} (suggested: {suggested})" if suggested else note
        )
        log.info(
            "report_plan_divergence: note=%r suggested=%r",
            note[:120], suggested[:80],
        )
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(kind="plan_divergence", detail=detail),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("refine_plan_on_drift(plan_divergence) raised: %s", exc)

    # ===== end reporting-tool handlers ================================

    def on_tool_start(self, tool: Any, tool_args: dict[str, Any], tool_context: Any) -> None:
        self._metrics.callbacks_fired["on_tool_start"] += 1
        tool_name = _safe_attr(tool, "name", "") or ""
        if tool_name in REPORTING_TOOL_NAMES:
            self._metrics.reporting_tools_invoked[tool_name] += 1
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        inv_id = _invocation_id_from_callback(tool_context)
        parent = self._current_llm_span(inv_id) or self._get_invocation_span(inv_id)
        agent_id, hsession_id = self._route_from_callback_or_invocation(tool_context, inv_id)
        is_long_running = bool(_safe_attr(tool, "is_long_running", False))
        name = _safe_attr(tool, "name", "tool") or "tool"
        payload = _safe_json(tool_args)
        tool_attrs: dict[str, Any] = {"is_long_running": is_long_running, "tool_name": name}
        # Emit a preview of tool arguments so the popover can show what the
        # tool is doing (truncated to 300 chars).
        if tool_args:
            try:
                args_preview = json.dumps(tool_args, default=str, ensure_ascii=False)[:300]
                tool_attrs["tool_args_preview"] = args_preview
            except Exception:
                pass
        if _is_agent_tool(tool):
            target_agent_name = (
                _safe_attr(_safe_attr(tool, "agent", None), "name", "") or name
            )
            tool_activity = f"Transferring to {target_agent_name}"
        else:
            # Include the most informative arg in the activity string.
            tool_activity = f"Calling {name}"
            if tool_args:
                key_arg = _most_informative_arg(tool_args)
                if key_arg:
                    tool_activity = f"Calling {name}({key_arg})"
        self._client.set_current_activity(tool_activity)
        # Write as task_report on the TOOL_CALL span so it reaches the correct
        # agent row in the UI via the span-attribute path.
        tool_attrs["task_report"] = tool_activity
        # Planner binding: if this agent has a pending task on the
        # active plan, stamp the span with hgraf.task_id so the server
        # can bind it.
        tool_attrs = self._stamp_attrs_with_task(
            tool_attrs, agent_id, hsession_id, span_kind="TOOL_CALL"
        ) or tool_attrs
        span_id = self._client.emit_span_start(
            kind="TOOL_CALL",
            name=name,
            parent_span_id=parent,
            attributes=tool_attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="args",
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        # Build a human-readable label for STATUS_QUERY ("search_web(query='…')").
        tool_label = name
        if tool_args:
            key_arg = _most_informative_arg(tool_args)
            if key_arg:
                tool_label = f"{name}({key_arg})"
        with self._lock:
            self._tools[call_id] = span_id
            self._tool_labels[call_id] = tool_label
            if is_long_running:
                self._long_running.add(call_id)
        self._bind_span_to_task(span_id, tool_attrs)
        if is_long_running:
            self._client.emit_span_update(span_id, status="AWAITING_HUMAN")

        # AgentTool dispatch reads as a sub-agent transfer in the Gantt.
        # Emit a TRANSFER span on the PARENT agent's row (coordinator),
        # with LINK_INVOKED back to the child TOOL_CALL so the frontend
        # can draw the cross-row arrow into the sub-agent's lane.
        if _is_agent_tool(tool):
            target_agent_name = (
                _safe_attr(_safe_attr(tool, "agent", None), "name", "") or name
            )
            transfer_sid = self._client.emit_span_start(
                kind="TRANSFER",
                name=f"transfer_to_{target_agent_name}",
                parent_span_id=parent,
                attributes={
                    "target_agent": target_agent_name,
                    "via": "agent_tool",
                },
                links=[
                    {
                        "target_span_id": span_id,
                        "target_agent_id": target_agent_name,
                        "relation": "INVOKED",
                    }
                ],
                agent_id=agent_id or None,
                session_id=hsession_id or None,
            )
            self._client.emit_span_end(transfer_sid, status="COMPLETED")

    def on_tool_end(
        self,
        tool: Any,
        tool_context: Any,
        *,
        result: Optional[dict[str, Any]],
        error: Optional[BaseException],
    ) -> None:
        self._metrics.callbacks_fired["on_tool_end"] += 1
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        tool_name = _safe_attr(tool, "name", "tool") or "tool"
        inv_id = _invocation_id_from_callback(tool_context)
        with self._lock:
            span_id = self._tools.pop(call_id, None)
            self._tool_labels.pop(call_id, None)
            self._long_running.discard(call_id)
        if not span_id:
            return
        self._client.set_current_activity(f"Completed tool {tool_name}")
        if error is not None:
            self._client.emit_span_end(
                span_id,
                status="FAILED",
                error={"type": type(error).__name__, "message": str(error)},
            )
            # Record the bound task as having a tool error this turn so
            # the classifier-and-sweep can route it to FAILED. We have to
            # peek the span→task map BEFORE _mark_task_for_span pops it.
            with self._lock:
                bound_tid = self._span_to_task.get(span_id, "")
                if not bound_tid:
                    bound_tid = (
                        _forced_task_id_var.get()
                        or self._forced_current_task_id
                        or ""
                    )
                if bound_tid:
                    self._recent_error_task_ids.add(bound_tid)
            self._mark_task_for_span(span_id, "FAILED")
            return
        payload = _safe_json(result) if result is not None else None
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            payload=payload,
            payload_mime="application/json",
            payload_role="result",
        )
        self._mark_task_for_span(span_id, "COMPLETED")
        # Auto-refine on tool_end has been removed — see note in on_model_end.

    # ------------------------------------------------------------------
    # Events (state_delta + transfers)
    # ------------------------------------------------------------------

    def on_event(self, ic: Any, event: Any) -> None:
        self._metrics.callbacks_fired["on_event"] += 1
        inv_id = _safe_attr(ic, "invocation_id", "")
        agent_id, hsession_id = self._route_from_context(ic)

        # Streaming-partial events drive liveness ticks on the in-flight
        # LLM span (thinking summary, streaming_text, live task_report).
        # Handled verbatim by the partial helper; the rest of this method
        # processes the post-turn Event.actions signals.
        if _safe_attr(event, "partial", False):
            self._on_event_partial(inv_id, event)
            return

        actions = _safe_attr(event, "actions", None)
        if actions is None:
            return

        state_delta = _safe_attr(actions, "state_delta", None)
        if state_delta:
            self._on_event_state_delta(
                inv_id, agent_id, hsession_id, state_delta
            )

        transfer_to = _safe_attr(actions, "transfer_to_agent", None)
        if transfer_to:
            self._on_event_transfer(
                inv_id, agent_id, hsession_id, str(transfer_to)
            )

        if bool(_safe_attr(actions, "escalate", False)):
            self._on_event_escalate(inv_id, hsession_id)

    # --- on_event private helpers ------------------------------------

    def _on_event_partial(self, inv_id: str, event: Any) -> None:
        """Streaming-partial event path: thinking summary ticks, live
        task_report, streaming_text attribute on the LLM span. Preserved
        from the pre-signal implementation — ADK emits these with
        ``partial=True`` while the model streams; the final event (with
        ``partial`` False/None) is handled by after_model_callback.
        """
        with self._lock:
            llm_span_id = self._llm_by_invocation.get(inv_id)
        if not llm_span_id:
            return
        content = _safe_attr(event, "content", None)
        parts = _safe_attr(content, "parts", None) or [] if content else []

        partial_thinking = _parts_text(parts, thought=True)
        partial_response = _parts_text(parts, thought=False)
        total_text_len = sum(
            len(_safe_attr(p, "text", "") or "")
            for p in parts
            if isinstance(_safe_attr(p, "text", None), str)
        )

        _new_thinking_acc: Optional[str] = None
        with self._lock:
            if total_text_len > self._llm_stream_len.get(llm_span_id, 0):
                self._llm_stream_len[llm_span_id] = total_text_len
            ticks = self._llm_stream_ticks.get(llm_span_id, 0) + 1
            self._llm_stream_ticks[llm_span_id] = ticks
            cur_len = self._llm_stream_len[llm_span_id]

            if partial_thinking:
                acc = self._llm_thinking_text.get(llm_span_id, "") + partial_thinking
                if len(acc) > 600:
                    acc = "\u2026" + acc[-580:]
                self._llm_thinking_text[llm_span_id] = acc
                _new_thinking_acc = acc
            if partial_response:
                acc = self._llm_streaming_text.get(llm_span_id, "") + partial_response
                if len(acc) > 500:
                    acc = "\u2026" + acc[-480:]
                self._llm_streaming_text[llm_span_id] = acc

            thinking_text = self._llm_thinking_text.get(llm_span_id)
            streaming_text = self._llm_streaming_text.get(llm_span_id)

        update_attrs: dict[str, Any] = {
            "streaming_text_len": cur_len,
            "streaming_tick": ticks,
        }
        if streaming_text:
            update_attrs["streaming_text"] = streaming_text
        if thinking_text:
            update_attrs["thinking_text"] = thinking_text
        self._client.emit_span_update(llm_span_id, attributes=update_attrs)

        if _new_thinking_acc:
            self.emit_thinking_as_task_report(llm_span_id, _new_thinking_acc)

        if ticks % 5 == 0:
            if thinking_text:
                snippet = thinking_text[-100:].replace("\n", " ").strip()
                live_text = f"Thinking: \u2026{snippet}"
            elif streaming_text:
                snippet = streaming_text[-100:].replace("\n", " ").strip()
                live_text = f"Responding: \u2026{snippet}"
            else:
                live_text = None
            if live_text:
                self._client.set_current_activity(live_text)
                self._emit_task_report(inv_id, live_text)

    def _on_event_state_delta(
        self,
        inv_id: str,
        agent_id: str,
        hsession_id: str,
        state_delta: Any,
    ) -> None:
        """Event-stream ``state_delta`` observation.

        1. Surfaces every delta key as a ``state_delta.<key>`` attribute
           on the enclosing span (preserved pre-rewrite behavior — the UI
           popover reads these).
        2. Filters harmonograf-namespaced keys via
           :func:`state_protocol.extract_agent_writes` and routes them:
           ``task_outcome`` → state-machine transitions + refine on
           failed/blocked; ``divergence_flag`` → refine with
           ``agent_reported_divergence``; ``agent_note`` → attribute on
           the invocation span for UI visibility.
        """
        target_span = (
            self._current_llm_span(inv_id) or self._get_invocation_span(inv_id)
        )
        if target_span:
            attrs = {
                f"state_delta.{k}": _stringify(v) for k, v in state_delta.items()
            }
            if attrs:
                self._client.emit_span_update(target_span, attributes=attrs)

        try:
            writes = _sp.extract_agent_writes({}, state_delta)
        except Exception as exc:  # noqa: BLE001
            log.debug("on_event: extract_agent_writes failed: %s", exc)
            return
        if not writes:
            return

        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)

        outcome_map = writes.get(_sp.KEY_TASK_OUTCOME)
        if isinstance(outcome_map, Mapping) and plan_state is not None:
            for tid, outcome in outcome_map.items():
                if not isinstance(tid, str):
                    continue
                tracked = plan_state.tasks.get(tid)
                if tracked is None:
                    continue
                val = str(outcome or "").lower()
                if val == "completed":
                    if _set_task_status(tracked, "COMPLETED"):
                        log.info(
                            "on_event: agent reported task %s COMPLETED via state_delta",
                            tid,
                        )
                elif val == "failed":
                    if _set_task_status(tracked, "FAILED"):
                        log.info(
                            "on_event: agent reported task %s FAILED via state_delta",
                            tid,
                        )
                    try:
                        self.refine_plan_on_drift(
                            hsession_id,
                            DriftReason(
                                kind="task_failed_by_agent",
                                detail=(
                                    f"agent reported task {tid} failed via state_delta"
                                ),
                            ),
                            current_task=tracked,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "on_event: refine (task_failed_by_agent) raised: %s",
                            exc,
                        )
                elif val == "blocked":
                    log.info(
                        "on_event: agent reported task %s BLOCKED via state_delta",
                        tid,
                    )
                    try:
                        self.refine_plan_on_drift(
                            hsession_id,
                            DriftReason(
                                kind="task_blocked",
                                detail=(
                                    f"agent reported task {tid} blocked via state_delta"
                                ),
                            ),
                            current_task=tracked,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "on_event: refine (task_blocked) raised: %s", exc
                        )

        if bool(writes.get(_sp.KEY_DIVERGENCE_FLAG)):
            note_text = _sp._safe_str(writes.get(_sp.KEY_AGENT_NOTE, ""))
            try:
                self.refine_plan_on_drift(
                    hsession_id,
                    DriftReason(
                        kind="agent_reported_divergence",
                        detail=note_text or "agent set harmonograf.divergence_flag",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("on_event: refine (divergence) raised: %s", exc)

        note_val = writes.get(_sp.KEY_AGENT_NOTE)
        if note_val is not None:
            inv_span = self._get_invocation_span(inv_id)
            if inv_span:
                self._client.emit_span_update(
                    inv_span,
                    attributes={"agent_note": _sp._safe_str(note_val)},
                )

    def _on_event_transfer(
        self,
        inv_id: str,
        agent_id: str,
        hsession_id: str,
        transfer_to: str,
    ) -> None:
        """ADK ``Event.actions.transfer_to_agent`` handler.

        Always emits the TRANSFER span for telemetry (so the Gantt keeps
        showing the edge). If an active plan exists, also compares the
        target against the plan's next expected assignee and fires
        ``unexpected_transfer`` drift on mismatch — ``refine_plan_on_drift``
        decides whether to actually revise.
        """
        parent = self._get_invocation_span(inv_id)
        transfer_sid = self._client.emit_span_start(
            kind="TRANSFER",
            name=f"transfer_to_{transfer_to}",
            parent_span_id=parent,
            attributes={"target_agent": transfer_to},
            links=[{"target_agent_id": transfer_to, "relation": "INVOKED"}],
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        self._client.emit_span_end(transfer_sid, status="COMPLETED")

        with self._lock:
            plan_state = self._active_plan_by_session.get(hsession_id)
        if plan_state is None:
            return
        expected = _expected_next_assignee(plan_state)
        if not expected:
            return
        if _normalize_agent_id(transfer_to) == _normalize_agent_id(expected):
            return
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(
                    kind="unexpected_transfer",
                    detail=(
                        f"transferred to {transfer_to} but plan expected {expected}"
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("on_event: refine (unexpected_transfer) raised: %s", exc)

    def _on_event_escalate(self, inv_id: str, hsession_id: str) -> None:
        """ADK ``Event.actions.escalate`` handler — surface the flag on
        the invocation span and fire ``agent_escalated`` drift so the
        planner can react (usually by re-routing to a different sub-agent
        or back to the coordinator).
        """
        inv_span = self._get_invocation_span(inv_id)
        if inv_span:
            self._client.emit_span_update(
                inv_span, attributes={"escalated": True}
            )
        try:
            self.refine_plan_on_drift(
                hsession_id,
                DriftReason(
                    kind="agent_escalated",
                    detail="sub-agent escalated — likely needs different handling",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("on_event: refine (agent_escalated) raised: %s", exc)

    # ------------------------------------------------------------------
    # Control → session mutations
    # ------------------------------------------------------------------

    def queue_session_mutation(self, key: str, value: str) -> None:
        with self._lock:
            self._pending_mutations.append((key, value))

    def pending_session_mutations(self) -> list[tuple[str, str]]:
        with self._lock:
            out = list(self._pending_mutations)
            self._pending_mutations.clear()
            return out

    def set_pending_steer(self, text: str) -> None:
        """Store a STEER instruction to be injected at the next model call boundary."""
        with self._lock:
            self._pending_steer = text

    def consume_pending_steer(self) -> Optional[str]:
        """Consume and return any pending STEER text (None if none queued)."""
        with self._lock:
            text = self._pending_steer
            self._pending_steer = None
            return text

    def has_pending_steer(self) -> bool:
        """Return True if a STEER instruction is queued (not yet consumed)."""
        with self._lock:
            return self._pending_steer is not None

    # ------------------------------------------------------------------
    # Interrupt infrastructure (cancel / pause / resume)
    # ------------------------------------------------------------------

    def register_running_task(
        self, task: asyncio.Task, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Called from AdkAdapter.run_async() on the ADK event loop."""
        with self._task_lock:
            self._running_task = task
            self._adk_loop = loop

    def clear_running_task(self) -> None:
        with self._task_lock:
            self._running_task = None
            self._adk_loop = None

    def cancel_running_task(self) -> bool:
        """Thread-safe cancel. Returns True if a task was scheduled for cancellation."""
        with self._task_lock:
            task = self._running_task
            loop = self._adk_loop
        if task is None or loop is None or task.done():
            return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def _do_pause(self) -> None:
        """Must be called on the ADK event loop (via call_soon_threadsafe)."""
        if self._pause_event is None:
            self._pause_event = asyncio.Event()
            # asyncio.Event() is created with no flags set — waiting will block.

    def _do_resume(self) -> None:
        """Must be called on the ADK event loop (via call_soon_threadsafe)."""
        evt = self._pause_event
        self._pause_event = None
        if evt is not None:
            evt.set()  # unblock any before_model_callback waiting on it

    def set_paused(self, paused: bool) -> None:
        """Thread-safe. Schedules _do_pause or _do_resume on the ADK loop."""
        with self._task_lock:
            loop = self._adk_loop
        if loop is None:
            return
        if paused:
            loop.call_soon_threadsafe(self._do_pause)
        else:
            loop.call_soon_threadsafe(self._do_resume)

    def _cleanup_cancelled_spans(self) -> None:
        """End all in-flight spans with CANCELLED status and clear tracking state.

        Called after asyncio.CancelledError is caught in AdkAdapter.run_async().
        Ends innermost spans first (tools → LLM calls → invocations) to preserve
        a sensible parent–child ordering in the trace.
        """
        with self._lock:
            tool_span_ids = list(self._tools.values())
            llm_span_ids = list(set(self._llm_by_invocation.values()))
            inv_span_ids = list(self._invocations.values())
            route_tokens = list(self._route_tokens.values())

            self._tools.clear()
            self._tool_labels.clear()
            self._long_running.clear()
            self._llm_by_invocation.clear()
            self._llm_stream_len.clear()
            self._llm_stream_ticks.clear()
            self._llm_streaming_text.clear()
            self._llm_thinking_text.clear()
            self._llm_to_invocation.clear()
            self._last_thinking_emit_len.clear()
            self._invocations.clear()
            self._invocation_route.clear()
            self._active_plan_by_session.clear()
            self._span_to_task.clear()
            self._pending_plan_guidance = None
            self._last_injected_plan_guidance.clear()
            self._forced_current_task_id = ""
            self._route_tokens.clear()

        for token in route_tokens:
            try:
                self._current_root_hsession_var.reset(token)
            except (LookupError, ValueError):
                pass

        for span_id in tool_span_ids:
            try:
                self._client.emit_span_end(span_id, status="CANCELLED")
            except Exception:
                pass
        for span_id in llm_span_ids:
            try:
                self._client.emit_span_end(span_id, status="CANCELLED")
            except Exception:
                pass
        for span_id in inv_span_ids:
            try:
                self._client.emit_span_end(span_id, status="CANCELLED")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def emit_thinking_as_task_report(self, llm_span_id: str, thinking_text: str) -> None:
        """Emit a task_report update from thinking text on the parent invocation span.

        Rate-limited: only emits after every ~200 new characters of thinking to
        avoid flooding the server with tiny fragments.
        """
        if not thinking_text or len(thinking_text) < 20:
            return
        with self._lock:
            last_len = self._last_thinking_emit_len.get(llm_span_id, 0)
            if len(thinking_text) - last_len < 200:
                return
            self._last_thinking_emit_len[llm_span_id] = len(thinking_text)
            inv_span_id = self._llm_to_invocation.get(llm_span_id)

        if not inv_span_id:
            return
        summary = _summarize_thinking(thinking_text)
        if not summary:
            return
        report = f"Thinking: {summary}"
        log.info("emit_thinking_as_task_report inv=%s report=%r", inv_span_id, report)
        self._client.emit_span_update(inv_span_id, attributes={"task_report": report})

    def _emit_task_report(self, inv_id: str, text: str) -> None:
        """Write ``task_report`` onto the INVOCATION span for ``inv_id``.

        Span-attribute updates carry the span's own ``agent_id`` (the ADK
        agent name string), so ``publish_task_report`` on the server always
        reaches the correct agent row — regardless of the ``agent_id`` the
        transport registered with during the Hello handshake.  This is the
        reliable path for live current-task display.
        """
        with self._lock:
            span_id = self._invocations.get(inv_id)
        if span_id:
            self._client.emit_span_update(span_id, attributes={"task_report": text})

    def _get_invocation_span(self, inv_id: str) -> Optional[str]:
        with self._lock:
            return self._invocations.get(inv_id)

    def _current_llm_span(self, inv_id: str) -> Optional[str]:
        with self._lock:
            return self._llm_by_invocation.get(inv_id)

    def current_llm_span_id(self, inv_id: str) -> Optional[str]:
        """Public accessor for the in-flight LLM_CALL span id for
        ``inv_id``, or ``None`` if no LLM call is open. Used by
        :class:`HarmonografAgent` to attach llm.thought updates
        mined from event streams."""
        return self._current_llm_span(inv_id)

    def current_task_info(
        self,
    ) -> Optional[dict[str, str]]:
        """Return a snapshot of the most-recently-started task on the
        active plan: ``{id, title, description, agent_id}``. Returns
        ``None`` if no task has been marked RUNNING yet."""
        with self._lock:
            if not self._current_task_id:
                return None
            return {
                "id": self._current_task_id,
                "title": self._current_task_title,
                "description": self._current_task_description,
                "agent_id": self._current_task_agent_id,
            }

    def record_llm_thought(self, llm_span_id: str, chunk: str) -> Optional[str]:
        """Append ``chunk`` to the accumulated thought text for
        ``llm_span_id`` and return the new aggregate. Bounded to ~2 KiB
        so runaway reasoning traces don't balloon memory.
        """
        if not llm_span_id or not chunk:
            return None
        with self._lock:
            acc = self._llm_thought_emit.get(llm_span_id, "")
            acc = (acc + chunk) if acc else chunk
            if len(acc) > 2048:
                acc = "\u2026" + acc[-2000:]
            self._llm_thought_emit[llm_span_id] = acc
            return acc

    def _route_from_callback_or_invocation(
        self, cc: Any, inv_id: str
    ) -> tuple[str, str]:
        """Resolve (agent_id, harmonograf_session_id) for a callback that
        carries an InvocationContext (CallbackContext, ToolContext, …).
        Falls back to whatever route the invocation was opened under so a
        SpanEnd that can't see the context still lands on the same row.
        """
        ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(
            cc, "invocation_context", None
        )
        if ic is None and _safe_attr(cc, "agent", None) is not None:
            ic = cc
        agent_id, hsession_id = (
            self._route_from_context(ic) if ic is not None else ("", "")
        )
        if not agent_id or not hsession_id:
            with self._lock:
                fallback = self._invocation_route.get(inv_id, ("", ""))
            agent_id = agent_id or fallback[0]
            hsession_id = hsession_id or fallback[1]
        return agent_id, hsession_id

    def _route_from_context(
        self, ic: Any, *, opening_root: bool = False
    ) -> tuple[str, str]:
        """Resolve (agent_id, harmonograf_session_id) from an
        InvocationContext-shaped object.

        Routing rules, in order:

          1. If the ADK session id is already in the pool, reuse it —
             this covers repeat callbacks on an established session.
          2. Otherwise, consult the ContextVar. If a root hsession is
             already set in this asyncio Task's Context, the current
             invocation is nested under it (AgentTool sub-run, which
             executes inside the parent task), so alias to it.
          3. Otherwise, if ``opening_root`` is set, mint a brand-new
             harmonograf session id for this ADK session id.

        Concurrent top-level /run calls land in independent asyncio
        Tasks, so each sees an empty ContextVar and hits rule 3 — no
        cross-request aliasing. AgentTool sub-invocations run within
        the parent Task, so they see the parent's ContextVar and hit
        rule 2.
        """
        if ic is None:
            return "", ""
        agent = _safe_attr(ic, "agent", None)
        agent_id = _safe_attr(agent, "name", "") if agent is not None else ""
        session = _safe_attr(ic, "session", None)
        adk_session_id = (
            _safe_attr(session, "id", "") if session is not None else ""
        )
        with self._lock:
            mapped = self._adk_to_h_session.get(adk_session_id, "")
        if not mapped:
            parent_hsession = self._current_root_hsession_var.get()
            if parent_hsession:
                mapped = parent_hsession
            elif opening_root:
                mapped = _harmonograf_session_id_for_adk(adk_session_id)
            if adk_session_id and mapped:
                with self._lock:
                    self._adk_to_h_session.setdefault(adk_session_id, mapped)
                    mapped = self._adk_to_h_session[adk_session_id]
        return agent_id or "", mapped


# ---------------------------------------------------------------------------
# Module-level helpers — defensive against ADK internals moving.
# ---------------------------------------------------------------------------


_HSESSION_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _harmonograf_session_id_for_adk(adk_session_id: str) -> str:
    """Build a harmonograf session_id (regex ^[a-zA-Z0-9_-]{1,128}$) that
    encodes an ADK session id. Non-matching characters are replaced with
    ``_``; oversize ids are truncated to fit the 128-char limit while
    keeping the ``adk_`` prefix readable.
    """
    if not adk_session_id:
        return ""
    safe = "".join(c if c in _HSESSION_SAFE else "_" for c in adk_session_id)
    out = f"adk_{safe}"
    return out[:128]


def _classify_tool_response(
    tool_name: str, response: Any
) -> Optional[DriftReason]:
    """Inspect a regular tool's return value and return a deferential
    drift signal if it looks like a failure or an unexpectedly-empty
    result. The caller (after_tool_callback) hands the resulting
    DriftReason to ``refine_plan_on_drift`` — the planner has the final
    say on whether to revise.

    Returns ``None`` for results that look healthy (non-empty, no error
    markers, not a sentinel for "no data").
    """
    if response is None:
        return DriftReason(
            kind="tool_returned_error",
            detail=f"{tool_name}: returned None",
        )
    if isinstance(response, Mapping):
        if "error" in response and response.get("error"):
            return DriftReason(
                kind="tool_returned_error",
                detail=f"{tool_name}: {response.get('error')}",
            )
        status = response.get("status")
        if isinstance(status, str) and status.lower() in (
            "failed", "failure", "error",
        ):
            return DriftReason(
                kind="tool_returned_error",
                detail=f"{tool_name}: status={status}",
            )
        if "ok" in response and response.get("ok") is False:
            return DriftReason(
                kind="tool_returned_error",
                detail=f"{tool_name}: ok=False",
            )
        if not response:
            return DriftReason(
                kind="tool_unexpected_result",
                detail=f"{tool_name}: empty dict",
            )
        return None
    if isinstance(response, (list, tuple, set)) and len(response) == 0:
        return DriftReason(
            kind="tool_unexpected_result",
            detail=f"{tool_name}: empty {type(response).__name__}",
        )
    if isinstance(response, str) and not response.strip():
        return DriftReason(
            kind="tool_unexpected_result",
            detail=f"{tool_name}: empty string",
        )
    return None


def _is_agent_tool(tool: Any) -> bool:
    """Structural check — True when ``tool`` is an ADK ``AgentTool``.

    Uses ``isinstance`` (preferred) when ADK is importable, falling back
    to a duck-typed check for a ``.agent`` attribute that itself looks
    like an ADK agent. Name-based matching is deliberately avoided.
    """
    try:
        from google.adk.tools.agent_tool import AgentTool  # type: ignore

        if isinstance(tool, AgentTool):
            return True
    except Exception:
        pass
    agent = getattr(tool, "agent", None)
    if agent is None:
        return False
    return hasattr(agent, "name") and hasattr(agent, "description")


def _safe_attr(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    try:
        val = getattr(obj, name, default)
    except Exception:
        return default
    return val if val is not None else default


def _agent_from_context(obj: Any) -> Any:
    """Return the ADK agent associated with an ``InvocationContext``,
    ``CallbackContext``, or ``ToolContext`` — or ``None`` if unavailable.

    Handles both the direct ``.agent`` attribute (present on
    ``InvocationContext``) and the nested ``._invocation_context.agent``
    attribute (present on ``CallbackContext`` / ``ToolContext``).
    """
    direct = _safe_attr(obj, "agent", None)
    if direct is not None:
        return direct
    ic = _safe_attr(obj, "_invocation_context", None) or _safe_attr(
        obj, "invocation_context", None
    )
    return _safe_attr(ic, "agent", None) if ic is not None else None


def _is_harmonograf_agent_context(obj: Any) -> bool:
    """True when the current agent on an IC / CC / TC is a
    :class:`HarmonografAgent`.

    HarmonografAgent is a meta-orchestration wrapper and must be
    invisible to telemetry — callbacks guarded on this predicate either
    no-op or substitute the wrapper's ``inner_agent`` so spans land on
    the real coordinator row instead of a phantom ``harmonograf`` row.
    The wrapper's own ``_run_async_impl`` drives the explicit
    ``maybe_run_planner(ctx, host_agent=inner_agent)`` path, so plans
    are submitted exactly once per invocation.
    """
    return bool(
        getattr(_agent_from_context(obj), "_is_harmonograf_agent", False)
    )


class _IcWithSubstitutedAgent:
    """Read-only proxy over an ADK ``InvocationContext`` that substitutes
    a different ``.agent``. Every other attribute falls through to the
    wrapped IC unchanged — including ``invocation_id``, ``session``,
    ``user_content``, and any private callback state the state machine
    reads via :func:`_safe_attr`.

    Used by :class:`HarmonografAdkPlugin` to present the inner agent's
    identity to telemetry for callbacks that receive the runner's
    top-level IC (whose ``.agent`` is always the root — HarmonografAgent
    when wrapping). Purely routing-level: no mutation of the underlying
    IC.
    """

    __slots__ = ("_ic", "agent")

    def __init__(self, ic: Any, agent: Any) -> None:
        object.__setattr__(self, "_ic", ic)
        object.__setattr__(self, "agent", agent)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ic, name)


def _extract_user_request_from_ic(ic: Any) -> str:
    """Pull the user's text message out of an ADK InvocationContext.

    ADK stores the incoming user message in ``ic.user_content`` as a
    ``genai.types.Content`` with ``parts=[Part(text=...)]``. Some
    callers may instead pass ``new_message`` or keep a copy on
    ``session.events[-1]``. We try each location and concatenate all
    text parts found in the first non-empty source.
    """
    candidates = []
    uc = _safe_attr(ic, "user_content", None)
    if uc is not None:
        candidates.append(uc)
    nm = _safe_attr(ic, "new_message", None)
    if nm is not None:
        candidates.append(nm)
    # Fall back to the last event's content on the session.
    session = _safe_attr(ic, "session", None)
    events = _safe_attr(session, "events", None) if session is not None else None
    if events:
        try:
            last = events[-1]
            content = _safe_attr(last, "content", None)
            if content is not None:
                candidates.append(content)
        except Exception:
            pass
    for c in candidates:
        parts = _safe_attr(c, "parts", None) or []
        pieces: list[str] = []
        for p in parts:
            text = _safe_attr(p, "text", None)
            if isinstance(text, str) and text:
                pieces.append(text)
        joined = " ".join(pieces).strip()
        if joined:
            return joined
    return ""


def _normalize_agent_id(raw: str) -> str:
    """Lowercase + strip hyphens/underscores/whitespace for fuzzy match."""
    if not raw:
        return ""
    return raw.lower().replace("-", "").replace("_", "").replace(" ", "")


def _canonicalize_assignee(raw: str, known_agents: list[str]) -> str:
    """Resolve ``raw`` against ``known_agents`` returning the canonical
    name. LLM planners hallucinate small formatting variations of agent
    names ("research-agent" vs "research_agent", "Research_Agent", or
    truncations like "research"); this helper tolerates all three.

    Resolution order:
      1. Exact match (cheap early return).
      2. Case/separator-insensitive exact match.
      3. Case/separator-insensitive prefix/substring match (either
         direction — handles LLM truncations).
      4. Give up and return ``raw`` unchanged so downstream code can
         still log/display what the LLM produced.
    """
    if not raw:
        return ""
    if raw in known_agents:
        return raw
    norm_raw = _normalize_agent_id(raw)
    if not norm_raw:
        return raw
    for agent in known_agents:
        if _normalize_agent_id(agent) == norm_raw:
            return agent
    for agent in known_agents:
        norm_agent = _normalize_agent_id(agent)
        if not norm_agent:
            continue
        if norm_raw.startswith(norm_agent) or norm_agent.startswith(norm_raw):
            return agent
    return raw


def _canonicalize_plan_assignees(
    plan: Any,
    known_agents: list[str],
    host_agent_name: str = "",
) -> None:
    """Rewrite every task's ``assignee_agent_id`` on ``plan`` in place so
    it matches one of ``known_agents`` exactly. Silently no-ops when the
    plan has no tasks or when the known list is empty.

    Empty assignees are backfilled to the first NON-HOST known agent: the
    coordinator/host typically lives at index 0 of ``known_agents`` (it's
    the agent we asked for the available list of), and routing tasks to
    the coordinator means the coordinator runs them itself instead of
    delegating to a worker. If ``host_agent_name`` is provided and matches
    ``known_agents[0]``, the next entry is used as the backfill target.
    Otherwise ``known_agents[0]`` is used as a last-resort fallback.

    Non-empty assignees that canonicalization can't resolve to a real
    agent (e.g. the LLM hallucinated a role) are preserved as-is but
    logged at WARNING so operators can see the drift surface in the UI
    instead of silently being swallowed.
    """
    if plan is None or not known_agents:
        return
    tasks = getattr(plan, "tasks", None) or []
    fallback = known_agents[0]
    if (
        host_agent_name
        and known_agents[0] == host_agent_name
        and len(known_agents) > 1
    ):
        fallback = known_agents[1]
    elif host_agent_name and known_agents[0] == host_agent_name:
        log.warning(
            "planner: only known agent is the host %r; cannot backfill "
            "empty assignees to a worker — leaving empty",
            host_agent_name,
        )
        fallback = ""
    known_set = set(known_agents)
    for t in tasks:
        raw = getattr(t, "assignee_agent_id", "") or ""
        if not raw:
            if not fallback:
                log.warning(
                    "planner: task %s has empty assignee and no non-host "
                    "fallback available — leaving empty",
                    getattr(t, "id", "?"),
                )
                continue
            log.info(
                "planner: task %s has empty assignee; reassigning to %r",
                getattr(t, "id", "?"), fallback,
            )
            try:
                t.assignee_agent_id = fallback
            except Exception:  # noqa: BLE001
                pass
            continue
        canonical = _canonicalize_assignee(raw, known_agents)
        if canonical in known_set:
            if canonical != raw:
                try:
                    t.assignee_agent_id = canonical
                except Exception:  # noqa: BLE001
                    pass
            continue
        # Canonicalization couldn't resolve. Warn so the drift is
        # visible to operators; preserve the raw value so walker
        # behaviour matches the planner's intent (tests cover the
        # "assigned to a non-matching agent" path this way).
        log.warning(
            "planner: task %s assignee %r not in available agents %s; "
            "preserving unresolved — task may stay PENDING until refine",
            getattr(t, "id", "?"), raw, known_agents,
        )


def _collect_available_agents_for(agent: Any) -> list[str]:
    """Collect agent ids visible from ``agent``: the agent itself plus
    its immediate sub_agents and agent-tools. Deduplicated, ordered.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _push(name: str) -> None:
        if name and name not in seen:
            out.append(name)
            seen.add(name)

    if agent is None:
        return out
    _push(_safe_attr(agent, "name", "") or "")
    for sub in (_safe_attr(agent, "sub_agents", None) or []):
        _push(_safe_attr(sub, "name", "") or "")
    for tool in (_safe_attr(agent, "tools", None) or []):
        wrapped = _safe_attr(tool, "agent", None)
        if wrapped is not None:
            _push(_safe_attr(wrapped, "name", "") or "")
    return out


def _collect_available_agents(ic: Any) -> list[str]:
    """Backwards-compatible wrapper around
    :func:`_collect_available_agents_for` that reads the host agent from
    the invocation context.
    """
    return _collect_available_agents_for(_safe_attr(ic, "agent", None))


def _invocation_id_from_callback(cc: Any) -> str:
    # CallbackContext exposes invocation_context via private attr in
    # current ADK; fall back to invocation_id if present directly.
    inv_id = _safe_attr(cc, "invocation_id", "")
    if inv_id:
        return inv_id
    ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(cc, "invocation_context", None)
    return _safe_attr(ic, "invocation_id", "")


def _safe_json(obj: Any) -> Optional[bytes]:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
    except Exception:
        return None


def _event_text_len(event: Any) -> int:
    """Cumulative text length across all text parts in an event's content."""
    content = _safe_attr(event, "content", None)
    if content is None:
        return 0
    parts = _safe_attr(content, "parts", None) or []
    total = 0
    for p in parts:
        text = _safe_attr(p, "text", None)
        if isinstance(text, str):
            total += len(text)
    return total


def _event_text(event: Any) -> str:
    """Concatenated text across all text parts in an event's content."""
    content = _safe_attr(event, "content", None)
    if content is None:
        return ""
    parts = _safe_attr(content, "parts", None) or []
    pieces: list[str] = []
    for p in parts:
        text = _safe_attr(p, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


def _stringify(v: Any) -> str:
    try:
        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:
        return str(v)


def _summarise_for_refine(result: Any) -> str:
    """Short, safe string summary of a tool result for refine prompts."""
    if result is None:
        return ""
    try:
        text = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        text = str(result)
    return text[:500]


def _most_informative_arg(tool_args: dict[str, Any]) -> str:
    """Return a short 'key=value' string for the most descriptive tool argument.

    Priority order: string args > everything else, shortest first (so we don't
    truncate big blobs). The result is capped at 60 chars so it fits on one line.
    """
    if not tool_args:
        return ""
    # Prefer string-valued args — they're human-readable.
    str_args = {k: v for k, v in tool_args.items() if isinstance(v, str)}
    candidates = str_args or tool_args
    # Pick the shortest value as the most likely to be a clean identifier/query.
    key = min(candidates, key=lambda k: len(str(candidates[k])))
    val = str(candidates[key])
    if len(val) > 50:
        val = val[:47] + "…"
    return f"{key}={val!r}"


def _part_is_thought(part: Any) -> bool:
    """Return True when a Content Part is a thinking/reasoning block.

    Handles two conventions:
    - ADK / google.genai Part with ``thought=True``.
    - OpenAI-shaped parts that expose ``reasoning_content`` or ``reasoning``
      without a plain text body — reasoning by construction.
    """
    if bool(_safe_attr(part, "thought", False)):
        return True
    if _safe_attr(part, "reasoning_content", None) or _safe_attr(part, "reasoning", None):
        if not _safe_attr(part, "text", None):
            return True
    return False


def _part_thought_text(part: Any) -> str:
    """Return the reasoning text for a thought part, covering both shapes."""
    for key in ("text", "reasoning_content", "reasoning"):
        t = _safe_attr(part, key, None)
        if isinstance(t, str) and t:
            return t
    return ""


def _parts_text(parts: Any, thought: bool) -> str:
    """Concatenate text from parts that match the given thought flag."""
    pieces: list[str] = []
    for p in (parts or []):
        if _part_is_thought(p) == thought:
            text = _part_thought_text(p) if thought else _safe_attr(p, "text", None)
            if isinstance(text, str) and text:
                pieces.append(text)
    return "".join(pieces)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_ACTION_HINTS = (
    "call", "calling", "invoke", "invoking", "use", "using",
    "search", "fetch", "query", "analyze", "compute", "plan",
    "draft", "writ", "check", "verify", "tool", "next",
)


def _summarize_thinking(text: str, max_chars: int = 200) -> str:
    """Extract a human-readable live summary from accumulated thinking text.

    Strategy:
    - Split the buffer into sentences on ``.``/``!``/``?``/newline.
    - Walk from the most recent sentence backwards and pick the first
      complete one that has at least 8 chars; prefer one that contains an
      action verb / tool reference.
    - Fall back to an ellipsised tail when no clean sentence boundary
      exists.
    - Always cap at ``max_chars``.
    """
    if not text:
        return ""
    cleaned = text.replace("\u2026", "").strip()
    if not cleaned:
        return ""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(cleaned) if s.strip()]
    # Drop a trailing fragment with no terminal punctuation — it is the
    # in-flight sentence and likely incomplete; the previous sentence is
    # what the model just *finished* thinking.
    complete: list[str] = []
    if sentences:
        for s in sentences[:-1]:
            complete.append(s)
        last = sentences[-1]
        if last and last[-1:] in ".!?":
            complete.append(last)

    def _truncate(s: str) -> str:
        s = s.replace("\n", " ").strip()
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 1].rstrip() + "\u2026"

    if complete:
        for s in reversed(complete):
            low = s.lower()
            if any(h in low for h in _ACTION_HINTS) and len(s) >= 8:
                return _truncate(s)
        for s in reversed(complete):
            if len(s) >= 8:
                return _truncate(s)

    tail = cleaned[-(max_chars - 1):].replace("\n", " ").strip()
    return f"\u2026{tail}" if tail else ""


def _safe_llm_request_payload(req: Any) -> Optional[bytes]:
    """Serialize the full LLM request: model, system prompt, tool names, messages."""
    try:
        payload: dict[str, Any] = {}

        model = _safe_attr(req, "model", "")
        if model:
            payload["model"] = str(model)

        config = _safe_attr(req, "config", None)
        if config is not None:
            # System instruction — important for understanding agent behaviour.
            sys_instr = _safe_attr(config, "system_instruction", None)
            if sys_instr:
                payload["system_instruction"] = str(sys_instr)[:2000]

            # Available tool names — shows what the agent can call.
            tools_raw = _safe_attr(config, "tools", None)
            if tools_raw:
                try:
                    names: list[str] = []
                    for t in tools_raw:
                        n = _safe_attr(t, "name", None)
                        if n:
                            names.append(str(n))
                        else:
                            # Tool object may wrap multiple FunctionDeclarations.
                            for fd in (_safe_attr(t, "function_declarations", None) or []):
                                fn = _safe_attr(fd, "name", None)
                                if fn:
                                    names.append(str(fn))
                    if names:
                        payload["tools"] = names
                except Exception:
                    pass

        # Conversation history (contents).
        contents = _safe_attr(req, "contents", None)
        if contents is not None:
            dumps: list[Any] = []
            for item in contents:
                d = getattr(item, "model_dump", None)
                dumps.append(d(mode="json") if callable(d) else str(item))
            payload["contents"] = dumps

        return _safe_json(payload)
    except Exception:
        return None


def _safe_llm_response_attrs(resp: Any) -> dict[str, Any]:
    """Extract scalar attributes from an LlmResponse for span attributes."""
    attrs: dict[str, Any] = {}
    usage = _safe_attr(resp, "usage_metadata", None)
    if usage is not None:
        for k in (
            "prompt_token_count",
            "candidates_token_count",
            "total_token_count",
            "thoughts_token_count",      # thinking/reasoning tokens
            "cached_content_token_count",
        ):
            v = _safe_attr(usage, k, None)
            if v is not None:
                try:
                    attrs[k] = int(v)
                except (TypeError, ValueError):
                    attrs[k] = str(v)
    model_version = _safe_attr(resp, "model_version", None)
    if model_version:
        attrs["model_version"] = str(model_version)
    return attrs


def _safe_llm_response_payload(resp: Any) -> Optional[bytes]:
    """Serialize the full LLM response: content (split by thinking vs text),
    usage metadata, model version, and finish reason."""
    try:
        payload: dict[str, Any] = {}

        content = _safe_attr(resp, "content", None)
        if content is not None:
            d = getattr(content, "model_dump", None)
            payload["content"] = d(mode="json") if callable(d) else str(content)

        usage = _safe_attr(resp, "usage_metadata", None)
        if usage is not None:
            ud = getattr(usage, "model_dump", None)
            if callable(ud):
                payload["usage_metadata"] = ud(mode="json")

        model_version = _safe_attr(resp, "model_version", None)
        if model_version:
            payload["model_version"] = str(model_version)

        finish_reason = _safe_attr(resp, "finish_reason", None)
        if finish_reason is not None:
            payload["finish_reason"] = str(finish_reason)

        if not payload:
            return None
        return _safe_json(payload)
    except Exception:
        return None


# Rough context-window caps by model family. The map is intentionally
# short — it just needs to cover the models harmonograf users actually
# run against. Unknown models resolve to 0 (the server treats that as
# "limit unknown" and the frontend will not draw a ceiling line). Users
# who care can force a value via HARMONOGRAF_CONTEXT_LIMIT.
_CONTEXT_WINDOW_LIMITS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 2_000_000,
    "gemini-2.5-flash": 1_000_000,
}


def _lookup_context_window_limit(model: str) -> int:
    """Best-effort context-window cap lookup for a model identifier.

    Env override HARMONOGRAF_CONTEXT_LIMIT wins so operators with a
    custom model or a different cap can force the limit without
    rebuilding the client.
    """
    override = os.environ.get("HARMONOGRAF_CONTEXT_LIMIT")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    if not model:
        return 0
    key = str(model).lower()
    # Strip provider prefix ("openai/gpt-4o", "google/gemini-...").
    if "/" in key:
        key = key.split("/", 1)[1]
    for prefix, limit in _CONTEXT_WINDOW_LIMITS.items():
        if key.startswith(prefix):
            return limit
    return 0


def _estimate_request_tokens(req: Any) -> int:
    """Cheap token-count estimate for an ADK LlmRequest.

    Sums the character length of every text part in ``req.contents`` and
    divides by 4, which tracks within ~20% of real tokenizer counts for
    English prose and is plenty accurate for a usage gauge. Falls back
    to 0 on any structural surprise — the heartbeat will just carry
    nothing for that tick until the precise usage_metadata count lands
    in on_model_end.
    """
    try:
        contents = _safe_attr(req, "contents", None)
        if contents is None:
            return 0
        total_chars = 0
        for item in contents:
            parts = _safe_attr(item, "parts", None) or []
            for p in parts:
                text = _safe_attr(p, "text", "") or ""
                if text:
                    total_chars += len(text)
        return total_chars // 4
    except Exception:
        return 0


def _inject_steer_into_request(llm_request: Any, steer_text: str) -> None:
    """Append a synthetic user turn carrying the STEER instruction to the request.

    Modifies ``llm_request.contents`` in-place so the model sees the instruction
    on this call without needing a new invocation. Uses duck-typed construction
    so we don't need to import google.genai at module level.
    """
    try:
        from google.genai import types as genai_types  # type: ignore

        part = genai_types.Part(text=f"[Steering instruction] {steer_text}")
        content = genai_types.Content(role="user", parts=[part])
        contents = _safe_attr(llm_request, "contents", None)
        if contents is None:
            llm_request.contents = [content]
        else:
            contents.append(content)
        log.info("Injected STEER text into LLM request: %.80s", steer_text)
    except Exception as exc:
        log.warning("Failed to inject STEER text into LLM request: %s", exc)


def _inject_plan_guidance_into_request(llm_request: Any, guidance: str) -> None:
    """Append a synthetic user turn carrying plan guidance to the request.

    Mirrors :func:`_inject_steer_into_request` — modifies
    ``llm_request.contents`` in-place so the model sees the plan on this
    call without mutating the root agent's instruction (which would
    persist across invocations).
    """
    try:
        from google.genai import types as genai_types  # type: ignore

        part = genai_types.Part(text=guidance)
        content = genai_types.Content(role="user", parts=[part])
        contents = _safe_attr(llm_request, "contents", None)
        if contents is None:
            llm_request.contents = [content]
        else:
            contents.append(content)
        log.debug("Injected plan guidance into LLM request (%d chars)", len(guidance))
    except Exception as exc:
        log.warning("Failed to inject plan guidance into LLM request: %s", exc)


def _repair_adk_session_after_cancel(ic: Any) -> None:
    """Remove dangling partial state from the ADK session after a cancel.

    The specific case we guard against: the last committed event in the
    session history is a model-role event whose parts include one or more
    function_call objects, but there are no subsequent tool-result events.
    This happens when the model decided to call a tool, that decision was
    written to session history, but the actual tool execution (and therefore
    the tool-result event) never completed due to the cancel.

    Removing this dangling event lets a re-run start from a consistent state
    where the model hasn't committed to tool calls it won't get results for.

    We are conservative: if we can't safely detect the condition, we do nothing.
    """
    try:
        session = _safe_attr(ic, "session", None)
        if session is None:
            return
        events = _safe_attr(session, "events", None)
        if not events:
            return

        last = events[-1]
        # Only inspect model-authored events.
        role = str(_safe_attr(last, "author", "") or "").lower()
        # ADK uses 'author' not 'role' on Event objects; model turns are authored
        # by the agent name or "model" depending on ADK version — check both.
        content = _safe_attr(last, "content", None)
        if content is None:
            return
        parts = _safe_attr(content, "parts", None) or []

        has_function_call = any(
            _safe_attr(p, "function_call", None) is not None for p in parts
        )
        if not has_function_call:
            return

        # Check whether the event AFTER this one (if any) has tool results.
        # If there are none, the function call was never answered — dangling.
        # Since we already checked events[-1], there are no subsequent events.
        # Remove the dangling event.
        try:
            events.pop()
            log.debug(
                "repair: removed dangling function-call event after cancel "
                "(author=%r, %d function call part(s))",
                role,
                sum(1 for p in parts if _safe_attr(p, "function_call", None) is not None),
            )
        except Exception as e:
            log.debug("repair: could not remove event: %s", e)

    except Exception as e:
        log.debug("repair: unexpected error, skipping: %s", e)


def _repair_session_for_steer_rerun(ic: Any) -> None:
    """Remove the user message added at the start of this invocation so a
    steer-triggered re-run can re-add it cleanly without duplication.

    The ADK runner adds ``new_message`` to the session before the first
    ``before_model_callback``.  When we cancel mid-run and immediately
    re-run with the same ``new_message`` kwargs, the runner would append
    the message a second time.  This helper pops it if and only if the
    last session event looks like a pure user-text message (no
    function_call / function_response parts) — which is the case when
    cancel happened during (or just after) the first LLM call.

    Conservative: if the condition is uncertain, does nothing.
    """
    try:
        session = _safe_attr(ic, "session", None)
        if session is None:
            return
        events = _safe_attr(session, "events", None)
        if not events:
            return
        last = events[-1]
        content = _safe_attr(last, "content", None)
        if content is None:
            return
        role = str(_safe_attr(content, "role", "") or "").lower()
        if role != "user":
            return  # Not a user-authored event, leave it alone.
        parts = _safe_attr(content, "parts", None) or []
        has_fc = any(_safe_attr(p, "function_call", None) is not None for p in parts)
        has_fr = any(_safe_attr(p, "function_response", None) is not None for p in parts)
        if has_fc or has_fr:
            return  # Last event is a tool turn, not the initial user message.
        try:
            events.pop()
            log.debug("steer re-run: removed user message from session for clean restart")
        except Exception as e:
            log.debug("steer re-run: could not remove user message: %s", e)
    except Exception as e:
        log.debug("steer re-run: unexpected error in session repair: %s", e)
