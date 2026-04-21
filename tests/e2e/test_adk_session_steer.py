"""End-to-end: STEER targeting an ADK session reaches a bound steerer
on the Client (harmonograf #53).

This is the critical regression test for the issue. The failure scenario
in the bug:

  1. ``HarmonografTelemetryPlugin`` stamps ADK ``session.id`` on every
     span (PR #48).
  2. The Client only opens ONE SubscribeControl at startup, keyed on
     the harmonograf-assigned home session.
  3. The frontend fires PostAnnotation(STEERING) targeting the ADK
     session_id.
  4. The server's ControlRouter finds no sub on (ADK_session, agent_id);
     delivery returns FAILURE; the run progresses unsteered.

The fix opens additional SubscribeControl streams keyed on each ADK
session that the plugin sees, and the router prefers those when a
STEER carries a matching session_id. This test proves the full loop:
boot server → create Client → register ADK session → simulate a
frontend STEER via ``router.deliver`` → assert a control handler on
the Client fires.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_client import Client
from harmonograf_server.control_router import DeliveryResult


ADK_SESSION = "adk-session-e2e-steer"


@pytest.mark.asyncio
async def test_steer_reaches_bound_steerer_via_adk_session_subscription(
    harmonograf_server,
) -> None:
    """The bug reproducer. Registers an ADK session on the Client, then
    fires a STEER via the server's router targeting that ADK session.
    The Client's bound handler must receive the event — not get starved
    because of the home-only sub topology."""
    addr = harmonograf_server["addr"]
    router = harmonograf_server["router"]

    client = Client(
        name="adk-session-steer",
        agent_id="agent-steer-e2e",
        server_addr=addr,
    )
    received_events: list[gf_control_pb2.ControlEvent] = []

    def on_steer(event: gf_control_pb2.ControlEvent):
        received_events.append(event)
        return None  # default "success"

    client.on_control("STEER", on_steer)
    try:
        # Wait for the home subscription to be in place on the router.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if router.live_stream_ids(client.agent_id):
                break
            await asyncio.sleep(0.02)
        assert router.live_stream_ids(client.agent_id), "home sub never registered"

        # Register the ADK session. This is the hook the plugin drives
        # on first span for a session — here we invoke it directly.
        client.register_session(ADK_SESSION)

        # Wait until the router sees the per-session sub. The router
        # indexes by agent_id, so we inspect the session_ids of the
        # live subs.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            subs = [
                sub
                for sub in router._subs.get(client.agent_id, {}).values()
                if sub.session_id == ADK_SESSION and not sub.closed
            ]
            if subs:
                break
            await asyncio.sleep(0.02)
        assert subs, "per-ADK-session sub never registered on router"

        # Fire a STEER at (ADK_SESSION, agent_id) — the shape PostAnnotation
        # would produce. Before the fix this returned delivery=FAILURE
        # because no sub matched; after the fix the session-scoped sub
        # receives it and the Client handler acks success.
        event = gf_control_pb2.ControlEvent(
            kind=gf_control_pb2.CONTROL_KIND_STEER,
            target=gf_control_pb2.ControlTarget(agent_id=client.agent_id),
        )
        event.steer.note = "focus on the next step"

        outcome = await router.deliver(
            session_id=ADK_SESSION,
            agent_id=client.agent_id,
            event=event,
            timeout_s=5.0,
        )
        assert outcome.result == DeliveryResult.SUCCESS, outcome
        # The Client's steer handler saw exactly one event, and the
        # payload landed with the right note.
        assert len(received_events) == 1
        assert received_events[0].steer.note == "focus on the next step"

    finally:
        client.shutdown(flush_timeout=1.0)


@pytest.mark.asyncio
async def test_steer_without_adk_session_falls_back_to_home_sub(
    harmonograf_server,
) -> None:
    """A STEER with an unknown session_id (or one for which the Client
    never called register_session) still reaches the home sub — the
    legacy behaviour. Protects against a regression where the session
    filter becomes strictly exclusive."""
    addr = harmonograf_server["addr"]
    router = harmonograf_server["router"]

    client = Client(
        name="fallback-steer",
        agent_id="agent-fallback-e2e",
        server_addr=addr,
    )
    received_events: list[gf_control_pb2.ControlEvent] = []
    client.on_control("STEER", lambda ev: received_events.append(ev) or None)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if router.live_stream_ids(client.agent_id):
                break
            await asyncio.sleep(0.02)

        event = gf_control_pb2.ControlEvent(
            kind=gf_control_pb2.CONTROL_KIND_STEER,
            target=gf_control_pb2.ControlTarget(agent_id=client.agent_id),
        )
        event.steer.note = "fall back please"

        outcome = await router.deliver(
            session_id="some-session-we-never-subscribed",
            agent_id=client.agent_id,
            event=event,
            timeout_s=5.0,
        )
        assert outcome.result == DeliveryResult.SUCCESS
        assert len(received_events) == 1
    finally:
        client.shutdown(flush_timeout=1.0)
