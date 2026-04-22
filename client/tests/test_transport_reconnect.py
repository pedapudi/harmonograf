"""Tests for the hardened Transport reconnect path.

Covers:

* Circuit breaker opens after N consecutive failed connect attempts.
* Half-open trial on cooldown expiry; re-opens on trial failure.
* Successful send closes the breaker and resets consecutive_failures.
* Integration: a real connection against FakeServerHarness marks the
  transport healthy and resets the breaker counter.

Most of the state-machine coverage uses the breaker helpers directly
rather than driving asyncio end-to-end, so these tests stay fast and
deterministic. One end-to-end test confirms the healthy-send path is
actually wired up against the send loop.
"""

from __future__ import annotations

import threading
import time

import pytest

from harmonograf_client import Client
from harmonograf_client.buffer import EventRingBuffer, PayloadBuffer
from harmonograf_client.transport import (
    BREAKER_CLOSED,
    BREAKER_HALF_OPEN,
    BREAKER_OPEN,
    Transport,
    TransportConfig,
)

from tests.test_transport_mock import (  # noqa: F401 — fixtures
    FakeServerHarness,
    isolated_identity,
    server,
    _wait,
)


def _make_transport(**overrides) -> Transport:
    cfg = overrides.pop("config", None) or TransportConfig(
        breaker_failure_threshold=3,
        breaker_cooldown_ms=50,
        reconnect_initial_ms=10,
        reconnect_max_ms=100,
    )
    return Transport(
        events=EventRingBuffer(capacity=16),
        payloads=PayloadBuffer(capacity_bytes=1024),
        agent_id="agent-test",
        session_id="sess-test",
        name="test",
        framework="custom",
        framework_version="0.0.0",
        capabilities=[],
        config=cfg,
    )


class TestBreakerStateMachine:
    def test_starts_closed(self):
        t = _make_transport()
        assert t.breaker_state == BREAKER_CLOSED
        assert t.consecutive_failures == 0

    def test_opens_after_threshold_failures(self):
        t = _make_transport()
        for _ in range(2):
            t._on_failed_attempt()
        assert t.breaker_state == BREAKER_CLOSED
        assert t.consecutive_failures == 2
        t._on_failed_attempt()
        assert t.breaker_state == BREAKER_OPEN
        assert t.consecutive_failures == 3

    def test_half_open_trial_success_closes_breaker(self):
        t = _make_transport()
        for _ in range(3):
            t._on_failed_attempt()
        assert t.breaker_state == BREAKER_OPEN
        t._breaker_half_open()
        assert t.breaker_state == BREAKER_HALF_OPEN
        # A successful send through the loop calls _mark_healthy.
        t._mark_healthy()
        assert t.breaker_state == BREAKER_CLOSED
        assert t.consecutive_failures == 0

    def test_half_open_trial_failure_reopens(self):
        t = _make_transport()
        for _ in range(3):
            t._on_failed_attempt()
        t._breaker_half_open()
        assert t.breaker_state == BREAKER_HALF_OPEN
        t._on_failed_attempt()
        assert t.breaker_state == BREAKER_OPEN

    def test_healthy_disconnect_resets_counter(self):
        t = _make_transport()
        for _ in range(2):
            t._on_failed_attempt()
        assert t.consecutive_failures == 2
        t._on_healthy_disconnect()
        assert t.consecutive_failures == 0
        assert t.breaker_state == BREAKER_CLOSED

    def test_mark_healthy_is_idempotent(self):
        t = _make_transport()
        t._on_failed_attempt()
        t._mark_healthy()
        assert t._healthy is True
        first_state = t.breaker_state
        # Further calls should not re-run state writes in ways that
        # affect anything observable.
        t._mark_healthy()
        assert t._healthy is True
        assert t.breaker_state == first_state

    def test_mark_healthy_clears_stale_failures(self):
        """Backoff reset must happen on successful SEND, not just on
        connect. This is the core of the hardening: a server that
        accepts then drops must not wedge the backoff at the cap.
        """
        t = _make_transport()
        t._on_failed_attempt()
        t._on_failed_attempt()
        assert t.consecutive_failures == 2
        t._mark_healthy()
        assert t.consecutive_failures == 0


class TestBreakerConfigurable:
    def test_threshold_from_config(self):
        cfg = TransportConfig(
            breaker_failure_threshold=1,
            breaker_cooldown_ms=10,
        )
        t = _make_transport(config=cfg)
        t._on_failed_attempt()
        assert t.breaker_state == BREAKER_OPEN

    def test_high_threshold_never_opens_within_bounds(self):
        cfg = TransportConfig(breaker_failure_threshold=100)
        t = _make_transport(config=cfg)
        for _ in range(50):
            t._on_failed_attempt()
        assert t.breaker_state == BREAKER_CLOSED


class TestBreakerThreadSafety:
    def test_concurrent_failures_count_correctly(self):
        cfg = TransportConfig(breaker_failure_threshold=1_000_000)
        t = _make_transport(config=cfg)

        def worker():
            for _ in range(500):
                t._on_failed_attempt()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert t.consecutive_failures == 2000


class TestIntegrationHealthyResetsBreaker:
    """End-to-end: drive a real Client against FakeServerHarness and
    verify that once the send loop actually pushes a span upstream, the
    breaker is closed and the failure counter is zero. This is the only
    test that exercises the _mark_healthy() hook wired into the send
    loop itself; the state-machine tests above stub it out.
    """

    def test_successful_send_keeps_breaker_closed(self, server, isolated_identity):
        client = Client(
            name="reconnect-agent",
            server_addr=f"127.0.0.1:{server.port}",
        )
        try:
            # Lazy Hello (harmonograf#83): nothing hits the wire until
            # the first real emit. Emit before waiting for Welcome.
            sid = client.emit_span_start(kind="LLM_CALL", name="m")
            client.emit_span_end(sid, status="COMPLETED")
            assert _wait(lambda: server.servicer.welcome_sent.is_set())
            assert _wait(lambda: server.servicer.first_span_seen.is_set())
            # Give the send loop a tick to call _mark_healthy.
            assert _wait(lambda: client._transport._healthy is True, timeout=2.0)
            assert client._transport.breaker_state == BREAKER_CLOSED
            assert client._transport.consecutive_failures == 0
        finally:
            client.shutdown(flush_timeout=1.0)
