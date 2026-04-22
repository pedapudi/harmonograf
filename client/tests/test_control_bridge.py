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
    steer_author: str = "",
    steer_annotation_id: str = "",
    rewind_task_id: str | None = None,
) -> gf_control_pb2.ControlEvent:
    """Build a goldfive ``ControlEvent`` proto with the requested kind.

    STEER gets a non-empty default note so bridge-level body validation
    (goldfive#171) doesn't reject it; tests that want to exercise the
    reject path pass ``steer_note`` explicitly (e.g. ``""``).
    """
    kind_enum = getattr(gf_control_pb2, f"CONTROL_KIND_{kind_name}")
    ev = gf_control_pb2.ControlEvent(id=control_id, kind=kind_enum)
    if kind_name == "STEER" and steer_note is None:
        steer_note = "nudge"
    if steer_note is not None:
        ev.steer.note = steer_note
    if steer_author:
        ev.steer.author = steer_author
    if steer_annotation_id:
        ev.steer.annotation_id = steer_annotation_id
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


# ----------------------------------------------------------------------
# goldfive#171 — STEER body validation + author propagation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_empty_body_acked_failure_not_forwarded(
    client: Client, made: list[FakeTransport]
) -> None:
    """Empty STEER body → FAILURE ack, no forward to runner."""
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-empty", steer_note="")
    )
    await _drain()

    # Ack bounced back as failure with the specific detail.
    assert ("s-empty", "failure", "body empty") in made[0].sent_acks
    # Runner must NOT have received the message.
    msg = await asyncio.wait_for(
        asyncio.shield(asyncio.ensure_future(runner.control.receive(timeout=0.05))),
        timeout=1.0,
    )
    assert msg is None

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_whitespace_body_acked_failure_not_forwarded(
    client: Client, made: list[FakeTransport]
) -> None:
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-ws", steer_note="   \t\n  ")
    )
    await _drain()

    assert ("s-ws", "failure", "body empty") in made[0].sent_acks
    msg = await runner.control.receive(timeout=0.05)
    assert msg is None

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_oversized_body_acked_failure_not_forwarded(
    client: Client, made: list[FakeTransport]
) -> None:
    """A 12KB body is rejected with the exact length in the detail string."""
    from harmonograf_client._control_bridge import STEER_BODY_MAX_BYTES

    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    # 12KB body — well above the 8KB cap.
    oversized = "x" * (12 * 1024)
    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-big", steer_note=oversized)
    )
    await _drain()

    # Find the ack and assert shape + len in detail.
    matching = [
        (cid, res, det)
        for cid, res, det in made[0].sent_acks
        if cid == "s-big"
    ]
    assert len(matching) == 1
    cid, res, det = matching[0]
    assert res == "failure"
    assert det.startswith("body too long")
    assert str(len(oversized.encode("utf-8"))) in det
    # Runner saw nothing.
    msg = await runner.control.receive(timeout=0.05)
    assert msg is None
    assert len(oversized) > STEER_BODY_MAX_BYTES  # sanity

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_body_at_cap_forwards_successfully(
    client: Client, made: list[FakeTransport]
) -> None:
    """Exactly-at-cap body passes validation and reaches the runner."""
    from harmonograf_client._control_bridge import STEER_BODY_MAX_BYTES

    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    at_cap = "a" * STEER_BODY_MAX_BYTES
    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-cap", steer_note=at_cap)
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg is not None
    assert msg.kind is ControlKind.STEER
    assert msg.payload["note"] == at_cap

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_body_control_chars_stripped_before_forward(
    client: Client, made: list[FakeTransport]
) -> None:
    """ASCII control chars (except tab/newline) are removed from the body."""
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    # Bel, backspace, escape, DEL(127 — printable-ish, kept), plus kept tab + newline.
    raw = "hello\x07\x08world\x1b\nmore\tend"
    made[0].deliver_control_event(
        _make_event("STEER", control_id="s-scrub", steer_note=raw)
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg is not None
    # Bel (7), backspace (8), escape (27) stripped; tab (9), newline (10) preserved.
    assert msg.payload["note"] == "helloworld\nmore\tend"

    await bridge.stop()


@pytest.mark.asyncio
async def test_steer_author_and_annotation_id_forwarded_to_channel(
    client: Client, made: list[FakeTransport]
) -> None:
    """``author`` and ``annotation_id`` on SteerPayload land in ControlMessage.payload."""
    runner = _runner_with_channel()
    bridge = ControlBridge(client, runner.control, asyncio.get_running_loop())
    bridge.start()

    made[0].deliver_control_event(
        _make_event(
            "STEER",
            control_id="s-auth",
            steer_note="pivot",
            steer_author="alice",
            steer_annotation_id="ann_xyz",
        )
    )

    msg = await asyncio.wait_for(runner.control.receive(), timeout=1.0)
    assert msg is not None
    assert msg.payload["note"] == "pivot"
    assert msg.payload["author"] == "alice"
    assert msg.payload["annotation_id"] == "ann_xyz"

    await bridge.stop()


def test_validate_steer_body_unit() -> None:
    """Unit test the validation helper directly."""
    from harmonograf_client._control_bridge import (
        STEER_BODY_MAX_BYTES,
        _validate_steer_body,
    )

    assert _validate_steer_body("") == (False, "body empty")
    assert _validate_steer_body("   ") == (False, "body empty")
    assert _validate_steer_body("\t\n ") == (False, "body empty")
    assert _validate_steer_body("ok") == (True, "")
    too_long = "x" * (STEER_BODY_MAX_BYTES + 1)
    ok, detail = _validate_steer_body(too_long)
    assert ok is False
    assert detail.startswith("body too long")
    # Multibyte: 3-byte char × 3000 > 8192 bytes.
    big_unicode = "☃" * 3000
    ok, detail = _validate_steer_body(big_unicode)
    assert ok is False
    assert detail.startswith("body too long")


def test_sanitise_steer_body_unit() -> None:
    """Unit test the scrub helper directly."""
    from harmonograf_client._control_bridge import _sanitise_steer_body

    # Empty passes through.
    assert _sanitise_steer_body("") == ""
    # Bell, backspace, escape dropped; tab + newline kept.
    assert _sanitise_steer_body("a\x07b\x08c\x1bd") == "abcd"
    assert _sanitise_steer_body("a\tb\nc") == "a\tb\nc"
    # \r dropped.
    assert _sanitise_steer_body("a\r\nb") == "a\nb"
    # Non-ASCII passes through unchanged.
    assert _sanitise_steer_body("héllo☃") == "héllo☃"
    # Printable ASCII unchanged.
    assert _sanitise_steer_body("regular text!") == "regular text!"
