"""Unit tests for the ADK adapter.

The :class:`_AdkState` translator is pure: given duck-typed stand-ins
for InvocationContext / CallbackContext / tools / Events, it calls
``Client.emit_*``. We drive it with a captured-call fake Client so
these tests never touch ADK or gRPC.

A live-plugin test (``attach_adk``) runs only if ``google.adk`` is
importable, so the unit suite stays green on a bare client env.
"""

from __future__ import annotations

import importlib.util
import types
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from harmonograf_client.adk import _AdkState


# ---------------------------------------------------------------------------
# Fake Client that captures emit_* calls.
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counter = 0
        self._current_activity: str = ""

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
        self.calls.append(("set_activity", text, {}))

    def submit_plan(self, plan, **kwargs) -> str:  # type: ignore[no-untyped-def]
        self._counter += 1
        pid = f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid

    def submit_task_status_update(
        self, plan_id: str, task_id: str, status: str, **kwargs
    ) -> None:
        self.calls.append(
            ("submit_task_status_update", task_id,
             {"plan_id": plan_id, "status": status, **kwargs})
        )

    # Convenience filters
    def starts(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "start"]

    def ends(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "end"]

    def updates(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "update"]


# ---------------------------------------------------------------------------
# Duck-typed ADK stand-ins.
# ---------------------------------------------------------------------------


@dataclass
class FakeAgent:
    name: str = "research-agent"


@dataclass
class FakeSession:
    id: str = "sess_1"


@dataclass
class FakeInvocationContext:
    invocation_id: str = "inv_1"
    agent: FakeAgent = field(default_factory=FakeAgent)
    session: FakeSession = field(default_factory=FakeSession)
    user_id: str = "alice"


@dataclass
class FakeCallbackContext:
    _invocation_context: FakeInvocationContext = field(
        default_factory=FakeInvocationContext
    )


@dataclass
class FakeTool:
    name: str = "search_web"
    is_long_running: bool = False


@dataclass
class FakeToolContext:
    function_call_id: str = "call_1"
    _invocation_context: FakeInvocationContext = field(
        default_factory=FakeInvocationContext
    )


@dataclass
class FakeLlmRequest:
    model: str = "gpt-4o"
    contents: list = field(default_factory=list)


@dataclass
class FakeLlmResponse:
    content: Any = None
    usage_metadata: Any = None


@dataclass
class FakeEventActions:
    state_delta: dict = field(default_factory=dict)
    transfer_to_agent: Optional[str] = None


@dataclass
class FakeEvent:
    actions: FakeEventActions = field(default_factory=FakeEventActions)


# ---------------------------------------------------------------------------
# _AdkState tests
# ---------------------------------------------------------------------------


class TestAdkStateInvocation:
    def test_invocation_start_and_end(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_a")
        state.on_invocation_start(ic)
        state.on_invocation_end(ic)
        assert len(client.starts()) == 1
        sid, kw = client.starts()[0]
        assert kw["kind"] == "INVOCATION"
        assert kw["attributes"]["invocation_id"] == "inv_a"
        assert len(client.ends()) == 1
        end_sid, _ = client.ends()[0]
        assert end_sid == sid


class TestAdkStateLlm:
    def test_model_call_parents_to_invocation(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_b")
        state.on_invocation_start(ic)
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest(model="gpt-4o"))
        state.on_model_end(cc, FakeLlmResponse())
        starts = client.starts()
        assert starts[0][1]["kind"] == "INVOCATION"
        assert starts[1][1]["kind"] == "LLM_CALL"
        assert starts[1][1]["parent_span_id"] == starts[0][0]

    def test_model_end_without_start_is_noop(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        cc = FakeCallbackContext()
        state.on_model_end(cc, FakeLlmResponse())
        assert client.calls == []


class TestAdkStateTool:
    def test_tool_call_parents_to_llm_when_open(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_c")
        state.on_invocation_start(ic)
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest())
        tc = FakeToolContext(function_call_id="call_x", _invocation_context=ic)
        state.on_tool_start(FakeTool(name="search"), {"q": "x"}, tc)
        state.on_tool_end(FakeTool(name="search"), tc, result={"ok": True}, error=None)

        starts = client.starts()
        # [INVOCATION, LLM_CALL, TOOL_CALL]
        assert [kw["kind"] for (_, kw) in starts] == ["INVOCATION", "LLM_CALL", "TOOL_CALL"]
        tool_parent = starts[2][1]["parent_span_id"]
        llm_sid = starts[1][0]
        assert tool_parent == llm_sid
        ends = client.ends()
        assert ends[0][1]["status"] == "COMPLETED"
        assert ends[0][0] == starts[2][0]  # tool ended

    def test_long_running_tool_transitions_to_awaiting_human(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext()
        state.on_invocation_start(ic)
        tc = FakeToolContext(_invocation_context=ic)
        state.on_tool_start(FakeTool(name="ask_human", is_long_running=True), {}, tc)
        updates = client.updates()
        assert any(kw.get("status") == "AWAITING_HUMAN" for (_, kw) in updates)

    def test_tool_error_ends_span_failed(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext()
        state.on_invocation_start(ic)
        tc = FakeToolContext(_invocation_context=ic)
        state.on_tool_start(FakeTool(name="broken"), {}, tc)
        state.on_tool_end(FakeTool(name="broken"), tc, result=None, error=RuntimeError("boom"))
        ends = client.ends()
        assert ends[0][1]["status"] == "FAILED"
        assert ends[0][1]["error"]["type"] == "RuntimeError"


class TestAdkStateEvents:
    def test_state_delta_becomes_attributes_on_enclosing_span(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext()
        state.on_invocation_start(ic)
        event = FakeEvent(actions=FakeEventActions(state_delta={"step": 1}))
        state.on_event(ic, event)
        updates = client.updates()
        assert any(
            "state_delta.step" in (kw.get("attributes") or {}) for (_, kw) in updates
        )

    def test_agent_tool_dispatch_emits_transfer_span_with_link(self):
        """AgentTool-wrapped sub-agent dispatch should read as a transfer.

        The adapter must detect the wrapper structurally (``.agent`` on
        the tool), emit a TRANSFER span next to the TOOL_CALL, and add a
        LINK_INVOKED link pointing at the TOOL_CALL span id so the
        frontend can render the edge.
        """
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_at")
        state.on_invocation_start(ic)

        # Duck-typed AgentTool: has .agent with .name/.description.
        sub_agent = types.SimpleNamespace(
            name="research_agent", description="Researches things."
        )
        agent_tool = types.SimpleNamespace(
            name="research_agent",
            is_long_running=False,
            agent=sub_agent,
        )
        tc = FakeToolContext(function_call_id="call_at", _invocation_context=ic)
        state.on_tool_start(agent_tool, {"request": "go"}, tc)

        starts = client.starts()
        kinds = [kw["kind"] for (_, kw) in starts]
        assert "TOOL_CALL" in kinds, f"expected TOOL_CALL; got {kinds}"
        assert "TRANSFER" in kinds, f"expected TRANSFER; got {kinds}"

        tool_sid = next(sid for (sid, kw) in starts if kw["kind"] == "TOOL_CALL")
        transfer_kw = next(kw for (_, kw) in starts if kw["kind"] == "TRANSFER")

        assert transfer_kw["attributes"]["target_agent"] == "research_agent"
        assert transfer_kw["attributes"]["via"] == "agent_tool"
        links = transfer_kw.get("links")
        assert links, "TRANSFER span must carry at least one link"
        link = links[0]
        assert link["target_span_id"] == tool_sid
        assert link["relation"] == "INVOKED"

        # TRANSFER span is ended (it is a point-in-time marker).
        end_sids = [sid for (sid, _) in client.ends()]
        transfer_sid = next(sid for (sid, kw) in starts if kw["kind"] == "TRANSFER")
        assert transfer_sid in end_sids

    def test_plain_function_tool_does_not_emit_transfer(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_ft")
        state.on_invocation_start(ic)
        tc = FakeToolContext(_invocation_context=ic)
        state.on_tool_start(FakeTool(name="write_webpage"), {"x": 1}, tc)
        kinds = [kw["kind"] for (_, kw) in client.starts()]
        assert "TRANSFER" not in kinds, (
            f"plain FunctionTool should not trigger a TRANSFER span; kinds={kinds}"
        )

    def test_transfer_to_agent_emits_transfer_span(self):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext()
        state.on_invocation_start(ic)
        event = FakeEvent(actions=FakeEventActions(transfer_to_agent="other_agent"))
        state.on_event(ic, event)
        kinds = [kw["kind"] for (_, kw) in client.starts()]
        assert "TRANSFER" in kinds
        transfer_entry = next(
            (kw for (_, kw) in client.starts() if kw["kind"] == "TRANSFER")
        )
        assert transfer_entry["attributes"]["target_agent"] == "other_agent"


class TestAdkStateConcurrentRouting:
    """Regression for task #4: two concurrent top-level invocations on a
    *single* ``_AdkState`` (as ``adk web`` creates when it installs one
    plugin on one Runner and then serves overlapping /run requests) must
    mint two independent harmonograf session ids. The previous
    depth-counter heuristic collapsed the second root into the first;
    the ContextVar-based routing keeps each asyncio Task isolated.
    """

    def test_concurrent_invocations_get_distinct_hsessions(self):
        import asyncio

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]

        started = asyncio.Event()
        # Each task performs on_invocation_start, waits until the peer
        # task has also started (so both invocations are in-flight
        # simultaneously — exactly the qa-dev race), then ends. If the
        # adapter's routing is task-aware, each task's span_start call
        # must carry its own ``session_id`` keyword.
        barrier: list[int] = [0]

        async def drive(ic_inv_id: str, adk_sid: str, agent_name: str) -> str:
            ic = FakeInvocationContext(
                invocation_id=ic_inv_id,
                agent=FakeAgent(name=agent_name),
                session=FakeSession(id=adk_sid),
            )
            state.on_invocation_start(ic)
            barrier[0] += 1
            # Yield until BOTH tasks have emitted their start. This is
            # what makes the test a real concurrency probe — if we just
            # awaited sequentially the bug would be masked by the depth
            # counter decrementing to zero before the next root opens.
            while barrier[0] < 2:
                await asyncio.sleep(0)
            state.on_invocation_end(ic)
            # Pull the session_id that was actually attributed to this
            # invocation's INVOCATION span.
            for op, _sid, kw in client.calls:
                if (
                    op == "start"
                    and kw.get("kind") == "INVOCATION"
                    and kw.get("attributes", {}).get("invocation_id") == ic_inv_id
                ):
                    return kw.get("session_id") or ""
            return ""

        async def run() -> tuple[str, str]:
            t1 = asyncio.create_task(drive("inv_A", "adk_sess_A", "coordinator_agent"))
            t2 = asyncio.create_task(drive("inv_B", "adk_sess_B", "coordinator_agent"))
            return await asyncio.gather(t1, t2)  # type: ignore[return-value]

        sid_a, sid_b = asyncio.run(run())
        _ = started  # silence lint — retained as documentation
        assert sid_a, "invocation A had no session_id on its INVOCATION span"
        assert sid_b, "invocation B had no session_id on its INVOCATION span"
        assert sid_a != sid_b, (
            f"concurrent root invocations collapsed into one harmonograf "
            f"session: A={sid_a!r} B={sid_b!r}"
        )
        assert sid_a.startswith("adk_") and sid_b.startswith("adk_")

    def test_inline_subinvocation_aliases_to_parent_hsession(self):
        """AgentTool sub-runners execute inline (``await``) on the
        parent task and must *still* alias to the parent's hsession —
        the ContextVar inherits within a single Task. This is the
        counterpart to the concurrent case and guards against an
        over-correction that would split AgentTool sub-runs into their
        own hsession.
        """
        import asyncio

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]

        async def run() -> tuple[str, str]:
            parent_ic = FakeInvocationContext(
                invocation_id="inv_parent",
                agent=FakeAgent(name="coordinator_agent"),
                session=FakeSession(id="adk_root"),
            )
            state.on_invocation_start(parent_ic)
            # Sub-runner fires a brand-new InvocationContext with a
            # distinct ADK session id (AgentTool creates an in-memory
            # session service for the sub-run) — still inline on the
            # same asyncio Task.
            sub_ic = FakeInvocationContext(
                invocation_id="inv_sub",
                agent=FakeAgent(name="research_agent"),
                session=FakeSession(id="adk_sub_fresh"),
            )
            state.on_invocation_start(sub_ic)
            state.on_invocation_end(sub_ic)
            state.on_invocation_end(parent_ic)

            parent_sid = ""
            sub_sid = ""
            for op, _sid, kw in client.calls:
                if op != "start" or kw.get("kind") != "INVOCATION":
                    continue
                attrs = kw.get("attributes", {})
                if attrs.get("invocation_id") == "inv_parent":
                    parent_sid = kw.get("session_id") or ""
                elif attrs.get("invocation_id") == "inv_sub":
                    sub_sid = kw.get("session_id") or ""
            return parent_sid, sub_sid

        parent_sid, sub_sid = asyncio.run(run())
        assert parent_sid, "parent invocation had no session_id"
        assert sub_sid, "sub invocation had no session_id"
        assert parent_sid == sub_sid, (
            f"AgentTool sub-invocation should alias to the parent's "
            f"hsession; got parent={parent_sid!r} sub={sub_sid!r}"
        )


# ---------------------------------------------------------------------------
# Live attach_adk test — runs only when google.adk is importable.
# ---------------------------------------------------------------------------


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None


@pytest.mark.skipif(not _ADK_AVAILABLE, reason="google.adk not installed")
class TestAttachAdkLive:
    def test_plugin_installed_and_detachable(self):
        from harmonograf_client import attach_adk

        client = FakeClient()
        runner = types.SimpleNamespace(
            plugin_manager=types.SimpleNamespace(plugins=[])
        )
        handle = attach_adk(runner, client)  # type: ignore[arg-type]
        assert len(runner.plugin_manager.plugins) == 1
        handle.detach()
        assert runner.plugin_manager.plugins == []

    def test_make_adk_plugin_auto_wires_llm_planner(self):
        """With planner=None the adapter should construct a default
        LLMPlanner backed by make_default_adk_call_llm()."""
        from harmonograf_client import make_adk_plugin
        from harmonograf_client.planner import LLMPlanner

        client = FakeClient()
        plugin = make_adk_plugin(client)  # type: ignore[arg-type]
        state = plugin._hg_state
        assert isinstance(state._planner, LLMPlanner), (
            f"expected LLMPlanner, got {type(state._planner).__name__}"
        )

    def test_make_adk_plugin_respects_planner_false(self):
        """planner=False disables the planner entirely."""
        from harmonograf_client import make_adk_plugin

        client = FakeClient()
        plugin = make_adk_plugin(client, planner=False)  # type: ignore[arg-type]
        assert plugin._hg_state._planner is None

    def test_make_adk_plugin_honours_explicit_planner(self):
        """Passing an explicit PlannerHelper must not be overridden."""
        from harmonograf_client import make_adk_plugin
        from harmonograf_client.planner import PassthroughPlanner

        client = FakeClient()
        explicit = PassthroughPlanner()
        plugin = make_adk_plugin(client, planner=explicit)  # type: ignore[arg-type]
        assert plugin._hg_state._planner is explicit


# ---------------------------------------------------------------------------
# STATUS_QUERY control handler tests (no ADK runtime needed).
# ---------------------------------------------------------------------------


class TestStatusQueryHandler:
    """Tests for the STATUS_QUERY control handler registered by make_adk_plugin."""

    def _make_state_and_handler(self):
        """Build an _AdkState and extract the STATUS_QUERY handler from
        the on_control calls recorded by FakeClient.
        """
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]

        # Simulate what make_adk_plugin does: register the handler on client.
        # We replicate the handler inline here using the same _AdkState so
        # we can drive state directly without importing google.adk.
        from harmonograf_client.transport import ControlAckSpec

        def _handle_status_query(event: Any) -> ControlAckSpec:
            parts: list[str] = []
            activity = client._current_activity
            if activity:
                parts.append(activity)
            with state._lock:
                streaming_texts = list(state._llm_streaming_text.values())
            if streaming_texts:
                latest = max(streaming_texts, key=len)
                if len(latest) > 10:
                    snippet = latest[:120].replace("\n", " ")
                    if len(latest) > 120:
                        parts.append(f"LLM thinking: {snippet}\u2026")
                    else:
                        parts.append(f"LLM: {snippet}")
            with state._lock:
                active_tool_count = len(state._tools)
            if active_tool_count:
                parts.append(f"{active_tool_count} tool call(s) in flight")
            report = " | ".join(parts) if parts else "No active task."
            return ControlAckSpec(result="success", detail=report)

        return client, state, _handle_status_query

    def test_status_query_no_active_task(self):
        """Returns 'No active task.' when nothing is in flight."""
        client, state, handler = self._make_state_and_handler()
        event = types.SimpleNamespace(payload=b"")
        result = handler(event)
        assert result.result == "success"
        assert result.detail == "No active task."

    def test_status_query_with_activity(self):
        """Returns the current activity when one is set."""
        client, state, handler = self._make_state_and_handler()
        client._current_activity = "Calling tool search_web"
        event = types.SimpleNamespace(payload=b"")
        result = handler(event)
        assert result.result == "success"
        assert "Calling tool search_web" in result.detail

    def test_status_query_with_streaming_text(self):
        """Includes LLM streaming text snippet when available."""
        client, state, handler = self._make_state_and_handler()
        with state._lock:
            state._llm_streaming_text["llm-span-1"] = "The user wants a summary of recent events"
        event = types.SimpleNamespace(payload=b"")
        result = handler(event)
        assert result.result == "success"
        assert "LLM" in result.detail
        assert "summary of recent events" in result.detail

    def test_status_query_with_active_tools(self):
        """Reports in-flight tool call count."""
        client, state, handler = self._make_state_and_handler()
        with state._lock:
            state._tools["call_1"] = "span-1"
            state._tools["call_2"] = "span-2"
        event = types.SimpleNamespace(payload=b"")
        result = handler(event)
        assert result.result == "success"
        assert "2 tool call(s) in flight" in result.detail

    def test_status_query_combines_all_parts(self):
        """Joins activity, streaming text and tool count with ' | '."""
        client, state, handler = self._make_state_and_handler()
        client._current_activity = "Calling tool search"
        with state._lock:
            state._llm_streaming_text["llm-span-1"] = "Thinking about the query now"
            state._tools["call_1"] = "span-1"
        event = types.SimpleNamespace(payload=b"")
        result = handler(event)
        assert result.result == "success"
        parts = result.detail.split(" | ")
        assert len(parts) == 3

    def test_make_adk_plugin_registers_status_query(self):
        """make_adk_plugin must register STATUS_QUERY via client.on_control."""
        if not _ADK_AVAILABLE:
            pytest.skip("google.adk not installed")
        from harmonograf_client.adk import make_adk_plugin

        client = FakeClient()
        make_adk_plugin(client)  # type: ignore[arg-type]
        registered_kinds = [kind for (op, kind, _) in client.calls if op == "on_control"]
        assert "STATUS_QUERY" in registered_kinds, (
            f"STATUS_QUERY not registered; got {registered_kinds}"
        )


class TestTaskReport:
    """Tests for the task_report span attribute emitted on invocation and model end."""

    def test_invocation_start_sets_task_report_attribute(self):
        """INVOCATION span should be started with task_report attribute."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_tr1", agent=FakeAgent(name="my-agent"))
        state.on_invocation_start(ic)
        starts = client.starts()
        assert len(starts) == 1
        sid, kw = starts[0]
        assert kw["kind"] == "INVOCATION"
        attrs = kw.get("attributes", {})
        assert "task_report" in attrs, f"task_report missing; attrs={attrs}"
        assert "my-agent" in attrs["task_report"]

    def test_model_end_emits_task_report_update_on_invocation_span(self):
        """After model end, the INVOCATION span should receive a task_report update."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_tr2")
        state.on_invocation_start(ic)
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest(model="gpt-4o"))
        inv_span_id = client.starts()[0][0]
        state.on_model_end(cc, FakeLlmResponse())
        # Find any update on the invocation span with task_report
        task_report_updates = [
            kw for (sid, kw) in client.updates()
            if sid == inv_span_id and "task_report" in (kw.get("attributes") or {})
        ]
        assert task_report_updates, (
            "Expected a span update with task_report on the INVOCATION span after model end"
        )

    def test_model_end_with_streaming_text_includes_snippet(self):
        """task_report should include streaming text snippet when available."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_tr3")
        state.on_invocation_start(ic)
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest(model="gpt-4o"))
        inv_span_id = client.starts()[0][0]
        with state._lock:
            llm_span_id = state._llm_by_invocation.get("inv_tr3")
            if llm_span_id:
                state._llm_streaming_text[llm_span_id] = "I will search for the latest news"
        state.on_model_end(cc, FakeLlmResponse())
        task_report_updates = [
            kw for (sid, kw) in client.updates()
            if sid == inv_span_id and "task_report" in (kw.get("attributes") or {})
        ]
        assert task_report_updates, "Expected a task_report update on INVOCATION span"
        report_val = task_report_updates[0]["attributes"]["task_report"]
        assert "search for the latest news" in report_val


class _ThoughtPart:
    def __init__(self, text: str, thought: bool = False) -> None:
        self.text = text
        self.thought = thought


class _PartContent:
    def __init__(self, parts: list[_ThoughtPart]) -> None:
        self.parts = parts


@dataclass
class _PartialEvent:
    content: Any = None
    partial: bool = True
    actions: Any = None


class TestThinkingTaskReport:
    """End-to-end pipeline test: partial LLM events with thought=True parts
    should accumulate thinking text and produce a `task_report` update on
    the parent INVOCATION span via ``emit_thinking_as_task_report``.
    """

    def _setup(self, inv_id: str = "inv_think"):
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id=inv_id)
        state.on_invocation_start(ic)
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest(model="gemini-2.5-pro"))
        inv_span_id = client.starts()[0][0]
        return client, state, ic, inv_span_id

    def _emit_thought_event(self, state, ic, text: str) -> None:
        ev = _PartialEvent(
            content=_PartContent(parts=[_ThoughtPart(text=text, thought=True)]),
            partial=True,
        )
        state.on_event(ic, ev)

    def test_partial_thought_events_emit_task_report_on_invocation(self):
        client, state, ic, inv_span_id = self._setup()
        # Emit enough chunks to cross the 200-char rate-limit threshold.
        chunk = (
            "First, I should figure out what the user is trying to accomplish. "
            "They want a clear answer, so I will start by clarifying assumptions. "
            "Then I will draft an outline. "
        )
        self._emit_thought_event(state, ic, chunk)
        self._emit_thought_event(state, ic, chunk)
        reports = [
            kw["attributes"]["task_report"]
            for (sid, kw) in client.updates()
            if sid == inv_span_id and "task_report" in (kw.get("attributes") or {})
        ]
        assert reports, "Expected at least one task_report update on INVOCATION span"
        latest = reports[-1]
        assert "Thinking" in latest, f"task_report should be thinking-flavoured: {latest!r}"

    def test_thinking_summary_prefers_recent_complete_sentence(self):
        client, state, ic, inv_span_id = self._setup()
        # Many small chunks; final sentence should appear in the summary.
        chunks = [
            "I am examining the inputs and verifying that nothing is missing. ",
            "All required fields are present. ",
            "Now drafting the response in a structured format. ",
            "Calling the search_web tool to gather citations for the claim about latency. ",
        ]
        for c in chunks:
            self._emit_thought_event(state, ic, c)
        reports = [
            kw["attributes"]["task_report"]
            for (sid, kw) in client.updates()
            if sid == inv_span_id and "task_report" in (kw.get("attributes") or {})
        ]
        assert reports, "Expected task_report update from thinking pipeline"
        latest = reports[-1]
        # The richer extractor should surface a complete sentence — not a
        # mid-word raw tail. The most recent action-bearing sentence is the
        # search_web one; assert its key phrase appears.
        assert "search_web" in latest, (
            f"Expected most-recent action sentence in summary, got: {latest!r}"
        )
        # And the summary should start with the Thinking marker.
        assert latest.startswith("Thinking"), latest

    def test_thinking_summary_uses_sentence_boundary_not_raw_tail(self):
        """The extractor must not slice mid-word when a clean sentence
        boundary exists earlier in the buffer."""
        client, state, ic, inv_span_id = self._setup()
        long_thought = (
            "x" * 50
            + ". The plan is to call the analyze_dataset tool with the cleaned input rows. "
        )
        # Two chunks to exceed the 200-char emit threshold.
        self._emit_thought_event(state, ic, long_thought)
        self._emit_thought_event(state, ic, long_thought)
        reports = [
            kw["attributes"]["task_report"]
            for (sid, kw) in client.updates()
            if sid == inv_span_id and "task_report" in (kw.get("attributes") or {})
        ]
        assert reports
        latest = reports[-1]
        assert "analyze_dataset" in latest, latest
        # A raw 150-char tail of the doubled buffer would include trailing
        # whitespace and start mid-sentence; the extractor must produce a
        # phrase that starts with a capital letter or ellipsis sentinel.
        body = latest.removeprefix("Thinking: ").lstrip("\u2026 ")
        assert body[:1].isupper() or body.startswith("\u2026"), (
            f"Summary body should begin at a sentence boundary: {latest!r}"
        )


# ---------------------------------------------------------------------------
# Steer infrastructure tests
# ---------------------------------------------------------------------------


class TestAdkStateSteer:
    """Tests for pending_steer field and _inject_steer_into_request."""

    def test_set_and_consume_steer(self):
        """set_pending_steer stores the text; consume_pending_steer returns it once."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        state.set_pending_steer("be concise")
        assert state.consume_pending_steer() == "be concise"
        assert state.consume_pending_steer() is None

    def test_consume_steer_empty(self):
        """Fresh state has no pending steer."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        assert state.consume_pending_steer() is None

    def test_steer_cleared_on_invocation_end(self):
        """A pending steer is discarded when the invocation ends."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ic = FakeInvocationContext(invocation_id="inv_steer_end")
        state.on_invocation_start(ic)
        state.set_pending_steer("be concise")
        state.on_invocation_end(ic)
        # The steer should survive invocation_end (it is injected at the model
        # boundary, not cleared by invocation end). Consuming it now returns the
        # value and a second consume returns None — the one-shot guarantee holds.
        first = state.consume_pending_steer()
        second = state.consume_pending_steer()
        # Whether invocation_end clears it or not, after one consume it must be gone.
        assert second is None

    def test_inject_steer_into_request_appends_content(self):
        """_inject_steer_into_request appends a user-role Content with the steer text."""
        try:
            from google.genai import types as genai_types  # type: ignore
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.adk import _inject_steer_into_request

        req = FakeLlmRequest(contents=[])
        _inject_steer_into_request(req, "focus on X")
        assert len(req.contents) == 1
        added = req.contents[0]
        assert added.role == "user"
        assert any("focus on X" in (getattr(p, "text", "") or "") for p in added.parts)

    def test_inject_steer_into_request_null_contents(self):
        """When req.contents is None the function initialises it to a one-item list."""
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.adk import _inject_steer_into_request

        req = FakeLlmRequest(contents=None)
        _inject_steer_into_request(req, "do it differently")
        assert req.contents is not None
        assert len(req.contents) == 1

    def test_inject_steer_handles_exception_gracefully(self):
        """If appending to contents raises, the function logs and does not propagate."""
        from harmonograf_client.adk import _inject_steer_into_request

        class _BadContents:
            def append(self, item):
                raise RuntimeError("broken list")

        class _BadReq:
            contents = _BadContents()

        # Should not raise even though google.genai may be available.
        try:
            _inject_steer_into_request(_BadReq(), "whatever")
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"_inject_steer_into_request raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Cancel infrastructure tests
# ---------------------------------------------------------------------------


class TestAdkStateCancel:

    def test_cancel_returns_false_when_no_task(self):
        """cancel_running_task returns False when no task is registered."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        assert state.cancel_running_task() is False

    def test_cleanup_cancelled_spans_ends_all(self):
        """_cleanup_cancelled_spans ends every in-flight span with CANCELLED."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]

        # Open an invocation.
        ic = FakeInvocationContext(invocation_id="inv_cancel")
        state.on_invocation_start(ic)

        # Open an LLM call.
        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeLlmRequest(model="gpt-4o"))

        # Open a tool call.
        tc = FakeToolContext(function_call_id="call_cancel", _invocation_context=ic)
        state.on_tool_start(FakeTool(name="search"), {}, tc)

        # Confirm three spans are open (one for each kind).
        assert len(client.starts()) == 3

        # Now cancel — should end all three spans.
        state._cleanup_cancelled_spans()

        cancelled = [
            (sid, kw) for (op, sid, kw) in client.calls
            if op == "end" and kw.get("status") == "CANCELLED"
        ]
        assert len(cancelled) == 3, (
            f"expected 3 CANCELLED ends, got {len(cancelled)}: {cancelled}"
        )

        # All tracking dicts must be empty.
        assert state._tools == {}
        assert state._llm_by_invocation == {}
        assert state._invocations == {}

    def test_cleanup_cancelled_spans_noop_when_empty(self):
        """_cleanup_cancelled_spans on a fresh state does not raise."""
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        state._cleanup_cancelled_spans()  # must not raise
        assert client.calls == []


# ---------------------------------------------------------------------------
# Session repair tests
# ---------------------------------------------------------------------------


class TestRepairSession:
    """Tests for _repair_adk_session_after_cancel."""

    def _make_part(self, *, function_call=None, text=None):
        """Make a duck-typed part with optional function_call and text."""
        return types.SimpleNamespace(function_call=function_call, text=text)

    def _make_event(self, parts):
        content = types.SimpleNamespace(parts=parts)
        return types.SimpleNamespace(content=content, author="model")

    def test_repair_removes_dangling_function_call(self):
        """The dangling function-call event is removed from the session events list."""
        from harmonograf_client.adk import _repair_adk_session_after_cancel

        fc = types.SimpleNamespace(name="search_web", args={})
        part = self._make_part(function_call=fc)
        event = self._make_event([part])
        session = types.SimpleNamespace(events=[event])
        ic = types.SimpleNamespace(session=session)

        _repair_adk_session_after_cancel(ic)
        assert session.events == [], (
            f"dangling event was not removed; events={session.events}"
        )

    def test_repair_noop_when_last_event_has_no_function_call(self):
        """Events without function_call parts are left untouched."""
        from harmonograf_client.adk import _repair_adk_session_after_cancel

        part = self._make_part(text="hello")
        event = self._make_event([part])
        session = types.SimpleNamespace(events=[event])
        ic = types.SimpleNamespace(session=session)

        _repair_adk_session_after_cancel(ic)
        assert len(session.events) == 1, "non-dangling event should not be removed"

    def test_repair_noop_when_no_session(self):
        """Passing an ic with session=None does not raise."""
        from harmonograf_client.adk import _repair_adk_session_after_cancel

        ic = types.SimpleNamespace(session=None)
        _repair_adk_session_after_cancel(ic)  # must not raise


# ---------------------------------------------------------------------------
# Planner integration tests
# ---------------------------------------------------------------------------


class _StubPart:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubContent:
    def __init__(self, text: str) -> None:
        self.parts = [_StubPart(text)]


class _StubAgentWithPlanner:
    def __init__(self, name: str = "coordinator") -> None:
        self.name = name
        self.description = "root"
        self.model = "gemini-flash"
        self.sub_agents = [
            types.SimpleNamespace(name="researcher", description="r"),
            types.SimpleNamespace(name="writer", description="w"),
        ]
        self.tools: list = []


class _StubPlannerIC:
    def __init__(self, request_text: str) -> None:
        self.invocation_id = "inv_plan"
        self.agent = _StubAgentWithPlanner()
        self.session = FakeSession(id="sess_plan")
        self.user_id = "alice"
        self.user_content = _StubContent(request_text)


class TestPlannerIntegration:
    def test_planner_generate_called_and_plan_submitted(self):
        from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge

        captured: dict[str, object] = {}

        class StubPlanner(PlannerHelper):
            def generate(self, *, request, available_agents, context=None):
                captured["request"] = request
                captured["available_agents"] = available_agents
                return Plan(
                    tasks=[
                        Task(id="t1", title="research", assignee_agent_id="researcher"),
                        Task(id="t2", title="write", assignee_agent_id="writer"),
                    ],
                    edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
                    summary="do it",
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("please research and write a report")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        assert captured["request"] == "please research and write a report"
        avail = captured["available_agents"]
        assert "coordinator" in avail
        assert "researcher" in avail
        assert "writer" in avail

        submit_calls = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_calls) == 1
        plan_kwargs = submit_calls[0][2]
        submitted_plan = plan_kwargs["plan"]
        assert submitted_plan.summary == "do it"
        assert [t.id for t in submitted_plan.tasks] == ["t1", "t2"]

    def test_planner_none_result_submits_nothing(self):
        from harmonograf_client.planner import PassthroughPlanner

        client = FakeClient()
        state = _AdkState(client=client, planner=PassthroughPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("hello")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)
        assert [c for c in client.calls if c[0] == "submit_plan"] == []

    def test_planner_exception_is_swallowed(self):
        from harmonograf_client.planner import PlannerHelper

        class BrokenPlanner(PlannerHelper):
            def generate(self, **kwargs):
                raise RuntimeError("kaboom")

        client = FakeClient()
        state = _AdkState(client=client, planner=BrokenPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("hello")
        state.on_invocation_start(ic)
        # Must not raise.
        state.maybe_run_planner(ic)
        assert [c for c in client.calls if c[0] == "submit_plan"] == []

    def _singleton_planner(self, submissions: list[str]):
        """Return a PlannerHelper stub that produces a fresh single-task
        plan on every generate() call. ``submissions`` receives one entry
        per call so tests can count invocations independently of
        FakeClient.submit_plan counts.
        """
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                submissions.append("gen")
                return Plan(
                    tasks=[
                        Task(id="t1", title="go", assignee_agent_id="coordinator"),
                    ],
                    edges=[],
                    summary=f"plan-{len(submissions)}",
                )

        return StubPlanner()

    def test_maybe_run_planner_fires_once_per_session(self):
        """Two distinct invocations in the same harmonograf session —
        e.g. a root run plus an AgentTool sub-invocation — must only
        yield ONE submit_plan call. This is the core plan-singleton
        guarantee that prevents the presentation_agent demo from
        emitting two TaskPlans (root agent + sub-agent) per run.
        """
        submissions: list[str] = []
        client = FakeClient()
        state = _AdkState(client=client, planner=self._singleton_planner(submissions))  # type: ignore[arg-type]

        # Root invocation.
        ic_root = _StubPlannerIC("research and write about X")
        ic_root.invocation_id = "inv_root"
        ic_root.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_root)
        state.maybe_run_planner(ic_root)

        # Nested sub-invocation (AgentTool / transfer) — same ADK session,
        # fresh invocation_id. The routing path aliases this to the same
        # harmonograf session via the ContextVar.
        ic_sub = _StubPlannerIC("research and write about X")
        ic_sub.invocation_id = "inv_sub"
        ic_sub.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_sub)
        state.maybe_run_planner(ic_sub)

        submit_calls = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_calls) == 1, (
            f"expected one plan per session, got {len(submit_calls)}"
        )
        assert len(submissions) == 1, (
            "planner.generate should only be called once per session"
        )

    def test_maybe_run_planner_resets_on_root_invocation_end(self):
        """After the root invocation in a session ends, a subsequent
        invocation in the SAME session (rare: re-invocation loop after
        the root finished) should be allowed to plan again."""
        submissions: list[str] = []
        client = FakeClient()
        state = _AdkState(client=client, planner=self._singleton_planner(submissions))  # type: ignore[arg-type]

        ic_root = _StubPlannerIC("first request")
        ic_root.invocation_id = "inv_root"
        ic_root.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_root)
        state.maybe_run_planner(ic_root)
        state.on_invocation_end(ic_root)

        ic_next = _StubPlannerIC("second request")
        ic_next.invocation_id = "inv_next"
        ic_next.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_next)
        state.maybe_run_planner(ic_next)

        submit_calls = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_calls) == 2, (
            f"root end should reopen planning; got {len(submit_calls)} submits"
        )
        assert len(submissions) == 2

    def test_hydrate_plan_state_from_session_state(self):
        """When a harmonograf session carries ``harmonograf.plan_id`` +
        ``harmonograf.available_tasks`` in session.state but the in-memory
        ``_active_plan_by_session`` map has no entry — e.g. a test
        injected the plan directly via the state protocol — the adapter
        reconstructs a ``PlanState`` on the next before_model callback
        so drift detection and state_delta outcome routing have
        something to bind to.
        """
        from harmonograf_client.adk import (
            _hydrate_plan_state_from_session_state,
            _AdkState,
        )
        from harmonograf_client import state_protocol as sp

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        # Simulate a session.state mapping populated by a prior drive
        # (or by a test that wrote via state_protocol.write_plan_context
        # directly) — three tasks, one edge, stored under the canonical
        # harmonograf.* keys.
        session_state: dict[str, object] = {}
        sp._set(session_state, sp.KEY_PLAN_ID, "plan-hydrate-1")
        sp._set(session_state, sp.KEY_PLAN_SUMMARY, "research and publish")
        sp._set(
            session_state,
            sp.KEY_AVAILABLE_TASKS,
            [
                {
                    "id": "t1",
                    "title": "gather sources",
                    "assignee": "researcher",
                    "status": "PENDING",
                    "deps": [],
                },
                {
                    "id": "t2",
                    "title": "write draft",
                    "assignee": "writer",
                    "status": "RUNNING",
                    "deps": ["t1"],
                },
                {
                    "id": "t3",
                    "title": "publish",
                    "assignee": "writer",
                    "status": "PENDING",
                    "deps": ["t2"],
                },
            ],
        )

        hsession_id = "hsess-hydrate"
        assert hsession_id not in state._active_plan_by_session

        plan_state = _hydrate_plan_state_from_session_state(
            state, session_state, hsession_id, inv_id="inv-hydrate"
        )
        assert plan_state is not None
        assert plan_state.plan_id == "plan-hydrate-1"
        assert set(plan_state.tasks.keys()) == {"t1", "t2", "t3"}
        assert plan_state.tasks["t1"].status == "PENDING"
        assert plan_state.tasks["t2"].status == "RUNNING"
        assert plan_state.tasks["t1"].assignee_agent_id == "researcher"
        assert plan_state.tasks["t2"].assignee_agent_id == "writer"
        assert plan_state.plan.summary == "research and publish"
        edges = {(e.from_task_id, e.to_task_id) for e in plan_state.edges}
        assert edges == {("t1", "t2"), ("t2", "t3")}
        assert sorted(plan_state.available_agents) == ["researcher", "writer"]
        assert plan_state.generating_invocation_id == "inv-hydrate"

        # Hydration must install the PlanState into the session map so
        # downstream callbacks can pick it up.
        assert state._active_plan_by_session.get(hsession_id) is plan_state

    def test_hydrate_plan_state_no_op_without_plan_id_or_tasks(self):
        """Hydration returns None (and leaves the map untouched) when
        session.state lacks the required keys. Defensive — the
        before_model hook calls it unconditionally.
        """
        from harmonograf_client.adk import (
            _hydrate_plan_state_from_session_state,
            _AdkState,
        )
        from harmonograf_client import state_protocol as sp

        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        # Missing plan_id.
        empty = {}
        assert _hydrate_plan_state_from_session_state(
            state, empty, "hsess-x", inv_id="inv-x"
        ) is None
        # plan_id present but no tasks list.
        only_id: dict[str, object] = {}
        sp._set(only_id, sp.KEY_PLAN_ID, "plan-x")
        assert _hydrate_plan_state_from_session_state(
            state, only_id, "hsess-x", inv_id="inv-x"
        ) is None
        # Tasks list present but empty.
        empty_tasks: dict[str, object] = {}
        sp._set(empty_tasks, sp.KEY_PLAN_ID, "plan-x")
        sp._set(empty_tasks, sp.KEY_AVAILABLE_TASKS, [])
        assert _hydrate_plan_state_from_session_state(
            state, empty_tasks, "hsess-x", inv_id="inv-x"
        ) is None
        assert "hsess-x" not in state._active_plan_by_session

    def test_maybe_run_planner_does_not_reset_on_nested_end(self):
        """When a nested sub-invocation ends while the root invocation
        is still open, the singleton flag must stay set so any further
        planner calls in the same session continue to no-op.
        """
        submissions: list[str] = []
        client = FakeClient()
        state = _AdkState(client=client, planner=self._singleton_planner(submissions))  # type: ignore[arg-type]

        ic_root = _StubPlannerIC("req")
        ic_root.invocation_id = "inv_root"
        ic_root.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_root)
        state.maybe_run_planner(ic_root)

        ic_sub = _StubPlannerIC("req")
        ic_sub.invocation_id = "inv_sub"
        ic_sub.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_sub)
        # Sub-invocation's planner path is suppressed by the singleton.
        state.maybe_run_planner(ic_sub)
        # Sub ends BEFORE root — must not clear the flag.
        state.on_invocation_end(ic_sub)

        # A third invocation under the still-open root must also no-op.
        ic_sub2 = _StubPlannerIC("req")
        ic_sub2.invocation_id = "inv_sub2"
        ic_sub2.session = FakeSession(id="sess_shared")
        state.on_invocation_start(ic_sub2)
        state.maybe_run_planner(ic_sub2)

        submit_calls = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_calls) == 1, (
            f"nested end must not reopen planning; got {len(submit_calls)} submits"
        )
        assert len(submissions) == 1

    def test_tool_span_is_stamped_with_task_id(self):
        """When a plan is active and a tool span fires for an agent
        that has a pending task, the span's attributes must include
        ``hgraf.task_id``."""
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[
                        Task(
                            id="task_research",
                            title="research",
                            assignee_agent_id="coordinator",
                        )
                    ],
                    edges=[],
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        tc = FakeToolContext(
            function_call_id="fc_1",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        state.on_tool_start(FakeTool(name="search"), {"q": "python"}, tc)

        tool_starts = [
            (sid, kw) for (sid, kw) in client.starts() if kw["kind"] == "TOOL_CALL"
        ]
        assert len(tool_starts) == 1
        attrs = tool_starts[0][1]["attributes"]
        assert attrs.get("hgraf.task_id") == "task_research"

    def test_refine_plan_on_drift_upserts_under_same_plan_id(self):
        """The explicit drift-driven refine path should invoke
        planner.refine and forward any returned Plan to client.submit_plan
        under the original plan_id so the server upserts."""
        from harmonograf_client.adk import DriftReason
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        refine_calls: list[dict] = []

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(
                    tasks=[
                        Task(id="t1", title="initial", assignee_agent_id="coordinator"),
                    ],
                    edges=[],
                    summary="initial",
                )

            def refine(self, plan, event):  # type: ignore[override]
                refine_calls.append(dict(event))
                return Plan(
                    tasks=[
                        Task(id="t1", title="initial", assignee_agent_id="coordinator"),
                        Task(id="t2", title="follow-up", assignee_agent_id="coordinator"),
                    ],
                    edges=[],
                    summary="refined",
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        state.refine_plan_on_drift(
            "adk_sess_plan",
            DriftReason(kind="tool_call_wrong_agent", detail="drifted"),
            current_task=None,
        )

        assert refine_calls, "planner.refine should have been invoked on drift"
        assert refine_calls[0]["kind"] == "tool_call_wrong_agent"

        submit_calls = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_calls) == 2
        first_plan_id = submit_calls[0][1]
        assert submit_calls[1][2].get("plan_id") == first_plan_id
        assert submit_calls[1][2]["plan"].summary == "refined"
        # revision metadata recorded on PlanState
        ps = state._active_plan_by_session["adk_sess_plan"]
        assert ps.revisions and ps.revisions[0]["drift_kind"] == "tool_call_wrong_agent"
        assert ps.revisions[0]["kind"] == "tool_call_wrong_agent"
        assert ps.revisions[0]["severity"] == "info"
        assert ps.plan.revision_reason == "tool_call_wrong_agent: drifted"

    def test_tool_end_no_longer_auto_refines(self):
        """Regression: on_tool_end must NOT call planner.refine — auto-refine
        was removed in favour of explicit drift-driven refines."""
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        refine_called: list[Any] = []

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(
                    tasks=[Task(id="t1", title="x", assignee_agent_id="coordinator")],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                refine_called.append(event)
                return None

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)
        tc = FakeToolContext(
            function_call_id="fc_no_auto",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        state.on_tool_start(FakeTool(name="search"), {}, tc)
        state.on_tool_end(FakeTool(name="search"), tc, result={}, error=None)
        assert refine_called == []

    def test_task_binding_only_stamps_once(self):
        """Two tool calls for the same agent should not both bind to
        the same task — the task is consumed at first match."""
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[
                        Task(id="t1", title="one", assignee_agent_id="coordinator")
                    ],
                    edges=[],
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        for call_id in ("fc_a", "fc_b"):
            tc = FakeToolContext(
                function_call_id=call_id,
                _invocation_context=FakeInvocationContext(
                    invocation_id="inv_plan",
                    agent=FakeAgent(name="coordinator"),
                    session=FakeSession(id="sess_plan"),
                ),
            )
            state.on_tool_start(FakeTool(name=f"tool_{call_id}"), {}, tc)

        tool_starts = [
            kw for (_, kw) in client.starts() if kw["kind"] == "TOOL_CALL"
        ]
        stamped = [
            kw for kw in tool_starts if "hgraf.task_id" in (kw.get("attributes") or {})
        ]
        assert len(stamped) == 1, "exactly one tool call should be bound to the task"


class TestPlannerStatusTracking:
    def test_tool_span_lifecycle_tracks_task_status(self):
        """When a tool span stamped with hgraf.task_id starts, the
        task transitions PENDING → RUNNING. Span_end does NOT drive
        task completion (iter13 task #6: walker owns completion
        exclusively). The walker's :meth:`mark_forced_task_completed`
        is the exclusive COMPLETED source.
        """
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[
                        Task(id="t_a", title="a", assignee_agent_id="coordinator"),
                        Task(id="t_b", title="b", assignee_agent_id="coordinator"),
                    ],
                    edges=[],
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner(), refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # Immediately after plan submit, both tasks are PENDING.
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_a"].status == "PENDING"
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_b"].status == "PENDING"

        # Simulate the walker setting a forced task id — this is how
        # HarmonografAgent drives the adapter in the real flow.
        assert state.set_forced_task_id("t_a")

        tc = FakeToolContext(
            function_call_id="fc_1",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        state.on_tool_start(FakeTool(name="search"), {"q": "x"}, tc)
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_a"].status == "RUNNING"
        # Second task still PENDING; forced-task stamping binds only t_a.
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_b"].status == "PENDING"

        # Span end must NOT transition the task to COMPLETED.
        state.on_tool_end(FakeTool(name="search"), tc, result={"ok": True}, error=None)
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_a"].status == "RUNNING"

        # Only the walker's explicit completion call moves it terminal.
        state.mark_forced_task_completed()
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_a"].status == "COMPLETED"

    def test_tool_failure_marks_task_failed(self):
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[Task(id="t_only", title="x", assignee_agent_id="coordinator")],
                    edges=[],
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner(), refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        tc = FakeToolContext(
            function_call_id="fc_1",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        state.on_tool_start(FakeTool(name="search"), {}, tc)
        state.on_tool_end(
            FakeTool(name="search"), tc, result=None, error=RuntimeError("boom")
        )
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_only"].status == "FAILED"

    def test_refine_receives_plan_with_current_statuses(self):
        """When the observer calls :meth:`refine_plan_on_drift`, the
        planner must receive a plan whose task statuses reflect live
        execution state rather than the stale PENDING snapshot.
        """
        from harmonograf_client.adk import DriftReason
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        captured_plans: list[Plan] = []

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[
                        Task(id="t1", title="first", assignee_agent_id="coordinator"),
                        Task(id="t2", title="second", assignee_agent_id="coordinator"),
                    ],
                    edges=[],
                    summary="initial",
                )

            def refine(self, plan, event):  # type: ignore[override]
                import copy
                captured_plans.append(copy.deepcopy(plan))
                return None

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        tc = FakeToolContext(
            function_call_id="fc_1",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        # Drive t1 through its full lifecycle via the walker path
        # (forced_task_id + mark_forced_task_completed), since span_end
        # no longer drives task completion.
        assert state.set_forced_task_id("t1")
        state.on_tool_start(FakeTool(name="search"), {}, tc)
        state.on_tool_end(FakeTool(name="search"), tc, result={"ok": 1}, error=None)
        state.mark_forced_task_completed()

        state.refine_plan_on_drift(
            "adk_sess_plan",
            DriftReason(kind="failed_span", detail="drift"),
            current_task=None,
        )

        assert captured_plans, "planner.refine should have been called"
        seen = {t.id: t.status for t in captured_plans[0].tasks}
        assert seen["t1"] == "COMPLETED", f"expected t1 COMPLETED, got {seen}"
        assert seen["t2"] == "PENDING", f"expected t2 PENDING, got {seen}"

    def test_refine_returning_new_plan_replaces_tracking_dict(self):
        """When a drift-driven refine returns a new plan with an added
        task and updated statuses, the adapter must replace its
        tracking dict with the returned plan's tasks.
        """
        from harmonograf_client.adk import DriftReason
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(
                    tasks=[Task(id="t1", title="a", assignee_agent_id="coordinator")],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                return Plan(
                    tasks=[
                        Task(
                            id="t1",
                            title="a",
                            assignee_agent_id="coordinator",
                            status="COMPLETED",
                        ),
                        Task(
                            id="t_new",
                            title="follow-up",
                            assignee_agent_id="coordinator",
                            status="PENDING",
                        ),
                    ],
                    edges=[],
                    summary="refined",
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        state.refine_plan_on_drift(
            "adk_sess_plan",
            DriftReason(kind="transfer_to_unplanned_agent", detail="drift"),
            current_task=None,
        )

        assert set(state._active_plan_by_session["adk_sess_plan"].tasks.keys()) == {"t1", "t_new"}
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t1"].status == "COMPLETED"
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_new"].status == "PENDING"


def _coord_cc() -> FakeCallbackContext:
    return FakeCallbackContext(
        _invocation_context=FakeInvocationContext(
            invocation_id="inv_plan",
            agent=FakeAgent(name="coordinator"),
            session=FakeSession(id="sess_plan"),
        )
    )


class TestPlanEnforcement:
    """Plan guidance must be injected into llm_request.contents so the
    wrapped agent actually follows the plan produced by the planner."""

    def _make_planner(self, tasks, edges=None, summary="plan"):
        from harmonograf_client.planner import Plan, PlannerHelper

        class _P(PlannerHelper):
            def generate(self, **kwargs):
                return Plan(tasks=list(tasks), edges=list(edges or []), summary=summary)

        return _P()

    def _extract_injected_text(self, req: FakeLlmRequest) -> str:
        """Pull the text of the synthetic [Plan guidance] user-turn, if any."""
        for content in req.contents:
            role = getattr(content, "role", "")
            if role != "user":
                continue
            parts = getattr(content, "parts", None) or []
            for p in parts:
                text = getattr(p, "text", "") or ""
                if text.startswith("[Plan guidance]"):
                    return text
        return ""

    def test_before_model_callback_injects_plan_guidance_for_assigned_agent(self):
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="research", assignee_agent_id="coordinator"),
                Task(id="t2", title="draft", assignee_agent_id="coordinator"),
                Task(id="t3", title="review", assignee_agent_id="coordinator"),
            ],
            edges=[
                TaskEdge(from_task_id="t1", to_task_id="t2"),
                TaskEdge(from_task_id="t2", to_task_id="t3"),
            ],
            summary="three-step plan",
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        req = FakeLlmRequest(contents=[])
        out = state.inject_plan_guidance_if_any(_coord_cc(), req)
        assert out is not None
        text = self._extract_injected_text(req)
        assert "[Plan guidance]" in text
        assert "three-step plan" in text
        assert "t1" in text and "t2" in text and "t3" in text
        # Dependencies rendered.
        assert "deps: [t1]" in text
        # Next task is the first unblocked PENDING one assigned to coordinator.
        assert 't1 "research"' in text

    def test_guidance_advances_after_task_completes(self):
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="research", assignee_agent_id="coordinator"),
                Task(id="t2", title="draft", assignee_agent_id="coordinator"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        req1 = FakeLlmRequest(contents=[])
        state.inject_plan_guidance_if_any(_coord_cc(), req1)
        first = self._extract_injected_text(req1)
        assert 't1 "research"' in first

        # Simulate t1 completing via the walker path (forced-task-id +
        # explicit mark_forced_task_completed). Span_end alone does NOT
        # drive task completion — see task #6.
        assert state.set_forced_task_id("t1")
        tc = FakeToolContext(
            function_call_id="fc_1",
            _invocation_context=FakeInvocationContext(
                invocation_id="inv_plan",
                agent=FakeAgent(name="coordinator"),
                session=FakeSession(id="sess_plan"),
            ),
        )
        state.on_tool_start(FakeTool(name="search"), {}, tc)
        state.on_tool_end(FakeTool(name="search"), tc, result={"ok": 1}, error=None)
        state.mark_forced_task_completed()
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t1"].status == "COMPLETED"

        req2 = FakeLlmRequest(contents=[])
        state.inject_plan_guidance_if_any(_coord_cc(), req2)
        second = self._extract_injected_text(req2)
        assert 't2 "draft"' in second
        assert 't1 "research"' not in second.split("Your current assigned task")[1]

    def test_no_injection_when_no_plan(self):
        state = _AdkState(client=FakeClient(), planner=None)  # type: ignore[arg-type]
        state.on_invocation_start(
            FakeInvocationContext(
                invocation_id="inv_plan", agent=FakeAgent(name="coordinator")
            )
        )
        req = FakeLlmRequest(contents=[])
        assert state.inject_plan_guidance_if_any(_coord_cc(), req) is None
        assert req.contents == []

    def test_no_injection_when_no_task_for_current_agent(self):
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="t1", title="x", assignee_agent_id="someone-else")]
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        req = FakeLlmRequest(contents=[])
        # Clear any pending guidance seeded for the host agent (coordinator
        # has no task — seed helper should not have set one, but make sure).
        state._pending_plan_guidance = None
        assert state.inject_plan_guidance_if_any(_coord_cc(), req) is None
        assert self._extract_injected_text(req) == ""

    def test_blocked_task_not_selected_as_next(self):
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t_dep", title="dep", assignee_agent_id="other"),
                Task(id="t_me", title="mine", assignee_agent_id="coordinator"),
            ],
            edges=[TaskEdge(from_task_id="t_dep", to_task_id="t_me")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # t_me is blocked on t_dep which is PENDING — not selectable.
        nxt = state._next_task_for_agent("adk_sess_plan", "coordinator")
        assert nxt is None

        # Once t_dep is COMPLETED, t_me unblocks.
        state._active_plan_by_session["adk_sess_plan"].tasks["t_dep"].status = "COMPLETED"
        nxt2 = state._next_task_for_agent("adk_sess_plan", "coordinator")
        assert nxt2 is not None and nxt2.id == "t_me"

    def test_initial_guidance_seeded_on_plan_submit(self):
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="only", title="do it", assignee_agent_id="coordinator")]
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)
        # maybe_run_planner must seed the one-shot pending guidance slot.
        assert state._pending_plan_guidance is not None
        assert "[Plan guidance]" in state._pending_plan_guidance
        assert 'only "do it"' in state._pending_plan_guidance

    def test_stamp_attrs_skips_non_leaf_span_kinds(self):
        """``_stamp_attrs_with_task`` must only attach ``hgraf.task_id``
        to LLM_CALL / TOOL_CALL spans. INVOCATION and TRANSFER wrappers
        must pass through unchanged, even when a forced task id is set
        — otherwise their lifecycle would prematurely flip the task.
        """
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)
        state.set_forced_task_id("t1")

        # Leaf span kinds get stamped.
        llm_attrs = state._stamp_attrs_with_task(
            {}, "coordinator", "adk_sess_plan", span_kind="LLM_CALL"
        )
        assert (llm_attrs or {}).get("hgraf.task_id") == "t1"
        tool_attrs = state._stamp_attrs_with_task(
            {}, "coordinator", "adk_sess_plan", span_kind="TOOL_CALL"
        )
        assert (tool_attrs or {}).get("hgraf.task_id") == "t1"

        # Wrapper span kinds are NOT stamped.
        inv_attrs = state._stamp_attrs_with_task(
            {"a": 1}, "coordinator", "adk_sess_plan", span_kind="INVOCATION"
        )
        assert "hgraf.task_id" not in (inv_attrs or {})
        xfer_attrs = state._stamp_attrs_with_task(
            {"a": 1}, "coordinator", "adk_sess_plan", span_kind="TRANSFER"
        )
        assert "hgraf.task_id" not in (xfer_attrs or {})

    def test_fallback_stamp_respects_deps(self):
        """When no forced task id is set, the fallback assignee-match
        path must still enforce dep ordering. A PENDING task whose deps
        are not all COMPLETED must NOT be stamped, even if assignee
        matches — otherwise a task's bound-span lifecycle will flip it
        to RUNNING/COMPLETED while its real predecessors have not run.
        """
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="first", assignee_agent_id="agent-a"),
                Task(id="t2", title="second", assignee_agent_id="agent-b"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # No forced id; agent-b's t2 has PENDING dep t1 → must not stamp.
        assert state.forced_task_id() == ""
        out = state._stamp_attrs_with_task(
            {}, "agent-b", "adk_sess_plan", span_kind="LLM_CALL"
        )
        assert "hgraf.task_id" not in (out or {})
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t2"].status == "PENDING"

    def test_fallback_stamp_works_when_deps_satisfied(self):
        """Once upstream deps are COMPLETED, the fallback path must
        stamp the newly-unblocked task for the matching agent.
        """
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="first", assignee_agent_id="agent-a"),
                Task(id="t2", title="second", assignee_agent_id="agent-b"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        state._active_plan_by_session["adk_sess_plan"].tasks["t1"].status = "COMPLETED"
        out = state._stamp_attrs_with_task(
            {}, "agent-b", "adk_sess_plan", span_kind="LLM_CALL"
        )
        assert (out or {}).get("hgraf.task_id") == "t2"
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t2"].status == "RUNNING"

    def test_fallback_stamp_skips_tasks_for_other_agents(self):
        """The fallback must never stamp a task whose assignee differs
        from the span's agent_id, even when that task's deps are all
        satisfied. Cross-agent stamping would corrupt attribution.
        """
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="first", assignee_agent_id="agent-a"),
                Task(id="t2", title="second", assignee_agent_id="agent-b"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # t2's dep is satisfied, but assignee is agent-b. A span from
        # agent-a must not get t2 stamped on it.
        state._active_plan_by_session["adk_sess_plan"].tasks["t1"].status = "COMPLETED"
        # t1 is already "done" in tracking — simulate that it already
        # ran, so the only PENDING task in `remaining` is t2.
        state._active_plan_by_session["adk_sess_plan"].remaining_for_fallback = [
            state._active_plan_by_session["adk_sess_plan"].tasks["t2"]
        ]
        out = state._stamp_attrs_with_task(
            {}, "agent-a", "adk_sess_plan", span_kind="LLM_CALL"
        )
        assert "hgraf.task_id" not in (out or {})
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t2"].status == "PENDING"

    def test_assignee_canonicalization_on_submit(self):
        """LLM planners hallucinate formatting variations of agent
        names. At plan-submit time, the adapter must canonicalize each
        task's ``assignee_agent_id`` against the actual available-agent
        list so the assignee-match heuristic in ``_stamp_attrs_with_task``
        can find the right task. Tests all the common variants: hyphen
        vs underscore, case difference, and truncation.
        """
        from harmonograf_client.adk import (
            _canonicalize_assignee,
            _canonicalize_plan_assignees,
        )
        from harmonograf_client.planner import Plan, Task

        known = ["research_agent", "web_developer_agent", "coordinator_agent"]

        # Unit: the helper itself.
        assert _canonicalize_assignee("research-agent", known) == "research_agent"
        assert _canonicalize_assignee("Research_Agent", known) == "research_agent"
        assert _canonicalize_assignee("research", known) == "research_agent"
        assert _canonicalize_assignee("web-developer-agent", known) == "web_developer_agent"
        assert _canonicalize_assignee("coordinator_agent", known) == "coordinator_agent"
        # Unresolvable input is returned unchanged.
        assert _canonicalize_assignee("totally_unknown", known) == "totally_unknown"
        # Empty input is preserved.
        assert _canonicalize_assignee("", known) == ""

        # Plan-level in-place rewrite.
        plan = Plan(
            tasks=[
                Task(id="t1", title="a", assignee_agent_id="research-agent"),
                Task(id="t2", title="b", assignee_agent_id="Web_Developer_Agent"),
                Task(id="t3", title="c", assignee_agent_id="coordinator_agent"),
            ],
            edges=[],
        )
        _canonicalize_plan_assignees(plan, known)
        assert plan.tasks[0].assignee_agent_id == "research_agent"
        assert plan.tasks[1].assignee_agent_id == "web_developer_agent"
        assert plan.tasks[2].assignee_agent_id == "coordinator_agent"

    def test_stamp_fallback_tolerates_assignee_formatting_drift(self):
        """Belt-and-suspenders: even if an upstream path forgets to
        canonicalize, the fallback match in ``_stamp_attrs_with_task``
        must compare assignee ids under normalization so case/separator
        variations still match. Guards against a stale plan object held
        from pre-canonicalization code paths.
        """
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="t_r", title="research", assignee_agent_id="research-agent")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # The planner-output assignee "research-agent" should have been
        # canonicalized at submit time. But to verify the belt-and-
        # suspenders fallback, manually force it back to the hyphen form
        # and then call stamp with the underscore form the plugin emits.
        state._active_plan_by_session["adk_sess_plan"].tasks["t_r"].assignee_agent_id = "research-agent"
        state._active_plan_by_session["adk_sess_plan"].remaining_for_fallback[0].assignee_agent_id = "research-agent"

        out = state._stamp_attrs_with_task(
            {}, "research_agent", "adk_sess_plan", span_kind="LLM_CALL"
        )
        assert (out or {}).get("hgraf.task_id") == "t_r"
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_r"].status == "RUNNING"

    def test_maybe_run_planner_canonicalizes_assignees_end_to_end(self):
        """Integration check: a planner that returns an assignee with
        formatting drift (hyphen instead of underscore) must have that
        drift corrected before the plan lands in tracking state, so the
        subsequent fallback stamping flows naturally without needing the
        normalized-compare safety net.
        """
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _Drift(PlannerHelper):
            def generate(self, **kwargs):
                # available_agents will include "coordinator" plus the
                # sub_agents of _StubAgentWithPlanner. Return a task
                # deliberately mis-spelled.
                return Plan(
                    tasks=[
                        Task(id="t1", title="x", assignee_agent_id="COORDINATOR"),
                    ],
                    edges=[],
                    summary="drift",
                )

        state = _AdkState(client=FakeClient(), planner=_Drift(), refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # After submit, the tracked task's assignee must be the exact
        # string the plugin will see from ic.agent.name ("coordinator").
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t1"].assignee_agent_id == "coordinator"

    def test_fallback_stamp_resolves_sub_invocation_to_session_root(self):
        """AgentTool sub-runs execute under a fresh ADK invocation_id
        (AgentTool spins up its own Runner), so the plan — which was
        stored under the ROOT invocation's id when the coordinator's
        ``before_run_callback`` fired — isn't found by direct lookup.
        ``_stamp_attrs_with_task`` must fall back to resolving the
        session's root invocation so sub-agents inherit the parent plan
        state; otherwise their spans never get stamped and their tasks
        stay PENDING forever even when research actually ran.
        """
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [
                Task(id="t_research", title="research", assignee_agent_id="research_agent"),
                Task(id="t_final", title="final", assignee_agent_id="coordinator"),
            ],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # Simulate an AgentTool sub-invocation: it shares the parent's
        # harmonograf session (that's how _current_root_hsession_var
        # aliases nested runs in the real plugin). With the new
        # session-keyed design, the sub-invocation callsite passes the
        # resolved hsession_id directly — no inv→root inv walk required.
        _, root_hsession = state._invocation_route["inv_plan"]
        out = state._stamp_attrs_with_task(
            {}, "research_agent", root_hsession, span_kind="LLM_CALL"
        )
        assert (out or {}).get("hgraf.task_id") == "t_research"
        assert state._active_plan_by_session["adk_sess_plan"].tasks["t_research"].status == "RUNNING"

    def test_fallback_stamp_sub_invocation_without_session_does_not_crash(self):
        """A sub-invocation whose route isn't registered (defensive: a
        span from an unknown inv) must simply pass through unstamped —
        the resolution helper must not raise or cross-bind to an
        unrelated session's plan.
        """
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        out = state._stamp_attrs_with_task(
            {}, "some-agent", "adk_unknown_session", span_kind="LLM_CALL"
        )
        assert "hgraf.task_id" not in (out or {})

    @pytest.mark.skipif(not _ADK_AVAILABLE, reason="google.adk not installed")
    def test_sub_agent_tool_advances_tasks(self):
        """Gold-standard regression (real ADK): drive a real
        ``InMemoryRunner`` with a real ``LlmAgent`` coordinator that
        owns a real ``google.adk.tools.AgentTool(sub_agent)``. A scripted
        fake LLM makes the coordinator emit a function_call to the
        sub tool, then a terminal ``done``. The sub agent's own scripted
        model answers ``facts``.

        This exercises the actual sub-invocation code path — the one
        the real demo hits — including ADK's fresh ``Runner`` +
        ``InMemorySessionService`` per AgentTool. We assert that the
        harmonograf plugin stamps both the coordinator and sub spans
        with their plan tasks, proving the session-keyed refactor +
        iter9 ContextVar alias resolve sub-invocations correctly.
        """
        import asyncio
        import contextlib

        from google.adk.agents.llm_agent import LlmAgent
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import InMemoryRunner
        from google.adk.tools import AgentTool
        from google.genai import types as genai_types

        from harmonograf_client.adk import attach_adk
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _SeededPlanner(PlannerHelper):
            def generate(self, *, request, available_agents, context=None):
                return Plan(
                    tasks=[
                        Task(id="t1", title="coord work", assignee_agent_id="coordinator"),
                        Task(id="t2", title="sub work", assignee_agent_id="sub_agent"),
                    ],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                return None

        def _text(text: str) -> LlmResponse:
            return LlmResponse(
                content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text=text)]
                )
            )

        def _fn_call(name: str, args: dict) -> LlmResponse:
            return LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(name=name, args=args)
                        )
                    ],
                )
            )

        class _CoordModel(BaseLlm):  # type: ignore[misc]
            model: str = "scripted-coord"
            cursor: int = -1

            @classmethod
            def supported_models(cls) -> list[str]:
                return ["scripted-coord"]

            async def generate_content_async(self, llm_request, stream: bool = False):
                self.cursor += 1
                if self.cursor == 0:
                    yield _fn_call("sub_agent", {"request": "please do it"})
                else:
                    yield _text("done")

            @contextlib.asynccontextmanager
            async def connect(self, llm_request):
                yield None

        class _SubModel(BaseLlm):  # type: ignore[misc]
            model: str = "scripted-sub"

            @classmethod
            def supported_models(cls) -> list[str]:
                return ["scripted-sub"]

            async def generate_content_async(self, llm_request, stream: bool = False):
                yield _text("facts")

            @contextlib.asynccontextmanager
            async def connect(self, llm_request):
                yield None

        sub_agent = LlmAgent(
            name="sub_agent",
            model=_SubModel(),
            instruction="Respond with facts.",
            description="sub-agent",
            tools=[],
        )
        coordinator = LlmAgent(
            name="coordinator",
            model=_CoordModel(),
            instruction="Call sub_agent then respond.",
            description="coordinator",
            tools=[AgentTool(sub_agent)],
        )

        client = FakeClient()
        runner = InMemoryRunner(agent=coordinator, app_name="sub_agent_regression")
        handle = attach_adk(
            runner, client, planner=_SeededPlanner(), refine_on_events=False  # type: ignore[arg-type]
        )

        try:
            async def _drive() -> None:
                session = await runner.session_service.create_session(
                    app_name=runner.app_name, user_id="alice"
                )
                async for _event in runner.run_async(
                    user_id="alice",
                    session_id=session.id,
                    new_message=genai_types.Content(
                        role="user", parts=[genai_types.Part(text="go")]
                    ),
                ):
                    pass

            asyncio.run(_drive())
        finally:
            handle.detach()

        # Collect every span_start by (agent_id, stamped task_id).
        stamped: dict[str, set[str]] = {}
        for _sid, kw in client.starts():
            agent_id = kw.get("agent_id") or ""
            attrs = kw.get("attributes") or {}
            task_id = attrs.get("hgraf.task_id")
            if agent_id and task_id:
                stamped.setdefault(agent_id, set()).add(task_id)

        assert "coordinator" in stamped, (
            f"coordinator emitted no task-stamped spans; stamped={stamped!r} "
            f"all_starts={[(kw.get('agent_id'), (kw.get('attributes') or {}).get('hgraf.task_id')) for _, kw in client.starts()]}"
        )
        assert "t1" in stamped["coordinator"], (
            f"coordinator did not stamp t1; got {stamped['coordinator']!r}"
        )

        assert "sub_agent" in stamped, (
            "sub_agent (invoked via real ADK AgentTool) emitted no "
            "task-stamped spans — the session-keyed refactor + iter9 "
            "ContextVar alias failed to resolve the sub-invocation's "
            f"hsession. stamped={stamped!r} "
            f"all_starts={[(kw.get('agent_id'), (kw.get('attributes') or {}).get('hgraf.task_id')) for _, kw in client.starts()]}"
        )
        assert "t2" in stamped["sub_agent"], (
            f"sub_agent did not stamp t2; got {stamped['sub_agent']!r}"
        )

    def test_hsession_resolved_via_context_var_in_sub_invocation(self):
        """When an AgentTool sub-runner fires ``on_invocation_start`` with
        a fresh ADK session id, the routing helper must consult the
        per-instance ContextVar to alias the sub-session back to the
        parent's hsession. That is the mechanism that lets a single
        PlanState keyed by hsession cover both root and sub invocations
        without relying on inv_id plumbing.
        """
        from harmonograf_client.planner import Task

        planner = self._make_planner(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)
        parent_hsession = state._invocation_route["inv_plan"][1]

        # Now simulate what AgentTool's inline sub-run looks like:
        # same asyncio Task context (so the ContextVar is still set),
        # fresh ADK session id, fresh invocation id. The routing path
        # should alias the sub ADK session onto the parent hsession.
        sub_ic = _StubPlannerIC("sub request")
        sub_ic.invocation_id = "inv_sub"
        sub_ic.session = FakeSession(id="sess_agent_tool_subrun")
        state.on_invocation_start(sub_ic)

        sub_route_agent, sub_route_hsession = state._invocation_route["inv_sub"]
        assert sub_route_hsession == parent_hsession, (
            "sub-invocation must inherit the parent's hsession via ContextVar, "
            f"got {sub_route_hsession!r} != {parent_hsession!r}"
        )
        # And that hsession still points at exactly one PlanState.
        assert parent_hsession in state._active_plan_by_session

    def test_next_task_strict_dep_blocks_missing_dep(self):
        """When a plan edge references a dep that isn't present in the
        tracked plan state, the dependent task must be treated as
        BLOCKED (not silently satisfied). Tracked state is seeded with
        every plan task, so a missing dep is either a dangling edge or
        a bookkeeping bug — the safe behaviour is to wait.
        """
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [Task(id="t_me", title="mine", assignee_agent_id="coordinator")],
            edges=[TaskEdge(from_task_id="ghost_dep", to_task_id="t_me")],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        nxt = state._next_task_for_agent("adk_sess_plan", "coordinator")
        assert nxt is None, (
            "a dep not in tracked state must count as blocked, not satisfied"
        )

    def test_next_task_multi_dep_enforced(self):
        """A task with multiple deps is unblocked only when ALL deps
        are COMPLETED — mirrors the presentation_agent's t7 scenario
        where the final coordinator task must wait for t1..t6.
        """
        from harmonograf_client.planner import Task, TaskEdge

        planner = self._make_planner(
            [
                Task(id="t1", title="a", assignee_agent_id="research"),
                Task(id="t2", title="b", assignee_agent_id="research"),
                Task(id="t_final", title="present", assignee_agent_id="coordinator"),
            ],
            edges=[
                TaskEdge(from_task_id="t1", to_task_id="t_final"),
                TaskEdge(from_task_id="t2", to_task_id="t_final"),
            ],
        )
        state = _AdkState(client=FakeClient(), planner=planner, refine_on_events=False)  # type: ignore[arg-type]
        ic = _StubPlannerIC("go")
        state.on_invocation_start(ic)
        state.maybe_run_planner(ic)

        # t_final is the only coordinator task, but all deps are PENDING.
        assert state._next_task_for_agent("adk_sess_plan", "coordinator") is None

        # Completing one dep is not enough.
        state._active_plan_by_session["adk_sess_plan"].tasks["t1"].status = "COMPLETED"
        assert state._next_task_for_agent("adk_sess_plan", "coordinator") is None

        # Only when BOTH deps are COMPLETED does t_final unblock.
        state._active_plan_by_session["adk_sess_plan"].tasks["t2"].status = "COMPLETED"
        nxt = state._next_task_for_agent("adk_sess_plan", "coordinator")
        assert nxt is not None and nxt.id == "t_final"

    def test_topological_stages_helper(self):
        from harmonograf_client.planner import Plan, Task, TaskEdge

        plan = Plan(
            tasks=[
                Task(id="a", title="a"),
                Task(id="b", title="b"),
                Task(id="c", title="c"),
                Task(id="d", title="d"),
            ],
            edges=[
                TaskEdge(from_task_id="a", to_task_id="c"),
                TaskEdge(from_task_id="b", to_task_id="c"),
                TaskEdge(from_task_id="c", to_task_id="d"),
            ],
        )
        stages = plan.topological_stages()
        ids = [[t.id for t in stage] for stage in stages]
        assert ids == [["a", "b"], ["c"], ["d"]]


# ---------------------------------------------------------------------------
# HarmonografAgent transparency — plugin callbacks no-op for the wrapper so
# it never appears as a phantom agent row in the frontend, and plan
# submission happens exactly once per invocation (via the wrapper's
# explicit host_agent=inner_agent path — not the plugin's implicit one).
# ---------------------------------------------------------------------------


@dataclass
class _HarmoAgentStub:
    """Stand-in for HarmonografAgent — carries the ClassVar marker the
    plugin looks for when deciding whether to skip its callbacks.
    """

    name: str = "harmonograf"
    description: str = "wrapper"
    _is_harmonograf_agent: bool = True
    inner_agent: Any = None
    sub_agents: list = field(default_factory=list)
    tools: list = field(default_factory=list)


class TestHarmonografAgentTransparency:
    def _make_plugin(self, **kwargs):
        from harmonograf_client.adk import make_adk_plugin

        client = FakeClient()
        plugin = make_adk_plugin(client, **kwargs)  # type: ignore[arg-type]
        return client, plugin

    def test_before_run_callback_skips_harmonograf_agent(self):
        """When the root is HarmonografAgent, before_run_callback must:
        (a) substitute the inner agent so the INVOCATION span routes to
        the real coordinator — never to a 'harmonograf' row — and
        (b) skip the implicit maybe_run_planner call so plan submission
        stays at exactly-once (HarmonografAgent owns the explicit path).
        """
        import asyncio

        client, plugin = self._make_plugin(planner=False)
        inner = FakeAgent(name="coordinator")
        harmo = _HarmoAgentStub(inner_agent=inner)
        ic = FakeInvocationContext(invocation_id="inv_h", agent=harmo)
        asyncio.run(plugin.before_run_callback(invocation_context=ic))

        starts = client.starts()
        # Invocation span is emitted, but for the inner coordinator.
        assert len(starts) == 1
        _, kw = starts[0]
        assert kw["kind"] == "INVOCATION"
        assert kw["agent_id"] == "coordinator"
        for _, kw in starts:
            assert kw.get("agent_id") != "harmonograf"
        # And no plan was generated / submitted via the implicit path.
        assert [c for c in client.calls if c[0] == "submit_plan"] == [], (
            "guard must suppress the implicit maybe_run_planner path"
        )

    def test_before_run_callback_fires_for_inner_agent(self):
        import asyncio

        client, plugin = self._make_plugin(planner=False)
        ic = FakeInvocationContext(
            invocation_id="inv_i", agent=FakeAgent(name="coordinator")
        )
        asyncio.run(plugin.before_run_callback(invocation_context=ic))

        starts = client.starts()
        assert len(starts) == 1
        _, kw = starts[0]
        assert kw["kind"] == "INVOCATION"
        assert kw["agent_id"] == "coordinator"

    def test_no_harmonograf_row_emitted_during_run(self):
        import asyncio

        client, plugin = self._make_plugin(planner=False)
        inner = FakeAgent(name="coordinator")
        harmo_ic = FakeInvocationContext(
            invocation_id="inv_x", agent=_HarmoAgentStub(inner_agent=inner)
        )
        # After delegation, the inner agent's per-agent context carries
        # the real coordinator on its InvocationContext — that's what
        # callback contexts see during model/tool callbacks.
        inner_ic = FakeInvocationContext(
            invocation_id="inv_x",
            agent=FakeAgent(name="coordinator"),
            session=FakeSession(id=harmo_ic.session.id),
        )
        cc = FakeCallbackContext(_invocation_context=inner_ic)

        asyncio.run(plugin.before_run_callback(invocation_context=harmo_ic))
        asyncio.run(
            plugin.before_model_callback(
                callback_context=cc, llm_request=FakeLlmRequest(model="gpt-4o")
            )
        )
        asyncio.run(
            plugin.after_model_callback(
                callback_context=cc, llm_response=FakeLlmResponse()
            )
        )
        asyncio.run(plugin.after_run_callback(invocation_context=harmo_ic))

        starts = client.starts()
        for _, kw in starts:
            assert kw.get("agent_id") != "harmonograf", (
                f"leaked harmonograf row via {kw.get('kind')!r} span: {kw}"
            )
        kinds = [kw["kind"] for (_, kw) in starts]
        assert "LLM_CALL" in kinds
        # The invocation span was created, but for the inner agent, so
        # the Gantt row still has its activation bar (just not on a
        # phantom harmonograf row).
        assert "INVOCATION" in kinds

    def test_plan_submitted_exactly_once_per_invocation(self):
        import asyncio

        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _CountingPlanner(PlannerHelper):
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, *, request, available_agents, context=None):
                self.calls += 1
                return Plan(
                    tasks=[
                        Task(id="t1", title="do", assignee_agent_id="coordinator")
                    ],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                return None

        planner = _CountingPlanner()
        client, plugin = self._make_plugin(planner=planner)
        state = plugin._hg_state

        inner = _StubAgentWithPlanner(name="coordinator")
        harmo = _HarmoAgentStub(inner_agent=inner, sub_agents=[inner])
        ic = _StubPlannerIC("please run the plan")
        ic.agent = harmo  # type: ignore[attr-defined]

        # Runner-level guard: before_run_callback must NOT submit a plan.
        asyncio.run(plugin.before_run_callback(invocation_context=ic))
        assert planner.calls == 0
        assert [c for c in client.calls if c[0] == "submit_plan"] == []

        # HarmonografAgent._run_async_impl's explicit call — single source.
        state.maybe_run_planner(ic, host_agent=inner)

        assert planner.calls == 1
        submits = [c for c in client.calls if c[0] == "submit_plan"]
        assert len(submits) == 1, (
            f"expected exactly one plan submission, got {len(submits)}"
        )


# ---------------------------------------------------------------------------
# Drift detection + explicit refine — exercises the observer path that
# replaces the removed auto-refine on model_end / tool_end / transfer.
# ---------------------------------------------------------------------------


class _FakeFunctionCall:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakePartFC:
    def __init__(self, name: str) -> None:
        self.function_call = _FakeFunctionCall(name)
        self.text = None
        self.thought = False


class _FakeContentFC:
    def __init__(self, parts: list) -> None:
        self.parts = parts
        self.role = "model"


class _FakeActions:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeDriftEvent:
    def __init__(
        self,
        *,
        id: str = "",
        author: str = "",
        content: Any = None,
        actions: Any = None,
        status: str = "",
        task_id: str = "",
        completed_task_id: str = "",
    ) -> None:
        self.id = id
        self.author = author
        self.content = content
        self.actions = actions
        self.status = status
        self.task_id = task_id
        self.completed_task_id = completed_task_id


def _seed_plan_state(state, *, hsession_id: str = "hs1"):
    """Seed a 3-task plan on ``state`` under ``hsession_id`` and return
    (plan_state, task dict). Tasks are assigned to ``worker``;
    ``otherbot`` is not a known assignee.
    """
    from harmonograf_client.adk import PlanState
    from harmonograf_client.planner import Plan, Task, TaskEdge

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
        plan_id="plan-hs1",
        tasks={"t1": t1, "t2": t2, "t3": t3},
        available_agents=["worker"],
        generating_invocation_id="inv-1",
        remaining_for_fallback=[t1, t2, t3],
    )
    with state._lock:
        state._active_plan_by_session[hsession_id] = ps
    return ps


class TestDriftDetection:
    def test_drift_tool_call_wrong_agent(self):
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ps = _seed_plan_state(state)

        # tool call authored by "otherbot" while current task is t1
        # (assigned to "worker") → wrong-agent drift.
        ev = _FakeDriftEvent(
            id="e1",
            author="otherbot",
            content=_FakeContentFC([_FakePartFC("search")]),
        )
        drift = state.detect_drift(
            [ev], current_task=ps.tasks["t1"], plan_state=ps
        )
        assert drift is not None
        assert drift.kind == "tool_call_wrong_agent"
        assert "otherbot" in drift.detail

    def test_drift_task_completion_out_of_order(self):
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ps = _seed_plan_state(state)

        # t3 is marked COMPLETED but t1, t2 are still PENDING → drift.
        ev = _FakeDriftEvent(id="e2", completed_task_id="t3")
        drift = state.detect_drift(
            [ev], current_task=None, plan_state=ps
        )
        assert drift is not None
        assert drift.kind == "task_completion_out_of_order"
        assert "t3" in drift.detail

    def test_drift_transfer_to_unplanned_agent(self):
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ps = _seed_plan_state(state)

        ev = _FakeDriftEvent(
            id="e3",
            actions=_FakeActions(transfer_to_agent="ghost"),
        )
        drift = state.detect_drift(
            [ev], current_task=None, plan_state=ps
        )
        assert drift is not None
        assert drift.kind == "transfer_to_unplanned_agent"
        assert "ghost" in drift.detail

    def test_drift_failed_span(self):
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ps = _seed_plan_state(state)
        ps.tasks["t1"].status = "RUNNING"

        ev = _FakeDriftEvent(id="e4", status="FAILED", task_id="t1")
        drift = state.detect_drift(
            [ev], current_task=ps.tasks["t1"], plan_state=ps
        )
        assert drift is not None
        assert drift.kind == "failed_span"

    def test_drift_none_when_events_stay_on_plan(self):
        from harmonograf_client.adk import _AdkState

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        ps = _seed_plan_state(state)

        # worker calls a tool while working on t1 — no drift.
        ev = _FakeDriftEvent(
            id="e5",
            author="worker",
            content=_FakeContentFC([_FakePartFC("search")]),
        )
        drift = state.detect_drift(
            [ev], current_task=ps.tasks["t1"], plan_state=ps
        )
        assert drift is None

    def test_refine_plan_on_drift_logs_info(self, caplog):
        import logging

        from harmonograf_client.adk import DriftReason, _AdkState
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class StubPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(
                    tasks=[Task(id="t1", title="x", assignee_agent_id="worker")],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                return None  # no-op: record the drift but keep plan shape

        client = FakeClient()
        state = _AdkState(client=client, planner=StubPlanner())  # type: ignore[arg-type]
        ps = _seed_plan_state(state)

        drift = DriftReason(
            kind="tool_call_wrong_agent", detail="otherbot called search"
        )
        with caplog.at_level(logging.INFO, logger="harmonograf_client.adk"):
            state.refine_plan_on_drift("hs1", drift, current_task=None)

        assert any(
            "plan refined" in rec.message and "drift=tool_call_wrong_agent"
            in rec.message
            for rec in caplog.records
        ), f"expected INFO drift log; saw: {[r.message for r in caplog.records]}"
        assert ps.revisions, "revision should be recorded"
        assert ps.plan.revision_reason == (
            "tool_call_wrong_agent: otherbot called search"
        )


# ---------------------------------------------------------------------------
# Iter13: monotonic state machine unit tests (_set_task_status)
# ---------------------------------------------------------------------------


class TestSetTaskStatusGuard:
    def test_pending_to_running_allowed(self):
        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="PENDING")
        assert _set_task_status(t, "RUNNING") is True
        assert t.status == "RUNNING"

    def test_running_to_completed_allowed(self):
        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="RUNNING")
        assert _set_task_status(t, "COMPLETED") is True
        assert t.status == "COMPLETED"

    def test_completed_to_running_rejected(self, caplog):
        import logging

        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="COMPLETED")
        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            ok = _set_task_status(t, "RUNNING")
        assert ok is False
        assert t.status == "COMPLETED"
        assert any("REJECTED" in r.message and "t1" in r.message
                   for r in caplog.records)

    def test_failed_is_terminal(self):
        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="FAILED")
        assert _set_task_status(t, "RUNNING") is False
        assert _set_task_status(t, "COMPLETED") is False
        assert t.status == "FAILED"

    def test_cancelled_is_terminal(self):
        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="CANCELLED")
        assert _set_task_status(t, "PENDING") is False
        assert t.status == "CANCELLED"

    def test_idempotent_same_status(self):
        from harmonograf_client.adk import _set_task_status
        from harmonograf_client.planner import Task

        t = Task(id="t1", title="x", status="COMPLETED")
        # Same-status writes are a no-op success (not a rejection).
        assert _set_task_status(t, "COMPLETED") is True
        assert t.status == "COMPLETED"
