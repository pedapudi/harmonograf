"""Tests for :class:`harmonograf_client._control_bridge.ControlBridge`.

The bridge sits between harmonograf's ``SubscribeControl`` gRPC stream
and a goldfive ``ControlChannel`` attached to a :class:`Runner`. After
the harmonograf #37 control-proto consolidation, the wire event IS a
``goldfive.v1.ControlEvent`` — the bridge is pure transport, no enum
translation and no bytes-payload decoding.

Scope covered here:

- Every Phase-1 kind (PAUSE / RESUME / CANCEL / STEER / REWIND_TO /
  APPROVE / REJECT) arrives at the goldfive channel with kind + payload
  intact and the proto id preserved for ack correlation.
- ``STEER`` and ``REWIND_TO`` payloads land in the goldfive
  ``ControlMessage.payload`` dict via the typed oneof (``note`` for
  STEER, ``task_id`` for REWIND_TO).
- ``INJECT_MESSAGE`` and ``INTERCEPT_TRANSFER`` ride through end-to-end
  as valid goldfive kinds (goldfive understands them even if the runner
  may choose to ack UNSUPPORTED).
- Goldfive ``ControlAck`` objects published via ``channel.ack()`` flow
  back out to the server as harmonograf ``ControlAck``\\s with the
  right result enum and detail string.
- ``observe(runner)`` attaches a bridge whose teardown fires via
  ``runner.close()``'s close hook (forward hook cleared, forwarding
  tasks finished).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from goldfive.control import (
    AckResult,
    ControlAck,
    ControlChannel,
    ControlKind,
)
from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_client import observe
from harmonograf_client._control_bridge import ControlBridge
from harmonograf_client.client import Client

from tests._fixtures import FakeTransport, make_factory


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_event(
    kind_name: str,
    *,
    control_id: str = "c-1",
    steer_note: str | None = None,
    rewind_task_id: str | None = None,
) -> gf_control_pb2.ControlEvent:
    """Build a goldfive ``ControlEvent`` proto with the requested kind."""
    kind_enum = getattr(gf_control_pb2, f"CONTROL_KIND_{kind_name}")
    ev = gf_control_pb2.ControlEvent(id=control_id, kind=kind_enum)
    if steer_note is not None:
        ev.steer.note = steer_note
    if rewind_task_id is not None:
        ev.rewind.task_id = rewind_task_id
    return ev


class _FakeRunner:
    """Stand-in for :class:`goldfive.Runner` — supports the extension API.

    Mirrors the narrow public surface ``observe()`` and the bridge rely
    on: ``sinks`` list, ``control`` attribute, ``add_sink``,
    ``add_close_hook``, and ``close`` that drives registered hooks.
    """

    def __init__(self) -> None:
        self.sinks: list[Any] = []
        self.control: ControlChannel | None = None
        self._close_hooks: list[Callable[[], Awaitable[None]]] = []
        self.close_calls = 0

    def add_sink(self, sink: Any) -> None:
        self.sinks.append(sink)

    def add_close_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._close_hooks.append(hook)

    async def close(self) -> None:
        self.close_calls += 1
        for hook in self._close_hooks:
            await hook()


def _runner_with_channel() -> _FakeRunner:
    """Build a fake runner pre-wired with a ControlChannel — mimics observe()."""
    runner = _FakeRunner()
    runner.control = ControlChannel()
    return runner


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
# Kind-by-kind pass-through
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
        ("APPROVE", ControlKind.APPROVE),
        ("REJECT", ControlKind.REJECT),
        ("INJECT_MESSAGE", ControlKind.INJECT_MESSAGE),
        ("INTERCEPT_TRANSFER", ControlKind.INTERCEPT_TRANSFER),
        ("STATUS_QUERY", ControlKind.STATUS_QUERY),
    ],
)
async def test_goldfive_event_forwards_to_channel(
    client: Client,
    made: list[FakeTransport],
    h_kind: str,
    expected_g_kind: ControlKind,
) -> None:
    runner = _runner_with_channel()
    loop = asyncio.get_running_loop()
    bridge = ControlBridge(client, runner.control, loop)
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
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-1", steer_note="focus on the last slide")
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.STEER
    assert msg.payload["note"] == "focus on the last slide"

    await bridge.stop()


@pytest.mark.asyncio
async def test_rewind_to_payload_lands_under_task_id(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("REWIND_TO", control_id="r-1", rewind_task_id="task-42")
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.REWIND_TO
    assert msg.payload == {"task_id": "task-42"}

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
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
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
async def test_runner_close_tears_down_bridge_via_close_hook(
    client: Client, made: list[FakeTransport]
) -> None:
    """``runner.close()`` runs registered close hooks — including ``bridge.stop``.

    Mirrors the wiring ``observe()`` sets up: attach a ControlChannel,
    start the bridge, register ``bridge.stop`` as a close hook. The
    teardown assertions match what the old monkey-patched close path
    guaranteed; only the wiring changed.
    """
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()
    runner.add_close_hook(bridge.stop)

    # Precondition — forward hook installed, tasks running.
    transport = made[0]
    assert transport.control_forward is not None
    assert bridge._events_task is not None
    assert not bridge._events_task.done()
    assert bridge._acks_task is not None
    assert not bridge._acks_task.done()

    await runner.close()

    # Close hook fired bridge.stop() — forwarding tasks are done and
    # the transport's forward hook is cleared.
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
    assert runner.control is not None  # attached by observe()

    # Forward a STEER event end-to-end through observe's bridge.
    made[0].deliver_control_event(
        _make_event("STEER", control_id="o-1", steer_note="try again")
    )
    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg.kind is ControlKind.STEER
    assert msg.payload["note"] == "try again"

    await runner.close()
    assert bridge._closed is True
