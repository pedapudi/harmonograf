"""Tests for ingest-time task→span binding (harmonograf#122).

Before #122 the only path from a planned task to the INVOCATION span
that executed it was a client-stamped ``hgraf.task_id`` span attribute.
That attribute relied on ADK's ``CallbackContext.state`` round-tripping
the current task id through sub-Runner sessions; goldfive's sub-Runner
mirror writes ``goldfive.current_task_id`` in ``before_run_callback``
while the binding reconciler runs during ``before_agent_callback``, so
the attribute lands on at most 1/19 spans in a representative run
(empirical measurement on 2026-04-22).

The new design correlates by agent id at ingest time: when
``task_started(task_id=T)`` arrives, the server reads the task's
``assignee_agent_id`` (already resolved to the canonical per-ADK-agent
id at plan ingest — harmonograf#113), finds an INVOCATION span owned
by that agent, and stamps ``bound_span_id`` on the task. If the span
hasn't arrived yet (stream ordering), a pending-binding intent queues
up and ``_handle_span_start`` fulfills it when the span lands.

These tests drive the ingest pipeline with synthetic
``TelemetryUp.span_start`` + ``TelemetryUp.goldfive_event`` messages
and assert the resulting ``bound_span_id`` on the stored task. The
attribute-based path from harmonograf#72 stays enabled and is NOT
exercised here — see ``test_goldfive_ingest.py`` for those tests.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.bus import SessionBus
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2, types_pb2
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Framework,
    Session,
    SessionStatus,
    TaskStatus,
    make_store,
)


# ---- fixtures ------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def pipeline(store):
    bus = SessionBus()
    pipe = IngestPipeline(store, bus, now_fn=lambda: 1_000_000.0)
    yield pipe, bus, store


SESSION_ID = "sess_bind"
STREAM_AGENT = "client-xyz"


def _stream_ctx(
    session_id: str = SESSION_ID, agent_id: str = STREAM_AGENT
) -> StreamContext:
    return StreamContext(
        stream_id="str_test",
        agent_id=agent_id,
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, agent_id)},
    )


def _ts(sec: float) -> Timestamp:
    t = Timestamp()
    t.seconds = int(sec)
    t.nanos = int((sec - int(sec)) * 1e9)
    return t


def _span_start_msg(
    *,
    span_id: str,
    agent_id: str,
    session_id: str = SESSION_ID,
    kind=types_pb2.SPAN_KIND_INVOCATION,
    start: float = 100.0,
    name: str = "invocation",
) -> telemetry_pb2.TelemetryUp:
    span = types_pb2.Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=kind,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name=name,
    )
    span.start_time.CopyFrom(_ts(start))
    return telemetry_pb2.TelemetryUp(
        span_start=telemetry_pb2.SpanStart(span=span)
    )


def _make_event(**kwargs) -> ge.Event:
    evt = ge.Event()
    evt.event_id = kwargs.get("event_id", "e1")
    evt.run_id = kwargs.get("run_id", "run-1")
    evt.sequence = kwargs.get("sequence", 0)
    return evt


def _wrap_gf(event: ge.Event) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goldfive_event=event)


def _plan_event(
    *,
    plan_id: str,
    task_id: str,
    assignee_bare_name: str,
    sequence: int = 0,
    event_id: str = "e-plan",
) -> ge.Event:
    evt = _make_event(event_id=event_id, sequence=sequence)
    plan = evt.plan_submitted.plan
    plan.id = plan_id
    plan.run_id = "run-1"
    plan.summary = "bind plan"
    t = plan.tasks.add()
    t.id = task_id
    t.title = task_id
    t.assignee_agent_id = assignee_bare_name
    t.status = gt.TASK_STATUS_PENDING
    return evt


def _task_started_event(
    *, task_id: str, sequence: int = 1, event_id: str = "e-ts"
) -> ge.Event:
    evt = _make_event(event_id=event_id, sequence=sequence)
    evt.task_started.task_id = task_id
    return evt


async def _ensure_session(store) -> None:
    await store.create_session(
        Session(
            id=SESSION_ID,
            title=SESSION_ID,
            created_at=1.0,
            status=SessionStatus.LIVE,
        )
    )


async def _register_agent(store, *, canonical_id: str, name: str) -> None:
    await store.register_agent(
        Agent(
            id=canonical_id,
            session_id=SESSION_ID,
            name=name,
            framework=Framework.ADK,
            connected_at=1000.0,
            last_heartbeat=1000.0,
            status=AgentStatus.CONNECTED,
        )
    )


# ---- tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_span_then_task_started_binds_immediately(pipeline, store):
    """SpanStart(INVOCATION, agent=A) → task_started(T, assignee=A)
    → task.bound_span_id == that span id.

    This is the common ordering: the sub-agent's INVOCATION opens
    first (its ``before_run_callback`` emits SpanStart), then the
    coordinator's planner fires ``task_started`` for the task
    assigned to that sub-agent.
    """
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    canonical = "client-xyz:research_agent"
    await _register_agent(store, canonical_id=canonical, name="research_agent")

    # Plan assigns t1 to research_agent (bare name — matches what
    # goldfive's planner emits; ingest rewrites to canonical).
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(
            _plan_event(
                plan_id="p1",
                task_id="t1",
                assignee_bare_name="research_agent",
            )
        ),
    )

    # INVOCATION span lands first, owned by the canonical agent id.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-1", agent_id=canonical, start=100.0),
    )

    # Now task_started fires.
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1")),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    assert t1.bound_span_id == "inv-1", (
        "task_started should bind to the RUNNING INVOCATION span"
    )


@pytest.mark.asyncio
async def test_task_started_then_span_binds_on_span_arrival(pipeline, store):
    """task_started arrives BEFORE the INVOCATION span — binding
    deferred until ``_handle_span_start`` sees the matching agent."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    canonical = "client-xyz:research_agent"
    await _register_agent(store, canonical_id=canonical, name="research_agent")

    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(
            _plan_event(
                plan_id="p1",
                task_id="t1",
                assignee_bare_name="research_agent",
            )
        ),
    )

    # task_started before the span lands — binding stashed.
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1")),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    assert t1.bound_span_id == "", "no binding yet — span hasn't arrived"

    # Now the INVOCATION span lands — pending binding fulfilled.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-1", agent_id=canonical, start=110.0),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.bound_span_id == "inv-1", (
        "late-arriving INVOCATION span should fulfill pending binding"
    )


@pytest.mark.asyncio
async def test_multi_agent_tasks_do_not_cross_bind(pipeline, store):
    """Two agents, two tasks — each task binds to its own agent's
    INVOCATION, never to the other's."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    agent_a = "client-xyz:research_agent"
    agent_b = "client-xyz:summarizer_agent"
    await _register_agent(store, canonical_id=agent_a, name="research_agent")
    await _register_agent(store, canonical_id=agent_b, name="summarizer_agent")

    # Plan with two tasks, each assigned to a different agent.
    plan_evt = _make_event(event_id="e-plan", sequence=0)
    plan = plan_evt.plan_submitted.plan
    plan.id = "p1"
    plan.run_id = "run-1"
    plan.summary = "two-agent plan"
    t1 = plan.tasks.add()
    t1.id = "t1"
    t1.title = "research"
    t1.assignee_agent_id = "research_agent"
    t1.status = gt.TASK_STATUS_PENDING
    t2 = plan.tasks.add()
    t2.id = "t2"
    t2.title = "summarize"
    t2.assignee_agent_id = "summarizer_agent"
    t2.status = gt.TASK_STATUS_PENDING
    await pipe.handle_message(_stream_ctx(), _wrap_gf(plan_evt))

    # Two INVOCATION spans, one per agent.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-a", agent_id=agent_a, start=100.0),
    )
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-b", agent_id=agent_b, start=101.0),
    )

    # Start both tasks.
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1", sequence=1, event_id="e-s1")),
    )
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t2", sequence=2, event_id="e-s2")),
    )

    plan_row = await store.get_task_plan("p1")
    t1_row = next(t for t in plan_row.tasks if t.id == "t1")
    t2_row = next(t for t in plan_row.tasks if t.id == "t2")
    assert t1_row.bound_span_id == "inv-a"
    assert t2_row.bound_span_id == "inv-b"


@pytest.mark.asyncio
async def test_leaf_spans_do_not_satisfy_invocation_binding(pipeline, store):
    """Only INVOCATION spans participate in ingest binding. A
    TOOL_CALL / LLM_CALL for the same agent must not be picked.

    This protects the existing attribute-based leaf binding path
    (harmonograf#72) which binds leaf spans to their executing task;
    the two paths must not fight over ``bound_span_id``.
    """
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    canonical = "client-xyz:research_agent"
    await _register_agent(store, canonical_id=canonical, name="research_agent")

    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(
            _plan_event(
                plan_id="p1",
                task_id="t1",
                assignee_bare_name="research_agent",
            )
        ),
    )

    # Only a TOOL_CALL span (no INVOCATION) — the ingest path should
    # queue a pending binding that is NEVER fulfilled by this span.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(
            span_id="tc-1",
            agent_id=canonical,
            kind=types_pb2.SPAN_KIND_TOOL_CALL,
            start=100.0,
            name="tool_x",
        ),
    )
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1")),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    assert t1.bound_span_id == "", (
        "TOOL_CALL must not satisfy INVOCATION binding"
    )

    # Now an INVOCATION lands for the same agent — binding fulfilled.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-1", agent_id=canonical, start=110.0),
    )
    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.bound_span_id == "inv-1"


@pytest.mark.asyncio
async def test_binding_resolves_bare_name_via_find_agent_id_by_name(
    pipeline, store
):
    """The plan's ``assignee_agent_id`` is rewritten from the bare ADK
    name to the canonical compound id at plan ingest (harmonograf#113).
    The binding path must agree with that resolution."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    canonical = "client-xyz:research_agent"
    await _register_agent(store, canonical_id=canonical, name="research_agent")

    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(
            _plan_event(
                plan_id="p1",
                task_id="t1",
                assignee_bare_name="research_agent",
            )
        ),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.assignee_agent_id == canonical, (
        "plan ingest should resolve bare ADK name → canonical id"
    )

    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-1", agent_id=canonical, start=100.0),
    )
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1")),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.bound_span_id == "inv-1"


@pytest.mark.asyncio
async def test_binding_preserves_existing_status_on_late_span(pipeline, store):
    """If the INVOCATION span lands AFTER task_completed (an odd but
    possible ordering under mixed stream timing), the late binding
    must not downgrade the task's terminal status back to RUNNING —
    it should stamp ``bound_span_id`` while leaving status at
    COMPLETED."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    canonical = "client-xyz:research_agent"
    await _register_agent(store, canonical_id=canonical, name="research_agent")

    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(
            _plan_event(
                plan_id="p1",
                task_id="t1",
                assignee_bare_name="research_agent",
            )
        ),
    )
    # task_started → task_completed without the span having arrived.
    await pipe.handle_message(
        _stream_ctx(),
        _wrap_gf(_task_started_event(task_id="t1")),
    )
    completed = _make_event(sequence=2, event_id="e-c")
    completed.task_completed.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap_gf(completed))

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.COMPLETED
    assert t1.bound_span_id == ""

    # Span lands last — binding must not clobber COMPLETED.
    await pipe.handle_message(
        _stream_ctx(),
        _span_start_msg(span_id="inv-1", agent_id=canonical, start=100.0),
    )

    plan = await store.get_task_plan("p1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.COMPLETED, (
        "late binding must preserve terminal status"
    )
    assert t1.bound_span_id == "inv-1"
