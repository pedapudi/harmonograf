"""End-to-end tests for the frontend-facing RPCs (doc 01 §4.6, doc 03 §7).

These drive a real grpc.aio server (TelemetryServicer composes the
FrontendServicerMixin) backed by an InMemoryStore and exercise every
frontend RPC over an insecure loopback channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import AsyncIterator, Iterable

import grpc
import pytest
import pytest_asyncio
from google.protobuf.timestamp_pb2 import Timestamp

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

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
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
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
    servicer = TelemetryServicer(ingest, router=router, data_dir="/tmp/hg-test")

    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {
            "server": server,
            "port": port,
            "store": store,
            "bus": bus,
            "router": router,
            "ingest": ingest,
            "servicer": servicer,
        }
    finally:
        await server.stop(grace=0.5)


@pytest_asyncio.fixture
async def stub(harness):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{harness['port']}")
    try:
        yield service_pb2_grpc.HarmonografStub(ch)
    finally:
        await ch.close()


# ---- helpers --------------------------------------------------------------


def _ts(sec: float) -> Timestamp:
    t = Timestamp()
    t.seconds = int(sec)
    t.nanos = int((sec - int(sec)) * 1e9)
    return t


async def _seed_session(
    store,
    *,
    session_id: str = "sess_demo",
    title: str = "demo",
    status: SessionStatus = SessionStatus.LIVE,
    agent_id: str = "agent-1",
    n_spans: int = 3,
    start: float = 1_000_000.0,
    awaiting: int = 0,
) -> tuple[Session, Agent, list[Span]]:
    sess = Session(
        id=session_id,
        title=title,
        created_at=start,
        status=status,
        agent_ids=[agent_id],
        metadata={"k": "v"},
    )
    await store.create_session(sess)
    agent = Agent(
        id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=Framework.ADK,
        framework_version="0.1.0",
        capabilities=[Capability.PAUSE_RESUME],
        connected_at=start,
        last_heartbeat=start,
        status=AgentStatus.CONNECTED,
    )
    await store.register_agent(agent)
    spans: list[Span] = []
    for i in range(n_spans):
        st = SpanStatus.AWAITING_HUMAN if i < awaiting else SpanStatus.COMPLETED
        sp = Span(
            id=f"sp_{session_id}_{i}",
            session_id=session_id,
            agent_id=agent_id,
            kind=SpanKind.TOOL_CALL,
            name=f"tool_{i}",
            start_time=start + i,
            end_time=start + i + 0.5,
            status=st,
            attributes={"i": i},
        )
        await store.append_span(sp)
        spans.append(sp)
    return sess, agent, spans


# ---- ListSessions ---------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_returns_pagination_and_counts(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_a", title="alpha", n_spans=2, awaiting=1)
    await _seed_session(store, session_id="sess_b", title="beta", n_spans=4)
    await _seed_session(
        store, session_id="sess_c", title="gamma", n_spans=1, status=SessionStatus.COMPLETED
    )

    resp = await stub.ListSessions(frontend_pb2.ListSessionsRequest(limit=10))
    ids = sorted(s.id for s in resp.sessions)
    assert ids == ["sess_a", "sess_b", "sess_c"]
    assert resp.total_count == 3

    by_id = {s.id: s for s in resp.sessions}
    assert by_id["sess_a"].attention_count == 1
    assert by_id["sess_a"].agent_count == 1
    assert by_id["sess_b"].attention_count == 0


@pytest.mark.asyncio
async def test_list_sessions_status_filter_and_search(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_live_1", title="research")
    await _seed_session(store, session_id="sess_live_2", title="codegen")
    await _seed_session(
        store, session_id="sess_done", title="research-old", status=SessionStatus.COMPLETED
    )

    live = await stub.ListSessions(
        frontend_pb2.ListSessionsRequest(status_filter=types_pb2.SESSION_STATUS_LIVE)
    )
    assert {s.id for s in live.sessions} == {"sess_live_1", "sess_live_2"}

    searched = await stub.ListSessions(frontend_pb2.ListSessionsRequest(search="research"))
    assert {s.id for s in searched.sessions} == {"sess_live_1", "sess_done"}


# ---- WatchSession ---------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_session_initial_burst_and_live_delta(harness, stub):
    store = harness["store"]
    bus = harness["bus"]
    now = time.time()
    _, agent, spans = await _seed_session(
        store, session_id="sess_watch", n_spans=2, start=now - 5
    )

    req = frontend_pb2.WatchSessionRequest(session_id="sess_watch")
    call = stub.WatchSession(req)

    seen = {"session": False, "agent": False, "spans": 0, "burst": False}
    tail_span = None

    async def consumer():
        nonlocal tail_span
        async for upd in call:
            which = upd.WhichOneof("kind")
            if which == "session":
                seen["session"] = True
            elif which == "agent":
                seen["agent"] = True
            elif which == "initial_span":
                seen["spans"] += 1
            elif which == "burst_complete":
                seen["burst"] = True
                # Publish a live delta only after burst_complete so the
                # consumer sees it as tail data, not part of the replay.
                new = Span(
                    id="sp_tail",
                    session_id="sess_watch",
                    agent_id=agent.id,
                    kind=SpanKind.LLM_CALL,
                    name="tail",
                    start_time=now,
                    status=SpanStatus.RUNNING,
                )
                await store.append_span(new)
                bus.publish_span_start(new)
            elif which == "new_span":
                tail_span = upd.new_span.span
                break

    await asyncio.wait_for(consumer(), timeout=5.0)
    call.cancel()
    assert seen == {"session": True, "agent": True, "spans": 2, "burst": True}
    assert tail_span is not None
    assert tail_span.id == "sp_tail"


@pytest.mark.asyncio
async def test_watch_session_unknown_returns_not_found(harness, stub):
    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id="sess_nope"))
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        async for _ in call:
            pass
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_watch_session_replays_persisted_plan_as_goldfive_events(
    harness, stub
):
    """Initial burst must include a plan_submitted + task_* events for the
    persisted plan. Regression for issue #12: Phase B wired ingest but not
    the WatchSession delivery path, so the UI always saw an empty Tasks
    panel on reconnect."""

    from harmonograf_server.storage import Task, TaskPlan, TaskStatus

    store = harness["store"]
    bus = harness["bus"]
    now = time.time()
    await _seed_session(store, session_id="sess_gf", n_spans=0, start=now - 10)

    plan = TaskPlan(
        id="plan-1",
        session_id="sess_gf",
        created_at=now - 8,
        planner_agent_id="agent-1",
        summary="Three echo tasks.",
        tasks=[
            Task(id="t1", title="first", status=TaskStatus.COMPLETED),
            Task(id="t2", title="second", status=TaskStatus.COMPLETED),
            Task(id="t3", title="third", status=TaskStatus.RUNNING),
            Task(id="t4", title="fourth", status=TaskStatus.PENDING),
        ],
    )
    await store.put_task_plan(plan)

    req = frontend_pb2.WatchSessionRequest(session_id="sess_gf")
    call = stub.WatchSession(req)

    plan_events: list = []
    task_events: list[tuple[str, str]] = []  # (kind, task_id)
    burst_seen = False
    live_run_completed = None

    async def consumer():
        nonlocal burst_seen, live_run_completed
        async for upd in call:
            which = upd.WhichOneof("kind")
            if which == "goldfive_event":
                kind = upd.goldfive_event.WhichOneof("payload")
                if kind == "plan_submitted":
                    plan_events.append(upd.goldfive_event.plan_submitted.plan)
                elif kind == "task_started":
                    task_events.append(("task_started", upd.goldfive_event.task_started.task_id))
                elif kind == "task_completed":
                    task_events.append(
                        ("task_completed", upd.goldfive_event.task_completed.task_id)
                    )
                elif kind == "run_completed":
                    live_run_completed = upd.goldfive_event.run_completed.outcome_summary
                    break
            elif which == "burst_complete":
                burst_seen = True
                # Publish a live goldfive delta after the burst so we can
                # assert the subscribe-path conversion as well.
                bus.publish_run_completed(
                    "sess_gf", "run-1", outcome_summary="done"
                )

    await asyncio.wait_for(consumer(), timeout=5.0)
    call.cancel()

    assert burst_seen, "burst_complete never arrived"
    assert len(plan_events) == 1, f"expected one plan_submitted, got {len(plan_events)}"
    replayed_plan = plan_events[0]
    assert replayed_plan.id == "plan-1"
    assert [t.id for t in replayed_plan.tasks] == ["t1", "t2", "t3", "t4"]
    # Two completed tasks + one running — each completed task emits
    # task_started + task_completed; the running task emits task_started.
    assert ("task_completed", "t1") in task_events
    assert ("task_completed", "t2") in task_events
    assert task_events.count(("task_started", "t1")) == 1
    assert task_events.count(("task_started", "t2")) == 1
    assert ("task_started", "t3") in task_events
    # PENDING task t4 must not have emitted any events.
    assert all(tid != "t4" for _, tid in task_events)
    # Live path: the run_completed delta landed on the stream.
    assert live_run_completed == "done"


# ---- GetPayload -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_payload_single_chunk_and_not_found(harness, stub):
    store = harness["store"]
    data = b'{"prompt": "hello world"}'
    digest = hashlib.sha256(data).hexdigest()
    await store.put_payload(digest, data, "application/json", summary=data.decode()[:50])

    chunks: list[frontend_pb2.PayloadChunk] = []
    async for c in stub.GetPayload(frontend_pb2.GetPayloadRequest(digest=digest)):
        chunks.append(c)
    assert len(chunks) >= 1
    assert chunks[0].digest == digest
    assert chunks[0].mime == "application/json"
    assert chunks[-1].last is True
    reassembled = b"".join(c.chunk for c in chunks)
    assert reassembled == data

    # summary_only path: no bytes, but summary + last=True
    only = []
    async for c in stub.GetPayload(
        frontend_pb2.GetPayloadRequest(digest=digest, summary_only=True)
    ):
        only.append(c)
    assert len(only) == 1
    assert only[0].summary
    assert only[0].last is True
    assert only[0].chunk == b""

    # missing
    missing = []
    async for c in stub.GetPayload(frontend_pb2.GetPayloadRequest(digest="deadbeef")):
        missing.append(c)
    assert len(missing) == 1
    assert missing[0].not_found is True


@pytest.mark.asyncio
async def test_get_payload_multi_chunk(harness, stub):
    store = harness["store"]
    data = b"x" * (256 * 1024 + 1024)  # 257 KiB, forces two chunks
    digest = hashlib.sha256(data).hexdigest()
    await store.put_payload(digest, data, "application/octet-stream")

    chunks = []
    async for c in stub.GetPayload(frontend_pb2.GetPayloadRequest(digest=digest)):
        chunks.append(c)
    assert len(chunks) >= 2
    assert chunks[-1].last is True
    # First chunk carries metadata but no payload bytes (too big to inline);
    # subsequent chunks carry the bytes.
    reassembled = b"".join(c.chunk for c in chunks)
    assert reassembled == data


# ---- GetSpanTree ----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_span_tree_returns_sorted_spans(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_tree", n_spans=5, start=2000.0)

    resp = await stub.GetSpanTree(
        frontend_pb2.GetSpanTreeRequest(session_id="sess_tree")
    )
    assert len(resp.spans) == 5
    times = [s.start_time.seconds + s.start_time.nanos / 1e9 for s in resp.spans]
    assert times == sorted(times)
    assert not resp.truncated


@pytest.mark.asyncio
async def test_get_span_tree_truncation(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_trunc", n_spans=5, start=3000.0)
    resp = await stub.GetSpanTree(
        frontend_pb2.GetSpanTreeRequest(session_id="sess_trunc", limit=2)
    )
    assert len(resp.spans) == 2
    assert resp.truncated is True


@pytest.mark.asyncio
async def test_get_span_tree_requires_session_id(harness, stub):
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetSpanTree(frontend_pb2.GetSpanTreeRequest())
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ---- PostAnnotation -------------------------------------------------------


@pytest.mark.asyncio
async def test_post_annotation_comment_skips_control(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_ann")
    req = frontend_pb2.PostAnnotationRequest(
        session_id="sess_ann",
        target=types_pb2.AnnotationTarget(span_id="sp_sess_ann_0"),
        kind=types_pb2.ANNOTATION_KIND_COMMENT,
        body="note to self",
        author="me",
    )
    resp = await stub.PostAnnotation(req)
    assert resp.delivery == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
    assert resp.annotation.body == "note to self"

    stored = await store.list_annotations(session_id="sess_ann")
    assert len(stored) == 1
    assert stored[0].kind == AnnotationKind.COMMENT


@pytest.mark.asyncio
async def test_post_annotation_steering_no_live_agent_is_unavailable(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_steer", agent_id="agent-x")
    req = frontend_pb2.PostAnnotationRequest(
        session_id="sess_steer",
        target=types_pb2.AnnotationTarget(span_id="sp_sess_steer_0"),
        kind=types_pb2.ANNOTATION_KIND_STEERING,
        body="stop that",
        ack_timeout_ms=200,
    )
    resp = await stub.PostAnnotation(req)
    assert resp.delivery == gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
    assert "offline" in resp.delivery_detail


# ---- SendControl ----------------------------------------------------------


@pytest.mark.asyncio
async def test_send_control_unavailable_with_no_subscription(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_ctl", agent_id="agent-ctl")
    req = frontend_pb2.SendControlRequest(
        session_id="sess_ctl",
        event=gf_control_pb2.ControlEvent(
            kind=gf_control_pb2.CONTROL_KIND_PAUSE,
            target=gf_control_pb2.ControlTarget(agent_id="agent-ctl"),
        ),
        ack_timeout_ms=200,
    )
    resp = await stub.SendControl(req)
    assert resp.result == gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED
    assert resp.control_id


@pytest.mark.asyncio
async def test_send_control_delivers_to_live_subscription_and_resolves_on_ack(
    harness, stub
):
    router: ControlRouter = harness["router"]
    store = harness["store"]
    await _seed_session(store, session_id="sess_ctl2", agent_id="agent-ctl2")

    sub = await router.subscribe("sess_ctl2", "agent-ctl2", "stream-xyz")

    async def agent_coroutine():
        event = await sub.queue.get()
        # Simulate ack coming back via the telemetry ingest path.
        ack = gf_control_pb2.ControlAck(
            control_id=event.id,
            result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
        )
        router.record_ack(ack, stream_id="stream-xyz")

    agent_task = asyncio.create_task(agent_coroutine())

    req = frontend_pb2.SendControlRequest(
        session_id="sess_ctl2",
        event=gf_control_pb2.ControlEvent(
            kind=gf_control_pb2.CONTROL_KIND_PAUSE,
            target=gf_control_pb2.ControlTarget(agent_id="agent-ctl2"),
        ),
        ack_timeout_ms=2000,
    )
    resp = await stub.SendControl(req)
    await agent_task
    assert resp.result == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
    assert len(resp.acks) == 1
    assert resp.acks[0].stream_id == "stream-xyz"


# ---- DeleteSession --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_live_blocked_without_force(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_del1", n_spans=3)
    resp = await stub.DeleteSession(
        frontend_pb2.DeleteSessionRequest(session_id="sess_del1")
    )
    assert resp.deleted is False
    assert "LIVE" in resp.reason_if_not

    still = await store.get_session("sess_del1")
    assert still is not None


@pytest.mark.asyncio
async def test_delete_session_completed_deletes_rows(harness, stub):
    store = harness["store"]
    await _seed_session(
        store, session_id="sess_del2", n_spans=2, status=SessionStatus.COMPLETED
    )
    ann = Annotation(
        id="ann1",
        session_id="sess_del2",
        target=AnnotationTarget(span_id="sp_sess_del2_0"),
        author="user",
        created_at=time.time(),
        kind=AnnotationKind.COMMENT,
        body="gone",
    )
    await store.put_annotation(ann)

    resp = await stub.DeleteSession(
        frontend_pb2.DeleteSessionRequest(session_id="sess_del2")
    )
    assert resp.deleted is True
    assert resp.spans_removed == 2
    assert resp.annotations_removed == 1
    assert await store.get_session("sess_del2") is None


@pytest.mark.asyncio
async def test_delete_session_force_removes_live(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_del3", n_spans=1)
    resp = await stub.DeleteSession(
        frontend_pb2.DeleteSessionRequest(session_id="sess_del3", force=True)
    )
    assert resp.deleted is True
    assert await store.get_session("sess_del3") is None


@pytest.mark.asyncio
async def test_delete_session_unknown_returns_not_found(harness, stub):
    resp = await stub.DeleteSession(
        frontend_pb2.DeleteSessionRequest(session_id="sess_nope")
    )
    assert resp.deleted is False
    assert "not found" in resp.reason_if_not


# ---- GetStats -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stats_reflects_contents(harness, stub):
    store = harness["store"]
    await _seed_session(store, session_id="sess_s1", n_spans=3)
    await _seed_session(
        store, session_id="sess_s2", n_spans=1, status=SessionStatus.COMPLETED
    )

    resp = await stub.GetStats(frontend_pb2.GetStatsRequest())
    assert resp.session_count == 2
    assert resp.live_session_count == 1
    assert resp.span_count == 4
    assert resp.agent_count == 2
    assert resp.data_dir == "/tmp/hg-test"
