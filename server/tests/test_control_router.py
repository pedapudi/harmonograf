"""Unit tests for ControlRouter (in-process, no gRPC)."""

from __future__ import annotations

import asyncio

import pytest

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_server.control_router import ControlRouter, DeliveryResult


def _event(kind: int, *, agent_id: str = "agent-1") -> gf_control_pb2.ControlEvent:
    return gf_control_pb2.ControlEvent(
        kind=kind,
        target=gf_control_pb2.ControlTarget(agent_id=agent_id),
    )


async def test_unavailable_when_no_subscribers():
    router = ControlRouter()
    outcome = await router.deliver(
        session_id="sess",
        agent_id="agent-1",
        event=_event(gf_control_pb2.CONTROL_KIND_PAUSE),
        timeout_s=0.1,
    )
    assert outcome.result == DeliveryResult.UNAVAILABLE
    assert outcome.acks == []


async def test_subscribe_deliver_ack_roundtrip():
    router = ControlRouter()
    sub = await router.subscribe("sess", "agent-1", "stream-a")

    # Kick off deliver; it will block until ack arrives.
    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="agent-1",
            event=_event(gf_control_pb2.CONTROL_KIND_PAUSE),
            timeout_s=2.0,
        )
    )

    # Simulate the agent draining its queue and acking via the telemetry stream.
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event.target.agent_id == "agent-1"
    assert event.kind == gf_control_pb2.CONTROL_KIND_PAUSE

    ack = gf_control_pb2.ControlAck(
        control_id=event.id,
        result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        detail="",
    )
    router.record_ack(ack, stream_id="stream-a")

    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    assert len(outcome.acks) == 1
    assert outcome.acks[0].stream_id == "stream-a"

    await router.unsubscribe(sub)


async def test_multi_stream_fanout_first_success_resolves():
    router = ControlRouter()
    sub_a = await router.subscribe("sess", "agent-multi", "str-a")
    sub_b = await router.subscribe("sess", "agent-multi", "str-b")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="agent-multi",
            event=_event(gf_control_pb2.CONTROL_KIND_CANCEL, agent_id="agent-multi"),
            timeout_s=2.0,
            require_all_acks=False,
        )
    )

    ev_a = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
    ev_b = await asyncio.wait_for(sub_b.queue.get(), timeout=1)
    assert ev_a.id == ev_b.id  # fan-out: same control_id to both

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev_a.id, result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        ),
        stream_id="str-a",
    )
    # One success is enough for require_all_acks=False.
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(sub_a)
    await router.unsubscribe(sub_b)


async def test_require_all_acks_waits_for_every_stream():
    router = ControlRouter()
    sub_a = await router.subscribe("sess", "agent-multi", "str-a")
    sub_b = await router.subscribe("sess", "agent-multi", "str-b")

    steer_event = gf_control_pb2.ControlEvent(
        kind=gf_control_pb2.CONTROL_KIND_STEER,
        target=gf_control_pb2.ControlTarget(agent_id="agent-multi"),
    )
    steer_event.steer.note = "refocus"

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="agent-multi",
            event=steer_event,
            timeout_s=2.0,
            require_all_acks=True,
        )
    )

    ev_a = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
    ev_b = await asyncio.wait_for(sub_b.queue.get(), timeout=1)
    assert ev_a.steer.note == "refocus"

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev_a.id, result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        ),
        stream_id="str-a",
    )
    # Still pending
    await asyncio.sleep(0.05)
    assert not deliver_task.done()

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev_b.id, result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        ),
        stream_id="str-b",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    assert len(outcome.acks) == 2

    await router.unsubscribe(sub_a)
    await router.unsubscribe(sub_b)


async def test_deadline_exceeded_with_partial_acks():
    router = ControlRouter()
    sub_a = await router.subscribe("sess", "agent-1", "str-a")
    sub_b = await router.subscribe("sess", "agent-1", "str-b")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="agent-1",
            event=_event(gf_control_pb2.CONTROL_KIND_PAUSE),
            timeout_s=0.15,
            require_all_acks=True,
        )
    )

    ev = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
    _ = await asyncio.wait_for(sub_b.queue.get(), timeout=1)

    # Only str-a acks; str-b is slow → timeout
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id, result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        ),
        stream_id="str-a",
    )

    outcome = await deliver_task
    assert outcome.result == DeliveryResult.DEADLINE_EXCEEDED
    assert len(outcome.acks) == 1
    assert outcome.acks[0].stream_id == "str-a"

    await router.unsubscribe(sub_a)
    await router.unsubscribe(sub_b)


async def test_unsubscribe_during_pending_deliver_resolves_future():
    router = ControlRouter()
    sub = await router.subscribe("sess", "agent-1", "str-a")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess",
            agent_id="agent-1",
            event=_event(gf_control_pb2.CONTROL_KIND_PAUSE),
            timeout_s=2.0,
            require_all_acks=True,
        )
    )
    await asyncio.sleep(0.05)  # let deliver register pending

    await router.unsubscribe(sub)
    outcome = await asyncio.wait_for(deliver_task, timeout=1)
    # No acks arrived, all expected streams gone → resolves as FAILED (not timeout).
    assert outcome.result == DeliveryResult.FAILED
