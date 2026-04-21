"""Tests for per-ADK-session control subscriptions (harmonograf #53).

The :class:`Client` opens exactly one control ``SubscribeControl`` RPC
at startup (the "home" subscription) keyed on the harmonograf-assigned
session id. ADK processes that share a single module-level Client
across many live sessions need *additional* per-session subscriptions
so a STEER targeting ``(ADK_session, agent_id)`` can be routed by the
server to a sub whose ``session_id`` matches.

These tests exercise the :meth:`Client.register_session` /
:meth:`Client.unregister_session` surface against the
:class:`FakeTransport` harness. Transport-level reconnect / task
lifecycle for the per-session subscription is covered separately in
``test_transport_session_subscriptions.py`` against the real
:class:`Transport`.
"""

from __future__ import annotations

import pytest

from harmonograf_client.client import Client

from tests._fixtures import FakeTransport, make_factory


ADK_SESSION = "adk-sess-abc123"
OTHER_SESSION = "adk-sess-def456"


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="session-sub-test",
        agent_id="agent-X",
        session_id="stream-default-session",
        buffer_size=16,
        _transport_factory=make_factory(made),
    )


def test_register_session_opens_new_control_stream(client: Client, made: list[FakeTransport]) -> None:
    """The critical invariant: register_session must reach the transport
    and (eventually) open an additional SubscribeControl stream."""
    client.register_session(ADK_SESSION)
    assert made[0].opened_sessions == [ADK_SESSION]
    assert client.registered_session_ids == (ADK_SESSION,)


def test_register_session_is_idempotent(client: Client, made: list[FakeTransport]) -> None:
    """A plugin calling register_session on every span must not create
    one subscription per span — the transport should see exactly one
    SubscribeControl open per distinct ADK session id."""
    client.register_session(ADK_SESSION)
    client.register_session(ADK_SESSION)
    client.register_session(ADK_SESSION)
    assert made[0].opened_sessions == [ADK_SESSION]


def test_register_session_empty_string_is_noop(client: Client, made: list[FakeTransport]) -> None:
    """The plugin passes ``""`` when the ADK context has no session
    service. That must not open a subscription (empty-session subs
    could never match anything on the router anyway)."""
    client.register_session("")
    assert made[0].opened_sessions == []
    assert client.registered_session_ids == ()


def test_register_multiple_distinct_sessions(client: Client, made: list[FakeTransport]) -> None:
    """Distinct ADK sessions in the same process each get their own
    subscription — the whole motivation for the fix."""
    client.register_session(ADK_SESSION)
    client.register_session(OTHER_SESSION)
    assert made[0].opened_sessions == [ADK_SESSION, OTHER_SESSION]
    assert set(client.registered_session_ids) == {ADK_SESSION, OTHER_SESSION}


def test_unregister_session_removes_subscription(client: Client, made: list[FakeTransport]) -> None:
    client.register_session(ADK_SESSION)
    client.unregister_session(ADK_SESSION)
    assert made[0].closed_sessions == [ADK_SESSION]
