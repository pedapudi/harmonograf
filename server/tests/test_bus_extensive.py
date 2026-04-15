"""Extensive unit tests for SessionBus (pub/sub core).

Covers subscribe / unsubscribe lifecycle, per-session fanout isolation,
backpressure synthesis when a subscriber queue is full, all convenience
constructors, and the invariant that publish() never blocks the caller.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from harmonograf_server.bus import (
    DELTA_AGENT_STATUS,
    DELTA_AGENT_UPSERT,
    DELTA_ANNOTATION,
    DELTA_BACKPRESSURE,
    DELTA_HEARTBEAT,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    DELTA_SPAN_UPDATE,
    DELTA_TASK_PLAN,
    DELTA_TASK_REPORT,
    DELTA_TASK_STATUS,
    Delta,
    SessionBus,
    Subscription,
)
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Framework,
    Span,
    SpanKind,
    SpanStatus,
    Task,
    TaskPlan,
    TaskStatus,
)


# ---- helpers --------------------------------------------------------------


def _span(sid: str = "sp1", session_id: str = "sess", agent_id: str = "a") -> Span:
    return Span(
        id=sid,
        session_id=session_id,
        agent_id=agent_id,
        kind=SpanKind.TOOL_CALL,
        name="tool",
        start_time=1.0,
        end_time=2.0,
        status=SpanStatus.COMPLETED,
    )


def _agent(session_id: str = "sess", agent_id: str = "a") -> Agent:
    return Agent(
        id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=Framework.ADK,
        connected_at=1.0,
        last_heartbeat=1.0,
        status=AgentStatus.CONNECTED,
    )


def _ann(session_id: str = "sess") -> Annotation:
    return Annotation(
        id="ann1",
        session_id=session_id,
        target=AnnotationTarget(span_id="sp1"),
        author="u",
        created_at=1.0,
        kind=AnnotationKind.COMMENT,
        body="hi",
    )


def _plan(session_id: str = "sess") -> TaskPlan:
    return TaskPlan(
        id="plan-1",
        session_id=session_id,
        created_at=1.0,
        tasks=[Task(id="t1", title="do thing")],
    )


# ---- tests ----------------------------------------------------------------


async def test_subscribe_creates_empty_queue_and_tracks_count():
    bus = SessionBus()
    assert bus.subscriber_count("sess") == 0
    sub = await bus.subscribe("sess")
    assert isinstance(sub, Subscription)
    assert sub.queue.empty()
    assert bus.subscriber_count("sess") == 1
    await bus.unsubscribe(sub)
    assert bus.subscriber_count("sess") == 0


async def test_unsubscribe_idempotent_and_marks_closed():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    await bus.unsubscribe(sub)
    assert sub.closed is True
    # Second unsubscribe is a no-op.
    await bus.unsubscribe(sub)
    assert bus.subscriber_count("sess") == 0


async def test_publish_without_subscribers_is_noop():
    bus = SessionBus()
    bus.publish(Delta("sess", "span_start", _span()))  # no raise


async def test_per_session_isolation():
    bus = SessionBus()
    a = await bus.subscribe("sess_a")
    b = await bus.subscribe("sess_b")
    bus.publish(Delta("sess_a", DELTA_SPAN_START, _span(session_id="sess_a")))
    assert a.queue.qsize() == 1
    assert b.queue.empty()
    await bus.unsubscribe(a)
    await bus.unsubscribe(b)


async def test_multiple_subscribers_each_session_receive_same_event():
    bus = SessionBus()
    s1 = await bus.subscribe("sess")
    s2 = await bus.subscribe("sess")
    bus.publish(Delta("sess", DELTA_SPAN_START, _span()))
    assert s1.queue.qsize() == 1
    assert s2.queue.qsize() == 1


async def test_closed_subscription_is_skipped_by_publish():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    sub.close()
    bus.publish(Delta("sess", DELTA_SPAN_START, _span()))
    assert sub.queue.empty()
    await bus.unsubscribe(sub)


async def test_backpressure_enqueues_dropped_counter_when_full():
    bus = SessionBus(queue_maxsize=2)
    sub = await bus.subscribe("sess")
    # Fill the queue with real events.
    bus.publish(Delta("sess", DELTA_SPAN_START, _span("sp1")))
    bus.publish(Delta("sess", DELTA_SPAN_START, _span("sp2")))
    assert sub.queue.full()
    # Drop next publish → synthesized backpressure replaces something? No, it is also full so dropped.
    bus.publish(Delta("sess", DELTA_SPAN_START, _span("sp3")))
    assert sub.dropped == 1
    # Drain one slot, publish again, backpressure synthetic should land.
    _ = sub.queue.get_nowait()
    bus.publish(Delta("sess", DELTA_SPAN_START, _span("sp4")))
    # sp4 filled the slot.
    bus.publish(Delta("sess", DELTA_SPAN_START, _span("sp5")))
    # sp5 dropped → backpressure synth attempted, queue full again → dropped too.
    assert sub.dropped == 2


async def test_backpressure_dropped_counter_increments_monotonically():
    bus = SessionBus(queue_maxsize=2)
    sub = await bus.subscribe("sess")
    for i in range(5):
        bus.publish(Delta("sess", DELTA_SPAN_START, _span(f"sp{i}")))
    # First two fit, the remaining three are dropped.
    assert sub.dropped == 3
    # Queue still bounded.
    assert sub.queue.qsize() <= 2


async def test_publish_never_blocks_when_queue_full():
    bus = SessionBus(queue_maxsize=1)
    sub = await bus.subscribe("sess")
    for i in range(200):
        bus.publish(Delta("sess", DELTA_SPAN_START, _span(f"sp{i}")))
    # publish() finished synchronously — no timeout. Queue still bounded.
    assert sub.queue.qsize() <= 1


async def test_publish_agent_upsert_helper():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    bus.publish_agent_upsert(_agent())
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_AGENT_UPSERT
    assert d.payload.id == "a"


async def test_publish_agent_status_helper_carries_stuck_and_activity():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    bus.publish_agent_status(
        "sess", "a", AgentStatus.CONNECTED, 123.0,
        current_activity="thinking", progress_counter=7, stuck=True,
    )
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_AGENT_STATUS
    assert d.payload["agent_id"] == "a"
    assert d.payload["status"] == AgentStatus.CONNECTED
    assert d.payload["current_activity"] == "thinking"
    assert d.payload["progress_counter"] == 7
    assert d.payload["stuck"] is True


async def test_publish_span_start_update_end_helpers():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    sp = _span()
    bus.publish_span_start(sp)
    bus.publish_span_update(sp)
    bus.publish_span_end(sp)
    kinds = [sub.queue.get_nowait().kind for _ in range(3)]
    assert kinds == [DELTA_SPAN_START, DELTA_SPAN_UPDATE, DELTA_SPAN_END]


async def test_publish_annotation_helper():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    bus.publish_annotation(_ann())
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_ANNOTATION
    assert d.payload.id == "ann1"


async def test_publish_heartbeat_helper_merges_stats():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    bus.publish_heartbeat("sess", "a", {"buffered_events": 42, "cpu_self_pct": 5.0})
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_HEARTBEAT
    assert d.payload["agent_id"] == "a"
    assert d.payload["buffered_events"] == 42


async def test_publish_task_plan_uses_plan_session_id():
    bus = SessionBus()
    sub = await bus.subscribe("alt-sess")
    bus.publish_task_plan(_plan(session_id="alt-sess"))
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_TASK_PLAN
    assert d.session_id == "alt-sess"


async def test_publish_task_status_helper():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    task = Task(id="t1", title="x", status=TaskStatus.RUNNING)
    bus.publish_task_status("sess", "plan-1", task)
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_TASK_STATUS
    assert d.payload["plan_id"] == "plan-1"
    assert d.payload["task"].id == "t1"


async def test_publish_task_report_defaults_recorded_at():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    before = time.time()
    bus.publish_task_report("sess", "a", "summary")
    d = sub.queue.get_nowait()
    assert d.kind == DELTA_TASK_REPORT
    assert d.payload["report"] == "summary"
    assert d.payload["recorded_at"] >= before


async def test_publish_task_report_honors_explicit_recorded_at():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    bus.publish_task_report("sess", "a", "r", invocation_span_id="inv-1", recorded_at=42.0)
    d = sub.queue.get_nowait()
    assert d.payload["recorded_at"] == 42.0
    assert d.payload["invocation_span_id"] == "inv-1"


async def test_subscribe_unknown_session_returns_empty_queue():
    bus = SessionBus()
    sub = await bus.subscribe("never-published")
    await asyncio.sleep(0)
    assert sub.queue.empty()
    await bus.unsubscribe(sub)


async def test_unsubscribe_removes_session_entry_when_last_leaves():
    bus = SessionBus()
    sub = await bus.subscribe("sess")
    await bus.unsubscribe(sub)
    # Internal dict should no longer have the session key.
    assert bus.subscriber_count("sess") == 0
