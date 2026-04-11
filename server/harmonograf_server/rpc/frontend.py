"""Frontend-facing RPCs (doc 01 §4.6, doc 03 §7).

Eight unary/server-stream methods the console talks to:
  ListSessions, WatchSession, GetPayload, GetSpanTree,
  PostAnnotation, SendControl, DeleteSession, GetStats.

All eight live on a single mixin so they can be composed with the
telemetry and control RPCs into one HarmonografServicer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import AsyncIterator, Optional

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.bus import (
    DELTA_AGENT_STATUS,
    DELTA_AGENT_UPSERT,
    DELTA_ANNOTATION,
    DELTA_BACKPRESSURE,
    DELTA_HEARTBEAT,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    DELTA_SPAN_UPDATE,
    Delta,
    SessionBus,
)
from harmonograf_server.control_router import ControlRouter, DeliveryResult
from harmonograf_server.convert import (
    _AGENT_STATUS_TO_PB,
    _ANNOTATION_KIND_TO_PB,
    float_to_ts,
    py_to_attr_value,
    storage_agent_to_pb,
    storage_span_to_pb,
    ts_to_float,
)
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import frontend_pb2, types_pb2
from harmonograf_server.storage import (
    Agent,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    SessionStatus,
    Span,
    Store,
)


logger = logging.getLogger(__name__)


PAYLOAD_CHUNK_BYTES = 256 * 1024
DEFAULT_WATCH_WINDOW_S = 3600.0
DEFAULT_SPAN_TREE_LIMIT = 10_000
DEFAULT_SEND_CONTROL_TIMEOUT_S = 5.0


def _now_ts() -> Timestamp:
    t = Timestamp()
    t.GetCurrentTime()
    return t


def _session_status_to_pb(s: SessionStatus) -> int:
    return {
        SessionStatus.LIVE: types_pb2.SESSION_STATUS_LIVE,
        SessionStatus.COMPLETED: types_pb2.SESSION_STATUS_COMPLETED,
        SessionStatus.ABORTED: types_pb2.SESSION_STATUS_ABORTED,
    }.get(s, types_pb2.SESSION_STATUS_UNSPECIFIED)


def _pb_session_status(pb: int) -> Optional[SessionStatus]:
    return {
        types_pb2.SESSION_STATUS_LIVE: SessionStatus.LIVE,
        types_pb2.SESSION_STATUS_COMPLETED: SessionStatus.COMPLETED,
        types_pb2.SESSION_STATUS_ABORTED: SessionStatus.ABORTED,
    }.get(pb)


def _storage_annotation_to_pb(ann: Annotation) -> types_pb2.Annotation:
    pb = types_pb2.Annotation(
        id=ann.id,
        session_id=ann.session_id,
        author=ann.author,
        kind=_ANNOTATION_KIND_TO_PB.get(ann.kind, types_pb2.ANNOTATION_KIND_COMMENT),
        body=ann.body,
    )
    created = float_to_ts(ann.created_at)
    if created is not None:
        pb.created_at.CopyFrom(created)
    if ann.delivered_at is not None:
        delivered = float_to_ts(ann.delivered_at)
        if delivered is not None:
            pb.delivered_at.CopyFrom(delivered)
    if ann.target.span_id:
        pb.target.span_id = ann.target.span_id
    elif ann.target.agent_id and ann.target.time_start is not None:
        pb.target.agent_time.agent_id = ann.target.agent_id
        at = float_to_ts(ann.target.time_start)
        if at is not None:
            pb.target.agent_time.at.CopyFrom(at)
    return pb


class FrontendServicerMixin:
    """Frontend RPC implementations. Composed into the main servicer.

    Expects these attrs from the composing class:
      self._store:   Store
      self._bus:     SessionBus
      self._ingest:  IngestPipeline
      self._router:  ControlRouter
      self._data_dir: str   (for GetStats reporting)
    """

    _store: Store
    _bus: SessionBus
    _ingest: IngestPipeline
    _router: ControlRouter
    _data_dir: str

    # ---- ListSessions -------------------------------------------------

    async def ListSessions(
        self,
        request: frontend_pb2.ListSessionsRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.ListSessionsResponse:
        status = _pb_session_status(request.status_filter)
        all_sessions = await self._store.list_sessions(status=status, limit=None)
        if request.search:
            needle = request.search.lower()
            all_sessions = [
                s
                for s in all_sessions
                if needle in s.id.lower() or needle in (s.title or "").lower()
            ]
        total = len(all_sessions)
        offset = request.offset or 0
        limit = request.limit or 50
        page = all_sessions[offset : offset + limit]

        resp = frontend_pb2.ListSessionsResponse(total_count=total)
        for sess in page:
            spans = await self._store.get_spans(sess.id)
            attention = sum(
                1 for sp in spans if sp.status.value == "AWAITING_HUMAN"
            )
            last_activity = max(
                (sp.end_time or sp.start_time for sp in spans), default=sess.created_at
            )
            summary = frontend_pb2.SessionSummary(
                id=sess.id,
                title=sess.title,
                status=_session_status_to_pb(sess.status),
                agent_count=len(sess.agent_ids),
                attention_count=attention,
            )
            created_ts = float_to_ts(sess.created_at)
            if created_ts is not None:
                summary.created_at.CopyFrom(created_ts)
            if sess.ended_at is not None:
                ended = float_to_ts(sess.ended_at)
                if ended is not None:
                    summary.ended_at.CopyFrom(ended)
            la = float_to_ts(last_activity)
            if la is not None:
                summary.last_activity.CopyFrom(la)
            resp.sessions.append(summary)
        return resp

    # ---- WatchSession -------------------------------------------------

    async def WatchSession(
        self,
        request: frontend_pb2.WatchSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[frontend_pb2.SessionUpdate]:
        session_id = request.session_id
        if not session_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id required")
            return

        sess = await self._store.get_session(session_id)
        if sess is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"session {session_id} not found")
            return

        # Subscribe BEFORE replaying history so we do not miss deltas that
        # arrive between the snapshot and the live tail.
        sub = await self._bus.subscribe(session_id)
        try:
            # 1. Session metadata
            session_pb = types_pb2.Session(
                id=sess.id,
                title=sess.title,
                status=_session_status_to_pb(sess.status),
                agent_ids=list(sess.agent_ids),
                metadata=sess.metadata,
            )
            created = float_to_ts(sess.created_at)
            if created is not None:
                session_pb.created_at.CopyFrom(created)
            if sess.ended_at is not None:
                ended = float_to_ts(sess.ended_at)
                if ended is not None:
                    session_pb.ended_at.CopyFrom(ended)
            yield frontend_pb2.SessionUpdate(session=session_pb)

            # 2. Agents
            agents = await self._store.list_agents_for_session(session_id)
            for ag in agents:
                yield frontend_pb2.SessionUpdate(agent=storage_agent_to_pb(ag))

            # 3. Spans within window
            window_start: Optional[float] = None
            window_end: Optional[float] = None
            if request.HasField("window_start"):
                window_start = ts_to_float(request.window_start)
            if request.HasField("window_end"):
                window_end = ts_to_float(request.window_end)
            if window_start is None and window_end is None:
                # default: last hour (or all, whichever smaller)
                window_start = max(0.0, time.time() - DEFAULT_WATCH_WINDOW_S)

            spans = await self._store.get_spans(
                session_id, time_start=window_start, time_end=window_end
            )
            for sp in spans:
                yield frontend_pb2.SessionUpdate(initial_span=storage_span_to_pb(sp))

            # 4. Annotations
            anns = await self._store.list_annotations(session_id=session_id)
            for ann in anns:
                yield frontend_pb2.SessionUpdate(
                    initial_annotation=_storage_annotation_to_pb(ann)
                )

            # 5. Burst complete marker
            yield frontend_pb2.SessionUpdate(
                burst_complete=frontend_pb2.InitialBurstComplete(
                    spans_sent=len(spans), agents_sent=len(agents)
                )
            )

            # 6. Tail: drain the bus until the client disconnects.
            while True:
                delta = await sub.queue.get()
                update = _delta_to_session_update(delta)
                if update is None:
                    continue
                yield update
        except asyncio.CancelledError:
            raise
        finally:
            await self._bus.unsubscribe(sub)

    # ---- GetPayload ---------------------------------------------------

    async def GetPayload(
        self,
        request: frontend_pb2.GetPayloadRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[frontend_pb2.PayloadChunk]:
        if not request.digest:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "digest required")
            return
        record = await self._store.get_payload(request.digest)
        if record is None:
            yield frontend_pb2.PayloadChunk(digest=request.digest, not_found=True)
            return

        meta = record.meta
        # First chunk carries the summary metadata. If summary_only, that is
        # also the last chunk.
        first = frontend_pb2.PayloadChunk(
            digest=meta.digest,
            total_size=meta.size,
            mime=meta.mime,
            summary=meta.summary,
            last=request.summary_only or len(record.bytes_) == 0,
        )
        if not request.summary_only and len(record.bytes_) <= PAYLOAD_CHUNK_BYTES:
            first.chunk = record.bytes_
            first.last = True
        yield first
        if request.summary_only or first.last:
            return

        data = record.bytes_
        for offset in range(0, len(data), PAYLOAD_CHUNK_BYTES):
            chunk_bytes = data[offset : offset + PAYLOAD_CHUNK_BYTES]
            is_last = offset + PAYLOAD_CHUNK_BYTES >= len(data)
            yield frontend_pb2.PayloadChunk(
                digest=meta.digest, chunk=chunk_bytes, last=is_last
            )

    # ---- GetSpanTree --------------------------------------------------

    async def GetSpanTree(
        self,
        request: frontend_pb2.GetSpanTreeRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.GetSpanTreeResponse:
        if not request.session_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id required")
            return frontend_pb2.GetSpanTreeResponse()

        window_start = (
            ts_to_float(request.window_start) if request.HasField("window_start") else None
        )
        window_end = (
            ts_to_float(request.window_end) if request.HasField("window_end") else None
        )
        limit = request.limit or DEFAULT_SPAN_TREE_LIMIT

        agent_ids = list(request.agent_ids) or [None]
        collected: list[Span] = []
        for aid in agent_ids:
            spans = await self._store.get_spans(
                request.session_id,
                agent_id=aid,
                time_start=window_start,
                time_end=window_end,
                limit=limit + 1,
            )
            collected.extend(spans)
        collected.sort(key=lambda s: s.start_time)
        truncated = len(collected) > limit
        if truncated:
            collected = collected[:limit]
        resp = frontend_pb2.GetSpanTreeResponse(truncated=truncated)
        for sp in collected:
            resp.spans.append(storage_span_to_pb(sp))
        return resp

    # ---- PostAnnotation -----------------------------------------------

    async def PostAnnotation(
        self,
        request: frontend_pb2.PostAnnotationRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.PostAnnotationResponse:
        if not request.session_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id required")
            return frontend_pb2.PostAnnotationResponse()

        kind_map = {
            types_pb2.ANNOTATION_KIND_COMMENT: AnnotationKind.COMMENT,
            types_pb2.ANNOTATION_KIND_STEERING: AnnotationKind.STEERING,
            types_pb2.ANNOTATION_KIND_HUMAN_RESPONSE: AnnotationKind.HUMAN_RESPONSE,
        }
        kind = kind_map.get(request.kind, AnnotationKind.COMMENT)

        target = AnnotationTarget()
        if request.target.span_id:
            target.span_id = request.target.span_id
        elif request.target.HasField("agent_time"):
            target.agent_id = request.target.agent_time.agent_id
            target.time_start = ts_to_float(request.target.agent_time.at)

        now = time.time()
        ann = Annotation(
            id=f"ann_{uuid.uuid4().hex[:16]}",
            session_id=request.session_id,
            target=target,
            author=request.author or "user",
            created_at=now,
            kind=kind,
            body=request.body,
        )
        await self._store.put_annotation(ann)
        self._bus.publish_annotation(ann)

        resp = frontend_pb2.PostAnnotationResponse(
            annotation=_storage_annotation_to_pb(ann),
            delivery=types_pb2.CONTROL_ACK_RESULT_UNSPECIFIED,
        )

        if kind == AnnotationKind.COMMENT:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_SUCCESS
            return resp

        # STEERING / HUMAN_RESPONSE route through ControlRouter.
        target_agent = target.agent_id
        if not target_agent and target.span_id:
            sp = await self._store.get_span(target.span_id)
            if sp is not None:
                target_agent = sp.agent_id
        if not target_agent:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "no target agent"
            return resp

        control_kind = (
            types_pb2.CONTROL_KIND_STEER
            if kind == AnnotationKind.STEERING
            else types_pb2.CONTROL_KIND_INJECT_MESSAGE
        )
        timeout = (request.ack_timeout_ms / 1000.0) if request.ack_timeout_ms else DEFAULT_SEND_CONTROL_TIMEOUT_S
        outcome = await self._router.deliver(
            session_id=request.session_id,
            agent_id=target_agent,
            kind=control_kind,
            payload=request.body.encode("utf-8"),
            span_id=target.span_id or None,
            timeout_s=timeout,
        )
        if outcome.result == DeliveryResult.SUCCESS:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_SUCCESS
            delivered_ts = time.time()
            await self._store.put_annotation(
                Annotation(
                    id=ann.id,
                    session_id=ann.session_id,
                    target=ann.target,
                    author=ann.author,
                    created_at=ann.created_at,
                    kind=ann.kind,
                    body=ann.body,
                    delivered_at=delivered_ts,
                )
            )
        elif outcome.result == DeliveryResult.UNAVAILABLE:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "agent offline"
        elif outcome.result == DeliveryResult.DEADLINE_EXCEEDED:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "ack timeout"
        else:
            resp.delivery = types_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "delivery failed"
        return resp

    # ---- SendControl --------------------------------------------------

    async def SendControl(
        self,
        request: frontend_pb2.SendControlRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.SendControlResponse:
        if not request.session_id or not request.target.agent_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "session_id and target.agent_id required"
            )
            return frontend_pb2.SendControlResponse()

        timeout = (request.ack_timeout_ms / 1000.0) if request.ack_timeout_ms else DEFAULT_SEND_CONTROL_TIMEOUT_S
        outcome = await self._router.deliver(
            session_id=request.session_id,
            agent_id=request.target.agent_id,
            kind=request.kind,
            payload=request.payload,
            span_id=request.target.span_id or None,
            timeout_s=timeout,
            require_all_acks=request.require_all_acks,
        )

        resp = frontend_pb2.SendControlResponse(control_id=outcome.control_id)
        if outcome.result == DeliveryResult.SUCCESS:
            resp.result = types_pb2.CONTROL_ACK_RESULT_SUCCESS
        elif outcome.result == DeliveryResult.UNAVAILABLE:
            resp.result = types_pb2.CONTROL_ACK_RESULT_UNSUPPORTED
        else:
            resp.result = types_pb2.CONTROL_ACK_RESULT_FAILURE
        for ack in outcome.acks:
            sa = resp.acks.add(
                stream_id=ack.stream_id,
                result=ack.result,
                detail=ack.detail,
            )
            at = float_to_ts(ack.acked_at)
            if at is not None:
                sa.acked_at.CopyFrom(at)
        return resp

    # ---- DeleteSession ------------------------------------------------

    async def DeleteSession(
        self,
        request: frontend_pb2.DeleteSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.DeleteSessionResponse:
        if not request.session_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id required")
            return frontend_pb2.DeleteSessionResponse()

        sess = await self._store.get_session(request.session_id)
        if sess is None:
            return frontend_pb2.DeleteSessionResponse(
                deleted=False, reason_if_not="not found"
            )
        if sess.status == SessionStatus.LIVE and not request.force:
            return frontend_pb2.DeleteSessionResponse(
                deleted=False, reason_if_not="session is LIVE; pass force=true to delete"
            )

        # Snapshot counts before deletion.
        spans = await self._store.get_spans(request.session_id)
        anns = await self._store.list_annotations(session_id=request.session_id)
        payload_bytes = 0
        for sp in spans:
            if sp.payload_digest:
                rec = await self._store.get_payload(sp.payload_digest)
                if rec is not None:
                    payload_bytes += rec.meta.size

        deleted = await self._store.delete_session(request.session_id)
        if deleted:
            try:
                await self._store.gc_payloads()
            except Exception:
                logger.exception(
                    "gc_payloads failed after delete session=%s", request.session_id
                )
        return frontend_pb2.DeleteSessionResponse(
            deleted=deleted,
            spans_removed=len(spans),
            annotations_removed=len(anns),
            payload_bytes_freed=payload_bytes,
        )

    # ---- GetStats -----------------------------------------------------

    async def GetStats(
        self,
        request: frontend_pb2.GetStatsRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.GetStatsResponse:
        stats = await self._store.stats()
        all_sessions = await self._store.list_sessions()
        live_count = sum(1 for s in all_sessions if s.status == SessionStatus.LIVE)
        anns = await self._store.list_annotations()

        active_telemetry = sum(
            len(b) for b in self._ingest._streams_by_agent.values()  # type: ignore[attr-defined]
        )
        active_control = sum(
            len(b) for b in self._router._subs.values()  # type: ignore[attr-defined]
        )

        return frontend_pb2.GetStatsResponse(
            session_count=stats.session_count,
            live_session_count=live_count,
            agent_count=stats.agent_count,
            span_count=stats.span_count,
            annotation_count=len(anns),
            payload_count=stats.payload_count,
            payload_bytes=stats.payload_bytes,
            disk_bytes=stats.disk_usage_bytes,
            data_dir=getattr(self, "_data_dir", ""),
            active_telemetry_streams=active_telemetry,
            active_control_streams=active_control,
        )


# ---- Delta → SessionUpdate mapping ----------------------------------------


def _delta_to_session_update(delta: Delta) -> Optional[frontend_pb2.SessionUpdate]:
    if delta.kind == DELTA_SPAN_START:
        span: Span = delta.payload
        return frontend_pb2.SessionUpdate(
            new_span=frontend_pb2.NewSpan(span=storage_span_to_pb(span))
        )
    if delta.kind == DELTA_SPAN_UPDATE:
        span = delta.payload
        updated = frontend_pb2.UpdatedSpan(span_id=span.id)
        from harmonograf_server.convert import _SPAN_STATUS_TO_PB

        updated.status = _SPAN_STATUS_TO_PB.get(span.status, types_pb2.SPAN_STATUS_RUNNING)
        for k, v in (span.attributes or {}).items():
            updated.attributes[k].CopyFrom(py_to_attr_value(v))
        return frontend_pb2.SessionUpdate(updated_span=updated)
    if delta.kind == DELTA_SPAN_END:
        span = delta.payload
        ended = frontend_pb2.EndedSpan(span_id=span.id)
        if span.end_time is not None:
            ts = float_to_ts(span.end_time)
            if ts is not None:
                ended.end_time.CopyFrom(ts)
        from harmonograf_server.convert import _SPAN_STATUS_TO_PB

        ended.status = _SPAN_STATUS_TO_PB.get(span.status, types_pb2.SPAN_STATUS_COMPLETED)
        if span.error:
            ended.error.type = span.error.get("type", "")
            ended.error.message = span.error.get("message", "")
            ended.error.stack = span.error.get("stack", "")
        return frontend_pb2.SessionUpdate(ended_span=ended)
    if delta.kind == DELTA_ANNOTATION:
        ann: Annotation = delta.payload
        return frontend_pb2.SessionUpdate(
            new_annotation=frontend_pb2.NewAnnotation(
                annotation=_storage_annotation_to_pb(ann)
            )
        )
    if delta.kind == DELTA_AGENT_UPSERT:
        agent: Agent = delta.payload
        return frontend_pb2.SessionUpdate(
            agent_joined=frontend_pb2.AgentJoined(agent=storage_agent_to_pb(agent))
        )
    if delta.kind == DELTA_AGENT_STATUS:
        p = delta.payload
        return frontend_pb2.SessionUpdate(
            agent_status_changed=frontend_pb2.AgentStatusChanged(
                agent_id=p["agent_id"],
                status=_AGENT_STATUS_TO_PB.get(
                    p["status"], types_pb2.AGENT_STATUS_UNSPECIFIED
                ),
            )
        )
    if delta.kind == DELTA_HEARTBEAT:
        p = delta.payload
        return frontend_pb2.SessionUpdate(
            agent_status_changed=frontend_pb2.AgentStatusChanged(
                agent_id=p["agent_id"],
                status=types_pb2.AGENT_STATUS_CONNECTED,
                buffered_events=p.get("buffered_events", 0),
                dropped_events=p.get("dropped_events", 0),
            )
        )
    if delta.kind == DELTA_BACKPRESSURE:
        return None
    return None
