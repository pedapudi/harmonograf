"""Planner helpers — decompose an incoming request into a task DAG.

A :class:`PlannerHelper` is a strategy object the ADK adapter (or any
other integration) consults when a new invocation begins. It receives
the raw user request plus the agent topology, and returns a
:class:`Plan` (a DAG of :class:`Task`\\s and :class:`TaskEdge`\\s) or
``None`` to skip planning entirely.

The Plan, once returned, is forwarded to the harmonograf server via
:meth:`Client.submit_plan` as a ``TaskPlan`` envelope. Downstream spans
bind to tasks by carrying the ``hgraf.task_id`` attribute (the server
then auto-updates task status from span lifecycle).

Three concrete helpers ship by default:

* :class:`PassthroughPlanner` — never plans. Useful as a default when a
  user hasn't opted in.
* :class:`LLMPlanner` — delegates to a caller-supplied LLM callable,
  parses its JSON output, and is robust to markdown fences.
* (custom subclasses implement their own :meth:`generate`)

The contract is that :meth:`PlannerHelper.generate` must never raise
into the host agent. Subclasses should catch their own exceptions; the
ADK adapter additionally wraps the call in a defensive try/except.
"""

from __future__ import annotations

import abc
import asyncio
import concurrent.futures
import dataclasses
import json
import logging
import re
from typing import Any, Callable, Mapping, Optional

log = logging.getLogger("harmonograf_client.planner")


@dataclasses.dataclass
class Task:
    id: str
    title: str
    description: str = ""
    assignee_agent_id: str = ""
    predicted_start_ms: int = 0
    predicted_duration_ms: int = 0
    # Latest known execution status for this task. Populated by the ADK
    # adapter from span lifecycle events and read by LLMPlanner.refine to
    # give the model an accurate picture of what has/has not run yet.
    # One of: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED.
    status: str = "PENDING"


@dataclasses.dataclass
class TaskEdge:
    from_task_id: str
    to_task_id: str


@dataclasses.dataclass
class Plan:
    tasks: list[Task]
    edges: list[TaskEdge]
    summary: str = ""
    # When the plan has been revised in response to observed drift,
    # a human-readable reason is stamped here by the observer before the
    # upsert. Empty on initial plans.
    revision_reason: str = ""
    # Structured drift kind that triggered the most recent revision (e.g.
    # "tool_error", "new_work_discovered"). Empty on initial plans.
    revision_kind: str = ""
    # Severity of the drift that triggered the revision: "info" | "warning"
    # | "critical". Empty on initial plans.
    revision_severity: str = ""
    # Monotonic revision index within the plan lineage. 0 for initial plans.
    revision_index: int = 0

    def topological_stages(self) -> list[list[Task]]:
        """Return tasks grouped into topological stages (Kahn's algorithm).

        Each stage contains tasks whose dependencies are all satisfied by
        tasks in earlier stages. Tasks with no deps live in stage 0.
        Cycles or edges referencing unknown task ids are tolerated — any
        task that can never be placed is appended to a final trailing
        stage so the full set is always returned.
        """
        tasks_by_id = {t.id: t for t in self.tasks if t.id}
        indeg: dict[str, int] = {tid: 0 for tid in tasks_by_id}
        children: dict[str, list[str]] = {tid: [] for tid in tasks_by_id}
        for e in self.edges:
            if e.from_task_id in tasks_by_id and e.to_task_id in tasks_by_id:
                children[e.from_task_id].append(e.to_task_id)
                indeg[e.to_task_id] += 1

        stages: list[list[Task]] = []
        ready = [tid for tid, d in indeg.items() if d == 0]
        placed: set[str] = set()
        while ready:
            stage_ids = sorted(ready)
            stages.append([tasks_by_id[tid] for tid in stage_ids])
            placed.update(stage_ids)
            next_ready: list[str] = []
            for tid in stage_ids:
                for child in children[tid]:
                    indeg[child] -= 1
                    if indeg[child] == 0:
                        next_ready.append(child)
            ready = next_ready

        leftover = [t for tid, t in tasks_by_id.items() if tid not in placed]
        if leftover:
            stages.append(leftover)
        return stages


class PlannerHelper(abc.ABC):
    """Strategy interface — subclass to define how a user request maps
    to a :class:`Plan`.

    Implementations must be side-effect free with respect to the host
    agent: returning ``None`` is always an acceptable fallback, and
    raising is discouraged (the adapter swallows exceptions but logs a
    warning).
    """

    @abc.abstractmethod
    def generate(
        self,
        *,
        request: str,
        available_agents: list[str],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Plan]:
        """Produce a plan for ``request`` or return ``None`` to skip."""

    def refine(
        self,
        plan: Plan,
        event: Mapping[str, Any],
    ) -> Optional[Plan]:
        """Produce an updated plan in response to a steering-moment event.

        Default implementation is a no-op — subclasses may override to
        incorporate fresh information (tool results, transfers, mid-run
        model responses) into the active plan.  Returning ``None`` means
        "keep the current plan as-is".
        """
        return None


class PassthroughPlanner(PlannerHelper):
    """No-op planner — always returns ``None``.

    Makes it safe to wire a ``planner=`` kwarg everywhere without
    forcing callers to opt-in immediately.
    """

    def generate(self, **kwargs: Any) -> Optional[Plan]:  # type: ignore[override]
        return None


_DEFAULT_SYSTEM_PROMPT = """\
You are a task-planning assistant for a multi-agent system. Your job
is to produce the COMPLETE end-to-end execution plan for the user's
request BEFORE any agent begins work. Do not plan just the first
step — enumerate every task you expect the system to perform from
start to finish, so a human can see the whole shape of the workflow
upfront.

Requirements for the plan:

1. COMPREHENSIVENESS. Decompose the request into the full DAG of
   tasks needed to satisfy it end-to-end. A typical plan has between
   5 and 20 tasks. Smaller is OK only for genuinely trivial requests;
   larger is OK for complex multi-phase work. Do not stop at "first
   research, then draft" — include verification, revision, handoffs,
   synthesis, and any follow-up the request implies.

2. DEPENDENCIES. Use `edges` to declare ordering between tasks: an
   edge {from_task_id, to_task_id} means the "from" task must finish
   before the "to" task starts. Parallel tasks (no edge between them)
   may run concurrently. Build a real DAG, not a single linear chain,
   when independent work exists.

3. ASSIGNMENTS. Every task's `assignee_agent_id` MUST be drawn from
   the provided available_agents list. If multiple agents could do a
   task, pick the most specialised one. If no agent fits, assign it
   to the coordinator/root agent rather than inventing an id.

4. STABILITY. Task ids must be short, unique, and stable strings
   (e.g. "research", "draft_intro", "review_final"). Descriptions
   should be one sentence describing what "done" looks like for the
   task.

5. SUMMARY. Provide a one-sentence `summary` describing the overall
   goal of the plan, as if writing a PR title.

Respond with a single JSON object and NOTHING ELSE — no prose, no
markdown fences. Schema:

{
  "summary": "<one-sentence description of the overall plan>",
  "tasks": [
    {
      "id": "research",
      "title": "short human-readable title",
      "description": "one sentence defining 'done' for this task",
      "assignee_agent_id": "<agent id from available list>"
    }
  ],
  "edges": [
    {"from_task_id": "research", "to_task_id": "draft"}
  ]
}
"""


_VALID_TASK_STATUSES = frozenset(
    {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fences(raw: str) -> str:
    """Remove a surrounding ```json ... ``` or ``` ... ``` fence, if any."""
    if not raw:
        return raw
    match = _FENCE_RE.match(raw)
    if match:
        return match.group(1)
    return raw


def _plan_from_json(obj: Any) -> Optional[Plan]:
    if not isinstance(obj, Mapping):
        return None
    raw_tasks = obj.get("tasks") or []
    raw_edges = obj.get("edges") or []
    if not isinstance(raw_tasks, list):
        return None
    tasks: list[Task] = []
    for t in raw_tasks:
        if not isinstance(t, Mapping):
            continue
        tid = str(t.get("id") or "").strip()
        title = str(t.get("title") or "").strip()
        if not tid or not title:
            continue
        raw_status = str(t.get("status") or "PENDING").upper()
        if raw_status not in _VALID_TASK_STATUSES:
            raw_status = "PENDING"
        tasks.append(
            Task(
                id=tid,
                title=title,
                description=str(t.get("description") or ""),
                assignee_agent_id=str(t.get("assignee_agent_id") or ""),
                predicted_start_ms=int(t.get("predicted_start_ms") or 0),
                predicted_duration_ms=int(t.get("predicted_duration_ms") or 0),
                status=raw_status,
            )
        )
    if not tasks:
        return None
    edges: list[TaskEdge] = []
    if isinstance(raw_edges, list):
        for e in raw_edges:
            if not isinstance(e, Mapping):
                continue
            frm = str(e.get("from_task_id") or "").strip()
            to = str(e.get("to_task_id") or "").strip()
            if frm and to:
                edges.append(TaskEdge(from_task_id=frm, to_task_id=to))
    summary = str(obj.get("summary") or "")
    return Plan(tasks=tasks, edges=edges, summary=summary)


class LLMPlanner(PlannerHelper):
    """Delegates to a caller-supplied LLM callable.

    Parameters
    ----------
    call_llm:
        A callable ``(system_prompt, user_prompt, model) -> str``. The
        string return value must be JSON conforming to the plan schema
        described in the default system prompt (it may be wrapped in
        triple-backtick ``json`` fences; this class will strip them).
    model:
        Model name to pass through to ``call_llm``. If empty, the
        adapter that constructed the planner is expected to have
        resolved a default from the host agent before calling
        :meth:`generate` (and may still pass empty, in which case it's
        up to ``call_llm`` to substitute its own default).
    system_prompt:
        Override the default task-planning prompt.

    On any parse error or exception from ``call_llm``, :meth:`generate`
    logs a warning and returns ``None`` — the host agent continues
    execution without a plan.
    """

    def __init__(
        self,
        *,
        call_llm: Callable[[str, str, str], str],
        model: str = "",
        system_prompt: Optional[str] = None,
    ) -> None:
        self._call_llm = call_llm
        self._model = model
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    @property
    def model(self) -> str:
        return self._model

    def _build_user_prompt(
        self, request: str, available_agents: list[str]
    ) -> str:
        agents_block = (
            "\n".join(f"- {a}" for a in available_agents) or "- (none listed)"
        )
        return (
            f"Available agents:\n{agents_block}\n\n"
            f"User request:\n{request}\n\n"
            "Respond with a single JSON object following the schema."
        )

    def generate(
        self,
        *,
        request: str,
        available_agents: list[str],
        context: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Plan]:
        if not request:
            return None
        prompt = self._build_user_prompt(request, available_agents)
        try:
            raw = self._call_llm(self._system_prompt, prompt, self._model)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLMPlanner: call_llm raised %s; skipping plan", exc)
            return None
        if not raw or not isinstance(raw, str):
            log.warning("LLMPlanner: empty/non-string LLM response; skipping plan")
            return None
        cleaned = _strip_code_fences(raw).strip()
        try:
            parsed = json.loads(cleaned)
        except (ValueError, TypeError) as exc:
            log.warning(
                "LLMPlanner: failed to parse LLM output as JSON (%s); skipping plan",
                exc,
            )
            return None
        plan = _plan_from_json(parsed)
        if plan is None:
            log.warning(
                "LLMPlanner: parsed JSON did not contain a usable plan; skipping"
            )
            return None
        return plan

    def refine(
        self,
        plan: Plan,
        event: Mapping[str, Any],
    ) -> Optional[Plan]:
        if plan is None:
            return None
        try:
            current = {
                "summary": plan.summary,
                "tasks": [dataclasses.asdict(t) for t in plan.tasks],
                "edges": [dataclasses.asdict(e) for e in plan.edges],
            }
            plan_json = json.dumps(current, default=str)
            event_json = json.dumps(dict(event), default=str)
        except (TypeError, ValueError) as exc:
            log.warning("LLMPlanner.refine: failed to serialise inputs (%s)", exc)
            return None
        user_prompt = (
            f"Current plan:\n{plan_json}\n\n"
            f"Latest event:\n{event_json}\n\n"
            "If the plan should change in light of this event, respond "
            "with an updated JSON plan using the same schema. If no "
            "change is warranted, respond with the current plan "
            "unchanged. Respond with JSON only."
        )
        try:
            raw = self._call_llm(_REFINE_SYSTEM_PROMPT, user_prompt, self._model)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLMPlanner.refine: call_llm raised %s", exc)
            return None
        if not raw or not isinstance(raw, str):
            return None
        cleaned = _strip_code_fences(raw).strip()
        try:
            parsed = json.loads(cleaned)
        except (ValueError, TypeError):
            return None
        return _plan_from_json(parsed)


_REFINE_SYSTEM_PROMPT = """\
You are a task-planning assistant maintaining an ACTIVE plan for a
multi-agent system. You will receive the current plan as JSON (with
each task carrying its live `status` — one of PENDING, RUNNING,
COMPLETED, FAILED, CANCELLED) and a single recent execution event
(tool result, model turn, or transfer). Your job is to return the
COMPLETE updated plan that reflects both what has happened so far and
what the system still needs to do.

You MUST:

1. PRESERVE HISTORY. Tasks that are already COMPLETED, FAILED, or
   CANCELLED must appear in the returned plan with the same id,
   title, assignee, and status. Do NOT drop or renumber them — the
   Gantt-chart view must be able to show the timeline continuously.

2. UPDATE STATUSES. If the latest event reveals that a RUNNING task
   has finished, mark it COMPLETED (or FAILED if the event is an
   error). If a PENDING task has implicitly become the current focus,
   you may leave it PENDING and let the adapter mark it RUNNING when
   a span actually starts.

3. ADD NEW TASKS. If the event reveals work that the original plan
   did not anticipate (e.g., a tool result surfaces a follow-up
   question, an error requires a retry/fallback path, a transfer
   introduces a sub-workflow), ADD new PENDING tasks for that work
   with fresh stable ids and appropriate edges back into the DAG.

4. DROP OBSOLETE PENDING TASKS. If the event makes a PENDING task
   unnecessary (e.g., the user's goal has been satisfied early, a
   dependency collapsed), you may omit it from the returned plan.
   Never drop tasks that already ran.

5. REASSIGN. If a task is better handled by a different available
   agent in light of the event, update its `assignee_agent_id`.

6. KEEP IDS STABLE. When the underlying work is the same, keep the
   task id unchanged. Reuse ids only for the same logical task.

7. RETURN A COMPLETE PLAN. Always return the full plan, not a delta.
   The adapter upserts under the same plan_id.

Respond with a single JSON object and NOTHING ELSE:

{
  "summary": "...",
  "tasks": [
    {
      "id": "...",
      "title": "...",
      "description": "...",
      "assignee_agent_id": "...",
      "status": "PENDING|RUNNING|COMPLETED|FAILED|CANCELLED"
    }
  ],
  "edges": [{"from_task_id": "...", "to_task_id": "..."}]
}

If nothing needs to change, return the current plan unchanged (but
still as a complete JSON plan, not an empty object).
"""


def make_default_adk_call_llm() -> Optional[Callable[[str, str, str], str]]:
    """Return a ``call_llm`` callable backed by ADK's own LLM registry.

    The returned callable has signature
    ``(system_prompt, user_prompt, model) -> str`` and issues a single
    non-streaming generation via ``LLMRegistry.new_llm(model)``.  If
    google.adk / google.genai are not importable, returns ``None`` so
    callers can fall back to :class:`PassthroughPlanner`.
    """
    try:
        from google.adk.models.llm_request import LlmRequest  # noqa: F401
        from google.adk.models.registry import LLMRegistry  # noqa: F401
        from google.genai import types as genai_types  # noqa: F401
        # Import the package init so the registry is populated.
        import google.adk.models  # noqa: F401
    except Exception:
        return None

    def _call_llm(system_prompt: str, user_prompt: str, model: str) -> str:
        if not model:
            raise ValueError("make_default_adk_call_llm: model is required")
        from google.adk.models.llm_request import LlmRequest
        from google.adk.models.registry import LLMRegistry
        from google.genai import types as genai_types

        async def _run() -> str:
            llm = LLMRegistry.new_llm(model)
            req = LlmRequest(
                model=model,
                contents=[
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=user_prompt)],
                    )
                ],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                ),
            )
            pieces: list[str] = []
            async for resp in llm.generate_content_async(req, stream=False):
                content = getattr(resp, "content", None)
                if content is None:
                    continue
                parts = getattr(content, "parts", None) or []
                for part in parts:
                    if getattr(part, "thought", False):
                        continue
                    text = getattr(part, "text", None)
                    if isinstance(text, str):
                        pieces.append(text)
            return "".join(pieces)

        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if not in_loop:
            return asyncio.run(_run())
        # Called from inside a running event loop — offload to a worker
        # thread that owns its own loop so we don't try to nest.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(_run())).result()

    return _call_llm


__all__ = [
    "LLMPlanner",
    "PassthroughPlanner",
    "Plan",
    "PlannerHelper",
    "Task",
    "TaskEdge",
    "make_default_adk_call_llm",
]
