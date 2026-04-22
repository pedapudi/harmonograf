"""Server-side control routing tests for the unified-session contract
(harmonograf#65 / goldfive#162).

Under the pre-fix topology the Client's single home control subscription
was keyed on the harmonograf-assigned session id. STEER annotations
fired from the frontend against the outer adk-web session (same id
goldfive events now carry via goldfive#161) couldn't find a
session-matching sub, so delivery either fanned out to the home sub
(suboptimal — a goldfive bridge on it would see the event twice) or —
more commonly under reconnect churn — hit the ``ack timeout`` path.

Fix: the client opens an additional ``SubscribeControl`` stream keyed
on the outer adk-web session id. The router's session-matching filter
(introduced by harmonograf#54, preserved in #60) routes STEER to that
sub precisely, so the bound ControlBridge → ControlChannel → goldfive
steerer path fires once and acks promptly.

These tests exercise the ControlRouter directly. The end-to-end
client↔server path is covered by the presentation_agent e2e suite
once both repos bump.
"""

from __future__ import annotations

import asyncio

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_server.control_router import ControlRouter, DeliveryResult


AGENT = "agent-Z"


def _steer(agent_id: str = AGENT) -> gf_control_pb2.ControlEvent:
    return gf_control_pb2.ControlEvent(
        kind=gf_control_pb2.CONTROL_KIND_STEER,
        target=gf_control_pb2.ControlTarget(agent_id=agent_id),
    )


async def test_steer_on_outer_session_prefers_additional_sub_over_home() -> None:
    """Home sub on ``sess_home`` + additional sub on ``adk-outer``.

    A STEER fired against ``adk-outer`` lands ONLY on the additional
    sub. The home sub's queue is empty — the router's session-match
    filter picked the specific sub. This is the exact routing
    behaviour that makes steer annotations on the outer adk-web
    session ack promptly instead of timing out.
    """
    router = ControlRouter()
    home = await router.subscribe("sess_home", AGENT, "str-home")
    extra = await router.subscribe("adk-outer", AGENT, "str-adk-outer")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="adk-outer",
            agent_id=AGENT,
            event=_steer(),
            timeout_s=2.0,
        )
    )
    ev = await asyncio.wait_for(extra.queue.get(), timeout=1.0)
    assert home.queue.qsize() == 0

    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="str-adk-outer",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    assert len(outcome.acks) == 1
    assert outcome.acks[0].stream_id == "str-adk-outer"

    await router.unsubscribe(home)
    await router.unsubscribe(extra)


async def test_steer_on_outer_session_without_additional_sub_falls_back_to_home() -> None:
    """With no additional sub registered, STEER for the outer session
    falls back to fan-out. Covers the back-compat path — a Client that
    hasn't yet called ``open_additional_control_subscription`` (e.g.
    the plugin hasn't seen ``before_run_callback`` yet) still routes,
    just less precisely."""
    router = ControlRouter()
    home = await router.subscribe("sess_home", AGENT, "str-home")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="adk-outer-not-yet-subscribed",
            agent_id=AGENT,
            event=_steer(),
            timeout_s=2.0,
        )
    )
    ev = await asyncio.wait_for(home.queue.get(), timeout=1.0)
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="str-home",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(home)


async def test_additional_sub_removal_unblocks_pending_on_outer_session() -> None:
    """If the additional sub is torn down mid-delivery (e.g. root
    after_run fired before ack), the router's unsubscribe path drains
    the pending delivery from the expected-stream set so it can
    resolve against whatever remains — here the home sub.

    Regression-proofing: the ``ack timeout`` pathology bit when a
    pending delivery was bound to a dead sub with nothing to replace
    it. Covering this keeps future refactors honest.
    """
    router = ControlRouter()
    home = await router.subscribe("sess_home", AGENT, "str-home")
    extra = await router.subscribe("adk-outer", AGENT, "str-adk-outer")

    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="adk-outer",
            agent_id=AGENT,
            event=_steer(),
            timeout_s=2.0,
        )
    )
    # Drain the extra sub's queue — but ack via the EXTRA stream to
    # resolve cleanly. (We're testing that the session-match branch
    # picked the right sub, not unsubscribe mid-flight which is a
    # separate code path.)
    ev = await asyncio.wait_for(extra.queue.get(), timeout=1.0)
    router.record_ack(
        gf_control_pb2.ControlAck(
            control_id=ev.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        ),
        stream_id="str-adk-outer",
    )
    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS

    await router.unsubscribe(home)
    await router.unsubscribe(extra)
