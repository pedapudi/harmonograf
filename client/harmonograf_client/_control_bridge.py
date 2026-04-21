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
   and hand it off to the bound goldfive :class:`ControlChannel`. The
   proto's ``id`` becomes the message ``id`` so acks correlate
   end-to-end.
2. Mirror goldfive :class:`ControlAck` objects back out as harmonograf
   ``ControlAck`` frames via :meth:`Client.send_control_ack`. The ack
   wire type is also goldfive's now, but the bridge still hops across
   transport+loop boundaries so callers on either side can stay naive.
3. Tear down cleanly when :meth:`stop` is called — forward hook
   uninstalled, both forwarding tasks cancelled, and the goldfive
   channel closed so any in-flight ``channel.acks()`` consumer exits
   its iterator.

The bridge operates directly on a :class:`ControlChannel` — it is the
caller's responsibility to attach that channel to the runner (via
:attr:`Runner.control`, or by passing ``control=`` to
:func:`goldfive.wrap`). Two helpers wire this up:

* :func:`harmonograf_client.observe` — for code paths that own a
  :class:`goldfive.Runner` directly. ``observe`` attaches a channel to
  ``runner.control``, builds a bridge, and registers ``bridge.stop`` as
  a runner close hook.
* :func:`harmonograf_client.control_channel` — for code paths that hand
  a wrapped ADK agent to ``App(root_agent=...)`` and therefore never see
  the underlying :class:`Runner`. ``control_channel`` returns a bare
  :class:`ControlChannel` backed by a live bridge; pass it to
  :func:`goldfive.wrap` via ``control=``. See harmonograf#55.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from goldfive.control import ControlChannel

    from .client import Client

log = logging.getLogger("harmonograf_client._control_bridge")


class ControlBridge:
    """Wire harmonograf control events into a goldfive ``ControlChannel``.

    The bridge is single-use: :meth:`start` installs the forward hook
    and spawns two asyncio tasks (one for incoming events, one for
    outgoing acks); :meth:`stop` tears everything down.

    Parameters
    ----------
    client:
        The :class:`Client` whose ``SubscribeControl`` stream feeds
        events in and whose ``ControlAck`` stream carries acks back out.
    channel:
        The goldfive :class:`ControlChannel` the bridge forwards events
        onto (and drains acks from). The caller is responsible for
        keeping this channel attached to a runner (either via
        ``runner.control = channel`` or ``goldfive.wrap(control=channel,
        ...)``) for the bridge's lifetime.
    loop:
        The asyncio loop the forwarding tasks run on. Must be the loop
        the runner is driven by.
    """

    def __init__(
        self,
        client: "Client",
        channel: "ControlChannel",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._client = client
        self._channel = channel
        self._loop = loop
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._events_task: Optional[asyncio.Task] = None
        self._acks_task: Optional[asyncio.Task] = None
        self._started = False
        self._closed = False

    @property
    def channel(self) -> "ControlChannel":
        """The goldfive :class:`ControlChannel` this bridge forwards onto."""
        return self._channel

    def start(self) -> None:
        """Install the forward hook and spin up forwarding tasks.

        Idempotent — calling ``start`` twice is a no-op.
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

            try:
                await self._channel.send(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to forward control to channel: %s", exc)
                self._client.send_control_ack(msg.id, "failure", repr(exc))

    async def _acks_loop(self) -> None:
        try:
            async for ack in self._channel.acks():
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
        :meth:`Runner.add_close_hook` by :func:`observe`, or driven
        explicitly by the caller when the bridge backs a standalone
        channel (:func:`control_channel`).
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

        try:
            self._channel.close()
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
