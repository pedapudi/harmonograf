"""Storage suite — runs against every backend via parametrized fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    Framework,
    LinkRelation,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanLink,
    SpanStatus,
    ContextWindowSample,
    make_store,
)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path: Path):
    if request.param == "memory":
        s = make_store("memory")
    else:
        s = make_store("sqlite", db_path=tmp_path / "harmonograf.db")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _make_session(sid: str = "sess_2026-04-10_alpha") -> Session:
    return Session(
        id=sid,
        title="Mission Alpha",
        created_at=1_700_000_000.0,
        status=SessionStatus.LIVE,
        metadata={"task": "research"},
    )


def _make_agent(session_id: str, agent_id: str = "research-agent") -> Agent:
    return Agent(
        id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=Framework.ADK,
        framework_version="1.0",
        capabilities=[Capability.PAUSE_RESUME, Capability.STEERING],
        metadata={"region": "local"},
        connected_at=1_700_000_001.0,
        last_heartbeat=1_700_000_001.0,
        status=AgentStatus.CONNECTED,
    )


def _make_span(session_id: str, agent_id: str, span_id: str, start: float, end: float | None) -> Span:
    return Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=SpanKind.TOOL_CALL,
        name="search_web",
        start_time=start,
        end_time=end,
        status=SpanStatus.COMPLETED if end is not None else SpanStatus.RUNNING,
        attributes={"q": "harmonograph"},
    )


# --- tests ------------------------------------------------------------------


async def test_session_round_trip(store):
    sess = _make_session()
    out = await store.create_session(sess)
    assert out.id == sess.id
    fetched = await store.get_session(sess.id)
    assert fetched is not None
    assert fetched.title == "Mission Alpha"
    assert fetched.metadata == {"task": "research"}
    listed = await store.list_sessions()
    assert any(s.id == sess.id for s in listed)


async def test_session_create_idempotent(store):
    sess = _make_session()
    await store.create_session(sess)
    again = await store.create_session(_make_session())
    assert again.id == sess.id
    listed = await store.list_sessions()
    assert sum(1 for s in listed if s.id == sess.id) == 1


async def test_session_update_metadata(store):
    sess = _make_session()
    await store.create_session(sess)
    updated = await store.update_session(
        sess.id,
        status=SessionStatus.COMPLETED,
        ended_at=1_700_000_500.0,
        metadata={"outcome": "ok"},
    )
    assert updated is not None
    assert updated.status == SessionStatus.COMPLETED
    assert updated.ended_at == 1_700_000_500.0
    assert updated.metadata["outcome"] == "ok"
    assert updated.metadata["task"] == "research"


async def test_agent_round_trip(store):
    sess = _make_session()
    await store.create_session(sess)
    agent = _make_agent(sess.id)
    await store.register_agent(agent)
    fetched = await store.get_agent(sess.id, agent.id)
    assert fetched is not None
    assert fetched.framework == Framework.ADK
    assert Capability.STEERING in fetched.capabilities

    await store.update_agent_status(
        sess.id, agent.id, AgentStatus.DISCONNECTED, last_heartbeat=1_700_000_999.0
    )
    fetched2 = await store.get_agent(sess.id, agent.id)
    assert fetched2.status == AgentStatus.DISCONNECTED
    assert fetched2.last_heartbeat == 1_700_000_999.0

    listed = await store.list_agents_for_session(sess.id)
    assert len(listed) == 1


async def test_span_round_trip_with_links(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    sp = _make_span(sess.id, "research-agent", "span-1", 100.0, 110.0)
    sp.links = [
        SpanLink(
            target_span_id="span-other",
            target_agent_id="planner",
            relation=LinkRelation.TRIGGERED_BY,
        )
    ]
    await store.append_span(sp)
    out = await store.get_span("span-1")
    assert out is not None
    assert out.name == "search_web"
    assert out.end_time == 110.0
    assert len(out.links) == 1
    assert out.links[0].relation == LinkRelation.TRIGGERED_BY


async def test_span_append_idempotent(store):
    """Reconnect replays the same span; second append must not duplicate."""
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    sp = _make_span(sess.id, "research-agent", "span-dupe", 100.0, 110.0)
    await store.append_span(sp)
    sp2 = _make_span(sess.id, "research-agent", "span-dupe", 100.0, 110.0)
    sp2.attributes["mutated"] = "ignored"
    await store.append_span(sp2)
    spans = await store.get_spans(sess.id)
    matching = [s for s in spans if s.id == "span-dupe"]
    assert len(matching) == 1
    # First-write-wins
    assert "mutated" not in matching[0].attributes


async def test_span_update_and_end(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    sp = _make_span(sess.id, "research-agent", "span-2", 100.0, None)
    await store.append_span(sp)

    await store.update_span("span-2", attributes={"k": "v"}, status=SpanStatus.RUNNING)
    after = await store.get_span("span-2")
    assert after.attributes == {"q": "harmonograph", "k": "v"}

    ended = await store.end_span("span-2", end_time=125.0, status=SpanStatus.COMPLETED)
    assert ended.end_time == 125.0
    assert ended.status == SpanStatus.COMPLETED


async def test_span_time_range_query(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    await store.register_agent(_make_agent(sess.id, "planner"))
    await store.append_span(_make_span(sess.id, "research-agent", "a", 100.0, 110.0))
    await store.append_span(_make_span(sess.id, "research-agent", "b", 200.0, 250.0))
    await store.append_span(_make_span(sess.id, "research-agent", "c", 400.0, 410.0))
    await store.append_span(_make_span(sess.id, "planner", "d", 150.0, 160.0))

    all_spans = await store.get_spans(sess.id)
    assert len(all_spans) == 4

    only_research = await store.get_spans(sess.id, agent_id="research-agent")
    assert {s.id for s in only_research} == {"a", "b", "c"}

    window = await store.get_spans(sess.id, time_start=140.0, time_end=260.0)
    ids = {s.id for s in window}
    assert "b" in ids
    assert "d" in ids
    assert "a" not in ids
    assert "c" not in ids


async def test_payload_dedup(store):
    data = b"hello world payload" * 100
    digest = _sha(data)
    meta1 = await store.put_payload(digest, data, mime="text/plain", summary="hello...")
    meta2 = await store.put_payload(digest, data, mime="text/plain", summary="hello...")
    assert meta1.digest == meta2.digest == digest
    assert meta1.size == len(data)
    assert await store.has_payload(digest)
    rec = await store.get_payload(digest)
    assert rec is not None
    assert rec.bytes_ == data
    stats = await store.stats()
    assert stats.payload_count == 1
    assert stats.payload_bytes == len(data)


async def test_annotation_round_trip(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    await store.append_span(_make_span(sess.id, "research-agent", "span-x", 100.0, 110.0))
    ann = Annotation(
        id="ann-1",
        session_id=sess.id,
        target=AnnotationTarget(span_id="span-x"),
        author="user",
        created_at=200.0,
        kind=AnnotationKind.COMMENT,
        body="why this tool?",
    )
    await store.put_annotation(ann)
    by_span = await store.list_annotations(span_id="span-x")
    assert len(by_span) == 1
    by_session = await store.list_annotations(session_id=sess.id)
    assert len(by_session) == 1


async def test_stats_accuracy(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    await store.append_span(_make_span(sess.id, "research-agent", "s1", 100.0, 110.0))
    await store.append_span(_make_span(sess.id, "research-agent", "s2", 120.0, 130.0))
    data = b"x" * 4096
    await store.put_payload(_sha(data), data, mime="application/octet-stream")

    stats = await store.stats()
    assert stats.session_count == 1
    assert stats.agent_count == 1
    assert stats.span_count == 2
    assert stats.payload_count == 1
    assert stats.payload_bytes == 4096


async def test_context_window_sample_roundtrip(store):
    # Server-side plumbing for task #2: append per-agent samples, list
    # them back in (agent_id, recorded_at) order, and confirm the
    # per-agent cap is enforced independently.
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id, agent_id="agent-a"))
    await store.register_agent(_make_agent(sess.id, agent_id="agent-b"))

    base = 1_700_000_100.0
    samples = [
        ContextWindowSample(sess.id, "agent-a", base + 0, 1000, 128000),
        ContextWindowSample(sess.id, "agent-a", base + 5, 1500, 128000),
        ContextWindowSample(sess.id, "agent-b", base + 2, 800, 200000),
        ContextWindowSample(sess.id, "agent-b", base + 4, 1200, 200000),
    ]
    for s in samples:
        await store.append_context_window_sample(s)

    all_samples = await store.list_context_window_samples(sess.id)
    # Sorted by (agent_id, recorded_at).
    assert [(s.agent_id, s.tokens) for s in all_samples] == [
        ("agent-a", 1000),
        ("agent-a", 1500),
        ("agent-b", 800),
        ("agent-b", 1200),
    ]
    assert all(s.limit_tokens in (128000, 200000) for s in all_samples)

    only_a = await store.list_context_window_samples(sess.id, agent_id="agent-a")
    assert [s.tokens for s in only_a] == [1000, 1500]

    capped = await store.list_context_window_samples(
        sess.id, agent_id="agent-a", limit_per_agent=1
    )
    assert len(capped) == 1
    assert capped[0].tokens == 1500  # most recent


async def test_delete_session_cascades(store):
    sess = _make_session()
    await store.create_session(sess)
    await store.register_agent(_make_agent(sess.id))
    await store.append_span(_make_span(sess.id, "research-agent", "s1", 100.0, 110.0))
    assert await store.delete_session(sess.id) is True
    assert await store.get_session(sess.id) is None
    assert await store.get_span("s1") is None
    assert await store.delete_session(sess.id) is False
