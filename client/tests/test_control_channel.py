"""Tests for :func:`harmonograf_client.control_channel`.

``control_channel(client)`` is the adk-web-friendly counterpart of
:func:`observe`: it returns a bare :class:`goldfive.ControlChannel`
backed by a live :class:`ControlBridge`, suitable for passing into
:func:`goldfive.wrap` via ``control=``. The use case is the
``App(root_agent=goldfive.wrap(tree, control=...))`` pattern where the
caller never holds a :class:`goldfive.Runner` reference — so
:func:`observe` (which mutates a runner) does not apply. See
harmonograf#55.

Coverage:

- :func:`control_channel` returns a :class:`ControlChannel` with a live
  :class:`ControlBridge` attached as ``_harmonograf_control_bridge``.
- A ``ControlEvent`` delivered on the underlying transport reaches the
  returned channel's inbox as a goldfive :class:`ControlMessage`.
- Acks pushed into the channel flow back out through the client's
  transport with the right result enum and detail string.
- End-to-end wiring against a real :class:`goldfive.Runner` via
  :func:`goldfive.wrap`: the channel we return IS the one
  ``runner.control`` ends up pointing at, so the runner's
  :meth:`receive` sees frontend-issued steers.
- Driving :meth:`ControlBridge.stop` via the stashed attribute tears
  the bridge down cleanly and closes the channel.
"""

from __future__ import annotations

import asyncio

import pytest

import goldfive
from goldfive.control import (
    AckResult,
    ControlAck,
    ControlChannel,
    ControlKind,
)
from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_client import control_channel
from harmonograf_client._control_bridge import ControlBridge
from harmonograf_client.client import Client

from tests._fixtures import FakeTransport, make_factory


def _make_event(
    kind_name: str,
    *,
    control_id: str = "c-1",
    steer_note: str | None = None,
) -> gf_control_pb2.ControlEvent:
    """Build a goldfive ``ControlEvent`` proto with the requested kind."""
    kind_enum = getattr(gf_control_pb2, f"CONTROL_KIND_{kind_name}")
    ev = gf_control_pb2.ControlEvent(id=control_id, kind=kind_enum)
    if steer_note is not None:
        ev.steer.note = steer_note
    return ev


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="cc-client",
        agent_id="agent-cc",
        session_id="sess-cc",
        framework="ADK",
        buffer_size=8,
        _transport_factory=make_factory(made),
    )


async def _drain(loop_count: int = 4) -> None:
    """Yield control enough times for call_soon_threadsafe hand-offs to land."""
    for _ in range(loop_count):
        await asyncio.sleep(0)


# ----------------------------------------------------------------------
# Shape + wiring
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_live_control_channel_with_bridge(
    client: Client, made: list[FakeTransport]
) -> None:
    channel = control_channel(client)

    try:
        assert isinstance(channel, ControlChannel)
        bridge = getattr(channel, "_harmonograf_control_bridge", None)
        assert isinstance(bridge, ControlBridge)
        # The bridge installed its transport-side forward hook during
        # start() — absence would mean no steer/cancel/pause event ever
        # reaches this channel.
        assert made[0].control_forward is not None
    finally:
        bridge = getattr(channel, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()


# ----------------------------------------------------------------------
# Event forwarding — the actual bug this helper fixes
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_event_forwards_to_returned_channel(
    client: Client, made: list[FakeTransport]
) -> None:
    """A STEER over the transport lands on the returned channel as a goldfive msg.

    This is the exact delivery path that was broken in harmonograf#55
    under ``adk web``: before the fix, no bridge existed between the
    client's ``SubscribeControl`` stream and the runner's control
    channel, so the server's PostAnnotation returned
    ``delivery=2 detail='delivery failed'``.
    """
    channel = control_channel(client)

    try:
        made[0].deliver_control_event(
            _make_event("STEER", control_id="cc-steer-1", steer_note="fix slide 3")
        )

        msg = await asyncio.wait_for(channel.receive(), timeout=1.0)
        assert msg is not None
        assert msg.kind is ControlKind.STEER
        assert msg.id == "cc-steer-1"
        assert msg.payload["note"] == "fix slide 3"
    finally:
        bridge = getattr(channel, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()


@pytest.mark.asyncio
async def test_ack_flows_back_to_transport(
    client: Client, made: list[FakeTransport]
) -> None:
    channel = control_channel(client)

    try:
        await channel.ack(
            ControlAck(
                control_id="cc-ack-1",
                result=AckResult.SUCCESS,
                detail="handled",
            )
        )
        await _drain()

        assert any(
            aid == "cc-ack-1" and res == "success" and det == "handled"
            for aid, res, det in made[0].sent_acks
        ), made[0].sent_acks
    finally:
        bridge = getattr(channel, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()


# ----------------------------------------------------------------------
# End-to-end: goldfive.wrap(control=channel, ...) — the adk-web path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_with_goldfive_wrap(
    client: Client, made: list[FakeTransport]
) -> None:
    """Pass the channel into ``goldfive.wrap(control=...)`` and see a steer land.

    This mirrors the presentation_agent_orchestrated demo's wiring:

        channel = harmonograf_client.control_channel(client)
        wrapped = goldfive.wrap(tree, control=channel, ...)
        app = App(root_agent=wrapped, ...)

    The Runner inside ``wrapped`` must expose the same channel as its
    ``runner.control`` — otherwise the Steerer polls a different inbox
    than the one the bridge publishes to.
    """

    async def _trivial_agent(task, session, tools):  # noqa: ARG001
        from goldfive.results import InvocationResult

        return InvocationResult(output_text="ok")

    channel = control_channel(client)

    try:
        runner = goldfive.wrap(_trivial_agent, control=channel)
        # Identity, not just equality — the Runner's control must BE the
        # bridge-backed channel or the steer path is broken.
        assert runner.control is channel

        made[0].deliver_control_event(
            _make_event("CANCEL", control_id="cc-e2e-1")
        )

        msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
        assert msg is not None
        assert msg.kind is ControlKind.CANCEL
        assert msg.id == "cc-e2e-1"
    finally:
        bridge = getattr(channel, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()


# ----------------------------------------------------------------------
# Teardown
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_stop_tears_down_cleanly(
    client: Client, made: list[FakeTransport]
) -> None:
    channel = control_channel(client)
    bridge: ControlBridge = channel._harmonograf_control_bridge  # type: ignore[attr-defined]

    assert made[0].control_forward is not None
    assert bridge._events_task is not None
    assert bridge._acks_task is not None

    await bridge.stop()

    assert made[0].control_forward is None
    assert bridge._events_task.done()
    assert bridge._acks_task.done()
    assert bridge._closed is True
