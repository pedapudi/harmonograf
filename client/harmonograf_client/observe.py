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
from typing import TYPE_CHECKING, Any

from .client import Client
from .config import ClientConfig
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
    config: ClientConfig | None = None,
    install_adk_telemetry: bool = True,
) -> "goldfive.Runner":
    """Attach a :class:`HarmonografSink` to ``runner`` and return it.

    This helper is observability-only: it registers a
    :class:`HarmonografSink` via ``runner.add_sink(...)`` and wires a
    :class:`ControlBridge` so pause / resume / cancel / steer / rewind
    issued from the harmonograf UI reach the live runner. It never
    touches the planner, steerer, executor, goal deriver, or any other
    orchestration component.

    ``observe()`` must be called from within a running asyncio event
    loop — the bridge needs a loop to consume events on, and the
    bridge's teardown is registered as a :meth:`Runner.add_close_hook`
    so ``runner.close()`` tears the wire down cleanly.

    Parameters
    ----------
    runner:
        An existing :class:`goldfive.Runner`, typically produced by
        :func:`goldfive.wrap`.
    client:
        Optional pre-built :class:`Client` to reuse (e.g. shared across
        multiple runners). When omitted, a new ``Client`` is constructed
        from ``name`` / ``framework`` / ``config`` (or ``server_addr``).
    name:
        Client display name. Defaults to ``"agent"``. Ignored when
        ``client`` is provided.
    framework:
        Client framework tag (shown in the UI). Defaults to
        ``"CUSTOM"``. Ignored when ``client`` is provided.
    server_addr:
        Harmonograf server address. Takes precedence over
        ``config.server_addr`` when both are provided. When neither is
        given, falls back to :data:`ClientConfig.server_addr`'s default
        (``127.0.0.1:7531``). Ignored when ``client`` is provided.
    config:
        Optional :class:`ClientConfig` carrying explicit server address
        and identity root. When omitted, a default
        :class:`ClientConfig` is used (no env reads). Pass
        :meth:`ClientConfig.from_environ` to preserve the legacy
        ``HARMONOGRAF_SERVER`` / ``HARMONOGRAF_HOME`` behaviour. Ignored
        when ``client`` is provided.
    install_adk_telemetry:
        When ``True`` (default), best-effort install
        :class:`HarmonografTelemetryPlugin` on the runner so ADK
        lifecycle callbacks turn into harmonograf spans. The install is
        skipped silently when the runner doesn't carry an
        ``add_plugin`` hook (e.g. a non-ADK runner) and any failure is
        DEBUG-logged rather than raised — a broken telemetry install
        must never break the surrounding ``observe`` call.

    Returns
    -------
    The same ``runner`` object (mutated), so callers can chain the two
    idiomatic calls on one line::

        runner = harmonograf_client.observe(goldfive.wrap(agent))

    Notes
    -----
    Calling ``observe`` twice on the same runner appends two sinks.
    That's deliberate — the caller is responsible for deciding whether
    deduping makes sense in their context. Attaching a second bridge to
    the same runner will raise from goldfive's ``control`` setter since
    a channel is already attached — callers who want multiple observers
    should share a single ``Client``+bridge pair instead.
    """
    if client is None:
        client_kwargs: dict[str, Any] = {
            "name": name or "agent",
            "framework": framework,
            # observe() attaches a ControlBridge below, which means this
            # runner can receive STEER / CANCEL / PAUSE / APPROVE /
            # REJECT control messages from the harmonograf UI. Advertise
            # the matching capabilities so the UI's Steer and Approve
            # buttons light up (frontend checks ``hasCapability(span,
            # 'STEERING')`` before enabling the action). Callers who
            # want a narrower capability set can pass their own pre-
            # built ``client=`` with custom capabilities.
            "capabilities": ["STEERING", "HUMAN_IN_LOOP"],
        }
        # Explicit ``server_addr`` kwarg wins over ``config.server_addr``
        # so the pre-#105 keyword-arg ergonomics are preserved. Only
        # forward to Client when the caller actually supplied an address
        # somewhere — leaving the kwarg out lets :class:`Client`'s
        # dataclass default apply, matching the legacy no-arg behaviour
        # and keeping the test-spy surface unchanged for that path.
        if server_addr is not None:
            client_kwargs["server_addr"] = server_addr
        elif config is not None:
            client_kwargs["server_addr"] = config.server_addr
        if config is not None and config.home_dir is not None:
            client_kwargs["identity_root"] = str(config.home_dir)
        client = Client(**client_kwargs)

    sink = HarmonografSink(client)
    runner.add_sink(sink)

    # Attach a goldfive ControlChannel if the runner doesn't already
    # have one. The setter is idempotent on same-identity channels and
    # raises if a different channel is already attached.
    if runner.control is None:
        from goldfive.control import ControlChannel

        runner.control = ControlChannel()

    # Spin up the bridge and register its teardown as a close hook so
    # ``runner.close()`` shuts the wire down cleanly. No monkey-patching,
    # no hasattr walks — this relies on goldfive's Runner extension API.
    from ._control_bridge import ControlBridge

    loop = asyncio.get_running_loop()
    bridge = ControlBridge(client, runner.control, loop)
    bridge.start()
    runner.add_close_hook(bridge.stop)

    # Stash for test + introspection access. Underscore prefix so it's
    # clear this is private plumbing, not part of the Runner's public
    # contract.
    runner._harmonograf_control_bridge = bridge  # type: ignore[attr-defined]

    # Best-effort: when the runner exposes an ADK plugin hook (i.e.
    # goldfive.wrap'd an ADK agent and goldfive >= 0.x ships
    # GoldfiveADKAgent.add_plugin), install the telemetry plugin so the
    # 2-line API also produces ADK spans. Lazy-imported and wrapped so
    # any failure DEBUG-logs instead of breaking observe().
    if install_adk_telemetry and hasattr(runner, "add_plugin"):
        try:
            from .telemetry_plugin import HarmonografTelemetryPlugin

            runner.add_plugin(HarmonografTelemetryPlugin(client))
        except Exception as exc:  # noqa: BLE001
            log.debug("observe: ADK telemetry plugin not attached: %s", exc)

    return runner
