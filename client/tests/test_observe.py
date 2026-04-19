"""Tests for :func:`harmonograf_client.observe`.

``observe(runner)`` is strictly observability — it appends exactly one
:class:`HarmonografSink` to the runner's sink list via the public
``add_sink`` extension API and returns the same runner object. It must
never touch planning, steering, goal derivation, or execution (those
belong to :func:`goldfive.wrap`).

See issue #22 for the rationale: the two-line form
``observe(goldfive.wrap(agent))`` makes the layering crystal-clear, and
conflating the two under a single ``wrap`` call is explicitly out of
scope. Issue #36 refactored ``observe`` to use the Runner extension
API (``add_sink`` / ``add_close_hook`` / ``control`` setter) instead of
monkey-patching, and required the helper to be called from within a
running event loop so the control bridge can wire itself up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from harmonograf_client import observe
from harmonograf_client.client import Client
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


class _FakeRunner:
    """Minimal stand-in for :class:`goldfive.Runner`.

    Mirrors the narrow public surface ``observe`` relies on:
    ``add_sink``, ``add_close_hook``, and a ``control`` property with a
    setter. ``close()`` drives registered close hooks so teardown tests
    stay honest.
    """

    def __init__(self, sinks: list[Any] | None = None) -> None:
        self.sinks: list[Any] = list(sinks) if sinks else []
        self._control: Any = None
        self._close_hooks: list[Callable[[], Awaitable[None]]] = []

    def add_sink(self, sink: Any) -> None:
        self.sinks.append(sink)

    def add_close_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._close_hooks.append(hook)

    @property
    def control(self) -> Any:
        return self._control

    @control.setter
    def control(self, value: Any) -> None:
        if self._control is value:
            return
        if self._control is not None:
            raise RuntimeError("control already attached")
        self._control = value

    async def close(self) -> None:
        for hook in self._close_hooks:
            await hook()


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="preset-client",
        agent_id="agent-observe",
        session_id="sess-observe",
        framework="ADK",
        buffer_size=8,
        _transport_factory=make_factory(made),
    )


@pytest.mark.asyncio
async def test_observe_returns_same_runner_and_appends_sink() -> None:
    runner = _FakeRunner()
    result = observe(runner, name="unit", framework="CUSTOM")

    assert result is runner
    assert len(runner.sinks) == 1
    assert isinstance(runner.sinks[0], HarmonografSink)

    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_preserves_existing_sinks() -> None:
    preexisting = object()
    runner = _FakeRunner(sinks=[preexisting])

    observe(runner, name="unit", framework="CUSTOM")

    assert runner.sinks[0] is preexisting
    assert isinstance(runner.sinks[1], HarmonografSink)
    assert len(runner.sinks) == 2

    await runner.close()
    runner.sinks[1]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_reuses_provided_client(client: Client) -> None:
    runner = _FakeRunner()

    observe(runner, client=client)

    sink = runner.sinks[0]
    assert isinstance(sink, HarmonografSink)
    assert sink._client is client

    await runner.close()


@pytest.mark.asyncio
async def test_observe_respects_harmonograf_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    original_init = Client.__init__

    def spy_init(self: Client, **kwargs: Any) -> None:
        captured.update(kwargs)
        # Use the fake transport so Client construction doesn't open a socket.
        kwargs["_transport_factory"] = make_factory([])
        original_init(self, **kwargs)

    monkeypatch.setattr(Client, "__init__", spy_init)
    monkeypatch.setenv("HARMONOGRAF_SERVER", "remote.example:9999")

    runner = _FakeRunner()
    observe(runner, name="env-client")

    assert captured["server_addr"] == "remote.example:9999"
    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_forwards_name_and_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    original_init = Client.__init__

    def spy_init(self: Client, **kwargs: Any) -> None:
        captured.update(kwargs)
        kwargs["_transport_factory"] = make_factory([])
        original_init(self, **kwargs)

    monkeypatch.setattr(Client, "__init__", spy_init)
    monkeypatch.delenv("HARMONOGRAF_SERVER", raising=False)

    runner = _FakeRunner()
    observe(runner, name="presentation", framework="ADK")

    assert captured["name"] == "presentation"
    assert captured["framework"] == "ADK"
    # server_addr not forwarded => Client's own default applies.
    assert "server_addr" not in captured
    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_on_adk_wrapped_runner() -> None:
    """``observe(goldfive.wrap(adk_agent))`` must work end-to-end.

    Exercises the whole extension API surface on a real
    :class:`GoldfiveADKAgent`: ``add_sink``, the ``control`` setter, and
    ``add_close_hook`` all delegate to the inner :class:`Runner`, so
    calling ``observe`` must not raise, the sink must land on the inner
    runner's sink list, and ``runner.close()`` must trigger the
    bridge's teardown via the registered close hook.
    """
    pytest.importorskip("google.adk.agents")
    import goldfive
    from google.adk.agents import BaseAgent  # type: ignore

    from harmonograf_client._control_bridge import ControlBridge

    class _NullADKAgent(BaseAgent):
        """Bare-minimum ADK BaseAgent — we never invoke its ADK path."""

    adk_agent = _NullADKAgent(name="null", description="")
    wrapped = goldfive.wrap(adk_agent)

    made: list[FakeTransport] = []
    client = Client(
        name="adk-wrap",
        agent_id="agent-adk",
        session_id="sess-adk",
        framework="ADK",
        buffer_size=8,
        _transport_factory=make_factory(made),
    )

    # goldfive.wrap() seeds the Runner with its own sinks (e.g.
    # LoggingSink); observe() appends the HarmonografSink on top of
    # whatever is already there.
    before = len(wrapped.runner.sinks)
    observe(wrapped, client=client)

    # Sink reached the inner Runner's list via add_sink delegation.
    inner_sinks = wrapped.runner.sinks
    assert len(inner_sinks) == before + 1
    assert any(isinstance(s, HarmonografSink) for s in inner_sinks)

    # Bridge was constructed and stashed for introspection.
    bridge = getattr(wrapped, "_harmonograf_control_bridge", None)
    assert isinstance(bridge, ControlBridge)
    assert bridge._closed is False

    # close() drives the registered close hook -> bridge.stop().
    await wrapped.close()
    assert bridge._closed is True

    client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_twice_appends_two_sinks(client: Client) -> None:
    runner = _FakeRunner()

    observe(runner, client=client)
    observe(runner, client=client)

    assert len(runner.sinks) == 2
    assert all(isinstance(s, HarmonografSink) for s in runner.sinks)
    # Deliberately no dedupe — caller's responsibility.
    assert runner.sinks[0] is not runner.sinks[1]

    await runner.close()
