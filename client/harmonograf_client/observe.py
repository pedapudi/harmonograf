"""Explicit observability-only helper for goldfive Runners.

``harmonograf_client.observe(runner)`` attaches a :class:`HarmonografSink`
to an existing :class:`goldfive.Runner`. It does **not** modify planning,
steering, goal derivation, or execution — those concerns belong to
``goldfive.wrap``. The two responsibilities stay crystal-clear::

    import goldfive
    import harmonograf_client

    runner = harmonograf_client.observe(goldfive.wrap(root_agent))
    outcome = await runner.run("make a presentation about waffles")

See issue #22 for the motivation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from .client import Client
from .sink import HarmonografSink

if TYPE_CHECKING:
    import goldfive

log = logging.getLogger("harmonograf_client.observe")


def observe(
    runner: "goldfive.Runner",
    *,
    client: Client | None = None,
    name: str | None = None,
    framework: str = "CUSTOM",
    server_addr: str | None = None,
) -> "goldfive.Runner":
    """Attach a :class:`HarmonografSink` to ``runner`` and return it.

    This helper is observability-only. It mutates ``runner.sinks`` by
    appending a single :class:`HarmonografSink`; it never touches the
    planner, steerer, executor, goal deriver, or any other orchestration
    component.

    Parameters
    ----------
    runner:
        An existing :class:`goldfive.Runner`, typically produced by
        :func:`goldfive.wrap`.
    client:
        Optional pre-built :class:`Client` to reuse (e.g. shared across
        multiple runners). When omitted, a new ``Client`` is constructed
        from ``name`` / ``framework`` / ``server_addr``.
    name:
        Client display name. Defaults to ``"agent"``. Ignored when
        ``client`` is provided.
    framework:
        Client framework tag (shown in the UI). Defaults to
        ``"CUSTOM"``. Ignored when ``client`` is provided.
    server_addr:
        Harmonograf server address. When omitted, reads the
        ``HARMONOGRAF_SERVER`` environment variable; falls back to
        ``Client``'s own default (``127.0.0.1:7531``). Ignored when
        ``client`` is provided.

    Returns
    -------
    The same ``runner`` object (mutated), so callers can chain the two
    idiomatic calls on one line::

        runner = harmonograf_client.observe(goldfive.wrap(agent))

    Notes
    -----
    Calling ``observe`` twice on the same runner appends two sinks.
    That's deliberate — the caller is responsible for deciding whether
    deduping makes sense in their context.
    """
    if client is None:
        resolved_addr = server_addr or os.environ.get("HARMONOGRAF_SERVER")
        client_kwargs: dict[str, Any] = {
            "name": name or "agent",
            "framework": framework,
        }
        if resolved_addr:
            client_kwargs["server_addr"] = resolved_addr
        client = Client(**client_kwargs)

    runner.sinks.append(HarmonografSink(client))

    # Attach a goldfive ControlChannel bridge so pause/resume/cancel/
    # steer/rewind issued from the harmonograf UI reach the live runner.
    # The bridge needs a running asyncio loop to consume events on; when
    # ``observe()`` is called outside async context (e.g. from a sync
    # script), we skip it — the sink half of ``observe`` still works.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        from ._control_bridge import ControlBridge

        bridge = ControlBridge(client, runner, loop)
        bridge.start()
        # Stash for test + introspection access. Underscore prefix so
        # it's clear this is private plumbing, not part of the Runner's
        # public contract.
        runner._harmonograf_control_bridge = bridge  # type: ignore[attr-defined]
    else:
        log.debug(
            "observe(): no running event loop — control bridge not started"
        )

    return runner
