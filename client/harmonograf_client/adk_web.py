"""Convenience bundle for ``adk web`` observability wiring.

Under ``adk web`` every orchestrated demo needs the same three
hook-ups from a :class:`Client`:

* :class:`HarmonografTelemetryPlugin` — ADK plugin that emits per-span
  telemetry (the ``plugins=`` kwarg on :class:`App` and
  :func:`goldfive.wrap`).
* :class:`HarmonografSink` — goldfive :class:`EventSink` that ships
  plan / task / drift events to harmonograf (the ``sinks=`` kwarg on
  :func:`goldfive.wrap`). Without this, harmonograf's Trajectory view
  shows "no plan yet" and the Task panel stays at "0 plans · 0 tasks"
  even while goldfive fires the full event stream — see
  harmonograf#57.
* :func:`control_channel` — bridges the UI's STEER / PAUSE / CANCEL /
  APPROVE / REJECT events back into goldfive's steerer (the
  ``control=`` kwarg on :func:`goldfive.wrap`). Requires a running
  event loop — see harmonograf#55 / harmonograf#56.

Spelling these out one at a time is easy to get wrong: the demo in
``tests/reference_agents/presentation_agent_orchestrated`` originally
wired plugin + control but forgot the sink, exactly the shape of bug
harmonograf#57 tracks. This helper bundles the three so every future
``adk web`` caller gets all three by construction::

    from harmonograf_client import adk_web_observability

    bundle = adk_web_observability(client)
    wrapped = goldfive.wrap(
        tree,
        planner=planner,
        goal_deriver=goal_deriver,
        plugins=[bundle.plugin],
        sinks=[bundle.sink],
        control=bundle.control,
    )
    app = App(root_agent=wrapped, plugins=[bundle.plugin])

``control_channel`` needs a running event loop. The bundle helper
mirrors the fallback the demo already handled: when no loop is
running, ``bundle.control`` is ``None`` and a warning is logged. The
plugin + sink are always populated — they do not depend on a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .control_channel import control_channel as _control_channel
from .sink import HarmonografSink
from .telemetry_plugin import HarmonografTelemetryPlugin

if TYPE_CHECKING:
    from goldfive.control import ControlChannel

    from .client import Client

log = logging.getLogger("harmonograf_client.adk_web")


@dataclass(frozen=True)
class AdkWebObservability:
    """Bundle of the three ``adk web`` observability hook-ups.

    Attributes
    ----------
    plugin:
        :class:`HarmonografTelemetryPlugin` — pass to ``plugins=`` on
        :class:`App` and :func:`goldfive.wrap`.
    sink:
        :class:`HarmonografSink` — pass to ``sinks=`` on
        :func:`goldfive.wrap`. The missing piece that harmonograf#57
        tracked.
    control:
        :class:`ControlChannel` or ``None``. ``None`` when
        :func:`adk_web_observability` is called outside a running
        event loop; a warning is logged in that case and the demo
        should fall through to ``control=None`` on :func:`goldfive.wrap`
        (steers will return ``delivery=FAILURE`` until the loop path
        is taken — same fallback as the pre-bundle demo).
    """

    plugin: HarmonografTelemetryPlugin
    sink: HarmonografSink
    control: Optional["ControlChannel"]


def adk_web_observability(client: "Client") -> AdkWebObservability:
    """Return the three ``adk web`` observability hook-ups for ``client``.

    Bundles :class:`HarmonografTelemetryPlugin`,
    :class:`HarmonografSink`, and :func:`control_channel` so every
    orchestrated demo wires all three at once. This is the shape the
    ``presentation_agent_orchestrated`` demo uses — see that module for
    an end-to-end example.

    Parameters
    ----------
    client:
        The :class:`Client` whose transport backs all three hook-ups.
        Span frames (from the plugin), goldfive events (from the
        sink), and control acks (from the channel bridge) share the
        same ``agent_id`` and stream so harmonograf correlates them
        into one run on the UI.

    Returns
    -------
    AdkWebObservability
        A frozen dataclass with ``plugin``, ``sink``, and ``control``.
        ``control`` is ``None`` when no asyncio loop is running (the
        only failure mode :func:`control_channel` raises on); the
        plugin and sink are always populated.

    Notes
    -----
    The caller owns ``client``'s lifecycle. The bundle does not
    register an atexit hook or take ownership of shutdown — call
    ``client.shutdown()`` when tearing down.
    """
    plugin = HarmonografTelemetryPlugin(client)
    sink = HarmonografSink(client)
    control: Optional["ControlChannel"]
    try:
        control = _control_channel(client)
    except RuntimeError as e:
        # ``control_channel`` raises when no asyncio loop is running
        # (``asyncio.get_running_loop()``). Mirror the demo's existing
        # fallback — log and return control=None so sink + plugin are
        # still usable in synchronous contexts.
        log.warning(
            "adk_web_observability: control_channel skipped (no running "
            "loop): %s; steers will return delivery=FAILURE until the "
            "caller rebuilds the bundle inside an event loop",
            e,
        )
        control = None
    return AdkWebObservability(plugin=plugin, sink=sink, control=control)


__all__ = ["AdkWebObservability", "adk_web_observability"]
