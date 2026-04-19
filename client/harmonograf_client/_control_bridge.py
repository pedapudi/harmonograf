"""Bridge harmonograf ``ControlEvent``\\s into a goldfive ``ControlChannel``.

The harmonograf server delivers control events over a dedicated
``SubscribeControl`` gRPC stream; goldfive's :class:`goldfive.control.ControlChannel`
is the in-process primitive a :class:`goldfive.Runner` consumes live
steering messages from. :class:`ControlBridge` is the wire between them.

Responsibilities:

1. Attach a :class:`ControlChannel` to the runner if one isn't already
   present (``runner.control`` / ``runner._control``).
2. Intercept every raw ``ControlEvent`` from the client (via
   :meth:`Client.set_control_forward`) and translate it to a goldfive
   :class:`ControlMessage`, preserving ``control_id`` as the message
   ``id`` for ack correlation.
3. Forward the ``ControlMessage`` into ``runner.control`` on the user's
   event loop (the transport delivers events on its own loop, so a
   thread-safe hop is required).
4. Mirror goldfive acks back out as harmonograf ``ControlAck`` frames
   via :meth:`Client.send_control_ack`.
5. Tear down cleanly when :meth:`Runner.close` is called — forward
   hook uninstalled, both forwarding tasks cancelled, and the goldfive
   channel closed so any in-flight ``runner.control.acks()`` consumer
   exits its iterator.

Phase-1 kind coverage mirrors goldfive issue #71: PAUSE, RESUME, CANCEL,
STEER, REWIND_TO translate one-to-one. INJECT_MESSAGE, APPROVE, REJECT
are out of scope and ack UNSUPPORTED directly back to the server. Any
kind goldfive does not know (e.g. STATUS_QUERY, INTERCEPT_TRANSFER —
both legal harmonograf kinds) also acks UNSUPPORTED.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("harmonograf_client._control_bridge")


# harmonograf ControlKind name → goldfive ControlKind name.
# Any harmonograf kind not in this table is acked UNSUPPORTED. Membership
# here is a necessary but not sufficient condition — the goldfive enum
# may still reject the value (e.g. if goldfive drops a kind in a later
# release), in which case we also ack UNSUPPORTED.
_KIND_MAP: dict[str, str] = {
    "PAUSE": "PAUSE",
    "RESUME": "RESUME",
    "CANCEL": "CANCEL",
    "STEER": "STEER",
    "REWIND_TO": "REWIND_TO",
    "APPROVE": "APPROVE",
    "REJECT": "REJECT",
}


class ControlBridge:
    """Wire harmonograf control events into a goldfive ``ControlChannel``.

    The bridge is single-use: :meth:`start` installs the forward hook
    and spawns two asyncio tasks (one for incoming events, one for
    outgoing acks); :meth:`stop` tears everything down. ``observe()``
    also monkey-patches ``runner.close`` so the bridge shuts down when
    the runner is closed by its owner — no manual wiring required from
    the user.
    """

    def __init__(
        self,
        client: "Client",
        runner: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._client = client
        self._runner = runner
        self._loop = loop
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._events_task: Optional[asyncio.Task] = None
        self._acks_task: Optional[asyncio.Task] = None
        self._started = False
        self._closed = False
        self._original_close: Any = None

    def start(self) -> None:
        """Attach a ``ControlChannel`` and spin up forwarding tasks.

        Idempotent — calling ``start`` twice is a no-op. Safe to call
        from the user's event loop; all three side effects (attach
        channel, install forward hook, wrap ``runner.close``) are
        synchronous.
        """
        if self._started:
            return
        self._started = True

        # Lazy-import goldfive so the module stays cheap to import in
        # tests that never use the bridge.
        from goldfive.control import ControlChannel

        # Attach a channel if the runner doesn't have one. Store on
        # both the public attribute (for user code + tests) and the
        # private one in case a future Runner version ever protects
        # the field.
        if getattr(self._runner, "control", None) is None:
            chan = ControlChannel()
            try:
                self._runner.control = chan
            except AttributeError:
                self._runner._control = chan  # type: ignore[attr-defined]

        self._client.set_control_forward(self._on_event_from_transport)
        self._events_task = self._loop.create_task(self._events_loop())
        self._acks_task = self._loop.create_task(self._acks_loop())
        self._wrap_runner_close()

    # ------------------------------------------------------------------
    # Transport-thread callback
    # ------------------------------------------------------------------

    def _on_event_from_transport(self, event: Any) -> None:
        """Hand off a raw ``ControlEvent`` to our loop (thread-safe)."""
        try:
            self._loop.call_soon_threadsafe(self._inbox.put_nowait, event)
        except RuntimeError:
            # Our loop is shutting down — dropping is correct; the
            # server will re-send on next subscribe.
            pass

    # ------------------------------------------------------------------
    # Forwarding coroutines
    # ------------------------------------------------------------------

    async def _events_loop(self) -> None:
        from goldfive.control import ControlKind, ControlMessage

        from .pb import types_pb2

        while True:
            try:
                event = await self._inbox.get()
            except asyncio.CancelledError:
                return

            h_kind_name = _h_kind_name(event.kind, types_pb2)
            g_kind_name = _KIND_MAP.get(h_kind_name)
            if g_kind_name is None:
                self._client.send_control_ack(
                    event.id,
                    "unsupported",
                    f"harmonograf ControlKind {h_kind_name} "
                    "is not supported by this bridge",
                )
                continue

            try:
                g_kind = ControlKind(g_kind_name)
            except ValueError:
                self._client.send_control_ack(
                    event.id,
                    "unsupported",
                    f"goldfive has no ControlKind.{g_kind_name}",
                )
                continue

            payload = _decode_payload(h_kind_name, event.payload)
            msg = ControlMessage(kind=g_kind, id=event.id, payload=payload)
            chan = getattr(self._runner, "control", None)
            if chan is None:
                self._client.send_control_ack(
                    event.id, "failure", "runner has no control channel"
                )
                continue
            try:
                await chan.send(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to forward control to runner: %s", exc)
                self._client.send_control_ack(event.id, "failure", repr(exc))

    async def _acks_loop(self) -> None:
        chan = getattr(self._runner, "control", None)
        if chan is None:
            return
        try:
            async for ack in chan.acks():
                result_name = _ack_result_name(ack.result)
                self._client.send_control_ack(
                    ack.control_id, result_name, getattr(ack, "detail", "") or ""
                )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("ack forwarding loop failed")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _wrap_runner_close(self) -> None:
        """Monkey-patch ``runner.close`` so it also stops the bridge.

        Goldfive's :meth:`Runner.close` is async and closes sinks; we
        chain ``stop()`` in front so the bridge tears down first. The
        original callable is stashed so a stop-only callsite still
        invokes it.
        """
        original_close = getattr(self._runner, "close", None)
        if original_close is None or not callable(original_close):
            return
        self._original_close = original_close
        bridge = self

        async def close_with_bridge_teardown() -> None:
            try:
                await bridge.stop()
            finally:
                await original_close()

        try:
            self._runner.close = close_with_bridge_teardown  # type: ignore[method-assign]
        except AttributeError:
            # Runner type forbids reassignment — best-effort, owner can
            # still call ``bridge.stop()`` explicitly.
            pass

    async def stop(self) -> None:
        """Cancel forwarding tasks, uninstall the hook, close the channel.

        Idempotent. Safe to call from any coroutine running on the same
        loop the bridge was started on.
        """
        if self._closed:
            return
        self._closed = True
        self._client.set_control_forward(None)

        tasks = [t for t in (self._events_task, self._acks_task) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t

        chan = getattr(self._runner, "control", None)
        if chan is not None:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _h_kind_name(kind_value: int, types_pb2: Any) -> str:
    """Return the short harmonograf ``ControlKind`` name (e.g. ``PAUSE``)."""
    try:
        raw = types_pb2.ControlKind.Name(kind_value)
    except Exception:
        return "UNSPECIFIED"
    return raw.removeprefix("CONTROL_KIND_")


def _ack_result_name(result: Any) -> str:
    """Map a goldfive ``AckResult`` to the lowercase harmonograf wire name."""
    # AckResult is a StrEnum; its value is the uppercase name.
    try:
        return str(result.value).lower()
    except AttributeError:
        return str(result).split(".")[-1].lower()


def _decode_payload(h_kind_name: str, payload: bytes) -> dict[str, Any]:
    """Decode a harmonograf control payload into goldfive's dict shape.

    Harmonograf delivers payloads as ``bytes`` (see ``ControlEvent.payload``
    in ``proto/harmonograf/v1/types.proto``); goldfive's
    :class:`ControlMessage` expects a ``dict``. The shape per kind is
    defined in goldfive issue #71.
    """
    if not payload:
        return {}
    text = payload.decode("utf-8", errors="replace")
    if h_kind_name == "STEER":
        return {"note": text}
    if h_kind_name == "REWIND_TO":
        return {"task_id": text}
    return {"data": text}
