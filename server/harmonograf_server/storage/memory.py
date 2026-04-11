"""In-memory store. Per-agent interval tree for span range queries.

Suitable for ephemeral sessions, tests, and the default `harmonograf-server`
launch when no on-disk path is configured.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Optional

from intervaltree import Interval, IntervalTree

from harmonograf_server.storage.base import (
    Agent,
    AgentStatus,
    Annotation,
    PayloadMeta,
    PayloadRecord,
    Session,
    SessionStatus,
    Span,
    SpanStatus,
    Stats,
    Store,
)


class InMemoryStore(Store):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._agents: dict[tuple[str, str], Agent] = {}  # (session_id, agent_id)
        self._spans: dict[str, Span] = {}
        # session_id -> agent_id -> IntervalTree of span_id intervals
        self._trees: dict[str, dict[str, IntervalTree]] = {}
        self._annotations: dict[str, Annotation] = {}
        self._payloads: dict[str, PayloadRecord] = {}
        self._payload_refcount: dict[str, int] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # sessions ------------------------------------------------------------
    async def create_session(self, session: Session) -> Session:
        async with self._lock:
            if session.id in self._sessions:
                return copy.deepcopy(self._sessions[session.id])
            self._sessions[session.id] = copy.deepcopy(session)
            self._trees.setdefault(session.id, {})
            return copy.deepcopy(session)

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            s = self._sessions.get(session_id)
            return copy.deepcopy(s) if s else None

    async def list_sessions(
        self,
        status: Optional[SessionStatus] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        async with self._lock:
            out = [copy.deepcopy(s) for s in self._sessions.values()]
        if status is not None:
            out = [s for s in out if s.status == status]
        out.sort(key=lambda s: s.created_at, reverse=True)
        if limit is not None:
            out = out[:limit]
        return out

    async def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        ended_at: Optional[float] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Optional[Session]:
        async with self._lock:
            s = self._sessions.get(session_id)
            if not s:
                return None
            if title is not None:
                s.title = title
            if status is not None:
                s.status = status
            if ended_at is not None:
                s.ended_at = ended_at
            if metadata is not None:
                s.metadata.update(metadata)
            return copy.deepcopy(s)

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._trees.pop(session_id, None)
            agent_keys = [k for k in self._agents if k[0] == session_id]
            for k in agent_keys:
                del self._agents[k]
            span_ids = [
                sid for sid, sp in self._spans.items() if sp.session_id == session_id
            ]
            for sid in span_ids:
                sp = self._spans.pop(sid)
                if sp.payload_digest:
                    self._decref_payload(sp.payload_digest)
            ann_ids = [
                aid for aid, a in self._annotations.items() if a.session_id == session_id
            ]
            for aid in ann_ids:
                del self._annotations[aid]
            return True

    # agents --------------------------------------------------------------
    async def register_agent(self, agent: Agent) -> Agent:
        async with self._lock:
            key = (agent.session_id, agent.id)
            existing = self._agents.get(key)
            if existing:
                existing.name = agent.name or existing.name
                existing.framework = agent.framework
                existing.framework_version = agent.framework_version
                existing.capabilities = list(agent.capabilities)
                existing.metadata.update(agent.metadata)
                existing.connected_at = agent.connected_at or existing.connected_at
                existing.last_heartbeat = agent.last_heartbeat or existing.last_heartbeat
                existing.status = agent.status
                return copy.deepcopy(existing)
            self._agents[key] = copy.deepcopy(agent)
            sess = self._sessions.get(agent.session_id)
            if sess and agent.id not in sess.agent_ids:
                sess.agent_ids.append(agent.id)
            self._trees.setdefault(agent.session_id, {}).setdefault(
                agent.id, IntervalTree()
            )
            return copy.deepcopy(agent)

    async def get_agent(self, session_id: str, agent_id: str) -> Optional[Agent]:
        async with self._lock:
            a = self._agents.get((session_id, agent_id))
            return copy.deepcopy(a) if a else None

    async def list_agents_for_session(self, session_id: str) -> list[Agent]:
        async with self._lock:
            return [
                copy.deepcopy(a)
                for (sid, _aid), a in self._agents.items()
                if sid == session_id
            ]

    async def update_agent_status(
        self,
        session_id: str,
        agent_id: str,
        status: AgentStatus,
        last_heartbeat: Optional[float] = None,
    ) -> None:
        async with self._lock:
            a = self._agents.get((session_id, agent_id))
            if not a:
                return
            a.status = status
            if last_heartbeat is not None:
                a.last_heartbeat = last_heartbeat

    # spans ---------------------------------------------------------------
    async def append_span(self, span: Span) -> Span:
        async with self._lock:
            existing = self._spans.get(span.id)
            if existing is not None:
                # idempotent
                return copy.deepcopy(existing)
            self._spans[span.id] = copy.deepcopy(span)
            tree = self._trees.setdefault(span.session_id, {}).setdefault(
                span.agent_id, IntervalTree()
            )
            self._index_span(tree, span)
            return copy.deepcopy(span)

    def _index_span(self, tree: IntervalTree, span: Span) -> None:
        end = span.end_time if span.end_time is not None else span.start_time
        # intervaltree requires begin < end
        if end <= span.start_time:
            end = span.start_time + 1e-9
        tree.add(Interval(span.start_time, end, span.id))

    async def update_span(
        self,
        span_id: str,
        *,
        status: Optional[SpanStatus] = None,
        attributes: Optional[dict] = None,
        payload_digest: Optional[str] = None,
        error: Optional[dict] = None,
    ) -> Optional[Span]:
        async with self._lock:
            sp = self._spans.get(span_id)
            if not sp:
                return None
            if status is not None:
                sp.status = status
            if attributes:
                sp.attributes.update(attributes)
            if payload_digest is not None and sp.payload_digest != payload_digest:
                if sp.payload_digest:
                    self._decref_payload(sp.payload_digest)
                sp.payload_digest = payload_digest
                if payload_digest in self._payloads:
                    self._payload_refcount[payload_digest] = (
                        self._payload_refcount.get(payload_digest, 0) + 1
                    )
            if error is not None:
                sp.error = error
            return copy.deepcopy(sp)

    async def end_span(
        self,
        span_id: str,
        end_time: float,
        status: SpanStatus,
        error: Optional[dict] = None,
    ) -> Optional[Span]:
        async with self._lock:
            sp = self._spans.get(span_id)
            if not sp:
                return None
            tree = self._trees.get(sp.session_id, {}).get(sp.agent_id)
            if tree is not None:
                # remove old interval, re-insert
                to_remove = [iv for iv in tree if iv.data == span_id]
                for iv in to_remove:
                    tree.remove(iv)
            sp.end_time = end_time
            sp.status = status
            if error is not None:
                sp.error = error
            if tree is not None:
                self._index_span(tree, sp)
            return copy.deepcopy(sp)

    async def get_span(self, span_id: str) -> Optional[Span]:
        async with self._lock:
            sp = self._spans.get(span_id)
            return copy.deepcopy(sp) if sp else None

    async def get_spans(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[Span]:
        async with self._lock:
            agent_trees = self._trees.get(session_id, {})
            agent_ids = [agent_id] if agent_id else list(agent_trees.keys())
            results: list[Span] = []
            for aid in agent_ids:
                tree = agent_trees.get(aid)
                if not tree:
                    continue
                if time_start is None and time_end is None:
                    intervals = list(tree)
                else:
                    lo = time_start if time_start is not None else float("-inf")
                    hi = time_end if time_end is not None else float("inf")
                    intervals = list(tree.overlap(lo, hi))
                for iv in intervals:
                    sp = self._spans.get(iv.data)
                    if sp:
                        results.append(copy.deepcopy(sp))
            results.sort(key=lambda s: s.start_time)
            if limit is not None:
                results = results[:limit]
            return results

    # annotations ---------------------------------------------------------
    async def put_annotation(self, annotation: Annotation) -> Annotation:
        async with self._lock:
            self._annotations[annotation.id] = copy.deepcopy(annotation)
            return copy.deepcopy(annotation)

    async def list_annotations(
        self,
        session_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> list[Annotation]:
        async with self._lock:
            out = []
            for a in self._annotations.values():
                if session_id is not None and a.session_id != session_id:
                    continue
                if span_id is not None and a.target.span_id != span_id:
                    continue
                out.append(copy.deepcopy(a))
            out.sort(key=lambda a: a.created_at)
            return out

    # payloads ------------------------------------------------------------
    async def put_payload(
        self, digest: str, data: bytes, mime: str, summary: str = ""
    ) -> PayloadMeta:
        async with self._lock:
            existing = self._payloads.get(digest)
            if existing is not None:
                self._payload_refcount[digest] = self._payload_refcount.get(digest, 0) + 1
                return copy.deepcopy(existing.meta)
            meta = PayloadMeta(digest=digest, size=len(data), mime=mime, summary=summary)
            self._payloads[digest] = PayloadRecord(meta=meta, bytes_=data)
            self._payload_refcount[digest] = 1
            return copy.deepcopy(meta)

    async def get_payload(self, digest: str) -> Optional[PayloadRecord]:
        async with self._lock:
            rec = self._payloads.get(digest)
            if rec is None:
                return None
            return PayloadRecord(meta=copy.deepcopy(rec.meta), bytes_=rec.bytes_)

    async def has_payload(self, digest: str) -> bool:
        async with self._lock:
            return digest in self._payloads

    def _decref_payload(self, digest: str) -> None:
        n = self._payload_refcount.get(digest, 0) - 1
        if n <= 0:
            self._payload_refcount.pop(digest, None)
            self._payloads.pop(digest, None)
        else:
            self._payload_refcount[digest] = n

    # stats ---------------------------------------------------------------
    async def stats(self) -> Stats:
        async with self._lock:
            payload_bytes = sum(rec.meta.size for rec in self._payloads.values())
            return Stats(
                session_count=len(self._sessions),
                agent_count=len(self._agents),
                span_count=len(self._spans),
                payload_count=len(self._payloads),
                payload_bytes=payload_bytes,
                disk_usage_bytes=0,
            )
