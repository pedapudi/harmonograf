"""Unit tests for :class:`HarmonografRunner` (post-pivot).

HarmonografRunner is now a thin convenience wrapper around an ADK
Runner. Plan enforcement moved to :class:`HarmonografAgent` — see
``test_agent.py`` for coverage of the re-invocation loop.

These tests focus on construction, plugin attachment, and pass-through
``run_async`` behavior in composition mode.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.runner import HarmonografRunner


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

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid


class FakeEvent:
    def __init__(self, invocation_id: str, payload: str = "") -> None:
        self.invocation_id = invocation_id
        self.payload = payload


class FakePluginManager:
    def __init__(self) -> None:
        self.plugins: list[Any] = []


class FakeAgent:
    def __init__(self, name: str = "coordinator") -> None:
        self.name = name


class StubRunner:
    """Duck-typed ADK Runner replacement for composition-mode tests."""

    def __init__(self, agent: FakeAgent, passes: list[dict]) -> None:
        self.agent = agent
        self.app_name = "stub-app"
        self.session_service = object()
        self.plugin_manager = FakePluginManager()
        self._passes = passes
        self.call_log: list[dict] = []

    async def run_async(self, **kwargs):
        self.call_log.append(dict(kwargs))
        idx = len(self.call_log) - 1
        if idx >= len(self._passes):
            return
        for ev in self._passes[idx].get("events", []):
            yield ev


class TestHarmonografRunnerConstruction:
    def test_plugin_attached_to_inner_runner(self):
        agent = FakeAgent()
        inner = StubRunner(agent, passes=[])
        client = FakeClient()

        runner = HarmonografRunner(client=client, runner=inner, planner=False)
        assert len(inner.plugin_manager.plugins) == 1
        assert runner.plugin is inner.plugin_manager.plugins[0]
        assert runner.agent is agent
        assert runner.runner is inner

    def test_detach_removes_plugin(self):
        agent = FakeAgent()
        inner = StubRunner(agent, passes=[])
        runner = HarmonografRunner(
            client=FakeClient(), runner=inner, planner=False
        )
        runner.detach()
        assert inner.plugin_manager.plugins == []

    def test_missing_agent_and_runner_raises(self):
        with pytest.raises(ValueError):
            HarmonografRunner(client=FakeClient(), planner=False)

    def test_as_adapter_returns_legacy_handle(self):
        from harmonograf_client.adk import AdkAdapter

        agent = FakeAgent()
        inner = StubRunner(agent, passes=[])
        runner = HarmonografRunner(
            client=FakeClient(), runner=inner, planner=False
        )
        handle = runner.as_adapter()
        assert isinstance(handle, AdkAdapter)
        assert handle.plugin is runner.plugin

    def test_composition_mode_without_client_is_plain_passthrough(self):
        agent = FakeAgent()
        inner = StubRunner(agent, passes=[])
        runner = HarmonografRunner(runner=inner)
        assert runner.plugin is None
        assert runner.runner is inner


class TestHarmonografRunnerRunAsync:
    @pytest.mark.asyncio
    async def test_passes_through_events(self):
        agent = FakeAgent()
        events = [FakeEvent("inv-1", "hello"), FakeEvent("inv-1", "world")]
        inner = StubRunner(agent, passes=[{"events": events}])
        runner = HarmonografRunner(
            client=FakeClient(), runner=inner, planner=False
        )
        got = [ev async for ev in runner.run_async(new_message="go")]
        assert [e.payload for e in got] == ["hello", "world"]
        assert len(inner.call_log) == 1

    @pytest.mark.asyncio
    async def test_telemetry_plugin_attached_without_planner(self):
        agent = FakeAgent(name="coordinator")
        events = [FakeEvent("inv-telemetry", "hi")]
        inner = StubRunner(agent, passes=[{"events": events}])
        client = FakeClient()
        runner = HarmonografRunner(client=client, runner=inner, planner=False)
        got = [ev async for ev in runner.run_async(new_message="go")]
        assert [e.payload for e in got] == ["hi"]
        assert not any(c[0] == "submit_plan" for c in client.calls)
        assert runner.plugin is not None
