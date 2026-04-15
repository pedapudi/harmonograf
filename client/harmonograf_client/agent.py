"""HarmonografAgent — a BaseAgent parent that wraps a user agent.

The point of HarmonografAgent is to slot plan-driven orchestration into
ADK's *native* extensibility seam (``BaseAgent._run_async_impl``) instead
of monkey-patching the Runner. Because HarmonografAgent IS an ADK agent,
any entry point that walks the ADK agent tree — ``adk web``, ``adk run``,
``adk api_server``, ``make demo``, custom Runners — picks it up
transparently by simply pointing at it as the App's ``root_agent``.

Usage::

    inner = Agent(name="coordinator", ..., tools=[AgentTool(research), ...])
    root_agent = HarmonografAgent(
        name="harmonograf",
        inner_agent=inner,
        harmonograf_client=client,
        planner=None,        # default LLMPlanner is auto-wired
    )

HarmonografAgent wires ``inner_agent`` into ``sub_agents`` so ADK's
parent/child bookkeeping and ``find_agent`` continue to work, and its
``_run_async_impl`` delegates to ``inner_agent.run_async(ctx)`` while
enforcing the planner's plan: if a task assigned to an inner agent is
still PENDING with satisfied dependencies after the inner generator
exhausts, the orchestrator appends a synthetic user turn to the session
and re-invokes the inner agent, up to ``max_plan_reinvocations`` times.

Plan state is owned by the :class:`HarmonografAdkPlugin` (see
``make_adk_plugin``). HarmonografAgent discovers the plugin via
``ctx.plugin_manager`` at invocation time, so the two can be composed
independently: an App is expected to include *both* the HarmonografAgent
as root and the HarmonografAdkPlugin in its plugin list. The plugin
provides telemetry + plan-state tracking; the agent provides the
orchestration loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, AsyncGenerator, ClassVar, Optional

from pydantic import Field, model_validator

from .invariants import check_plan_state, enforce as _enforce_invariants
from .tools import (
    REPORTING_TOOL_NAMES,
    augment_instruction,
    build_reporting_function_tools,
)

try:
    from google.adk.agents.base_agent import BaseAgent
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event
    from google.adk.utils.context_utils import Aclosing
except Exception:  # pragma: no cover — ADK optional at import time
    BaseAgent = object  # type: ignore[assignment,misc]
    InvocationContext = Any  # type: ignore[assignment,misc]
    Event = Any  # type: ignore[assignment,misc]
    Aclosing = None  # type: ignore[assignment,misc]

log = logging.getLogger("harmonograf_client.agent")


_DEFAULT_MAX_PLAN_REINVOCATIONS = 3
_HARMONOGRAF_PLUGIN_NAME = "harmonograf"
_ORCHESTRATOR_WALKER_SAFETY_CAP = 20
# Upper bound on retries for a task the classifier flagged as "partial".
# Mirrors ``_AdkState.reinvocation_budget()`` — keep in sync.
_PARTIAL_REINVOCATION_BUDGET = 3


def _extract_thought_text(event: Any) -> str:
    """Return the concatenated text of all ``thought=True`` parts in
    ``event.content.parts``, or empty string. Gemini 2.5 emits these
    as its internal reasoning trace when thinking_config is set.
    Tolerates any event shape; never raises.
    """
    try:
        content = getattr(event, "content", None)
        if content is None:
            return ""
        parts = getattr(content, "parts", None) or []
        chunks: list[str] = []
        for part in parts:
            if not getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
        return "".join(chunks)
    except Exception:
        return ""


def _augment_subtree_with_reporting(root: Any) -> tuple[int, int]:
    """Walk ``root``'s agent subtree and ensure every LlmAgent in it
    (including ``root`` itself when it happens to be an LlmAgent)
    carries the harmonograf reporting tools AND has its instruction
    augmented with the reporting-tool appendix.

    Traversal follows three edges: ``sub_agents`` (native ADK tree),
    ``inner_agent`` (HarmonografAgent wrapper), and ``AgentTool.agent``
    (agents exposed to a parent as a tool). The root is NOT skipped:
    wrapper-style roots like ``HarmonografAgent`` don't declare a
    ``tools`` field so the guard below is a no-op for them, while
    bare-``LlmAgent`` roots (the shape used by integration tests that
    don't wrap their agent) correctly pick up the reporting tools.

    Idempotent: agents that already carry the reporting tools are left
    alone, and ``augment_instruction`` is itself idempotent.

    Returns ``(tools_touched, instructions_touched)`` so callers can log
    or assert on what changed.
    """
    if root is None:
        return (0, 0)
    try:
        reporting_tools = build_reporting_function_tools()
    except Exception as exc:  # noqa: BLE001 — no ADK means nothing to do
        log.debug("reporting tool construction skipped: %s", exc)
        return (0, 0)

    tools_touched = 0
    instructions_touched = 0
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        children: list[Any] = list(getattr(cur, "sub_agents", None) or ())
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
        if existing is not None:
            existing_names: set[str] = set()
            for t in existing:
                n = getattr(t, "name", None) or getattr(
                    getattr(t, "func", None), "__name__", None
                )
                if n:
                    existing_names.add(n)
            if not any(n in existing_names for n in REPORTING_TOOL_NAMES):
                try:
                    cur.tools = list(existing) + list(reporting_tools)
                    tools_touched += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "could not augment tools on %s: %s",
                        getattr(cur, "name", "?"), exc,
                    )

        current_instruction = getattr(cur, "instruction", None)
        if isinstance(current_instruction, str):
            new_instruction = augment_instruction(current_instruction)
            if new_instruction != current_instruction:
                try:
                    cur.instruction = new_instruction
                    instructions_touched += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "could not augment instruction on %s: %s",
                        getattr(cur, "name", "?"), exc,
                    )

    if tools_touched or instructions_touched:
        log.info(
            "harmonograf: registered reporting tools on %d sub-agents, "
            "augmented instructions on %d sub-agents",
            tools_touched, instructions_touched,
        )
    return (tools_touched, instructions_touched)


def _build_nudge_content(task: Any) -> Any:
    """Construct a ``google.genai.types.Content`` nudge telling the
    inner agent to execute ``task``.
    """
    tid = getattr(task, "id", "") or ""
    title = getattr(task, "title", "") or ""
    description = getattr(task, "description", "") or ""
    text = (
        f"Continue executing the plan. Your next task is {tid}: {title}. "
        f"{description} "
        "Execute it now, then proceed to the next pending task assigned "
        "to you whose dependencies are complete."
    ).strip()
    from google.genai import types as genai_types  # type: ignore

    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


class HarmonografAgent(BaseAgent):  # type: ignore[misc]
    """BaseAgent parent that wraps an inner agent and enforces the
    harmonograf planner's plan via an orchestration loop.

    Orchestration modes
    -------------------

    The agent dispatches into one of three execution paths based on the
    ``orchestrator_mode`` and ``parallel_mode`` fields:

    * **Sequential** (``orchestrator_mode=True``, ``parallel_mode=False``
      — the default) runs ``_run_orchestrated`` in single-pass mode:
      the whole plan is fed as one user turn, the coordinator LLM is
      responsible for executing each task in order, and per-task state
      transitions come from the agent calling the reporting tools in
      :mod:`harmonograf_client.tools`. Preferred for most workflows —
      the model decides sub-task ordering while harmonograf still
      observes progress via explicit tool calls.

    * **Parallel** (``orchestrator_mode=True``, ``parallel_mode=True``)
      runs the rigid DAG batch walker: the walker drives each sub-agent
      directly per task using a forced ``task_id`` ContextVar,
      respecting plan edges as dependencies, and schedules ready tasks
      in parallel when their predecessors are COMPLETED. The walker
      honours the per-task partial re-invocation cap
      (:data:`_PARTIAL_REINVOCATION_BUDGET`) and the overall safety cap
      (:data:`_ORCHESTRATOR_WALKER_SAFETY_CAP`).

    * **Delegated** (``orchestrator_mode=False``) falls through to
      ``_run_delegated``: a single delegation with the event observer
      scanning events for drift afterwards. Plan state is still tracked
      — the inner agent is just in charge of its own task sequencing.
      Useful for agents that already have their own orchestration logic
      and only want harmonograf's telemetry + refine behaviour.

    All three modes share the same state-machine path
    (:class:`_AdkState`), the same reporting-tool interception in the
    plugin's ``before_tool_callback``, and the same refine loop on
    drift events. What differs is who drives the inner agent's turn
    boundaries.

    Pydantic fields:
        inner_agent: the wrapped :class:`BaseAgent` the orchestrator
            delegates to. Automatically added to ``sub_agents`` so the
            ADK agent tree (find_agent, parent wiring) sees it.
        harmonograf_client: optional :class:`Client`. Required only when
            the agent should submit plans. Defaults to ``None`` so the
            wrapper degrades to a pure pass-through parent.
        planner, planner_model, refine_on_events: forwarded to the
            :class:`_AdkState` helper at plan-generation time.
        enforce_plan: when ``False``, short-circuits the re-invocation
            loop (plan guidance injection via the plugin still runs).
        max_plan_reinvocations: upper bound on automatic re-invocations
            per outer ``_run_async_impl`` call.
        orchestrator_mode: when ``True`` (default) run
            ``_run_orchestrated``; when ``False`` run ``_run_delegated``.
        parallel_mode: only read in orchestrator mode. ``True`` selects
            the rigid DAG walker; ``False`` selects the single-pass
            sequential path.
    """

    _is_harmonograf_agent: ClassVar[bool] = True

    inner_agent: Any = Field(...)
    harmonograf_client: Any = Field(default=None, exclude=True)
    planner: Any = Field(default=None, exclude=True)
    planner_model: str = ""
    refine_on_events: bool = True
    enforce_plan: bool = True
    max_plan_reinvocations: int = _DEFAULT_MAX_PLAN_REINVOCATIONS
    # When True (default), the agent runs ``_run_orchestrated``, which
    # dispatches to a single-pass sequential delegation by default and
    # to the rigid parallel DAG walker when ``parallel_mode`` is set.
    # When False, falls through to ``_run_delegated``: a single
    # delegation with the observer scanning events for drift afterward.
    orchestrator_mode: bool = True
    # When True, ``_run_orchestrated`` runs the rigid DAG batch walker
    # that drives sub-agents directly per task with a forced task id
    # ContextVar. When False (default), ``_run_orchestrated`` runs the
    # single-pass sequential path: the whole plan is fed as one user
    # turn and the coordinator LLM is responsible for executing it,
    # reporting per-task lifecycle via the reporting tools.
    parallel_mode: bool = False

    @model_validator(mode="before")
    @classmethod
    def _wire_inner_agent_as_subagent(cls, data: Any) -> Any:
        """Copy ``inner_agent`` into ``sub_agents`` so ADK's parent
        wiring picks up the child. Users can pass either or both; we
        normalise so the sub_agents list has exactly ``[inner_agent]``
        when it isn't explicitly overridden.
        """
        if not isinstance(data, dict):
            return data
        inner = data.get("inner_agent")
        if inner is None:
            return data
        sub = data.get("sub_agents")
        if not sub:
            data["sub_agents"] = [inner]
        return data

    @model_validator(mode="after")
    def _publish_execution_mode(self) -> "HarmonografAgent":
        """Stamp ``harmonograf.execution_mode`` on the wired client.

        The frontend reads this out of the Hello frame's agent metadata
        to render the mode chip in the CurrentTaskStrip. We derive the
        label from ``orchestrator_mode`` + ``parallel_mode`` here so
        there is exactly one authoritative mapping and downstream UIs
        don't have to reverse-engineer it.
        """
        client = self.harmonograf_client
        if client is None:
            return self
        set_metadata = getattr(client, "set_metadata", None)
        if not callable(set_metadata):
            return self
        if not self.orchestrator_mode:
            mode = "delegated"
        elif self.parallel_mode:
            mode = "parallel"
        else:
            mode = "sequential"
        try:
            set_metadata("harmonograf.execution_mode", mode)
        except Exception as exc:  # noqa: BLE001 — never fail construction
            log.debug("publish_execution_mode: set_metadata raised: %s", exc)
        return self

    @model_validator(mode="after")
    def _auto_register_reporting_tools(self) -> "HarmonografAgent":
        """Walk the inner_agent + sub_agents subtree at construction
        time and append the harmonograf reporting tools + instruction
        appendix to every LlmAgent below this wrapper.

        Earlier iterations registered tools via the ADK plugin's
        ``before_run_callback``, which worked for entry points that
        attach the plugin (``make_adk_plugin`` / ``App(plugins=...)``).
        Tests (and any bespoke runners) that construct a
        HarmonografAgent directly and invoke it without attaching the
        plugin never saw the tools — so ``report_plan_divergence`` &
        friends ended up unresolved at tool-call time. Doing it here at
        construction means every code path that materialises a
        HarmonografAgent gets the tools registered regardless of how
        the agent is run.
        """
        try:
            _augment_subtree_with_reporting(self)
        except Exception as exc:  # noqa: BLE001 — never fail construction
            log.debug("auto reporting-tool registration raised: %s", exc)
        return self

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_state(self, ctx: Any) -> Any:
        """Locate the :class:`_AdkState` held by the HarmonografAdkPlugin
        attached to this invocation's plugin_manager. Returns ``None``
        if no harmonograf plugin is installed.
        """
        pm = getattr(ctx, "plugin_manager", None)
        if pm is None:
            return None
        for p in getattr(pm, "plugins", []) or []:
            if getattr(p, "name", "") == _HARMONOGRAF_PLUGIN_NAME:
                return getattr(p, "_hg_state", None)
        return None

    # ------------------------------------------------------------------
    # _run_async_impl — the orchestration loop
    # ------------------------------------------------------------------

    async def _run_async_impl(
        self, ctx: Any
    ) -> AsyncGenerator[Any, None]:
        """Dispatch to either the DAG walker (``_run_orchestrated``) or
        the pure-delegation path (``_run_delegated``) based on
        :attr:`orchestrator_mode`. In both modes, the observer is always
        on: drift detection runs after each inner-agent turn and
        refines the plan when drift is detected.

        Cancellation safety: the body is wrapped in try/finally so that
        ``clear_plan_snapshot`` and in-flight span cleanup always runs,
        even when STEER(cancel) raises ``asyncio.CancelledError`` mid-
        iteration or the ASGI driver triggers ``GeneratorExit`` on the
        outer async generator. Without this, a steer-cancel against a
        plugin registered directly on ``adk api_server`` (i.e. not
        through :class:`AdkAdapter.run_async`) leaked plan state and
        surfaced as "ASGI callable returned without completing
        response" because forced-task-id / span bookkeeping was never
        torn down before the exception propagated.
        """
        state = self._find_state(ctx)
        log.debug(
            "HarmonografAgent.run_async: entering inv_id=%s mode=%s host=%s",
            getattr(ctx, "invocation_id", "") or "",
            "orchestrator" if self.orchestrator_mode else "delegated",
            getattr(self.inner_agent, "name", "") or "",
        )
        if state is not None:
            try:
                state.maybe_run_planner(ctx, host_agent=self.inner_agent)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "HarmonografAgent: planner hook raised; continuing: %s", exc
                )

        inv_id = getattr(ctx, "invocation_id", "") or ""
        host_agent_id = getattr(self.inner_agent, "name", "") or ""
        hsession_id = self._resolve_hsession(state, ctx, inv_id)

        try:
            if self.orchestrator_mode:
                async for event in self._run_orchestrated(
                    ctx, state, hsession_id, inv_id, host_agent_id
                ):
                    yield event
            else:
                async for event in self._run_delegated(
                    ctx, state, hsession_id, inv_id, host_agent_id
                ):
                    yield event
            # End-of-turn invariant check — runs after inner passes have
            # drained so classify_and_sweep and any refine have already
            # landed and the plan snapshot is authoritative. Under pytest,
            # error-severity violations raise AssertionError.
            self._validate_invariants(
                state,
                hsession_id,
                context=f"inv_id={inv_id} mode={'orch' if self.orchestrator_mode else 'delegated'}",
            )
        except (asyncio.CancelledError, GeneratorExit) as exc:
            log.info(
                "HarmonografAgent: cancelled mid-run (%s); cleaning up",
                type(exc).__name__,
            )
            if state is not None:
                try:
                    state._cleanup_cancelled_spans()
                except Exception as cleanup_exc:  # noqa: BLE001
                    log.debug(
                        "HarmonografAgent: _cleanup_cancelled_spans raised: %s",
                        cleanup_exc,
                    )
                try:
                    state.set_forced_task_id("")
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if state is not None and inv_id:
                try:
                    state.clear_plan_snapshot(inv_id)
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "HarmonografAgent: clear_plan_snapshot raised: %s",
                        exc,
                    )

    # ------------------------------------------------------------------
    # Delegated mode: single pass, observer-only
    # ------------------------------------------------------------------

    async def _run_delegated(
        self,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        host_agent_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Run the inner agent once and let the LLM drive freely. After
        the turn exhausts, sweep any task that was stamped RUNNING
        during this turn to COMPLETED (delegated mode has no walker
        signal; the inner agent's full turn ending is the semantic
        "done" marker), then scan collected events for drift and
        refine the plan explicitly if any drift signal fires.
        """
        # Snapshot of task ids that were already RUNNING when this
        # delegated turn started; those were stamped by a *previous*
        # turn and must not be swept by this one.
        log.debug(
            "_run_delegated: entry hsession=%s inv_id=%s",
            hsession_id, inv_id,
        )
        pre_running: set[str] = set()
        pre_plan_state = None
        if state is not None and hsession_id:
            try:
                pre_plan_state = state._active_plan_by_session.get(hsession_id)
            except Exception:  # noqa: BLE001
                pre_plan_state = None
            if pre_plan_state is not None:
                for tid, tracked in pre_plan_state.tasks.items():
                    if (getattr(tracked, "status", "") or "") == "RUNNING":
                        pre_running.add(tid)

        # Write plan context to session.state so before_model callbacks
        # see the whole plan even on the first turn of a delegated run.
        self._write_plan_context_if_possible(
            ctx, pre_plan_state, host_agent_id
        )

        events: list = []
        if Aclosing is not None:
            async with Aclosing(self.inner_agent.run_async(ctx)) as agen:
                async for event in agen:
                    events.append(event)
                    self._maybe_emit_thought(state, inv_id, event)
                    yield event
        else:  # pragma: no cover — ADK import failed at module load
            async for event in self.inner_agent.run_async(ctx):
                events.append(event)
                self._maybe_emit_thought(state, inv_id, event)
                yield event

        log.debug(
            "_run_delegated: exit events=%d hsession=%s",
            len(events), hsession_id,
        )
        if state is None or not hsession_id:
            return

        # Inner-run exhaustion is the authoritative "turn done" signal,
        # but per-task outcome (completed/failed/partial) is the
        # classifier's call, not an unconditional COMPLETED stamp. Tasks
        # already RUNNING before this turn belong to a previous turn and
        # must be excluded from the sweep.
        delegated_summary = ""
        try:
            delegated_summary = self._extract_result_summary(events)
        except Exception:  # noqa: BLE001
            delegated_summary = ""
        try:
            state.classify_and_sweep_running_tasks(
                hsession_id,
                result_summary=delegated_summary,
                exclude=pre_running,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: delegated classify_and_sweep failed: %s",
                exc,
            )
        try:
            plan_state = state._active_plan_by_session.get(hsession_id)
        except Exception:  # noqa: BLE001
            plan_state = None
        if plan_state is None:
            return
        try:
            drift = state.detect_drift(
                events, current_task=None, plan_state=plan_state
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("HarmonografAgent: detect_drift raised: %s", exc)
            return
        if drift is not None:
            try:
                state.refine_plan_on_drift(hsession_id, drift, current_task=None)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "HarmonografAgent: refine_plan_on_drift raised: %s", exc
                )

    # ------------------------------------------------------------------
    # Sequential single-pass path (default orchestrated mode)
    # ------------------------------------------------------------------

    async def _run_sequential(
        self,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        host_agent_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Single-pass delegated path with plan-context and overview.

        Writes the full plan into ``ctx.session.state`` via
        ``state_protocol.write_plan_context`` so before/after model
        callbacks see it on the very first turn, injects ONE plan
        overview user nudge, runs ``inner_agent.run_async(ctx)`` exactly
        once, then runs the classifier sweep against any tasks
        still RUNNING when the inner generator exhausts. If the sweep
        flags a task ``partial`` and the run-level re-invocation budget
        is non-zero, re-runs the inner agent up to ``budget`` times with
        a small "continue" nudge, classifying after each pass.
        """
        log.debug(
            "_run_sequential: entry hsession=%s inv_id=%s",
            hsession_id, inv_id,
        )
        plan_state = self._get_plan_state(state, hsession_id)
        if plan_state is None:
            # No plan: degrade to the delegated path so a plan-less run
            # still produces output.
            async for ev in self._run_delegated(
                ctx, state, hsession_id, inv_id, host_agent_id
            ):
                yield ev
            return

        # Snapshot RUNNING tasks pre-turn so the classifier sweep does
        # not retroactively classify anything from a previous turn.
        pre_running: set[str] = set()
        try:
            for tid, tracked in plan_state.tasks.items():
                if (getattr(tracked, "status", "") or "") == "RUNNING":
                    pre_running.add(tid)
        except Exception:  # noqa: BLE001
            pass

        # Write plan context into session.state so before_model_callback
        # sees the whole plan on the very first model call.
        self._write_plan_context_if_possible(ctx, plan_state, host_agent_id)

        # Inject a single plan-overview user turn.
        try:
            overview_content = self._build_plan_overview_prompt(plan_state)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: build plan overview prompt failed: %s", exc
            )
            overview_content = None
        if overview_content is not None:
            self._append_nudge_event(ctx, overview_content)

        # Run the inner agent ONCE, streaming events upward.
        events: list = []
        if Aclosing is not None:
            async with Aclosing(self.inner_agent.run_async(ctx)) as agen:
                async for event in agen:
                    events.append(event)
                    self._maybe_emit_thought(state, inv_id, event)
                    yield event
        else:  # pragma: no cover — ADK import failed at module load
            async for event in self.inner_agent.run_async(ctx):
                events.append(event)
                self._maybe_emit_thought(state, inv_id, event)
                yield event
        log.debug(
            "_run_sequential: inner_agent.run_async exited events=%d",
            len(events),
        )

        if state is None or not hsession_id:
            return

        result_summary = ""
        try:
            result_summary = self._extract_result_summary(events)
        except Exception:  # noqa: BLE001
            result_summary = ""

        outcomes: dict[str, str] = {}
        try:
            outcomes = state.classify_and_sweep_running_tasks(
                hsession_id,
                result_summary=result_summary,
                exclude=pre_running,
            )
        except TypeError:
            try:
                outcomes = state.classify_and_sweep_running_tasks(
                    hsession_id, result_summary=result_summary
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "HarmonografAgent: classify_and_sweep_running_tasks failed: %s",
                    exc,
                )
                outcomes = {}
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: classify_and_sweep_running_tasks failed: %s",
                exc,
            )
            outcomes = {}

        # Whole-run re-invocation budget: re-nudge the coordinator if any
        # task came back partial, OR if it reached at least one terminal
        # task but closed its turn with others still PENDING (early-stop
        # mitigation). Zero-progress turns deliberately skip this loop —
        # they usually mean the inner agent never drove the protocol at
        # all, so retrying just burns budget. Pending-task detection
        # re-reads live plan_state each iteration so successful reporting
        # tool calls inside the retry loop shrink the pending set.
        try:
            budget = state.reinvocation_budget()
        except Exception:  # noqa: BLE001
            budget = _PARTIAL_REINVOCATION_BUDGET
        retries = 0
        early_stop_fired = False
        while retries < budget:
            plan_state_live = self._get_plan_state(state, hsession_id)
            pending_ids = self._pending_task_ids(plan_state_live)
            has_partial = self._has_partial(outcomes)
            had_progress = self._any_task_terminal(plan_state_live)
            trigger_early_stop = bool(pending_ids) and had_progress
            if not has_partial and not trigger_early_stop:
                break
            retries += 1
            if trigger_early_stop and not early_stop_fired:
                # Record this as a drift so the UI surfaces it on the
                # revision banner. Only fire once per sequential run —
                # the retry loop itself is the corrective action; we
                # don't want a new revision entry on every retry pass.
                early_stop_fired = True
                self._fire_coordinator_early_stop(
                    state, hsession_id, pending_ids, plan_state_live
                )
            log.info(
                "HarmonografAgent: sequential run has partial=%s pending=%s — "
                "re-invoking (attempt %d/%d)",
                has_partial, pending_ids, retries, budget,
            )
            try:
                continue_content = self._build_continue_prompt(
                    plan_state_live, pending_task_ids=pending_ids or None
                )
            except Exception:  # noqa: BLE001
                continue_content = None
            if continue_content is not None:
                self._append_nudge_event(ctx, continue_content)
            retry_events: list = []
            if Aclosing is not None:
                async with Aclosing(
                    self.inner_agent.run_async(ctx)
                ) as ragen:
                    async for ev in ragen:
                        retry_events.append(ev)
                        self._maybe_emit_thought(state, inv_id, ev)
                        yield ev
            else:  # pragma: no cover
                async for ev in self.inner_agent.run_async(ctx):
                    retry_events.append(ev)
                    self._maybe_emit_thought(state, inv_id, ev)
                    yield ev
            events.extend(retry_events)
            try:
                retry_summary = self._extract_result_summary(retry_events)
            except Exception:  # noqa: BLE001
                retry_summary = ""
            if retry_summary:
                result_summary = retry_summary
            try:
                outcomes = state.classify_and_sweep_running_tasks(
                    hsession_id,
                    result_summary=result_summary,
                    exclude=pre_running,
                )
            except TypeError:
                try:
                    outcomes = state.classify_and_sweep_running_tasks(
                        hsession_id, result_summary=result_summary
                    )
                except Exception:  # noqa: BLE001
                    break
            except Exception:  # noqa: BLE001
                break

        # Best-effort drift detection at run end. Same pattern as the
        # delegated path: detect_drift first, fall through to semantic
        # drift if no structural signal, then refine.
        plan_state_after = self._get_plan_state(state, hsession_id)
        if plan_state_after is not None:
            drift: Optional[Any] = None
            try:
                drift = state.detect_drift(
                    events, current_task=None, plan_state=plan_state_after
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("HarmonografAgent: detect_drift raised: %s", exc)
                drift = None
            if drift is None:
                try:
                    drift = state.detect_semantic_drift(
                        None, result_summary, events
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "HarmonografAgent: detect_semantic_drift raised: %s",
                        exc,
                    )
                    drift = None
            if drift is not None:
                try:
                    state.refine_plan_on_drift(
                        hsession_id, drift, current_task=None
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "HarmonografAgent: refine_plan_on_drift raised: %s",
                        exc,
                    )

    @staticmethod
    def _validate_invariants(
        state: Any, hsession_id: str, *, context: str
    ) -> None:
        """Run the plan-state invariant checker at a walker turn
        boundary and log / assert any violations. No-op when state or
        hsession_id is missing. Never raises in production; only under
        pytest does an error-severity violation become an assertion.
        """
        if state is None or not hsession_id:
            return
        try:
            violations = check_plan_state(state, hsession_id)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: invariant checker raised: %s", exc
            )
            return
        if violations:
            _enforce_invariants(violations, context=context)

    @staticmethod
    def _has_partial(outcomes: dict[str, str]) -> bool:
        if not outcomes:
            return False
        for v in outcomes.values():
            if v == "partial":
                return True
        return False

    def _write_plan_context_if_possible(
        self, ctx: Any, plan_state: Any, host_agent_id: str
    ) -> None:
        """Project ``plan_state`` into the ADK session.state via the
        state_protocol writer. Best effort: silently no-ops if the
        session, state mapping, or plan is missing.
        """
        if plan_state is None:
            return
        session = getattr(ctx, "session", None)
        if session is None:
            return
        session_state = getattr(session, "state", None)
        if session_state is None:
            return
        try:
            from .state_protocol import write_plan_context
        except Exception:  # noqa: BLE001
            return
        try:
            from types import SimpleNamespace
            plan_obj = getattr(plan_state, "plan", None)
            plan_view = SimpleNamespace(
                id=getattr(plan_state, "plan_id", ""),
                plan_id=getattr(plan_state, "plan_id", ""),
                summary=getattr(plan_obj, "summary", "") or "",
                tasks=list(getattr(plan_obj, "tasks", []) or []),
                edges=list(getattr(plan_obj, "edges", []) or []),
            )
            write_plan_context(
                session_state,
                plan_view,
                completed_results={},
                host_agent=host_agent_id or "",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: write_plan_context failed: %s", exc
            )

    def _build_plan_overview_prompt(self, plan_state: Any) -> Any:
        """Build a single ``Content`` user nudge that lists every task
        in the active plan and instructs the coordinator LLM to drive
        per-task lifecycle via the reporting tools.
        """
        from google.genai import types as genai_types  # type: ignore

        plan_obj = getattr(plan_state, "plan", None) or plan_state
        tasks = list(getattr(plan_obj, "tasks", []) or [])
        edges = list(getattr(plan_obj, "edges", []) or [])
        deps_by_task: dict[str, list[str]] = {}
        for e in edges:
            deps_by_task.setdefault(
                str(getattr(e, "to_task_id", "") or ""), []
            ).append(str(getattr(e, "from_task_id", "") or ""))

        lines: list[str] = ["Here is your plan for this request:", ""]
        for idx, t in enumerate(tasks, start=1):
            tid = str(getattr(t, "id", "") or "")
            title = str(getattr(t, "title", "") or "")
            assignee = str(getattr(t, "assignee_agent_id", "") or "")
            deps = deps_by_task.get(tid, [])
            extras: list[str] = []
            if assignee:
                extras.append(f"assignee: {assignee}")
            if deps:
                extras.append(f"depends on: {', '.join(deps)}")
            extras_str = f" ({'; '.join(extras)})" if extras else ""
            lines.append(f"{idx}. [{tid}] {title}{extras_str}")
        lines.extend(
            [
                "",
                f"You MUST complete every one of the {len(tasks)} tasks "
                "above before ending your turn. Do not stop after the "
                "first task — keep going until all tasks are done or you "
                "have explicitly reported failure on one.",
                "",
                "For each task, as you work on it:",
                "- Call `report_task_started(task_id=\"...\")` before "
                "beginning the task",
                "- Call `report_task_completed(task_id=\"...\", "
                "summary=\"...\")` when done",
                "- Call `report_task_failed(task_id=\"...\", "
                "reason=\"...\")` if you cannot complete it",
                "- Call `report_new_work_discovered(parent_task_id="
                "\"...\", title=\"...\", description=\"...\")` if the "
                "work reveals new required tasks",
                "",
                "Proceed now with task 1 and continue through every "
                "remaining task.",
            ]
        )
        text = "\n".join(lines)
        return genai_types.Content(
            role="user", parts=[genai_types.Part(text=text)]
        )

    def _build_continue_prompt(
        self, plan_state: Any, pending_task_ids: Optional[list[str]] = None
    ) -> Any:
        """Short nudge used between sequential re-invocation passes when
        the classifier still has partial tasks. Keeps the original plan
        context (already in session.state) — only injects the directive.

        If ``pending_task_ids`` is given, the nudge names each remaining
        task id explicitly so the coordinator can't ambiguate its way
        back to another early stop.
        """
        from google.genai import types as genai_types  # type: ignore

        if pending_task_ids:
            id_list = ", ".join(pending_task_ids)
            text = (
                "You ended your turn with work remaining. The following "
                f"task(s) still need to be started and completed: {id_list}. "
                "Continue now and do not stop until every one of them has "
                "reached COMPLETED or FAILED via the report_task_* tools."
            )
        else:
            text = (
                "Some tasks in the plan still need work. Continue executing "
                "the remaining tasks. Remember to call the report_task_* "
                "tools as you progress."
            )
        return genai_types.Content(
            role="user", parts=[genai_types.Part(text=text)]
        )

    def _fire_coordinator_early_stop(
        self,
        state: Any,
        hsession_id: str,
        pending_ids: list[str],
        plan_state: Any,
    ) -> None:
        """Record a ``coordinator_early_stop`` drift against the active
        plan when the sequential coordinator closed its turn with
        PENDING tasks still on the board. Best-effort: logged and
        swallowed — the retry loop is the corrective action, this is
        just the observability hook that puts the revision on the UI.
        """
        try:
            from .adk import (
                DRIFT_KIND_COORDINATOR_EARLY_STOP,
                DriftReason,
            )
        except Exception:  # noqa: BLE001
            return
        drift = DriftReason(
            kind=DRIFT_KIND_COORDINATOR_EARLY_STOP,
            detail=(
                f"coordinator ended turn with {len(pending_ids)} "
                f"task(s) still PENDING: {', '.join(pending_ids)}"
            ),
            severity="warning",
            recoverable=True,
            hint={"pending_task_ids": list(pending_ids)},
        )
        try:
            state.refine_plan_on_drift(
                hsession_id, drift, current_task=None
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: coordinator_early_stop refine raised: %s",
                exc,
            )

    @staticmethod
    def _pending_task_ids(plan_state: Any) -> list[str]:
        """Return the ids of every tracked task still in PENDING status
        on ``plan_state``. Used by the sequential walker to detect
        coordinator early-stop (#26) — when the inner agent closes its
        turn while some tasks were never even started.
        """
        if plan_state is None:
            return []
        out: list[str] = []
        for tid, tracked in (getattr(plan_state, "tasks", {}) or {}).items():
            status = getattr(tracked, "status", "") or ""
            if status == "PENDING":
                out.append(str(tid))
        return out

    @staticmethod
    def _any_task_terminal(plan_state: Any) -> bool:
        """True iff at least one tracked task has reached a terminal
        status. Used as a progress-evidence guard on the sequential
        walker's early-stop retry: if no task ever moved out of PENDING,
        the coordinator never engaged the protocol at all and retrying
        the same turn won't help — that's the partial/no-progress path.
        """
        if plan_state is None:
            return False
        for tracked in (getattr(plan_state, "tasks", {}) or {}).values():
            status = getattr(tracked, "status", "") or ""
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                return True
        return False

    # ------------------------------------------------------------------
    # Orchestrated mode: DAG walker
    # ------------------------------------------------------------------

    async def _run_orchestrated(
        self,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        host_agent_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Dispatch to the sequential single-pass path or the rigid
        parallel DAG walker based on :attr:`parallel_mode`.

        Sequential (default): feed the full plan as one user turn and
        let the coordinator LLM execute it; per-task lifecycle is driven
        by callbacks watching reporting-tool calls.

        Parallel: walk the DAG, batch eligible tasks, dispatch each via
        ``_run_single_task_isolated`` against its assignee with a
        task-local forced task id ContextVar.
        """
        if not self.parallel_mode:
            async for ev in self._run_sequential(
                ctx, state, hsession_id, inv_id, host_agent_id
            ):
                yield ev
            return
        async for ev in self._run_parallel(
            ctx, state, hsession_id, inv_id, host_agent_id
        ):
            yield ev

    async def _run_parallel(
        self,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        host_agent_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Walk the active plan DAG in dependency order, executing
        within-stage eligible tasks in parallel (batched per iteration).

        For each iteration the walker picks ALL tasks whose deps are
        satisfied (``_pick_next_batch``), groups them by assignee (tasks
        with the same assignee still serialize), and runs the groups
        concurrently via ``asyncio.gather``. Each task sets its own
        task-local forced-task-id via a ContextVar so parallel runs
        don't clobber each other. After each batch completes, results
        are folded into ``completed_results`` in deterministic (task id)
        order and structural + semantic drift detection fires per task.
        """
        log.debug(
            "_run_orchestrated: entry hsession=%s inv_id=%s",
            hsession_id, inv_id,
        )
        completed_results: dict[str, str] = {}
        iterations = 0
        # Per-walker-run set of task ids that have already been picked
        # in this _run_async_impl call. The walker physically cannot
        # re-pick a task in this set, even if a refine accidentally
        # resets its status — defensive catch for the cycle bug.
        seen_in_walk: set[str] = set()
        # Cycle detection: track the previous batch's task ids and log
        # ERROR if the same set comes back twice in a row (which would
        # be the cycle returning).
        prev_batch_ids: tuple[str, ...] = ()
        # Serialize concurrent writes to the shared parent session from
        # parallel tasks (nudge append + fold-back synthetic events).
        session_lock = asyncio.Lock()

        while True:
            if iterations >= _ORCHESTRATOR_WALKER_SAFETY_CAP:
                log.warning(
                    "HarmonografAgent: orchestrator walker hit safety cap (%d)",
                    _ORCHESTRATOR_WALKER_SAFETY_CAP,
                )
                log.debug(
                    "_run_orchestrated: exit iterations=%d reason=safety_cap",
                    iterations,
                )
                break
            iterations += 1

            plan_state = self._get_plan_state(state, hsession_id)
            batch = self._pick_next_batch(
                plan_state, completed_results, seen_in_walk
            )
            cur_batch_ids = tuple(
                str(getattr(t, "id", "") or "") for t in batch
            )
            if cur_batch_ids and cur_batch_ids == prev_batch_ids:
                log.error(
                    "HarmonografAgent: CYCLE DETECTED — _pick_next_batch "
                    "returned identical batch %s twice in a row "
                    "(iteration %d). Breaking out to prevent infinite loop.",
                    cur_batch_ids, iterations,
                )
                log.debug(
                    "_run_orchestrated: exit iterations=%d reason=cycle",
                    iterations,
                )
                break
            prev_batch_ids = cur_batch_ids
            for tid in cur_batch_ids:
                if tid:
                    seen_in_walk.add(tid)
            if not batch:
                # First iteration with no plan or no eligible task: fall
                # back to a single delegated pass so a plan-less run
                # still produces output. Subsequent iterations just stop.
                if iterations == 1:
                    async for event in self._run_delegated(
                        ctx, state, hsession_id, inv_id, host_agent_id
                    ):
                        yield event
                log.debug(
                    "_run_orchestrated: exit iterations=%d reason=empty_batch",
                    iterations,
                )
                break

            # Single-task batch (common case): keep the serial in-place
            # path so events yield directly to the parent generator and
            # existing semantics are preserved bit-for-bit.
            if len(batch) == 1:
                task = batch[0]
                async for ev in self._run_task_inplace(
                    task, ctx, state, hsession_id, inv_id,
                    plan_state, completed_results,
                ):
                    yield ev
                continue

            # Multi-task batch: group by assignee, run groups in
            # parallel, serialize within group. Events stream back to
            # the parent via an asyncio.Queue.
            groups: dict[str, list[Any]] = {}
            for task in batch:
                a = str(getattr(task, "assignee_agent_id", "") or "")
                groups.setdefault(a, []).append(task)

            event_queue: asyncio.Queue[Any] = asyncio.Queue()
            SENTINEL = object()

            async def run_group(group_tasks: list[Any]) -> list[tuple[Any, list, str]]:
                out: list[tuple[Any, list, str]] = []
                for task in group_tasks:
                    task_events, summary = await self._run_single_task_isolated(
                        task, ctx, state, hsession_id, inv_id,
                        plan_state, completed_results,
                        event_queue, session_lock,
                    )
                    out.append((task, task_events, summary))
                return out

            gather_fut = asyncio.gather(
                *(run_group(g) for g in groups.values())
            )

            async def drain_when_done() -> None:
                try:
                    await gather_fut
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    await event_queue.put(SENTINEL)

            drainer = asyncio.create_task(drain_when_done())

            try:
                while True:
                    item = await event_queue.get()
                    if item is SENTINEL:
                        break
                    yield item
                group_results_all = await gather_fut
            finally:
                if not drainer.done():
                    try:
                        await drainer
                    except Exception:  # noqa: BLE001
                        pass

            # Fold results deterministically by task id.
            batch_results: list[tuple[Any, list, str]] = []
            for group_results in group_results_all:
                batch_results.extend(group_results)
            batch_results.sort(key=lambda x: str(getattr(x[0], "id", "") or ""))

            for task, task_events, result_summary in batch_results:
                tid = str(getattr(task, "id", "") or "")
                if tid:
                    completed_results[tid] = result_summary

            # Drift detection (structural + semantic) per completed task.
            if state is not None and hsession_id:
                plan_state_after = self._get_plan_state(state, hsession_id)
                for task, task_events, result_summary in batch_results:
                    drift: Optional[Any] = None
                    try:
                        drift = state.detect_drift(
                            task_events,
                            current_task=task,
                            plan_state=plan_state_after,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "HarmonografAgent: detect_drift raised: %s", exc
                        )
                        drift = None
                    if drift is None:
                        try:
                            drift = state.detect_semantic_drift(
                                task, result_summary, task_events
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug(
                                "HarmonografAgent: detect_semantic_drift raised: %s",
                                exc,
                            )
                            drift = None
                    if drift is not None:
                        try:
                            state.refine_plan_on_drift(
                                hsession_id, drift, current_task=task
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "HarmonografAgent: refine_plan_on_drift raised: %s",
                                exc,
                            )

    async def _run_task_inplace(
        self,
        task: Any,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        plan_state: Any,
        completed_results: dict[str, str],
    ) -> AsyncGenerator[Any, None]:
        """Single-task serial path: set forced-task-id, inject a nudge,
        run the inner agent directly on the parent ctx, yield events,
        mark completion, run structural + semantic drift observers.
        """
        task_id = str(getattr(task, "id", "") or "")
        log.debug(
            "run_task: task=%s assignee=%s",
            task_id,
            str(getattr(task, "assignee_agent_id", "") or ""),
        )
        if state is not None and task_id:
            try:
                state.set_forced_task_id(task_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "HarmonografAgent: set_forced_task_id failed: %s", exc
                )

        try:
            prompt_content = self._build_task_prompt(
                task, plan_state, completed_results
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: build task prompt failed: %s", exc
            )
            return
        self._append_nudge_event(ctx, prompt_content)

        turn_events: list = []
        if Aclosing is not None:
            async with Aclosing(self.inner_agent.run_async(ctx)) as agen:
                async for event in agen:
                    turn_events.append(event)
                    self._maybe_emit_thought(state, inv_id, event)
                    yield event
        else:  # pragma: no cover
            async for event in self.inner_agent.run_async(ctx):
                turn_events.append(event)
                self._maybe_emit_thought(state, inv_id, event)
                yield event
        log.debug(
            "run_task: inner_agent.run_async exited task=%s events=%d",
            task_id, len(turn_events),
        )

        result_summary = self._extract_result_summary(turn_events)
        if task_id:
            completed_results[task_id] = result_summary

        # The classifier inspects the result summary (and any tool
        # errors recorded against the task this turn) and routes each
        # RUNNING task to COMPLETED / FAILED / partial-keep-RUNNING.
        # Partial-keep-RUNNING triggers an in-place re-invocation loop
        # capped by ``state.reinvocation_budget()``; on cap exhaustion
        # the task transitions to FAILED via ``mark_task_failed``.
        outcomes: dict[str, str] = {}
        if state is not None:
            try:
                outcomes = state.classify_and_sweep_running_tasks(
                    hsession_id, result_summary=result_summary
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "HarmonografAgent: classify_and_sweep_running_tasks failed: %s",
                    exc,
                )
                outcomes = {}

            partial_retries = 0
            try:
                budget = state.reinvocation_budget()
            except Exception:  # noqa: BLE001
                budget = _PARTIAL_REINVOCATION_BUDGET
            while (
                task_id
                and outcomes.get(task_id) == "partial"
                and state.task_status(hsession_id, task_id) == "RUNNING"
            ):
                if partial_retries >= budget:
                    log.info(
                        "HarmonografAgent: task %s exhausted re-invocation "
                        "budget (%d) — marking FAILED",
                        task_id, budget,
                    )
                    try:
                        state.mark_task_failed(
                            hsession_id, task_id,
                            "reinvocation budget exhausted",
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug(
                            "HarmonografAgent: mark_task_failed failed: %s", exc
                        )
                    break
                try:
                    state.note_task_reinvocation(task_id)
                except Exception:  # noqa: BLE001
                    pass
                partial_retries += 1
                log.info(
                    "HarmonografAgent: task %s partial — re-invoking "
                    "(attempt %d/%d)",
                    task_id, partial_retries, budget,
                )
                try:
                    state.set_forced_task_id(task_id)
                except Exception:  # noqa: BLE001
                    pass
                # Re-use _build_task_prompt so the retry still carries
                # full task context + predecessor summaries; appending
                # only the boilerplate "continue" nudge would strip
                # predecessor results from the LLM's view.
                try:
                    retry_prompt = self._build_task_prompt(
                        task, plan_state, completed_results
                    )
                except Exception:  # noqa: BLE001
                    break
                self._append_nudge_event(ctx, retry_prompt)
                retry_events: list = []
                if Aclosing is not None:
                    async with Aclosing(
                        self.inner_agent.run_async(ctx)
                    ) as ragen:
                        async for ev in ragen:
                            retry_events.append(ev)
                            self._maybe_emit_thought(state, inv_id, ev)
                            yield ev
                else:  # pragma: no cover
                    async for ev in self.inner_agent.run_async(ctx):
                        retry_events.append(ev)
                        self._maybe_emit_thought(state, inv_id, ev)
                        yield ev
                turn_events.extend(retry_events)
                retry_summary = self._extract_result_summary(retry_events)
                if retry_summary:
                    result_summary = retry_summary
                    if task_id:
                        completed_results[task_id] = result_summary
                try:
                    outcomes = state.classify_and_sweep_running_tasks(
                        hsession_id, result_summary=result_summary
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "HarmonografAgent: retry classify failed: %s", exc
                    )
                    break

        if state is not None and hsession_id:
            plan_state_after = self._get_plan_state(state, hsession_id)
            drift: Optional[Any] = None
            try:
                drift = state.detect_drift(
                    turn_events,
                    current_task=task,
                    plan_state=plan_state_after,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "HarmonografAgent: detect_drift raised: %s", exc
                )
                drift = None
            if drift is None:
                try:
                    drift = state.detect_semantic_drift(
                        task, result_summary, turn_events
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "HarmonografAgent: detect_semantic_drift raised: %s",
                        exc,
                    )
                    drift = None
            if drift is not None:
                try:
                    state.refine_plan_on_drift(
                        hsession_id, drift, current_task=task
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "HarmonografAgent: refine_plan_on_drift raised: %s",
                        exc,
                    )

    async def _run_single_task_isolated(
        self,
        task: Any,
        ctx: Any,
        state: Any,
        hsession_id: str,
        inv_id: str,
        plan_state: Any,
        completed_results: dict[str, str],
        event_queue: asyncio.Queue,
        session_lock: asyncio.Lock,
    ) -> tuple[list, str]:
        """Run a single task in parallel isolation: sets the forced task
        id via the task-local ContextVar (so sibling parallel tasks each
        get their own), resolves the assignee sub-agent, builds a task
        prompt, runs the inner agent, pushes every yielded event onto
        ``event_queue`` so the parent generator can stream them, and
        returns ``(events, summary)``.

        Session mutations (nudge append + fold-back synthetic event) are
        guarded by ``session_lock`` so concurrent parallel tasks don't
        clobber ``ctx.session.events``.
        """
        task_id = str(getattr(task, "id", "") or "")
        assignee = str(getattr(task, "assignee_agent_id", "") or "")

        # Task-local forced id: write the ContextVar directly with
        # explicit token scoping so sibling parallel coroutines each see
        # their own task id and the write is reset when this helper
        # returns (no leakage across tests or future iterations).
        from .adk import _forced_task_id_var as _tid_var
        token = _tid_var.set(task_id or None)

        sub_agent = self._resolve_assignee_agent(assignee)

        try:
            prompt_content = self._build_task_prompt(
                task, plan_state, completed_results
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: build task prompt failed: %s", exc
            )
            return [], ""

        async with session_lock:
            self._append_nudge_event(ctx, prompt_content)

        turn_events: list = []
        try:
            if Aclosing is not None:
                async with Aclosing(sub_agent.run_async(ctx)) as agen:
                    async for event in agen:
                        turn_events.append(event)
                        self._maybe_emit_thought(state, inv_id, event)
                        await event_queue.put(event)
            else:  # pragma: no cover
                async for event in sub_agent.run_async(ctx):
                    turn_events.append(event)
                    self._maybe_emit_thought(state, inv_id, event)
                    await event_queue.put(event)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: isolated task %s raised: %s", task_id, exc
            )

        result_summary = self._extract_result_summary(turn_events)

        if state is not None:
            try:
                state.mark_forced_task_completed()
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "HarmonografAgent: mark_forced_task_completed failed: %s",
                    exc,
                )

        try:
            _tid_var.reset(token)
        except Exception:  # noqa: BLE001
            _tid_var.set(None)

        return turn_events, result_summary

    def _resolve_assignee_agent(self, assignee: str) -> Any:
        """Map a plan task's ``assignee_agent_id`` to a concrete
        :class:`BaseAgent` to run. Prefers an exact-name match on
        ``inner_agent.sub_agents`` (the inner coordinator's direct
        children, typically the real worker agents), then checks
        ``inner_agent`` itself, then falls through to ``inner_agent`` as
        the default. Parallel execution uses whatever this returns.
        """
        if not assignee:
            return self.inner_agent
        inner = self.inner_agent
        # Direct name match on inner_agent first.
        if getattr(inner, "name", "") == assignee:
            return inner
        # Search one level down for matching sub-agents.
        for sa in getattr(inner, "sub_agents", None) or []:
            if getattr(sa, "name", "") == assignee:
                return sa
        return inner

    def _pick_next_batch(
        self,
        plan_state: Any,
        completed_results: dict[str, str],
        seen_in_walk: Optional[set[str]] = None,
    ) -> list[Any]:
        """Return ALL PENDING tasks whose deps are satisfied, in plan
        order. Used by the parallel walker to run within-stage eligible
        tasks concurrently. Returns ``[]`` if none.

        IMPORTANT: status authority is ``plan_state.tasks[tid].status``,
        NEVER ``completed_results``. ``completed_results`` is used only
        to build predecessor-context prompts; it is not authoritative on
        whether a task is done. Dep satisfaction is judged purely from
        the tracked status of each predecessor task.

        ``seen_in_walk`` is a set of task ids already executed in the
        current walker run. Tasks present in this set are NEVER
        re-selected, even if their tracked status is PENDING (defensive
        catch for any future cycle bug — the walker physically cannot
        re-pick a task it already ran).
        """
        if plan_state is None:
            return []
        plan = getattr(plan_state, "plan", None)
        if plan is None:
            return []
        tasks_by_id = getattr(plan_state, "tasks", {}) or {}
        edges = list(getattr(plan, "edges", []) or [])
        deps_by_task: dict[str, list[str]] = {}
        for e in edges:
            deps_by_task.setdefault(
                getattr(e, "to_task_id", ""), []
            ).append(getattr(e, "from_task_id", ""))
        out: list[Any] = []
        for t in getattr(plan, "tasks", []) or []:
            tid = getattr(t, "id", "") or ""
            if not tid:
                continue
            if seen_in_walk is not None and tid in seen_in_walk:
                continue
            tracked = tasks_by_id.get(tid, t)
            status = (getattr(tracked, "status", "") or "PENDING")
            if status != "PENDING":
                continue
            deps = deps_by_task.get(tid, [])
            blocked = False
            for dep_id in deps:
                dep = tasks_by_id.get(dep_id)
                if dep is None:
                    blocked = True
                    break
                if (getattr(dep, "status", "") or "") != "COMPLETED":
                    blocked = True
                    break
            if not blocked:
                out.append(tracked)
        return out

    # ------------------------------------------------------------------
    # Walker helpers
    # ------------------------------------------------------------------

    def _get_plan_state(self, state: Any, hsession_id: str) -> Any:
        if state is None or not hsession_id:
            return None
        try:
            return state._active_plan_by_session.get(hsession_id)
        except Exception:  # noqa: BLE001
            return None

    def _pick_next_task(
        self, plan_state: Any, completed_results: dict[str, str]
    ) -> Any:
        """Return the first PENDING task whose deps are all COMPLETED in
        ``plan_state.tasks`` (or already in ``completed_results``).
        Returns None if no eligible task exists.
        """
        if plan_state is None:
            return None
        plan = getattr(plan_state, "plan", None)
        if plan is None:
            return None
        tasks_by_id = getattr(plan_state, "tasks", {}) or {}
        edges = list(getattr(plan, "edges", []) or [])
        deps_by_task: dict[str, list[str]] = {}
        for e in edges:
            deps_by_task.setdefault(
                getattr(e, "to_task_id", ""), []
            ).append(getattr(e, "from_task_id", ""))
        for t in getattr(plan, "tasks", []) or []:
            tid = getattr(t, "id", "") or ""
            if not tid:
                continue
            tracked = tasks_by_id.get(tid, t)
            status = (getattr(tracked, "status", "") or "PENDING")
            if status != "PENDING":
                continue
            deps = deps_by_task.get(tid, [])
            blocked = False
            for dep_id in deps:
                if dep_id in completed_results:
                    continue
                dep = tasks_by_id.get(dep_id)
                if dep is None:
                    blocked = True
                    break
                if (getattr(dep, "status", "") or "") != "COMPLETED":
                    blocked = True
                    break
            if not blocked:
                return tracked
        return None

    def _build_task_prompt(
        self,
        task: Any,
        plan_state: Any,
        completed_results: dict[str, str],
    ) -> Any:
        """Build a synthetic user Content with the task prompt plus
        predecessor-task context.
        """
        from google.genai import types as genai_types  # type: ignore

        title = str(getattr(task, "title", "") or "")
        description = str(getattr(task, "description", "") or "")
        tid = str(getattr(task, "id", "") or "")
        parts: list[str] = [f"Your current task is: {title or tid}"]
        if description:
            parts.append(description)
        predecessor_ids: list[str] = []
        if plan_state is not None:
            edges = list(getattr(plan_state, "edges", []) or [])
            for e in edges:
                if getattr(e, "to_task_id", "") == tid:
                    predecessor_ids.append(getattr(e, "from_task_id", ""))
        if predecessor_ids:
            parts.append("\nContext from completed predecessor tasks:")
            tasks_by_id = getattr(plan_state, "tasks", {}) or {}
            for pid in predecessor_ids:
                pred = tasks_by_id.get(pid)
                pred_title = (
                    str(getattr(pred, "title", "") or "") if pred else pid
                )
                result = completed_results.get(pid, "")
                if result:
                    parts.append(f"- {pred_title}: {result}")
                else:
                    parts.append(f"- {pred_title}: (no recorded output)")
        parts.append(
            "\nExecute this task now. Use your tools as needed. "
            "When complete, produce your final response.\n"
            "When you have finished this task, end your response with "
            "the line: 'Task complete: <one-line summary>'. "
            "If you cannot finish the task (for example: a tool failed, "
            "you lack the data, or the request is impossible), instead "
            "end your response with: 'Task failed: <one-line reason>'. "
            "These markers tell the orchestrator the task's outcome — "
            "do not omit them."
        )
        text = "\n".join(parts)
        return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])

    def _extract_result_summary(
        self, events: list, max_len: int = 500
    ) -> str:
        """Return the trailing text from the last model event that
        carries non-thought text parts, truncated to ``max_len``.
        """
        for event in reversed(events or []):
            content = getattr(event, "content", None)
            if content is None:
                continue
            parts = getattr(content, "parts", None) or []
            chunks: list[str] = []
            for p in parts:
                text = getattr(p, "text", None)
                if text and not getattr(p, "thought", False):
                    chunks.append(str(text))
            if chunks:
                return "".join(chunks)[:max_len]
        return ""

    def _resolve_hsession(self, state: Any, ctx: Any, inv_id: str) -> str:
        """Resolve the harmonograf session id for this invocation.

        The plugin's ``on_invocation_start`` has already populated
        ``_invocation_route`` for the current ``inv_id``, so we can read
        the hsession directly off it. Falls back to the ContextVar for
        nested/sub-invocations and finally to the empty string.
        """
        if state is None:
            return ""
        try:
            with state._lock:
                _, hs = state._invocation_route.get(inv_id, ("", ""))
        except Exception:  # noqa: BLE001
            hs = ""
        if hs:
            return hs
        try:
            return state._current_root_hsession_var.get() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _assign_forced_task(
        self, state: Any, hsession_id: str, host_agent_id: str
    ) -> None:
        """Pick the next unblocked task assigned to ``host_agent_id`` and
        declare it as the forced current task on ``state``. Spans emitted
        during the next inner-agent step will then bind to that task by
        id (via :meth:`_AdkState._stamp_attrs_with_task`), rather than
        by the fragile assignee-string heuristic.
        """
        if state is None or not hsession_id or not host_agent_id:
            return
        try:
            next_task = state._next_task_for_agent(hsession_id, host_agent_id)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: _next_task_for_agent failed: %s", exc
            )
            return
        try:
            state.set_forced_task_id(
                getattr(next_task, "id", "") if next_task is not None else ""
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "HarmonografAgent: set_forced_task_id failed: %s", exc
            )

    def _maybe_emit_thought(self, state: Any, inv_id: str, event: Any) -> None:
        """If the yielded event carries a thinking/reasoning part, attach
        the accumulated text as an ``llm.thought`` attribute on the
        in-flight LLM_CALL span. Falls through silently if the event has
        no thought content, if no LLM span is open, or if the harmonograf
        client isn't wired up. Idempotent across streaming chunks — the
        plugin state accumulates chunks into a single bounded string.
        """
        if state is None:
            return
        client = self.harmonograf_client
        if client is None:
            client = getattr(state, "_client", None)
        if client is None:
            return
        chunk = _extract_thought_text(event)
        if not chunk:
            return
        try:
            span_id = state.current_llm_span_id(inv_id)
        except Exception:  # noqa: BLE001
            span_id = None
        if not span_id:
            return
        try:
            aggregated = state.record_llm_thought(span_id, chunk)
        except Exception as exc:  # noqa: BLE001
            log.debug("HarmonografAgent: record_llm_thought failed: %s", exc)
            return
        if not aggregated:
            return
        try:
            client.emit_span_update(
                span_id, attributes={"llm.thought": aggregated}
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("HarmonografAgent: emit_span_update(llm.thought) failed: %s", exc)

    def _append_nudge_event(self, ctx: Any, content: Any) -> None:
        """Append a synthetic user event to the session so the next
        ``inner_agent.run_async(ctx)`` call sees the nudge as if the
        user had typed it.
        """
        session = getattr(ctx, "session", None)
        if session is None:
            return
        inv_id = getattr(ctx, "invocation_id", "") or ""
        try:
            event = Event(
                invocation_id=inv_id,
                author="user",
                content=content,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: could not construct nudge Event (%s); stopping",
                exc,
            )
            return
        events = getattr(session, "events", None)
        if events is None:
            return
        try:
            events.append(event)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HarmonografAgent: could not append nudge event (%s); stopping",
                exc,
            )


def make_harmonograf_agent(
    *,
    name: str,
    inner_agent: Any,
    harmonograf_client: Any = None,
    planner: Any = None,
    planner_model: str = "",
    refine_on_events: bool = True,
    enforce_plan: bool = True,
    max_plan_reinvocations: int = _DEFAULT_MAX_PLAN_REINVOCATIONS,
    description: str = "",
) -> HarmonografAgent:
    """Construct a :class:`HarmonografAgent` around ``inner_agent``.

    Thin factory that mirrors :func:`make_adk_plugin` / :func:`attach_adk`
    style so callers have a single verb to reach for.
    """
    return HarmonografAgent(
        name=name,
        description=description or f"Harmonograf orchestrator wrapping {inner_agent.name}",
        inner_agent=inner_agent,
        harmonograf_client=harmonograf_client,
        planner=planner,
        planner_model=planner_model,
        refine_on_events=refine_on_events,
        enforce_plan=enforce_plan,
        max_plan_reinvocations=max_plan_reinvocations,
    )


__all__ = ["HarmonografAgent", "make_harmonograf_agent"]
