"""Tests for :class:`harmonograf_client._control_bridge.ControlBridge`.

The bridge sits between harmonograf's ``SubscribeControl`` gRPC stream
and a goldfive ``ControlChannel`` attached to a :class:`Runner`. These
tests verify the kind-by-kind translation table, ack round-trip, the
UNSUPPORTED-kind fast path, and cleanup on ``runner.close``. They use a
:class:`FakeTransport` so no gRPC sockets open, and they construct
``ControlEvent`` messages by hand from the generated ``types_pb2``
module so the integration stays honest about the wire format.

Scope covered here:

- Every Phase-1 kind (PAUSE / RESUME / CANCEL / STEER / REWIND_TO)
  translates to the matching goldfive ``ControlKind`` and preserves
  the ``control_id`` for ack correlation.
- ``STEER`` and ``REWIND_TO`` payloads land in the goldfive
  ``ControlMessage.payload`` dict under the keys goldfive expects
  (``note`` and ``task_id``).
- Each UNSUPPORTED harmonograf kind (INJECT_MESSAGE, APPROVE, REJECT,
  STATUS_QUERY, INTERCEPT_TRANSFER) acks UNSUPPORTED back to the server
  without touching the runner.
- Goldfive ``ControlAck`` objects published via ``channel.ack()`` flow
  back out to the server as harmonograf ``ControlAck``\\s with the
  right result enum and detail string.
- ``observe(runner)`` attaches a bridge when called inside an event
  loop; ``runner.close()`` tears it down (forward hook cleared,
  forwarding tasks finished).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from goldfive.control import (
    AckResult,
    ControlAck,
    ControlChannel,
    ControlKind,
    ControlMessage,
)

from harmonograf_client import observe
from harmonograf_client._control_bridge import ControlBridge, _KIND_MAP
from harmonograf_client.client import Client
from harmonograf_client.pb import types_pb2

from tests._fixtures import FakeTransport, make_factory


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_event(
    kind_name: str, *, control_id: str = "c-1", payload: bytes = b""
) -> Any:
    """Build a ``types_pb2.ControlEvent`` with the given kind + payload."""
    kind_enum = getattr(types_pb2, f"CONTROL_KIND_{kind_name}")
    return types_pb2.ControlEvent(
        id=control_id,
        kind=kind_enum,
        payload=payload,
    )


class _FakeRunner:
    """Stand-in for :class:`goldfive.Runner` — ``close`` + optional channel."""

    def __init__(self) -> None:
        self.sinks: list[Any] = []
        self.control: ControlChannel | None = None
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="bridge-client",
        agent_id="agent-bridge",
        session_id="sess-bridge",
        framework="ADK",
        buffer_size=8,
        _transport_factory=make_factory(made),
    )


async def _drain(loop_count: int = 4) -> None:
    """Yield control enough times for call_soon_threadsafe hand-offs to land."""
    for _ in range(loop_count):
        await asyncio.sleep(0)


# ----------------------------------------------------------------------
# Kind-by-kind translation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "h_kind, expected_g_kind",
    [
        ("PAUSE", ControlKind.PAUSE),
        ("RESUME", ControlKind.RESUME),
        ("CANCEL", ControlKind.CANCEL),
        ("STEER", ControlKind.STEER),
        ("REWIND_TO", ControlKind.REWIND_TO),
    ],
)
async def test_supported_kind_forwards_to_channel(
    client: Client,
    made: list[FakeTransport],
    h_kind: str,
    expected_g_kind: ControlKind,
) -> None:
    runner = _FakeRunner()
    loop = asyncio.get_running_loop()
    bridge = ControlBridge(client, runner, loop)
    bridge.start()

    transport = made[0]
    transport.deliver_control_event(_make_event(h_kind, control_id="cid-123"))

    assert runner.control is not None
    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg is not None
    assert msg.kind is expected_g_kind
    # Preserve control_id → goldfive message id for ack correlation.
    assert msg.id == "cid-123"

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_payload_lands_under_note(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _FakeRunner()
    bridge = ControlBridge(client, runner, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-1", payload=b"focus on the last slide")
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.STEER
    assert msg.payload == {"note": "focus on the last slide"}

    await bridge.stop()


@pytest.mark.asyncio
async def test_rewind_to_payload_lands_under_task_id(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _FakeRunner()
    bridge = ControlBridge(client, runner, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("REWIND_TO", control_id="r-1", payload=b"task-42")
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.REWIND_TO
    assert msg.payload == {"task_id": "task-42"}

    await bridge.stop()


# ----------------------------------------------------------------------
# Unsupported kinds
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "h_kind",
    # APPROVE and REJECT are now bridged to goldfive (landed alongside
    # goldfive #83). INJECT_MESSAGE, STATUS_QUERY, INTERCEPT_TRANSFER
    # remain out of scope and still ack UNSUPPORTED.
    ["INJECT_MESSAGE", "STATUS_QUERY", "INTERCEPT_TRANSFER"],
)
async def test_unsupported_kind_acks_unsupported(
    client: Client, made: list[FakeTransport], h_kind: str
) -> None:
    runner = _FakeRunner()
    bridge = ControlBridge(client, runner, asyncio.get_running_loop())
    bridge.start()

    transport = made[0]
    transport.deliver_control_event(
        _make_event(h_kind, control_id=f"u-{h_kind}", payload=b"anything")
    )

    # Give the events_loop a tick to process and push the ack.
    await _drain()

    # Runner's channel must not receive any message for unsupported kinds.
    assert runner.control is not None
    maybe = await runner.control.receive(timeout=0.05)
    assert maybe is None

    assert len(transport.sent_acks) == 1
    ack_id, result, detail = transport.sent_acks[0]
    assert ack_id == f"u-{h_kind}"
    assert result == "unsupported"
    assert detail  # non-empty explanatory string

    await bridge.stop()


# ----------------------------------------------------------------------
# Ack round-trip
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "g_result, expected_name",
    [
        (AckResult.SUCCESS, "success"),
        (AckResult.FAILURE, "failure"),
        (AckResult.UNSUPPORTED, "unsupported"),
    ],
)
async def test_goldfive_ack_flows_back_to_server(
    client: Client,
    made: list[FakeTransport],
    g_result: AckResult,
    expected_name: str,
) -> None:
    runner = _FakeRunner()
    bridge = ControlBridge(client, runner, asyncio.get_running_loop())
    bridge.start()

    assert runner.control is not None
    await runner.control.ack(
        ControlAck(control_id="ack-1", result=g_result, detail="because reasons")
    )

    # Give acks_loop a chance to see it and push to the fake transport.
    await _drain()

    transport = made[0]
    assert any(
        aid == "ack-1" and res == expected_name and det == "because reasons"
        for aid, res, det in transport.sent_acks
    ), transport.sent_acks

    await bridge.stop()


# ----------------------------------------------------------------------
# Runner.close shutdown path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_close_tears_down_bridge(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _FakeRunner()
    bridge = ControlBridge(client, runner, asyncio.get_running_loop())
    bridge.start()

    # Precondition — forward hook installed, tasks running.
    transport = made[0]
    assert transport.control_forward is not None
    assert bridge._events_task is not None
    assert not bridge._events_task.done()
    assert bridge._acks_task is not None
    assert not bridge._acks_task.done()

    await runner.close()

    # The wrapped close first awaits bridge.stop, then the original.
    assert runner.close_calls == 1
    assert transport.control_forward is None
    assert bridge._events_task.done()
    assert bridge._acks_task.done()
    assert bridge._closed is True


# ----------------------------------------------------------------------
# observe() integration
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_attaches_bridge_inside_event_loop(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _FakeRunner()
    observe(runner, client=client)

    bridge = getattr(runner, "_harmonograf_control_bridge", None)
    assert isinstance(bridge, ControlBridge)
    assert runner.control is not None  # attached by the bridge

    # Forward a STEER event end-to-end through observe's bridge.
    made[0].deliver_control_event(
        _make_event("STEER", control_id="o-1", payload=b"try again")
    )
    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.STEER
    assert msg.payload == {"note": "try again"}

    await runner.close()
    assert bridge._closed is True


def test_observe_without_running_loop_skips_bridge(
    client: Client,
) -> None:
    """observe() stays usable from sync call sites — just no bridge."""
    runner = _FakeRunner()
    observe(runner, client=client)

    # Sink still attached (observability path unchanged).
    assert len(runner.sinks) == 1
    # Bridge deliberately not started outside an event loop.
    assert getattr(runner, "_harmonograf_control_bridge", None) is None


# ----------------------------------------------------------------------
# Kind map invariant
# ----------------------------------------------------------------------


def test_kind_map_only_contains_goldfive_phase1_kinds() -> None:
    """If goldfive adds/removes a Phase-1 kind, fail loudly here."""
    assert set(_KIND_MAP.values()) == {k.value for k in ControlKind}
