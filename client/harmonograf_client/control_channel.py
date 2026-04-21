"""Standalone control channel factory for the ``adk web`` code path.

Under ``adk web`` (and more broadly, anywhere callers hand a wrapped ADK
agent to ``App(root_agent=...)`` instead of driving a
:class:`goldfive.Runner` themselves), :func:`observe` cannot be used:
there is no :class:`Runner` instance to attach a :class:`ControlBridge`
to before ``App`` takes ownership of the wrapped agent.

:func:`control_channel` plugs the gap. It builds a goldfive
:class:`ControlChannel` up front, wires a live :class:`ControlBridge`
into it against the caller's :class:`Client`, and returns the channel so
it can be passed into :func:`goldfive.wrap` via ``control=``::

    client = harmonograf_client.Client(...)
    channel = harmonograf_client.control_channel(client)
    wrapped = goldfive.wrap(tree, control=channel, ...)
    app = App(root_agent=wrapped, plugins=[HarmonografTelemetryPlugin(client)])

Once ``adk web`` drives ``wrapped`` the returned channel forwards every
STEER / PAUSE / CANCEL / APPROVE / REJECT event from the harmonograf UI
into the goldfive steerer / runner. See harmonograf#55 for the bug this
fixes — before this helper, ``HarmonografTelemetryPlugin`` wired only
the span sink and the control bridge was never installed, so steers
returned ``delivery=FAILURE``.

Unlike :func:`observe`, this helper does NOT register a close hook
(there is no runner to register against). The bridge lives for as long
as the caller's event loop survives; when the caller is done the
returned channel's ``close()`` method (or
:meth:`ControlBridge.stop`, if the caller held onto it via the
``_harmonograf_control_bridge`` attribute on the channel) shuts things
down cleanly. For the common ``adk web`` lifecycle this is handled by
the client's own atexit shutdown — the forwarding tasks are daemon-ish
(cancelled when the loop tears down) and no resource leaks if they
outlive a run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ._control_bridge import ControlBridge

if TYPE_CHECKING:
    from goldfive.control import ControlChannel

    from .client import Client

log = logging.getLogger("harmonograf_client.control_channel")


def control_channel(client: "Client") -> "ControlChannel":
    """Return a :class:`ControlChannel` that receives steers from harmonograf.

    The returned channel is backed by a live :class:`ControlBridge` that
    bridges ``client``'s ``SubscribeControl`` gRPC stream onto the
    channel's inbox, and mirrors goldfive :class:`ControlAck` objects
    back out as harmonograf ``ControlAck`` frames. Pass the channel to
    :func:`goldfive.wrap` via ``control=``::

        channel = harmonograf_client.control_channel(client)
        wrapped = goldfive.wrap(tree, control=channel, ...)

    Must be called from within a running asyncio event loop — the bridge
    needs a loop to consume events on.

    Parameters
    ----------
    client:
        The :class:`Client` whose control stream feeds the returned
        channel. Typically the same :class:`Client` passed to
        :class:`HarmonografTelemetryPlugin` so spans and steers share
        identity.

    Returns
    -------
    ControlChannel
        A goldfive :class:`ControlChannel` with a live bridge attached.
        The bridge is stashed on the channel as
        ``channel._harmonograf_control_bridge`` so callers (and tests)
        that need explicit teardown can drive :meth:`ControlBridge.stop`.

    Notes
    -----
    The returned channel is not attached to any :class:`Runner` — the
    caller is responsible for passing it to :func:`goldfive.wrap` via
    ``control=`` (or assigning it to :attr:`Runner.control` directly).
    Attaching the same channel to multiple runners is undefined.
    """
    from goldfive.control import ControlChannel

    channel = ControlChannel()
    loop = asyncio.get_running_loop()
    bridge = ControlBridge(client, channel, loop)
    bridge.start()

    # Stash the bridge on the channel so tests and advanced callers can
    # drive ``bridge.stop`` for deterministic teardown. Underscore prefix
    # flags this as private plumbing, not part of ControlChannel's
    # public contract.
    channel._harmonograf_control_bridge = bridge  # type: ignore[attr-defined]

    return channel


__all__ = ["control_channel"]
