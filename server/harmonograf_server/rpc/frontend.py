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
from google.protobuf import timestamp_pb2

from harmonograf_server.bus import (
    DELTA_AGENT_INVOCATION_COMPLETED,
    DELTA_AGENT_INVOCATION_STARTED,
    DELTA_AGENT_STATUS,
    DELTA_AGENT_UPSERT,
    DELTA_ANNOTATION,
    DELTA_BACKPRESSURE,
    DELTA_DELEGATION_OBSERVED,
    DELTA_DRIFT,
    DELTA_GOAL_DERIVED,
    DELTA_HEARTBEAT,
    DELTA_RUN_ABORTED,
    DELTA_RUN_COMPLETED,
    DELTA_RUN_STARTED,
    DELTA_SESSION_ENDED,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    DELTA_SPAN_UPDATE,
    DELTA_TASK_PLAN,
    DELTA_TASK_PROGRESS,
    DELTA_TASK_REPORT,
    DELTA_TASK_STATUS,
    DELTA_CONTEXT_WINDOW_SAMPLE,
    Delta,
    SessionBus,
)
from harmonograf_server.config import ServerConfig
from harmonograf_server.control_router import ControlRouter, DeliveryResult
from harmonograf_server.convert import (
    _AGENT_STATUS_TO_PB,
    _ANNOTATION_KIND_TO_PB,
    _drift_kind_string_to_pb,
    _drift_severity_string_to_pb,
    float_to_ts,
    py_to_attr_value,
    storage_agent_to_pb,
    storage_plan_to_goldfive_pb,
    storage_span_to_pb,
    task_status_to_pb,
    ts_to_float,
)
from goldfive.v1 import events_pb2 as goldfive_events_pb2
from goldfive.v1 import types_pb2 as goldfive_types_pb2
from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.interventions import list_interventions, record_to_pb
from harmonograf_server.pb import frontend_pb2, types_pb2
from harmonograf_server.storage import (
    Agent,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    SessionStatus,
    Span,
    Store,
    Task,
    TaskStatus,
)


logger = logging.getLogger(__name__)


# Module-level tunables were moved to ``ServerConfig`` in
# harmonograf#102; the mixin reads ``self._config.rpc_*`` below.


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
      self._config:  ServerConfig — RPC tunables (watch window,
                     span-tree limit, payload chunk size, ack timeout)
                     plus ``legacy_plan_attribution_window_ms`` read by
                     ListInterventions.
    """

    _store: Store
    _bus: SessionBus
    _ingest: IngestPipeline
    _router: ControlRouter
    _data_dir: str
    _config: ServerConfig

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
                # default: last ``rpc_watch_window_seconds`` (or all,
                # whichever smaller)
                window_start = max(
                    0.0, time.time() - self._config.rpc_watch_window_seconds
                )

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

            # 4b. Replay persisted plans + task state as synthesized
            # goldfive.v1.Event frames so a client that joins after the
            # orchestrator already ran still sees a populated Tasks panel.
            # Plans are emitted in created_at order; for each plan we emit
            # PlanSubmitted, then one event per task whose status has
            # advanced past PENDING (TaskStarted for RUNNING, plus the
            # terminal event for COMPLETED/FAILED/BLOCKED/CANCELLED so
            # the frontend lands in the right final state).
            try:
                plans = await self._store.list_task_plans_for_session(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("list_task_plans_for_session failed: %s", exc)
                plans = []
            plans = sorted(plans, key=lambda p: p.created_at)
            for plan in plans:
                submitted = frontend_pb2.SessionUpdate()
                submitted.goldfive_event.plan_submitted.plan.CopyFrom(
                    storage_plan_to_goldfive_pb(plan)
                )
                yield submitted
                for task in plan.tasks:
                    for ev in _synthesize_task_events(task):
                        yield frontend_pb2.SessionUpdate(goldfive_event=ev)

            # 4b.1. Drifts — replay the in-memory drift ring so the
            # synthetic-actor rows (user / goldfive) and trajectory drift
            # markers reappear on reconnect. The ring is bounded and
            # process-local (not persisted across server restarts); a
            # full persistence layer is tracked as followup.
            try:
                drift_records = self._ingest.drifts_for_session(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("drifts_for_session failed: %s", exc)
                drift_records = []
            for dr in drift_records:
                ev = goldfive_events_pb2.Event(run_id=dr.get("run_id", ""))
                ev.drift_detected.kind = _drift_kind_string_to_pb(
                    dr.get("kind", "") or ""
                )
                ev.drift_detected.severity = _drift_severity_string_to_pb(
                    dr.get("severity", "") or ""
                )
                ev.drift_detected.detail = dr.get("detail", "") or ""
                ev.drift_detected.current_task_id = dr.get("current_task_id", "") or ""
                ev.drift_detected.current_agent_id = dr.get("current_agent_id", "") or ""
                # Propagate annotation_id so reconnecting clients can dedup
                # the drift row against the source user annotation on the
                # initial burst just like they do for live arrivals
                # (harmonograf#75).
                ev.drift_detected.annotation_id = dr.get("annotation_id", "") or ""
                # Propagate the goldfive-minted drift id (goldfive#199 /
                # harmonograf#99) so reconnecting clients have the strict
                # join key for autonomous-drift plan-revision merges.
                ev.drift_detected.id = dr.get("id", "") or ""
                ra = dr.get("recorded_at")
                if isinstance(ra, (int, float)):
                    ts = float_to_ts(float(ra))
                    if ts is not None:
                        ev.emitted_at.CopyFrom(ts)
                yield frontend_pb2.SessionUpdate(goldfive_event=ev)

            # 4c. Context window samples — replay the most recent per-agent
            # series so the Gantt context-window lane renders immediately
            # on reconnect instead of waiting for the next heartbeat tick.
            try:
                ctx_samples = await self._store.list_context_window_samples(
                    session_id, limit_per_agent=200
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("list_context_window_samples failed: %s", exc)
                ctx_samples = []
            for cs in ctx_samples:
                pb_sample = frontend_pb2.ContextWindowSample(
                    agent_id=cs.agent_id,
                    tokens=cs.tokens,
                    limit_tokens=cs.limit_tokens,
                )
                ra = float_to_ts(cs.recorded_at)
                if ra is not None:
                    pb_sample.recorded_at.CopyFrom(ra)
                yield frontend_pb2.SessionUpdate(context_window_sample=pb_sample)

            # 4d. DelegationObserved — replay persisted delegation events so
            # the Agent Graph / Gantt delegation arrows reappear on
            # reconnect or for clients that open the view after the
            # orchestrator already emitted the events. Without this,
            # delegations only land on live bus subscribers — a viewer
            # that joins late sees the agent lifelines (from span replay)
            # but no arrows between them.
            #
            # The persisted payload_bytes carry the *raw* bare ADK agent
            # names (ingest persists verbatim before resolving), so we
            # re-run find_agent_id_by_name here to rewrite bare → compound
            # ids. This matches the live path in
            # IngestPipeline._on_delegation_observed.
            try:
                delegation_events = await self._store.list_goldfive_events(
                    session_id, kind="delegation_observed"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("list_goldfive_events(delegation_observed) failed: %s", exc)
                delegation_events = []
            for rec in delegation_events:
                try:
                    ev = goldfive_events_pb2.Event()
                    ev.ParseFromString(rec.payload_bytes)
                except Exception as exc:  # noqa: BLE001 — proto edge cases
                    logger.debug(
                        "delegation_observed replay parse failed session_id=%s seq=%s: %s",
                        session_id,
                        rec.sequence,
                        exc,
                    )
                    continue
                d = ev.delegation_observed
                resolved_from = d.from_agent
                resolved_to = d.to_agent
                try:
                    f_id = await self._store.find_agent_id_by_name(
                        session_id, d.from_agent
                    )
                    if f_id:
                        resolved_from = f_id
                    t_id = await self._store.find_agent_id_by_name(
                        session_id, d.to_agent
                    )
                    if t_id:
                        resolved_to = t_id
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "delegation_observed replay resolve failed session_id=%s: %s",
                        session_id,
                        exc,
                    )
                ev.delegation_observed.from_agent = resolved_from
                ev.delegation_observed.to_agent = resolved_to
                # Stamp emitted_at from observed_at so the frontend's
                # observedAtMs calculation (which reads Event.emitted_at)
                # lines up with the Gantt timeline — same invariant as
                # the live delta path in _delta_to_session_update.
                if d.HasField("observed_at"):
                    ev.emitted_at.CopyFrom(d.observed_at)
                yield frontend_pb2.SessionUpdate(goldfive_event=ev)

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
        chunk_bytes_limit = self._config.rpc_payload_chunk_bytes
        # First chunk carries the summary metadata. If summary_only, that is
        # also the last chunk.
        first = frontend_pb2.PayloadChunk(
            digest=meta.digest,
            total_size=meta.size,
            mime=meta.mime,
            summary=meta.summary,
            last=request.summary_only or len(record.bytes_) == 0,
        )
        if not request.summary_only and len(record.bytes_) <= chunk_bytes_limit:
            first.chunk = record.bytes_
            first.last = True
        yield first
        if request.summary_only or first.last:
            return

        data = record.bytes_
        for offset in range(0, len(data), chunk_bytes_limit):
            chunk_bytes = data[offset : offset + chunk_bytes_limit]
            is_last = offset + chunk_bytes_limit >= len(data)
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
        limit = request.limit or self._config.rpc_span_tree_limit

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
            delivery=gf_control_pb2.CONTROL_ACK_RESULT_UNSPECIFIED,
        )

        if kind == AnnotationKind.COMMENT:
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
            return resp

        # STEERING / HUMAN_RESPONSE route through ControlRouter.
        target_agent = target.agent_id
        if not target_agent and target.span_id:
            sp = await self._store.get_span(target.span_id)
            if sp is not None:
                target_agent = sp.agent_id
        if not target_agent:
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "no target agent"
            return resp

        event = gf_control_pb2.ControlEvent(
            target=gf_control_pb2.ControlTarget(agent_id=target_agent),
        )
        if kind == AnnotationKind.STEERING:
            event.kind = gf_control_pb2.CONTROL_KIND_STEER
            event.steer.note = request.body
            # goldfive#171: propagate author + source annotation id so the
            # goldfive-side steerer can attribute the drift + dedupe
            # delivery retries / UI double-fires of the same annotation.
            event.steer.author = ann.author
            event.steer.annotation_id = ann.id
        else:
            event.kind = gf_control_pb2.CONTROL_KIND_INJECT_MESSAGE
            event.inject_message.text = request.body
        timeout = (
            (request.ack_timeout_ms / 1000.0)
            if request.ack_timeout_ms
            else self._config.rpc_send_control_timeout_seconds
        )
        outcome = await self._router.deliver(
            session_id=request.session_id,
            agent_id=target_agent,
            event=event,
            timeout_s=timeout,
        )
        if outcome.result == DeliveryResult.SUCCESS:
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
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
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "agent offline"
        elif outcome.result == DeliveryResult.DEADLINE_EXCEEDED:
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "ack timeout"
        else:
            resp.delivery = gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
            resp.delivery_detail = "delivery failed"
        return resp

    # ---- SendControl --------------------------------------------------

    async def SendControl(
        self,
        request: frontend_pb2.SendControlRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.SendControlResponse:
        if not request.session_id or not request.event.target.agent_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "session_id and event.target.agent_id required",
            )
            return frontend_pb2.SendControlResponse()

        timeout = (
            (request.ack_timeout_ms / 1000.0)
            if request.ack_timeout_ms
            else self._config.rpc_send_control_timeout_seconds
        )
        # Copy so the router can stamp ``id``/``issued_at`` without mutating
        # the caller's request message.
        event = gf_control_pb2.ControlEvent()
        event.CopyFrom(request.event)
        outcome = await self._router.deliver(
            session_id=request.session_id,
            agent_id=request.event.target.agent_id,
            event=event,
            timeout_s=timeout,
            require_all_acks=request.require_all_acks,
        )

        resp = frontend_pb2.SendControlResponse(control_id=outcome.control_id)
        if outcome.result == DeliveryResult.SUCCESS:
            resp.result = gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        elif outcome.result == DeliveryResult.UNAVAILABLE:
            resp.result = gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED
        else:
            resp.result = gf_control_pb2.CONTROL_ACK_RESULT_FAILURE
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

    # ---- ListInterventions --------------------------------------------

    async def ListInterventions(
        self,
        request: frontend_pb2.ListInterventionsRequest,
        context: grpc.aio.ServicerContext,
    ) -> frontend_pb2.ListInterventionsResponse:
        if not request.session_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id required")
            return frontend_pb2.ListInterventionsResponse()
        sess = await self._store.get_session(request.session_id)
        if sess is None:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"session {request.session_id} not found"
            )
            return frontend_pb2.ListInterventionsResponse()
        legacy_window_ms = float(
            getattr(self._config, "legacy_plan_attribution_window_ms", 0.0) or 0.0
        )
        records = await list_interventions(
            request.session_id,
            store=self._store,
            drifts_provider=self._ingest,
            legacy_plan_attribution_window_ms=legacy_window_ms,
        )
        resp = frontend_pb2.ListInterventionsResponse()
        for rec in records:
            resp.interventions.append(record_to_pb(rec, types_pb2))
        return resp

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
        if span.payload_digest:
            updated.payload_refs.add(
                digest=span.payload_digest,
                size=span.payload_size,
                mime=span.payload_mime,
                summary=span.payload_summary,
                role=span.payload_role,
                evicted=span.payload_evicted,
            )
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
        if span.payload_digest:
            ended.payload_refs.add(
                digest=span.payload_digest,
                size=span.payload_size,
                mime=span.payload_mime,
                summary=span.payload_summary,
                role=span.payload_role,
                evicted=span.payload_evicted,
            )
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
                current_activity=p.get("current_activity", ""),
                progress_counter=p.get("progress_counter", 0),
                stuck=p.get("stuck", False),
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
                current_activity=p.get("current_activity", ""),
                progress_counter=p.get("progress_counter", 0),
                stuck=p.get("stuck", False),
            )
        )
    if delta.kind == DELTA_TASK_REPORT:
        p = delta.payload
        tr = frontend_pb2.TaskReport(
            agent_id=p["agent_id"],
            report=p["report"],
            invocation_span_id=p.get("invocation_span_id", ""),
        )
        ts = timestamp_pb2.Timestamp()
        ts.FromSeconds(int(p.get("recorded_at", time.time())))
        # Preserve sub-second precision if available.
        recorded_at_f = p.get("recorded_at", time.time())
        ts.seconds = int(recorded_at_f)
        ts.nanos = int((recorded_at_f - int(recorded_at_f)) * 1e9)
        tr.recorded_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(task_report=tr)
    if delta.kind == DELTA_TASK_PLAN:
        plan = delta.payload
        ev = goldfive_events_pb2.Event()
        ev.plan_submitted.plan.CopyFrom(storage_plan_to_goldfive_pb(plan))
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_TASK_STATUS:
        p = delta.payload
        task = p["task"]
        ev = _task_status_to_goldfive_event(task)
        if ev is None:
            return None
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_RUN_STARTED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.run_started.run_id = p.get("run_id", "")
        ev.run_started.goal_summary = p.get("goal_summary", "") or ""
        started_at = p.get("started_at")
        if started_at is not None:
            ts = float_to_ts(started_at)
            if ts is not None:
                ev.run_started.started_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_RUN_COMPLETED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.run_completed.outcome_summary = p.get("outcome_summary", "") or ""
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_RUN_ABORTED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.run_aborted.reason = p.get("reason", "") or ""
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_SESSION_ENDED:
        # Session terminal transition (harmonograf#96). Stamped by the
        # ingest pipeline when a goldfive run_completed / run_aborted
        # arrives and the session flips out of LIVE. Frontend consumes
        # the ``session_ended`` variant and updates ``sessionStatus`` so
        # ``sessionIsInactive`` returns true, clearing the LIVE ACTIVITY
        # "N RUNNING" header even when stuck INVOCATION spans remain in
        # the DB. The same payload also carries ended_at so the session
        # header can render the terminal timestamp.
        p = delta.payload
        ended_at = p.get("ended_at")
        final_status = p.get("final_status", SessionStatus.COMPLETED)
        ended_pb = frontend_pb2.SessionEnded(
            final_status=_session_status_to_pb(final_status),
        )
        if isinstance(ended_at, (int, float)):
            ts = float_to_ts(float(ended_at))
            if ts is not None:
                ended_pb.ended_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(session_ended=ended_pb)
    if delta.kind == DELTA_GOAL_DERIVED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        for goal_dict in p.get("goals", []) or []:
            g = ev.goal_derived.goals.add(
                id=goal_dict.get("id", "") or "",
                summary=goal_dict.get("summary", "") or "",
            )
            for k, v in (goal_dict.get("metadata") or {}).items():
                g.metadata[k] = v
            if goal_dict.get("has_success_predicate"):
                g.has_success_predicate = True
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_DRIFT:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.drift_detected.kind = _drift_kind_string_to_pb(p.get("kind", "") or "")
        ev.drift_detected.severity = _drift_severity_string_to_pb(
            p.get("severity", "") or ""
        )
        ev.drift_detected.detail = p.get("detail", "") or ""
        ev.drift_detected.current_task_id = p.get("current_task_id", "") or ""
        ev.drift_detected.current_agent_id = p.get("current_agent_id", "") or ""
        # Propagate the source annotation_id so the frontend can dedup the
        # drift row against the source user annotation (harmonograf#75).
        ev.drift_detected.annotation_id = p.get("annotation_id", "") or ""
        # Propagate the goldfive-minted drift id (goldfive#199 /
        # harmonograf#99) so the frontend has the strict join key for
        # autonomous-drift plan-revision merges.
        ev.drift_detected.id = p.get("drift_id", "") or ""
        # Stamp ``emitted_at`` so the frontend renders a correct
        # session-relative timestamp. Previously this was omitted on the
        # live DELTA_DRIFT path, which caused the deriver to fall back to
        # an emittedMs of 0 (rendering as "0:00") or — when sessionStartMs
        # was not yet set — to an absolute wall-clock ms that showed as
        # "29613707:05". Closes #73.
        recorded_at = p.get("recorded_at")
        if isinstance(recorded_at, (int, float)):
            ts = float_to_ts(float(recorded_at))
            if ts is not None:
                ev.emitted_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_TASK_PROGRESS:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.task_progress.task_id = p.get("task_id", "") or ""
        ev.task_progress.fraction = float(p.get("fraction", 0.0) or 0.0)
        ev.task_progress.detail = p.get("detail", "") or ""
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_AGENT_INVOCATION_STARTED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.agent_invocation_started.agent_name = p.get("agent_name", "") or ""
        ev.agent_invocation_started.task_id = p.get("task_id", "") or ""
        ev.agent_invocation_started.invocation_id = p.get("invocation_id", "") or ""
        ev.agent_invocation_started.parent_invocation_id = (
            p.get("parent_invocation_id", "") or ""
        )
        started_at = p.get("started_at")
        if started_at is not None:
            ts = float_to_ts(float(started_at))
            if ts is not None:
                ev.agent_invocation_started.started_at.CopyFrom(ts)
                ev.emitted_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_AGENT_INVOCATION_COMPLETED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.agent_invocation_completed.agent_name = p.get("agent_name", "") or ""
        ev.agent_invocation_completed.task_id = p.get("task_id", "") or ""
        ev.agent_invocation_completed.invocation_id = (
            p.get("invocation_id", "") or ""
        )
        ev.agent_invocation_completed.summary = p.get("summary", "") or ""
        completed_at = p.get("completed_at")
        if completed_at is not None:
            ts = float_to_ts(float(completed_at))
            if ts is not None:
                ev.agent_invocation_completed.completed_at.CopyFrom(ts)
                ev.emitted_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_DELEGATION_OBSERVED:
        p = delta.payload
        ev = goldfive_events_pb2.Event(run_id=p.get("run_id", ""))
        ev.delegation_observed.from_agent = p.get("from_agent", "") or ""
        ev.delegation_observed.to_agent = p.get("to_agent", "") or ""
        ev.delegation_observed.task_id = p.get("task_id", "") or ""
        ev.delegation_observed.invocation_id = p.get("invocation_id", "") or ""
        observed_at = p.get("observed_at")
        if observed_at is not None:
            ts = float_to_ts(float(observed_at))
            if ts is not None:
                ev.delegation_observed.observed_at.CopyFrom(ts)
                # Frontend uses Event.emitted_at to compute observedAtMs for
                # delegation records — stamp it here so the edge lines up
                # with the Gantt timeline.
                ev.emitted_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(goldfive_event=ev)
    if delta.kind == DELTA_CONTEXT_WINDOW_SAMPLE:
        p = delta.payload
        pb_sample = frontend_pb2.ContextWindowSample(
            agent_id=p["agent_id"],
            tokens=int(p["tokens"]),
            limit_tokens=int(p["limit_tokens"]),
        )
        recorded_at_f = p.get("recorded_at", time.time())
        ts = Timestamp()
        ts.seconds = int(recorded_at_f)
        ts.nanos = int((recorded_at_f - int(recorded_at_f)) * 1e9)
        pb_sample.recorded_at.CopyFrom(ts)
        return frontend_pb2.SessionUpdate(context_window_sample=pb_sample)
    if delta.kind == DELTA_BACKPRESSURE:
        return None
    return None


def _task_status_to_goldfive_event(task: Task) -> Optional[goldfive_events_pb2.Event]:
    """Map a terminal/running storage ``Task`` to a ``goldfive.v1.Event``.

    PENDING maps to ``None`` — the plan itself already carries PENDING as
    the default state, so re-emitting it as an event would be noise.
    """

    ev = goldfive_events_pb2.Event()
    if task.status == TaskStatus.RUNNING:
        ev.task_started.task_id = task.id
        return ev
    if task.status == TaskStatus.COMPLETED:
        ev.task_completed.task_id = task.id
        return ev
    if task.status == TaskStatus.FAILED:
        ev.task_failed.task_id = task.id
        # harmonograf#110 / goldfive#205: thread the structured cancel
        # reason through so late-joining watchers see the same context
        # the live stream carried.
        cr = getattr(task, "cancel_reason", "") or ""
        if cr:
            ev.task_failed.reason = cr
        return ev
    if task.status == TaskStatus.BLOCKED:
        ev.task_blocked.task_id = task.id
        return ev
    if task.status == TaskStatus.CANCELLED:
        ev.task_cancelled.task_id = task.id
        cr = getattr(task, "cancel_reason", "") or ""
        if cr:
            ev.task_cancelled.reason = cr
        return ev
    return None


def _synthesize_task_events(task: Task) -> list[goldfive_events_pb2.Event]:
    """Produce the ordered events a late-joining watcher needs for this task.

    The orchestrator would normally emit ``TaskStarted`` before any terminal
    event; replaying that sequence keeps frontend state machines that gate
    on "saw started first" behaving identically to the live path.
    """

    if task.status in (TaskStatus.PENDING, None):
        return []
    events: list[goldfive_events_pb2.Event] = []
    if task.status != TaskStatus.RUNNING:
        started = goldfive_events_pb2.Event()
        started.task_started.task_id = task.id
        events.append(started)
    terminal = _task_status_to_goldfive_event(task)
    if terminal is not None:
        events.append(terminal)
    return events
