"""Extra frontend RPC coverage — complements test_frontend_rpcs.py.

Focuses on branches not already asserted: pagination offset, windowed
span queries, agent_ids filter, summary_only payloads, delete reporting
of payload bytes freed, stats reflecting payload bytes, WatchSession
delta→SessionUpdate translation of heartbeat/task_plan/task_status/
task_report/annotation/agent_status deltas.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import grpc
import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import (
    frontend_pb2,
    service_pb2_grpc,
    telemetry_pb2,
    types_pb2,
)
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    Framework,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanStatus,
    Task,
    TaskPlan,
    TaskStatus,
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def harness(store):
    bus = SessionBus()
    router = ControlRouter()
    ingest = IngestPipeline(store, bus, control_sink=router)
    servicer = TelemetryServicer(ingest, router=router, data_dir="/var/harmonograf")

    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {"port": port, "store": store, "bus": bus, "router": router, "ingest": ingest}
    finally:
        await server.stop(grace=0.3)


@pytest_asyncio.fixture
async def stub(harness):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{harness['port']}")
    try:
        yield service_pb2_grpc.HarmonografStub(ch)
    finally:
        await ch.close()


# ---- helpers --------------------------------------------------------------


async def _seed_minimal(store, session_id: str, *, n_spans: int = 0,
                        created_at: float = 1_000.0,
                        status: SessionStatus = SessionStatus.LIVE) -> None:
    await store.create_session(
        Session(id=session_id, title=session_id, created_at=created_at,
                status=status, agent_ids=["a"])
    )
    await store.register_agent(
        Agent(id="a", session_id=session_id, name="a", framework=Framework.ADK,
              connected_at=created_at, last_heartbeat=created_at,
              status=AgentStatus.CONNECTED)
    )
    for i in range(n_spans):
        await store.append_span(
            Span(
                id=f"sp_{session_id}_{i}",
                session_id=session_id,
                agent_id="a",
                kind=SpanKind.TOOL_CALL,
                name=f"tool_{i}",
                start_time=created_at + i,
                end_time=created_at + i + 0.5,
                status=SpanStatus.COMPLETED,
            )
        )


# ---- ListSessions ---------------------------------------------------------


async def test_list_sessions_offset_paginates(harness, stub):
    store = harness["store"]
    for i in range(5):
        await _seed_minimal(store, f"sess_p{i}", created_at=1000.0 + i)

    resp = await stub.ListSessions(
        frontend_pb2.ListSessionsRequest(limit=2, offset=0)
    )
    assert len(resp.sessions) == 2
    assert resp.total_count == 5
    page2 = await stub.ListSessions(
        frontend_pb2.ListSessionsRequest(limit=2, offset=2)
    )
    first_ids = {s.id for s in resp.sessions}
    second_ids = {s.id for s in page2.sessions}
    assert first_ids.isdisjoint(second_ids)


async def test_list_sessions_empty_returns_zero_count(harness, stub):
    resp = await stub.ListSessions(frontend_pb2.ListSessionsRequest(limit=10))
    assert resp.total_count == 0
    assert list(resp.sessions) == []


async def test_list_sessions_search_case_insensitive(harness, stub):
    store = harness["store"]
    await store.create_session(
        Session(id="sess_x", title="Codegen Alpha", created_at=1.0,
                status=SessionStatus.LIVE)
    )
    resp = await stub.ListSessions(
        frontend_pb2.ListSessionsRequest(search="CODEGEN")
    )
    assert {s.id for s in resp.sessions} == {"sess_x"}


# ---- WatchSession ---------------------------------------------------------


async def test_watch_session_requires_session_id(harness, stub):
    call = stub.WatchSession(frontend_pb2.WatchSessionRequest())
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        async for _ in call:
            pass
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_watch_session_translates_heartbeat_delta(harness, stub):
    store = harness["store"]
    bus = harness["bus"]
    await _seed_minimal(store, "sess_hb", n_spans=0, created_at=time.time() - 1)

    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_hb"))
    got_status = None

    async def run():
        nonlocal got_status
        burst_done = False
        async for upd in call:
            w = upd.WhichOneof("kind")
            if w == "burst_complete":
                burst_done = True
                bus.publish_heartbeat(
                    "sess_hb", "a",
                    {"buffered_events": 7, "dropped_events": 0,
                     "current_activity": "reading", "progress_counter": 3,
                     "stuck": False, "dropped_spans_critical": 0,
                     "buffered_payload_bytes": 0, "payloads_evicted": 0,
                     "cpu_self_pct": 1.0, "last_heartbeat": time.time()},
                )
            elif w == "agent_status_changed" and burst_done:
                got_status = upd.agent_status_changed
                break

    await asyncio.wait_for(run(), timeout=3.0)
    call.cancel()
    assert got_status is not None
    assert got_status.agent_id == "a"
    assert got_status.current_activity == "reading"


async def test_watch_session_translates_annotation_delta(harness, stub):
    store = harness["store"]
    bus = harness["bus"]
    await _seed_minimal(store, "sess_ann", n_spans=1, created_at=time.time() - 1)

    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_ann"))
    got = None

    async def run():
        nonlocal got
        burst_done = False
        async for upd in call:
            w = upd.WhichOneof("kind")
            if w == "burst_complete":
                burst_done = True
                ann = Annotation(
                    id="ann-new",
                    session_id="sess_ann",
                    target=AnnotationTarget(span_id="sp_sess_ann_0"),
                    author="u",
                    created_at=time.time(),
                    kind=AnnotationKind.COMMENT,
                    body="hey",
                )
                await store.put_annotation(ann)
                bus.publish_annotation(ann)
            elif w == "new_annotation" and burst_done:
                got = upd.new_annotation
                break

    await asyncio.wait_for(run(), timeout=3.0)
    call.cancel()
    assert got is not None
    assert got.annotation.body == "hey"


async def test_watch_session_translates_task_plan_delta(harness, stub):
    store = harness["store"]
    bus = harness["bus"]
    await _seed_minimal(store, "sess_tp", n_spans=0, created_at=time.time() - 1)

    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_tp"))
    got_plan = None

    async def run():
        nonlocal got_plan
        burst_done = False
        async for upd in call:
            w = upd.WhichOneof("kind")
            if w == "burst_complete":
                burst_done = True
                plan = TaskPlan(
                    id="plan-live",
                    session_id="sess_tp",
                    created_at=time.time(),
                    planner_agent_id="a",
                    tasks=[Task(id="t1", title="first")],
                )
                await store.put_task_plan(plan)
                bus.publish_task_plan(plan)
            elif w == "task_plan" and burst_done:
                got_plan = upd.task_plan
                break

    await asyncio.wait_for(run(), timeout=3.0)
    call.cancel()
    assert got_plan is not None
    assert got_plan.id == "plan-live"


async def test_watch_session_translates_task_status_delta(harness, stub):
    store = harness["store"]
    bus = harness["bus"]
    await _seed_minimal(store, "sess_ts", created_at=time.time() - 1)
    plan = TaskPlan(
        id="plan-ts", session_id="sess_ts", created_at=time.time(),
        tasks=[Task(id="t1", title="x")],
    )
    await store.put_task_plan(plan)

    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_ts"))
    got = None

    async def run():
        nonlocal got
        burst_done = False
        async for upd in call:
            w = upd.WhichOneof("kind")
            if w == "burst_complete":
                burst_done = True
                t = Task(id="t1", title="x", status=TaskStatus.RUNNING, bound_span_id="sp1")
                bus.publish_task_status("sess_ts", "plan-ts", t)
            elif w == "updated_task_status" and burst_done:
                got = upd.updated_task_status
                break

    await asyncio.wait_for(run(), timeout=3.0)
    call.cancel()
    assert got is not None
    assert got.plan_id == "plan-ts"
    assert got.task_id == "t1"
    assert got.bound_span_id == "sp1"


async def test_watch_session_task_plan_burst_on_reconnect(harness, stub):
    """Existing plans are replayed during the initial burst."""
    store = harness["store"]
    await _seed_minimal(store, "sess_rp", created_at=time.time() - 1)
    plan = TaskPlan(
        id="plan-rp", session_id="sess_rp", created_at=time.time(),
        tasks=[Task(id="t1", title="x")],
    )
    await store.put_task_plan(plan)

    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_rp"))
    saw_plan = False

    async def run():
        nonlocal saw_plan
        async for upd in call:
            w = upd.WhichOneof("kind")
            if w == "task_plan":
                saw_plan = True
            if w == "burst_complete":
                break

    await asyncio.wait_for(run(), timeout=3.0)
    call.cancel()
    assert saw_plan


# ---- GetSpanTree ----------------------------------------------------------


async def test_get_span_tree_agent_ids_filter(harness, stub):
    store = harness["store"]
    await store.create_session(
        Session(id="sess_ft", title="t", created_at=1.0,
                status=SessionStatus.LIVE)
    )
    for agent_id in ("alpha", "beta"):
        await store.register_agent(
            Agent(id=agent_id, session_id="sess_ft", name=agent_id,
                  framework=Framework.ADK, connected_at=1.0,
                  last_heartbeat=1.0, status=AgentStatus.CONNECTED)
        )
        await store.append_span(
            Span(
                id=f"sp_{agent_id}",
                session_id="sess_ft",
                agent_id=agent_id,
                kind=SpanKind.TOOL_CALL,
                name="x",
                start_time=10.0,
                end_time=20.0,
                status=SpanStatus.COMPLETED,
            )
        )
    resp = await stub.GetSpanTree(
        frontend_pb2.GetSpanTreeRequest(session_id="sess_ft", agent_ids=["alpha"])
    )
    assert {s.id for s in resp.spans} == {"sp_alpha"}


# ---- GetPayload -----------------------------------------------------------


async def test_get_payload_summary_only_empty_bytes(harness, stub):
    store = harness["store"]
    data = b"hello"
    digest = hashlib.sha256(data).hexdigest()
    await store.put_payload(digest, data, "text/plain", summary="hello")

    chunks = []
    async for c in stub.GetPayload(
        frontend_pb2.GetPayloadRequest(digest=digest, summary_only=True)
    ):
        chunks.append(c)
    assert len(chunks) == 1
    assert chunks[0].last is True
    assert chunks[0].chunk == b""
    assert chunks[0].summary == "hello"


async def test_get_payload_missing_digest_argument(harness, stub):
    call = stub.GetPayload(frontend_pb2.GetPayloadRequest())
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        async for _ in call:
            pass
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ---- PostAnnotation -------------------------------------------------------


async def test_post_annotation_steering_target_span_resolves_agent(harness, stub):
    store = harness["store"]
    router = harness["router"]
    await _seed_minimal(store, "sess_stee", n_spans=1)
    # Subscribe so steering has a path.
    sub = await router.subscribe("sess_stee", "a", "str-1")

    async def ack_agent():
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
        router.record_ack(
            types_pb2.ControlAck(
                control_id=event.id,
                result=types_pb2.CONTROL_ACK_RESULT_SUCCESS,
            ),
            stream_id="str-1",
        )

    agent_task = asyncio.create_task(ack_agent())
    resp = await stub.PostAnnotation(
        frontend_pb2.PostAnnotationRequest(
            session_id="sess_stee",
            target=types_pb2.AnnotationTarget(span_id="sp_sess_stee_0"),
            kind=types_pb2.ANNOTATION_KIND_STEERING,
            body="refocus",
            ack_timeout_ms=2000,
        )
    )
    await agent_task
    assert resp.delivery == types_pb2.CONTROL_ACK_RESULT_SUCCESS


async def test_post_annotation_requires_session_id(harness, stub):
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.PostAnnotation(frontend_pb2.PostAnnotationRequest(body="x"))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_post_annotation_steering_without_target_agent_fails(harness, stub):
    store = harness["store"]
    await _seed_minimal(store, "sess_notgt")
    resp = await stub.PostAnnotation(
        frontend_pb2.PostAnnotationRequest(
            session_id="sess_notgt",
            kind=types_pb2.ANNOTATION_KIND_STEERING,
            body="stop",
            ack_timeout_ms=100,
        )
    )
    assert resp.delivery == types_pb2.CONTROL_ACK_RESULT_FAILURE
    assert "no target agent" in resp.delivery_detail


# ---- SendControl ----------------------------------------------------------


async def test_send_control_requires_session_and_agent(harness, stub):
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.SendControl(
            frontend_pb2.SendControlRequest(session_id="", target=types_pb2.ControlTarget())
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_send_control_require_all_acks_honored(harness, stub):
    router = harness["router"]
    sub_a = await router.subscribe("sess_all", "ag", "sa")
    sub_b = await router.subscribe("sess_all", "ag", "sb")

    async def ack_both():
        ev_a = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
        ev_b = await asyncio.wait_for(sub_b.queue.get(), timeout=1)
        router.record_ack(
            types_pb2.ControlAck(
                control_id=ev_a.id, result=types_pb2.CONTROL_ACK_RESULT_SUCCESS
            ),
            stream_id="sa",
        )
        router.record_ack(
            types_pb2.ControlAck(
                control_id=ev_b.id, result=types_pb2.CONTROL_ACK_RESULT_SUCCESS
            ),
            stream_id="sb",
        )

    task = asyncio.create_task(ack_both())
    resp = await stub.SendControl(
        frontend_pb2.SendControlRequest(
            session_id="sess_all",
            target=types_pb2.ControlTarget(agent_id="ag"),
            kind=types_pb2.CONTROL_KIND_STEER,
            ack_timeout_ms=2000,
            require_all_acks=True,
        )
    )
    await task
    assert resp.result == types_pb2.CONTROL_ACK_RESULT_SUCCESS
    assert len(resp.acks) == 2


# ---- DeleteSession --------------------------------------------------------


async def test_delete_session_reports_payload_bytes_freed(harness, stub):
    store = harness["store"]
    await store.create_session(
        Session(id="sess_pl", title="t", created_at=1.0,
                status=SessionStatus.COMPLETED)
    )
    await store.register_agent(
        Agent(id="a", session_id="sess_pl", name="a",
              framework=Framework.ADK, connected_at=1.0,
              last_heartbeat=1.0, status=AgentStatus.CONNECTED)
    )
    data = b"payload-bytes-here"
    digest = hashlib.sha256(data).hexdigest()
    await store.put_payload(digest, data, "text/plain")
    sp = Span(
        id="sp1",
        session_id="sess_pl",
        agent_id="a",
        kind=SpanKind.TOOL_CALL,
        name="x",
        start_time=1.0,
        end_time=2.0,
        status=SpanStatus.COMPLETED,
        payload_digest=digest,
        payload_size=len(data),
        payload_mime="text/plain",
    )
    await store.append_span(sp)

    resp = await stub.DeleteSession(
        frontend_pb2.DeleteSessionRequest(session_id="sess_pl")
    )
    assert resp.deleted is True
    assert resp.payload_bytes_freed == len(data)


async def test_delete_session_requires_session_id(harness, stub):
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.DeleteSession(frontend_pb2.DeleteSessionRequest())
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ---- GetStats -------------------------------------------------------------


async def test_get_stats_counts_payloads_and_bytes(harness, stub):
    store = harness["store"]
    data = b"x" * 512
    digest = hashlib.sha256(data).hexdigest()
    await store.put_payload(digest, data, "application/octet-stream")
    resp = await stub.GetStats(frontend_pb2.GetStatsRequest())
    assert resp.payload_count >= 1
    assert resp.payload_bytes >= 512
    assert resp.data_dir == "/var/harmonograf"


async def test_get_stats_active_streams_reflect_live_subscriptions(harness, stub):
    router = harness["router"]
    sub = await router.subscribe("sess_s", "ag", "sid-1")
    resp = await stub.GetStats(frontend_pb2.GetStatsRequest())
    assert resp.active_control_streams >= 1
    await router.unsubscribe(sub)
