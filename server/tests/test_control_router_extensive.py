"""Extensive ControlRouter tests — complement test_control_router.py.

These cover alias routing (ADK sub-agent name → transport agent_id),
live_stream_ids enumeration, queue-full synthetic FAILURE acks,
STATUS_QUERY callbacks, ack with unknown control_id, and pending delivery
resolution when a subscription is replaced.
"""

from __future__ import annotations

import asyncio

import pytest

from harmonograf_server.control_router import ControlRouter, DeliveryResult
from harmonograf_server.pb import types_pb2


def _ack(control_id: str, result=types_pb2.CONTROL_ACK_RESULT_SUCCESS, detail=""):
    return types_pb2.ControlAck(control_id=control_id, result=result, detail=detail)


async def test_live_stream_ids_returns_empty_on_unknown_agent():
    router = ControlRouter()
    assert router.live_stream_ids("none") == []


async def test_live_stream_ids_lists_open_streams():
    router = ControlRouter()
    a = await router.subscribe("sess", "ag", "s1")
    b = await router.subscribe("sess", "ag", "s2")
    ids = router.live_stream_ids("ag")
    assert set(ids) == {"s1", "s2"}
    a.close()
    # Closed stream filtered out.
    assert "s1" not in router.live_stream_ids("ag")
    await router.unsubscribe(b)


async def test_alias_routes_control_to_transport_agent():
    router = ControlRouter()
    sub = await router.subscribe("sess", "transport-uuid", "s1")
    router.register_alias("sub-agent-name", "transport-uuid")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="sub-agent-name",
            kind=types_pb2.CONTROL_KIND_PAUSE,
            timeout_s=2.0,
        )
    )
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event.target.agent_id == "sub-agent-name"
    router.record_ack(_ack(event.id), stream_id="s1")
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    await router.unsubscribe(sub)


async def test_register_alias_is_noop_if_ids_equal_or_empty():
    router = ControlRouter()
    router.register_alias("", "x")
    router.register_alias("x", "")
    router.register_alias("same", "same")
    assert router._aliases == {}  # nothing added


async def test_deliver_unavailable_when_alias_targets_dead_agent():
    router = ControlRouter()
    router.register_alias("adk-name", "never-connected")
    outcome = await router.deliver(
        session_id="sess",
        agent_id="adk-name",
        kind=types_pb2.CONTROL_KIND_PAUSE,
        timeout_s=0.1,
    )
    assert outcome.result == DeliveryResult.UNAVAILABLE


async def test_ack_with_unknown_control_id_ignored():
    router = ControlRouter()
    # Should not raise even though nothing is pending.
    router.record_ack(_ack("not-a-real-id"), stream_id="s1")


async def test_failure_ack_yields_failed_outcome():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    deliver = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="ag",
            kind=types_pb2.CONTROL_KIND_CANCEL,
            timeout_s=2.0,
        )
    )
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    router.record_ack(
        _ack(event.id, result=types_pb2.CONTROL_ACK_RESULT_FAILURE, detail="nope"),
        stream_id="s1",
    )
    outcome = await deliver
    assert outcome.result == DeliveryResult.FAILED
    assert outcome.acks[0].detail == "nope"
    await router.unsubscribe(sub)


async def test_ack_without_stream_id_attributed_to_expected_stream():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    deliver = asyncio.create_task(
        router.deliver(
            session_id="sess", agent_id="ag", kind=types_pb2.CONTROL_KIND_PAUSE,
            timeout_s=2.0,
        )
    )
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    router.record_ack(_ack(event.id))  # stream_id omitted
    outcome = await deliver
    assert outcome.result == DeliveryResult.SUCCESS
    assert outcome.acks[0].stream_id == "s1"
    await router.unsubscribe(sub)


async def test_queue_full_synthesizes_failure_ack():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    # Saturate the subscription's queue so deliver has nowhere to enqueue.
    for i in range(256):
        sub.queue.put_nowait(
            types_pb2.ControlEvent(id=f"filler-{i}")
        )
    outcome = await router.deliver(
        session_id="sess", agent_id="ag", kind=types_pb2.CONTROL_KIND_PAUSE,
        timeout_s=1.0,
    )
    assert outcome.result == DeliveryResult.FAILED
    assert any("queue full" in a.detail for a in outcome.acks)


async def test_status_query_callback_fires_on_success_ack():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    captured = []

    async def cb(session_id, agent_id, span_id, detail):
        captured.append((session_id, agent_id, detail))

    router.on_status_query_response(cb)

    deliver = asyncio.create_task(
        router.deliver(
            session_id="sess", agent_id="ag",
            kind=types_pb2.CONTROL_KIND_STATUS_QUERY, timeout_s=2.0,
        )
    )
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    router.record_ack(
        _ack(event.id, detail="all good"), stream_id="s1"
    )
    await deliver
    # Let the scheduled callback run.
    await asyncio.sleep(0.01)
    assert captured == [("sess", "ag", "all good")]
    await router.unsubscribe(sub)


async def test_status_query_callback_not_called_on_failure_ack():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    captured = []

    async def cb(*args):
        captured.append(args)

    router.on_status_query_response(cb)
    deliver = asyncio.create_task(
        router.deliver(
            session_id="sess", agent_id="ag",
            kind=types_pb2.CONTROL_KIND_STATUS_QUERY, timeout_s=2.0,
        )
    )
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    router.record_ack(
        _ack(event.id, result=types_pb2.CONTROL_ACK_RESULT_FAILURE), stream_id="s1"
    )
    await deliver
    await asyncio.sleep(0.01)
    assert captured == []
    await router.unsubscribe(sub)


async def test_unsubscribe_clears_alias_when_agent_drops():
    router = ControlRouter()
    sub = await router.subscribe("sess", "transport", "s1")
    router.register_alias("adk-name", "transport")
    await router.unsubscribe(sub)
    assert "adk-name" not in router._aliases


async def test_control_id_can_be_supplied_and_echoed():
    router = ControlRouter()
    outcome = await router.deliver(
        session_id="sess", agent_id="nobody",
        kind=types_pb2.CONTROL_KIND_PAUSE, timeout_s=0.1,
        control_id="fixed-id-42",
    )
    assert outcome.control_id == "fixed-id-42"


async def test_concurrent_delivers_independent_futures():
    router = ControlRouter()
    sub = await router.subscribe("sess", "ag", "s1")
    t1 = asyncio.create_task(
        router.deliver(session_id="sess", agent_id="ag",
                       kind=types_pb2.CONTROL_KIND_PAUSE, timeout_s=2.0)
    )
    t2 = asyncio.create_task(
        router.deliver(session_id="sess", agent_id="ag",
                       kind=types_pb2.CONTROL_KIND_CANCEL, timeout_s=2.0)
    )
    e1 = await asyncio.wait_for(sub.queue.get(), timeout=1)
    e2 = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert e1.id != e2.id
    router.record_ack(_ack(e2.id), stream_id="s1")
    router.record_ack(_ack(e1.id), stream_id="s1")
    r1 = await t1
    r2 = await t2
    assert r1.result == DeliveryResult.SUCCESS
    assert r2.result == DeliveryResult.SUCCESS
    await router.unsubscribe(sub)
