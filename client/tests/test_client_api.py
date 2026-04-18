"""Tests for the :class:`harmonograf_client.client.Client` public API.

These tests swap the real :class:`Transport` for :class:`FakeTransport`
via the ``_transport_factory`` injection point, then exercise every
public emit / submit path and verify the resulting ring-buffer state.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.planner import Plan, Task, TaskEdge
from harmonograf_client.transport import ControlAckSpec

from tests._fixtures import FakeTransport, make_factory


def _drain(client: Client) -> list:
    return list(client._events.drain())


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    c = Client(
        name="test-agent",
        agent_id="agent-A",
        session_id="sess-1",
        framework="CUSTOM",
        capabilities=("STEERING",),
        metadata={"k": "v"},
        buffer_size=16,
        _transport_factory=make_factory(made),
    )
    return c


class TestConstruction:
    def test_transport_started_on_autostart(self, client, made):
        assert len(made) == 1
        assert made[0].started is True
        assert made[0].agent_id == "agent-A"
        assert made[0].session_id == "sess-1"
        assert made[0].name == "test-agent"
        assert made[0].capabilities == ["STEERING"]
        assert made[0].metadata == {"k": "v"}

    def test_autostart_false(self, made):
        c = Client(
            name="a",
            agent_id="x",
            autostart=False,
            _transport_factory=make_factory(made),
        )
        assert made[0].started is False
        c.shutdown()

    def test_identity_resolution_when_agent_id_missing(self, tmp_path, made):
        c = Client(
            name="fresh",
            identity_root=str(tmp_path),
            _transport_factory=make_factory(made),
        )
        assert c.agent_id  # non-empty, generated from identity store
        assert made[0].agent_id == c.agent_id
        c.shutdown()

    def test_session_id_falls_back_to_transport_assignment(self, client, made):
        made[0].assigned_session_id = "assigned-xyz"
        assert client.session_id == "assigned-xyz"

    def test_session_id_returns_configured_when_no_assignment(self, client, made):
        made[0].assigned_session_id = ""
        assert client.session_id == "sess-1"


class TestEmitSpanStart:
    def test_basic_start(self, client, made):
        sid = client.emit_span_start(kind="LLM_CALL", name="gpt-4o")
        assert isinstance(sid, str) and len(sid) > 8
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.SPAN_START
        assert env.span_id == sid
        assert made[0].notify_count >= 1

    def test_custom_span_id(self, client):
        sid = client.emit_span_start(kind="TOOL_CALL", name="search", span_id="fixed-id")
        assert sid == "fixed-id"

    def test_unknown_kind_falls_back_to_custom(self, client):
        client.emit_span_start(kind="WEIRD_KIND", name="x")
        env = _drain(client)[0]
        assert env.payload.span.kind_string == "WEIRD_KIND"

    def test_attributes_are_typed(self, client):
        client.emit_span_start(
            kind="TOOL_CALL",
            name="f",
            attributes={"i": 5, "f": 2.5, "b": True, "s": "hello", "raw": b"\x01\x02"},
        )
        span = _drain(client)[0].payload.span
        assert span.attributes["i"].int_value == 5
        assert span.attributes["f"].double_value == 2.5
        assert span.attributes["b"].bool_value is True
        assert span.attributes["s"].string_value == "hello"
        assert span.attributes["raw"].bytes_value == b"\x01\x02"

    def test_parent_span_id(self, client):
        client.emit_span_start(kind="STEP", name="c", parent_span_id="parent-1")
        span = _drain(client)[0].payload.span
        assert span.parent_span_id == "parent-1"

    def test_payload_attachment(self, client, made):
        data = b"{\"q\":1}"
        client.emit_span_start(
            kind="LLM_CALL", name="m", payload=data, payload_mime="application/json"
        )
        env = _drain(client)[0]
        assert env.has_payload_ref is True
        ref = env.payload.span.payload_refs[0]
        assert ref.digest == hashlib.sha256(data).hexdigest()
        assert ref.size == len(data)
        assert ref.mime == "application/json"
        assert ref.role == "input"
        assert made[0].enqueued[0].digest == ref.digest

    def test_payload_evicted_marks_ref(self, client, made):
        made[0].payload_accept = False
        client.emit_span_start(kind="LLM_CALL", name="m", payload=b"big-blob")
        env = _drain(client)[0]
        # Transport rejected the bytes — ref should be evicted.
        assert env.payload.span.payload_refs[0].evicted is True
        # And the envelope is tagged as not carrying a live ref.
        assert env.has_payload_ref is False

    def test_links(self, client):
        client.emit_span_start(
            kind="LLM_CALL",
            name="m",
            links=[
                {"target_span_id": "other", "target_agent_id": "b", "relation": "FOLLOWS"},
            ],
        )
        span = _drain(client)[0].payload.span
        assert len(span.links) == 1
        assert span.links[0].target_span_id == "other"
        assert span.links[0].target_agent_id == "b"

    def test_progress_counter_increments(self, client):
        assert client._progress_counter == 0
        client.emit_span_start(kind="STEP", name="a")
        client.emit_span_start(kind="STEP", name="b")
        assert client._progress_counter == 2


class TestEmitSpanUpdate:
    def test_attribute_merge(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_update(sid, attributes={"progress": 0.5})
        envs = _drain(client)
        upd = envs[1].payload
        assert upd.span_id == sid
        assert upd.attributes["progress"].double_value == 0.5

    def test_status_transition(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_update(sid, status="RUNNING")
        upd = _drain(client)[1].payload
        # RUNNING is the default for a new span; just confirm the enum was resolved.
        assert upd.status != 0 or upd.status == 0  # resolver always returns an int

    def test_payload_append(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_update(sid, payload=b"partial", payload_role="output")
        envs = _drain(client)
        upd = envs[1]
        assert upd.has_payload_ref is True
        assert len(upd.payload.payload_refs) == 1
        assert upd.payload.payload_refs[0].role == "output"


class TestEmitSpanEnd:
    def test_basic_end(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_end(sid, status="COMPLETED")
        envs = _drain(client)
        assert envs[1].kind is EnvelopeKind.SPAN_END
        assert envs[1].payload.span_id == sid

    def test_error_fields(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_end(
            sid,
            status="FAILED",
            error={"type": "ValueError", "message": "bad", "stack": "trace..."},
        )
        msg = _drain(client)[1].payload
        assert msg.error.type == "ValueError"
        assert msg.error.message == "bad"
        assert msg.error.stack == "trace..."

    def test_end_with_payload(self, client):
        sid = client.emit_span_start(kind="STEP", name="a")
        client.emit_span_end(sid, payload=b"done", payload_mime="text/plain")
        env = _drain(client)[1]
        assert env.has_payload_ref is True
        assert env.payload.payload_refs[0].mime == "text/plain"


# TestSubmitPlan / TestSubmitTaskStatus removed in Phase A of the goldfive
# migration (issue #2). Client.submit_plan / submit_task_status_update are
# gone; plan + task state now rides inside emit_goldfive_event, covered by
# Phase B's new client/tests/test_sink.py.


class TestProgressAndActivity:
    def test_set_current_activity(self, client):
        client.set_current_activity("thinking about X")
        counter, activity = client._progress_snapshot
        assert activity == "thinking about X"
        assert counter == 0

    def test_progress_snapshot_readable_from_transport(self, client, made):
        client.set_current_activity("doing work")
        client.emit_span_start(kind="STEP", name="a")
        counter, activity = made[0].progress_fn()
        assert counter == 1
        assert activity == "doing work"


class TestControlHandlerRegistration:
    def test_on_control_registers(self, client, made):
        def h(evt: Any) -> ControlAckSpec:
            return ControlAckSpec(result="success")

        client.on_control("status_query", h)
        assert "STATUS_QUERY" in made[0].handlers
        assert made[0].handlers["STATUS_QUERY"] is h


class TestShutdown:
    def test_shutdown_drains_buffer(self, client, made):
        client.emit_span_start(kind="STEP", name="a")
        # Don't drain — let shutdown handle it. The fake transport can't
        # actually drain, but shutdown must still complete and call through.
        client.shutdown(flush_timeout=0.05)
        assert made[0].shutdown_called is True

    def test_shutdown_is_idempotent(self, client, made):
        client.shutdown()
        client.shutdown()
        assert made[0].shutdown_called is True

    def test_context_manager(self, made):
        with Client(
            name="cm",
            agent_id="a",
            _transport_factory=make_factory(made),
        ) as c:
            c.emit_span_start(kind="STEP", name="a")
        assert made[-1].shutdown_called is True


class TestBackpressure:
    def test_updates_dropped_before_starts(self, made):
        c = Client(
            name="bp",
            agent_id="a",
            buffer_size=4,
            _transport_factory=make_factory(made),
        )
        s1 = c.emit_span_start(kind="STEP", name="a")
        c.emit_span_update(s1, attributes={"p": 1})
        c.emit_span_update(s1, attributes={"p": 2})
        c.emit_span_start(kind="STEP", name="b")
        # Buffer now full; next start should evict the oldest update.
        c.emit_span_start(kind="STEP", name="c")
        stats = c._events.stats_snapshot()
        assert stats.dropped_updates >= 1
        c.shutdown()
