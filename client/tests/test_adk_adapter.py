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
        self.calls.append(("set_activity", text, {}))

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
