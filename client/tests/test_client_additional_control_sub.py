"""Tests for :meth:`Client.open_additional_control_subscription`
(harmonograf#65 / goldfive#162).

The helper opens an additional ``SubscribeControl`` stream keyed on
an outer adk-web session id so STEER annotations targeting that id
find a matching subscription on the server's :class:`ControlRouter`.
Under the cached-root-session contract from harmonograf#65 the
plugin calls this once per adk-web run.

These tests exercise the Client's surface directly, not the plugin —
the plugin-driven lifecycle is covered in
``test_telemetry_plugin_session_id.py``.
"""

from __future__ import annotations

import pytest

from harmonograf_client.client import Client

from tests._fixtures import FakeTransport, make_factory


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="ctl-sub-test",
        agent_id="agent-X",
        session_id="home-sess",
        buffer_size=64,
        _transport_factory=make_factory(made),
    )


def test_open_additional_control_subscription_delegates_to_transport(
    client: Client, made: list[FakeTransport]
) -> None:
    """The helper forwards the session id to ``Transport.open_session_subscription``."""
    client.open_additional_control_subscription("adk-sess-outer")
    [transport] = made
    assert transport.opened_session_subs == ["adk-sess-outer"]


def test_open_additional_control_subscription_is_idempotent(
    client: Client, made: list[FakeTransport]
) -> None:
    """Re-opening the same session id is a no-op (dedup handled on transport)."""
    client.open_additional_control_subscription("adk-sess-outer")
    client.open_additional_control_subscription("adk-sess-outer")
    [transport] = made
    assert transport.opened_session_subs == ["adk-sess-outer"]


def test_open_additional_control_subscription_empty_is_noop(
    client: Client, made: list[FakeTransport]
) -> None:
    """Empty session id must not drop into the transport — degraded path."""
    client.open_additional_control_subscription("")
    [transport] = made
    assert transport.opened_session_subs == []


def test_close_additional_control_subscription_delegates(
    client: Client, made: list[FakeTransport]
) -> None:
    """The close helper forwards cleanly to ``Transport.close_session_subscription``."""
    client.open_additional_control_subscription("sess-1")
    client.close_additional_control_subscription("sess-1")
    [transport] = made
    assert transport.closed_session_subs == ["sess-1"]


def test_close_additional_control_subscription_empty_is_noop(
    client: Client, made: list[FakeTransport]
) -> None:
    client.close_additional_control_subscription("")
    [transport] = made
    assert transport.closed_session_subs == []


def test_multiple_sessions_are_independent(
    client: Client, made: list[FakeTransport]
) -> None:
    """Closing one session's sub doesn't affect another's — tests pass
    through to the transport map semantics."""
    client.open_additional_control_subscription("sess-A")
    client.open_additional_control_subscription("sess-B")
    client.close_additional_control_subscription("sess-A")
    [transport] = made
    assert transport.opened_session_subs == ["sess-A", "sess-B"]
    assert transport.closed_session_subs == ["sess-A"]
    assert transport.registered_session_ids == ("sess-B",)
