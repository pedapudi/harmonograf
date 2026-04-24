"""In-process pub/sub for SessionUpdate deltas.

Telemetry ingest publishes per-session events; WatchSession subscribers each
get their own asyncio.Queue. Subscribers that fall behind are dropped with a
backpressure signal; ingest is never blocked on a slow consumer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    SessionStatus,
    Span,
    Task,
    TaskPlan,
)


DELTA_AGENT_UPSERT = "agent_upsert"
DELTA_AGENT_STATUS = "agent_status"
DELTA_SPAN_START = "span_start"
DELTA_SPAN_UPDATE = "span_update"
DELTA_SPAN_END = "span_end"
DELTA_ANNOTATION = "annotation"
DELTA_HEARTBEAT = "heartbeat"
DELTA_BACKPRESSURE = "backpressure"
DELTA_TASK_REPORT = "task_report"
DELTA_TASK_PLAN = "task_plan"
DELTA_TASK_STATUS = "task_status"
DELTA_CONTEXT_WINDOW_SAMPLE = "context_window_sample"
# Goldfive-originated run / drift / task-progress signals (issue #2, Phase B).
DELTA_RUN_STARTED = "run_started"
DELTA_RUN_COMPLETED = "run_completed"
DELTA_RUN_ABORTED = "run_aborted"
DELTA_GOAL_DERIVED = "goal_derived"
DELTA_DRIFT = "drift"
DELTA_TASK_PROGRESS = "task_progress"
# Registry-dispatch observability events (goldfive 2986775+). Observability
# only: the server doesn't mutate state for these, just forwards them so
# the frontend can render delegation edges and per-invocation rows.
DELTA_AGENT_INVOCATION_STARTED = "agent_invocation_started"
DELTA_AGENT_INVOCATION_COMPLETED = "agent_invocation_completed"
DELTA_DELEGATION_OBSERVED = "delegation_observed"
# Session lifecycle terminal signal (harmonograf#96). Fires when a goldfive
# run_completed / run_aborted event arrives and the session flips out of
# LIVE. The frontend's ``sessionIsInactive`` check reads this to clear the
# LIVE ACTIVITY panel even when INVOCATION spans are still stuck RUNNING
# in the DB (orphan-span cleanup is a belt-and-suspenders layer on top).
DELTA_SESSION_ENDED = "session_ended"
# Operator-observability: goldfive cooperatively cancelled one agent
# invocation (goldfive#251 Stream C / #259). Carries the dict-sourced
# harmonograf proto variant verbatim; the frontend renders a distinct
# cancel marker on the Trajectory / Gantt / Graph views. Plays the
# same "intervention timeline marker" role as DELTA_DRIFT but with a
# stop-glyph rather than a drift chevron.
DELTA_INVOCATION_CANCELLED = "invocation_cancelled"


@dataclass
class Delta:
    """A single event to push to WatchSession subscribers.

    payload shape depends on kind; consumers map kind → field.
    """

    session_id: str
    kind: str
    payload: Any


class Subscription:
    def __init__(self, session_id: str, maxsize: int) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[Delta] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class SessionBus:
    """Per-session fan-out.

    publish() is sync-safe-from-async: it never blocks the caller. If a
    subscriber's queue is full the event is dropped on that subscriber's
    floor and a backpressure delta is enqueued for it.
    """

    def __init__(self, queue_maxsize: int = 1024) -> None:
        self._subs: dict[str, list[Subscription]] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize

    async def subscribe(self, session_id: str) -> Subscription:
        sub = Subscription(session_id, self._queue_maxsize)
        async with self._lock:
            self._subs.setdefault(session_id, []).append(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        async with self._lock:
            lst = self._subs.get(sub.session_id)
            if not lst:
                return
            try:
                lst.remove(sub)
            except ValueError:
                pass
            if not lst:
                del self._subs[sub.session_id]

    def publish(self, delta: Delta) -> None:
        # Snapshot subscriber list without the async lock; subscribe/unsubscribe
        # under the lock atomically mutate the list object but reads of the
        # dict are fine from the same event loop.
        subs = list(self._subs.get(delta.session_id, ()))
        for sub in subs:
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(delta)
            except asyncio.QueueFull:
                sub.dropped += 1
                try:
                    sub.queue.put_nowait(
                        Delta(
                            session_id=delta.session_id,
                            kind=DELTA_BACKPRESSURE,
                            payload={"dropped": sub.dropped},
                        )
                    )
                except asyncio.QueueFull:
                    pass

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    # ---- convenience constructors ------------------------------------

    def publish_agent_upsert(self, agent: Agent) -> None:
        self.publish(Delta(agent.session_id, DELTA_AGENT_UPSERT, agent))

    def publish_agent_status(
        self,
        session_id: str,
        agent_id: str,
        status: AgentStatus,
        last_heartbeat: Optional[float],
        *,
        current_activity: str = "",
        progress_counter: int = 0,
        stuck: bool = False,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_AGENT_STATUS,
                {
                    "agent_id": agent_id,
                    "status": status,
                    "last_heartbeat": last_heartbeat,
                    "current_activity": current_activity,
                    "progress_counter": progress_counter,
                    "stuck": stuck,
                },
            )
        )

    def publish_span_start(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_START, span))

    def publish_span_update(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_UPDATE, span))

    def publish_span_end(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_END, span))

    def publish_annotation(self, annotation: Annotation) -> None:
        self.publish(Delta(annotation.session_id, DELTA_ANNOTATION, annotation))

    def publish_heartbeat(self, session_id: str, agent_id: str, stats: dict) -> None:
        self.publish(
            Delta(session_id, DELTA_HEARTBEAT, {"agent_id": agent_id, **stats})
        )

    def publish_task_plan(self, plan: TaskPlan) -> None:
        self.publish(Delta(plan.session_id, DELTA_TASK_PLAN, plan))

    def publish_task_status(
        self, session_id: str, plan_id: str, task: Task
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_TASK_STATUS,
                {"plan_id": plan_id, "task": task},
            )
        )

    def publish_context_window_sample(
        self,
        session_id: str,
        agent_id: str,
        tokens: int,
        limit_tokens: int,
        recorded_at: float,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_CONTEXT_WINDOW_SAMPLE,
                {
                    "agent_id": agent_id,
                    "tokens": tokens,
                    "limit_tokens": limit_tokens,
                    "recorded_at": recorded_at,
                },
            )
        )

    def publish_task_report(
        self,
        session_id: str,
        agent_id: str,
        report: str,
        invocation_span_id: str = "",
        recorded_at: Optional[float] = None,
    ) -> None:
        """Broadcast a TaskReport delta to WatchSession subscribers."""
        self.publish(
            Delta(
                session_id,
                DELTA_TASK_REPORT,
                {
                    "agent_id": agent_id,
                    "report": report,
                    "invocation_span_id": invocation_span_id,
                    "recorded_at": recorded_at or time.time(),
                },
            )
        )

    # Goldfive-originated deltas (issue #2, Phase B). Payloads are plain dicts
    # so WatchSession can route them to frontend-facing proto shapes in Phase
    # C/D without coupling the bus to goldfive's pb types.

    def publish_run_started(
        self,
        session_id: str,
        run_id: str,
        *,
        goal_summary: str = "",
        started_at: Optional[float] = None,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_RUN_STARTED,
                {
                    "run_id": run_id,
                    "goal_summary": goal_summary,
                    "started_at": started_at,
                },
            )
        )

    def publish_run_completed(
        self, session_id: str, run_id: str, *, outcome_summary: str = ""
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_RUN_COMPLETED,
                {"run_id": run_id, "outcome_summary": outcome_summary},
            )
        )

    def publish_run_aborted(
        self, session_id: str, run_id: str, *, reason: str = ""
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_RUN_ABORTED,
                {"run_id": run_id, "reason": reason},
            )
        )

    def publish_session_ended(
        self,
        session_id: str,
        *,
        ended_at: float,
        final_status: SessionStatus,
    ) -> None:
        """Announce that ``session_id`` has terminally left the LIVE state.

        Fired by the ingest pipeline when a goldfive ``run_completed`` or
        ``run_aborted`` event arrives. The frontend's ``useSessionWatch``
        listens for the matching :class:`SessionUpdate.session_ended`
        variant and flips ``sessionStatus`` to ``COMPLETED`` / ``ABORTED``
        so :func:`sessionIsInactive` returns ``true`` and the LIVE
        ACTIVITY panel's "N RUNNING" header clears — even when the
        underlying INVOCATION spans are still ``status=RUNNING`` in the
        DB (harmonograf#96). Orphan INVOCATION spans are also swept by
        the same handler as a belt-and-suspenders cleanup.
        """
        self.publish(
            Delta(
                session_id,
                DELTA_SESSION_ENDED,
                {"ended_at": ended_at, "final_status": final_status},
            )
        )

    def publish_goal_derived(
        self, session_id: str, run_id: str, goals: list[dict]
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_GOAL_DERIVED,
                {"run_id": run_id, "goals": goals},
            )
        )

    def publish_drift(
        self,
        session_id: str,
        run_id: str,
        *,
        kind: str,
        severity: str,
        detail: str = "",
        current_task_id: str = "",
        current_agent_id: str = "",
        annotation_id: str = "",
        drift_id: str = "",
        recorded_at: float | None = None,
    ) -> None:
        # ``annotation_id`` is non-empty for user-control drifts (USER_STEER,
        # USER_CANCEL) minted from a ControlMessage with an annotation id in
        # its payload (goldfive#176). The frontend dedup path (harmonograf#75)
        # uses it to collapse the drift row into the source annotation row
        # so a single user STEER renders as one intervention card, not three.
        #
        # ``drift_id`` (goldfive#199 / harmonograf#99) is the goldfive-minted
        # UUID4 on every DriftDetected. The intervention aggregator uses
        # it as the strict join key when merging PlanRevised rows
        # triggered by autonomous drifts onto the drift row.
        #
        # ``recorded_at`` is the wall-clock moment the drift was ingested
        # (seconds since epoch). Forwarded to the delta payload so the
        # frontend.py DELTA_DRIFT translator can stamp ``emitted_at`` on
        # the outgoing SessionUpdate — without this the frontend falls
        # back to a session-relative-ms of 0 for live drifts (closes #73).
        self.publish(
            Delta(
                session_id,
                DELTA_DRIFT,
                {
                    "run_id": run_id,
                    "kind": kind,
                    "severity": severity,
                    "detail": detail,
                    "current_task_id": current_task_id,
                    "current_agent_id": current_agent_id,
                    "annotation_id": annotation_id,
                    "drift_id": drift_id,
                    "recorded_at": recorded_at,
                },
            )
        )

    def publish_invocation_cancelled(
        self,
        session_id: str,
        run_id: str,
        *,
        sequence: int = 0,
        emitted_at: float | None = None,
        invocation_id: str = "",
        agent_name: str = "",
        reason: str = "",
        severity: str = "",
        drift_id: str = "",
        drift_kind: str = "",
        detail: str = "",
        tool_name: str = "",
        recorded_at: float | None = None,
    ) -> None:
        """Publish an ``invocation_cancelled`` record onto the session bus.

        Fields mirror the wire ``InvocationCancelled`` message (see
        ``telemetry.proto``). ``recorded_at`` is the ingest-side wall
        clock; ``emitted_at`` is the goldfive-side wall clock from the
        envelope — kept separately so the frontend can use either (the
        frontend.py translator prefers ``emitted_at`` and falls back to
        ``recorded_at``, matching the DELTA_DRIFT pattern).
        """
        self.publish(
            Delta(
                session_id,
                DELTA_INVOCATION_CANCELLED,
                {
                    "run_id": run_id,
                    "sequence": int(sequence or 0),
                    "emitted_at": emitted_at,
                    "invocation_id": invocation_id,
                    "agent_name": agent_name,
                    "reason": reason,
                    "severity": severity,
                    "drift_id": drift_id,
                    "drift_kind": drift_kind,
                    "detail": detail,
                    "tool_name": tool_name,
                    "recorded_at": recorded_at,
                },
            )
        )

    def publish_task_progress(
        self,
        session_id: str,
        run_id: str,
        *,
        task_id: str,
        fraction: float,
        detail: str = "",
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_TASK_PROGRESS,
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "fraction": fraction,
                    "detail": detail,
                },
            )
        )

    def publish_agent_invocation_started(
        self,
        session_id: str,
        run_id: str,
        *,
        agent_name: str,
        task_id: str = "",
        invocation_id: str = "",
        parent_invocation_id: str = "",
        started_at: Optional[float] = None,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_AGENT_INVOCATION_STARTED,
                {
                    "run_id": run_id,
                    "agent_name": agent_name,
                    "task_id": task_id,
                    "invocation_id": invocation_id,
                    "parent_invocation_id": parent_invocation_id,
                    "started_at": started_at,
                },
            )
        )

    def publish_agent_invocation_completed(
        self,
        session_id: str,
        run_id: str,
        *,
        agent_name: str,
        task_id: str = "",
        invocation_id: str = "",
        summary: str = "",
        completed_at: Optional[float] = None,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_AGENT_INVOCATION_COMPLETED,
                {
                    "run_id": run_id,
                    "agent_name": agent_name,
                    "task_id": task_id,
                    "invocation_id": invocation_id,
                    "summary": summary,
                    "completed_at": completed_at,
                },
            )
        )

    def publish_delegation_observed(
        self,
        session_id: str,
        run_id: str,
        *,
        from_agent: str,
        to_agent: str,
        task_id: str = "",
        invocation_id: str = "",
        observed_at: Optional[float] = None,
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_DELEGATION_OBSERVED,
                {
                    "run_id": run_id,
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "task_id": task_id,
                    "invocation_id": invocation_id,
                    "observed_at": observed_at,
                },
            )
        )
