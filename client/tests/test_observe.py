"""Tests for :func:`harmonograf_client.observe`.

``observe(runner)`` is strictly observability — it appends exactly one
:class:`HarmonografSink` to ``runner.sinks`` and returns the same runner
object. It must never touch planning, steering, goal derivation, or
execution (those belong to :func:`goldfive.wrap`).

See issue #22 for the rationale: the two-line form
``observe(goldfive.wrap(agent))`` makes the layering crystal-clear, and
conflating the two under a single ``wrap`` call is explicitly out of
scope.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client import observe
from harmonograf_client.client import Client
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


class _FakeRunner:
    """Minimal stand-in for :class:`goldfive.Runner`.

    ``observe`` only touches ``runner.sinks``; a plain list is enough to
    verify the contract without spinning up a real Runner + planner +
    executor.
    """

    def __init__(self, sinks: list[Any] | None = None) -> None:
        self.sinks: list[Any] = list(sinks) if sinks else []


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


def test_observe_returns_same_runner_and_appends_sink() -> None:
    runner = _FakeRunner()
    result = observe(runner, name="unit", framework="CUSTOM")

    assert result is runner
    assert len(runner.sinks) == 1
    assert isinstance(runner.sinks[0], HarmonografSink)

    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


def test_observe_preserves_existing_sinks() -> None:
    preexisting = object()
    runner = _FakeRunner(sinks=[preexisting])

    observe(runner, name="unit", framework="CUSTOM")

    assert runner.sinks[0] is preexisting
    assert isinstance(runner.sinks[1], HarmonografSink)
    assert len(runner.sinks) == 2

    runner.sinks[1]._client.shutdown(flush_timeout=0.1)


def test_observe_reuses_provided_client(client: Client) -> None:
    runner = _FakeRunner()

    observe(runner, client=client)

    sink = runner.sinks[0]
    assert isinstance(sink, HarmonografSink)
    assert sink._client is client


def test_observe_respects_harmonograf_server_env(
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
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


def test_observe_forwards_name_and_framework(
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
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


def test_observe_twice_appends_two_sinks(client: Client) -> None:
    runner = _FakeRunner()

    observe(runner, client=client)
    observe(runner, client=client)

    assert len(runner.sinks) == 2
    assert all(isinstance(s, HarmonografSink) for s in runner.sinks)
    # Deliberately no dedupe — caller's responsibility.
    assert runner.sinks[0] is not runner.sinks[1]
