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
