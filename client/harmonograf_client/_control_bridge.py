"""Bridge harmonograf ``SubscribeControl`` events into a goldfive ``ControlChannel``.

The harmonograf server delivers control events over a dedicated
``SubscribeControl`` gRPC stream; goldfive's :class:`goldfive.control.ControlChannel`
is the in-process primitive a :class:`goldfive.Runner` consumes live
steering messages from. :class:`ControlBridge` is the wire between them.

Since the harmonograf + goldfive control schemas were consolidated in
harmonograf #37 the wire event *is* a ``goldfive.v1.ControlEvent`` —
there is no harmonograf-owned enum to translate against, and no bytes
payload to decode. The bridge's responsibilities are now purely
transport:

1. Intercept every raw ``goldfive.v1.ControlEvent`` from the client (via
   :meth:`Client.set_control_forward`), convert it to a goldfive
   :class:`ControlMessage` with :func:`goldfive.conv.from_pb_control_event`,
   and hand it off to the runner's ``control`` channel. The proto's
   ``id`` becomes the message ``id`` so acks correlate end-to-end.
2. Mirror goldfive :class:`ControlAck` objects back out as harmonograf
   ``ControlAck`` frames via :meth:`Client.send_control_ack`. The ack
   wire type is also goldfive's now, but the bridge still hops across
   transport+loop boundaries so callers on either side can stay naive.
3. Tear down cleanly when :meth:`Runner.close` is called (via the close
   hook :func:`observe` registers) — forward hook uninstalled, both
   forwarding tasks cancelled, and the goldfive channel closed so any
   in-flight ``runner.control.acks()`` consumer exits its iterator.

The bridge does NOT attach a control channel to the runner — that is
:func:`observe`'s responsibility, done before :meth:`start` is called.
This keeps wiring in one place and lets the bridge assume
``runner.control`` is non-None for its entire lifetime.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger("harmonograf_client._control_bridge")


class ControlBridge:
    """Wire harmonograf control events into a goldfive ``ControlChannel``.

    The bridge is single-use: :meth:`start` installs the forward hook
    and spawns two asyncio tasks (one for incoming events, one for
    outgoing acks); :meth:`stop` tears everything down. The caller
    (:func:`observe`) is responsible for attaching a ``ControlChannel``
    to the runner BEFORE calling :meth:`start`, and for registering
    :meth:`stop` as a runner close hook.
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

    def start(self) -> None:
        """Install the forward hook and spin up forwarding tasks.

        Idempotent — calling ``start`` twice is a no-op. The runner must
        already have a ``ControlChannel`` attached via
        ``runner.control`` when this is called.
        """
        if self._started:
            return
        self._started = True

        self._client.set_control_forward(self._on_event_from_transport)
        self._events_task = self._loop.create_task(self._events_loop())
        self._acks_task = self._loop.create_task(self._acks_loop())

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
        from goldfive.conv import from_pb_control_event

        while True:
            try:
                event = await self._inbox.get()
            except asyncio.CancelledError:
                return

            try:
                msg = from_pb_control_event(event)
            except Exception as exc:  # noqa: BLE001
                # Malformed / unknown kind — ack UNSUPPORTED so the server
                # resolves the pending deliver instead of timing out.
                log.warning("failed to decode control event: %s", exc)
                self._client.send_control_ack(
                    getattr(event, "id", ""),
                    "unsupported",
                    f"failed to decode control event: {exc!r}",
                )
                continue

            chan = self._runner.control
            if chan is None:
                self._client.send_control_ack(
                    msg.id, "failure", "runner has no control channel"
                )
                continue
            try:
                await chan.send(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to forward control to runner: %s", exc)
                self._client.send_control_ack(msg.id, "failure", repr(exc))

    async def _acks_loop(self) -> None:
        chan = self._runner.control
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

    async def stop(self) -> None:
        """Cancel forwarding tasks, uninstall the hook, close the channel.

        Idempotent. Safe to call from any coroutine running on the same
        loop the bridge was started on. Intended to be registered as a
        :meth:`Runner.add_close_hook` by :func:`observe`.
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

        chan = self._runner.control
        if chan is not None:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ack_result_name(result: Any) -> str:
    """Map a goldfive ``AckResult`` to the lowercase harmonograf wire name."""
    # AckResult is a StrEnum; its value is the uppercase name.
    try:
        return str(result.value).lower()
    except AttributeError:
        return str(result).split(".")[-1].lower()
