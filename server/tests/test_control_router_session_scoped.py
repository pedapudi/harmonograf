"""Router-level tests for per-ADK-session scoped control delivery
(harmonograf #53).

A single Client may open multiple SubscribeControl streams for the same
agent_id — a home sub keyed on the harmonograf-assigned session and one
per live ADK session. The router must prefer the session-matching sub
when routing a STEER / INJECT_MESSAGE targeting a specific session, so
a frontend steer action against the ADK session reaches the right sub
without being fanned out to every sub on that agent (which would cause
a goldfive bridge to process the same event twice).

Falls back to every live sub when no session-matching sub exists — that
is the legacy behaviour a home-only topology always took.
"""

from __future__ import annotations

import asyncio

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_server.control_router import ControlRouter, DeliveryResult


def _event(kind: int, *, agent_id: str = "agent-1") -> gf_control_pb2.ControlEvent:
    return gf_control_pb2.ControlEvent(
        kind=kind,
        target=gf_control_pb2.ControlTarget(agent_id=agent_id),
    )


async def test_delivery_prefers_session_matching_sub_over_home():
    """Two subs on the same agent, different session_ids. A STEER at a
    specific session_id lands only on the matching sub."""
    router = ControlRouter()
    home = await router.subscribe("home-session", "agent-X", "home-str")
    adk = await router.subscribe("adk-session-abc", "agent-X", "adk-str")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="adk-session-abc",
            agent_id="agent-X",
            event=_event(gf_control_pb2.CONTROL_KIND_STEER, agent_id="agent-X"),
            timeout_s=2.0,
        )
    )

    # The ADK sub's queue gets the event.
    ev = await asyncio.wait_for(adk.queue.get(), timeout=1.0)
    assert ev.kind == gf_control_pb2.CONTROL_KIND_STEER

    # The home sub's queue is empty — the router filtered us out.
    assert home.queue.qsize() == 0

    # Ack via the ADK stream to resolve the delivery.
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="adk-str",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    assert len(outcome.acks) == 1
    assert outcome.acks[0].stream_id == "adk-str"

    await router.unsubscribe(home)
    await router.unsubscribe(adk)


async def test_delivery_falls_back_to_home_when_no_session_match():
    """With no session-matching sub, deliver fans out to every live sub
    on the agent — the legacy home-only path. This preserves backwards
    compatibility for Clients that never called register_session()."""
    router = ControlRouter()
    home = await router.subscribe("home-session", "agent-X", "home-str")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="an-adk-session-never-subscribed",
            agent_id="agent-X",
            event=_event(gf_control_pb2.CONTROL_KIND_STEER, agent_id="agent-X"),
            timeout_s=2.0,
        )
    )

    ev = await asyncio.wait_for(home.queue.get(), timeout=1.0)
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="home-str",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(home)


async def test_delivery_without_session_id_fans_out_to_all():
    """When the caller passes an empty session_id (legacy SendControl
    without session scoping), every live sub on the agent gets the
    event. This preserves the multi-stream-per-agent semantics the
    original router tests cover."""
    router = ControlRouter()
    sub_a = await router.subscribe("sess-a", "agent-X", "str-a")
    sub_b = await router.subscribe("sess-b", "agent-X", "str-b")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="",
            agent_id="agent-X",
            event=_event(gf_control_pb2.CONTROL_KIND_PAUSE, agent_id="agent-X"),
            timeout_s=2.0,
        )
    )

    ev_a = await asyncio.wait_for(sub_a.queue.get(), timeout=1.0)
    ev_b = await asyncio.wait_for(sub_b.queue.get(), timeout=1.0)
    assert ev_a.id == ev_b.id  # fan-out, not session-filtered

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev_a.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="str-a",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(sub_a)
    await router.unsubscribe(sub_b)


async def test_delivery_with_multiple_session_matched_subs_fans_out_to_them_only():
    """Two streams on the same (session_id, agent_id) — e.g. a client
    with two redundant control channels for the same ADK session — both
    get the event, but a sub on a DIFFERENT session on that agent does
    NOT. Tests the filter is by session_id equality, not "pick one"."""
    router = ControlRouter()
    sess_a_1 = await router.subscribe("sess-A", "agent-X", "a1")
    sess_a_2 = await router.subscribe("sess-A", "agent-X", "a2")
    sess_b = await router.subscribe("sess-B", "agent-X", "b1")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess-A",
            agent_id="agent-X",
            event=_event(gf_control_pb2.CONTROL_KIND_STEER, agent_id="agent-X"),
            timeout_s=2.0,
        )
    )

    ev_a1 = await asyncio.wait_for(sess_a_1.queue.get(), timeout=1.0)
    ev_a2 = await asyncio.wait_for(sess_a_2.queue.get(), timeout=1.0)
    assert ev_a1.id == ev_a2.id

    # The other-session sub must NOT receive the event.
    assert sess_b.queue.qsize() == 0

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev_a1.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="a1",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(sess_a_1)
    await router.unsubscribe(sess_a_2)
    await router.unsubscribe(sess_b)


async def test_alias_path_still_works_with_session_scoping():
    """Alias path (sub-agent name → stream agent_id) must coexist with
    session filtering. The alias resolves the bucket; session filter
    then scopes within it."""
    router = ControlRouter()
    real = await router.subscribe("sess-A", "agent-real-uuid", "rx")
    router.register_alias("sub-agent-name", "agent-real-uuid")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess-A",
            agent_id="sub-agent-name",
            event=_event(
                gf_control_pb2.CONTROL_KIND_STEER, agent_id="sub-agent-name"
            ),
            timeout_s=2.0,
        )
    )

    ev = await asyncio.wait_for(real.queue.get(), timeout=1.0)
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="rx",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(real)
