"""Tests for :func:`harmonograf_client.adk_web_observability`.

The bundle helper plugs the three-wiring shape every ``adk web`` caller
needs: telemetry plugin (span stream), plan sink (goldfive event
stream — the piece harmonograf#57 tracked), and control channel (UI →
steerer, from harmonograf#55 / #56). Spelling them out one at a time
is what let the presentation demo ship without the sink; the bundle
closes that gap by construction.

Coverage:

- In an event loop the bundle populates all three fields with the
  right types, and the control channel is live (bridge attached).
- Outside an event loop the helper swallows the ``RuntimeError``
  ``control_channel`` raises, sets ``control=None``, and still
  returns plugin + sink — mirroring the demo's pre-bundle fallback.
- The sink actually ships goldfive events to the client's transport,
  so a caller passing ``bundle.sink`` into ``goldfive.wrap(sinks=...)``
  gets plan / task / drift events on the wire (the harmonograf#57 fix).
"""

from __future__ import annotations

import logging

import pytest

from harmonograf_client import (
    AdkWebObservability,
    HarmonografSink,
    HarmonografTelemetryPlugin,
    adk_web_observability,
)
from harmonograf_client._control_bridge import ControlBridge
from harmonograf_client.client import Client

from tests._fixtures import FakeTransport, make_factory


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="adk-web-client",
        agent_id="agent-adkweb",
        session_id="sess-adkweb",
        framework="ADK",
        buffer_size=8,
        _transport_factory=make_factory(made),
    )


@pytest.mark.asyncio
async def test_bundle_populates_all_three_hookups(client: Client) -> None:
    bundle = adk_web_observability(client)

    try:
        assert isinstance(bundle, AdkWebObservability)
        assert isinstance(bundle.plugin, HarmonografTelemetryPlugin)
        assert isinstance(bundle.sink, HarmonografSink)
        # Plugin + sink share the same client so span frames, goldfive
        # events, and control acks land on one agent_id in harmonograf.
        assert bundle.sink.client is client
        # ``control`` is live — has a bridge attached. This only works
        # inside an event loop, which pytest-asyncio provides here.
        assert bundle.control is not None
        bridge = getattr(bundle.control, "_harmonograf_control_bridge", None)
        assert isinstance(bridge, ControlBridge)
    finally:
        bridge = getattr(bundle.control, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()


def test_bundle_without_running_loop_returns_control_none(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside an event loop ``control`` is ``None`` but the rest is live.

    ``control_channel`` calls ``asyncio.get_running_loop()`` which
    raises ``RuntimeError`` in a synchronous context. The bundle
    helper must swallow that and log, not propagate — the demo's
    ``_build_app`` relies on this so imports stay offline-safe.
    """
    caplog.set_level(logging.WARNING, logger="harmonograf_client.adk_web")

    bundle = adk_web_observability(client)

    assert isinstance(bundle, AdkWebObservability)
    assert isinstance(bundle.plugin, HarmonografTelemetryPlugin)
    assert isinstance(bundle.sink, HarmonografSink)
    assert bundle.control is None
    # A warning must be emitted so the demo's operator can spot the
    # skipped bridge in the adk log.
    assert any(
        "control_channel skipped" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]


@pytest.mark.asyncio
async def test_sink_forwards_goldfive_events_to_client_buffer(
    client: Client,
) -> None:
    """The bundle's sink must actually push goldfive events onto the client buffer.

    This is the regression guard for harmonograf#57: before the fix,
    the demo built a telemetry plugin + control channel but no sink,
    so goldfive's ``plan_submitted`` / ``plan_revised`` /
    ``drift_detected`` / ``task_*`` events ran through goldfive's
    default ``LoggingSink`` only and never reached harmonograf's UI.
    """
    from goldfive.pb.goldfive.v1 import events_pb2 as ge

    from harmonograf_client.buffer import EnvelopeKind

    bundle = adk_web_observability(client)
    try:
        event = ge.Event()
        event.event_id = "e-adkweb-1"
        event.run_id = "run-adkweb-1"
        event.sequence = 0
        event.run_started.run_id = "run-adkweb-1"
        event.run_started.goal_summary = "bundle demo"

        await bundle.sink.emit(event)

        envelopes = list(client._events.drain())
        assert any(
            env.kind is EnvelopeKind.GOLDFIVE_EVENT
            and getattr(env.payload, "run_id", "") == "run-adkweb-1"
            for env in envelopes
        ), [(e.kind, e.payload) for e in envelopes]
    finally:
        bridge = getattr(bundle.control, "_harmonograf_control_bridge", None)
        if bridge is not None:
            await bridge.stop()
