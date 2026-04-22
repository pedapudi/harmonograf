"""Postgres-backed Store stub.

This module is a *template*, not an implementation. Every abstract method
raises ``NotImplementedError``. It exists so contributors who want to add
a Postgres backend can ``cp postgres.py mybackend.py`` and fill in the
blanks against a stable interface, instead of starting from a blank file.

A real implementation would need (at minimum):

* ``asyncpg`` (recommended) or ``psycopg[binary]>=3`` with ``async`` support
* schema migrations — the SQLite backend declares its schema inline in
  ``sqlite.py`` (see the ``SCHEMA`` constant) but anything multi-process
  should ship versioned migrations (alembic, sqitch, hand-rolled)
* a connection pool — one ``Store`` instance is shared across the whole
  process, so the underlying handle must be safe for many concurrent
  callers (sqlite uses an ``asyncio.Lock`` to serialize; postgres should
  rely on a real pool)
* a payload storage decision — the in-tree backends store payload bytes
  on the local filesystem (``sqlite``) or in the process heap
  (``memory``). For a multi-node deployment you almost certainly want
  object storage (S3, GCS) keyed by the same content-addressed digest
  the rest of the system already uses.

See ``docs/dev-guide/storage-backends.md`` for the full contract — every
``Store`` method, its idempotency requirements, the deltas the ingest
pipeline expects to see published on the bus, and the conformance suite
in ``tests/storage_conformance_test.py`` you should make pass before
shipping.

The ``hgraf-add-storage-backend`` skill walks through the steps in
order. Read it before starting.
"""

from __future__ import annotations

from typing import Any, Optional

from harmonograf_server.storage.base import (
    Agent,
    AgentStatus,
    Annotation,
    ContextWindowSample,
    PayloadMeta,
    PayloadRecord,
    Session,
    SessionStatus,
    Span,
    SpanStatus,
    Stats,
    Store,
    Task,
    TaskPlan,
    TaskStatus,
)


_NOT_IMPLEMENTED = (
    "PostgresStore is a stub. See docs/dev-guide/storage-backends.md and the "
    "hgraf-add-storage-backend skill for how to fill it in."
)


class PostgresStore(Store):
    """Stub Postgres backend. Every method raises NotImplementedError."""

    def __init__(self, dsn: str, **opts: Any) -> None:
        self.dsn = dsn
        self.opts = opts

    async def start(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def close(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def create_session(self, session: Session) -> Session:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_session(self, session_id: str) -> Optional[Session]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_sessions(
        self,
        status: Optional[SessionStatus] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        ended_at: Optional[float] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Optional[Session]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def delete_session(self, session_id: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def register_agent(self, agent: Agent) -> Agent:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_agent(self, session_id: str, agent_id: str) -> Optional[Agent]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_agents_for_session(self, session_id: str) -> list[Agent]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def update_agent_status(
        self,
        session_id: str,
        agent_id: str,
        status: AgentStatus,
        last_heartbeat: Optional[float] = None,
    ) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def append_span(self, span: Span) -> Span:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def update_span(
        self,
        span_id: str,
        *,
        status: Optional[SpanStatus] = None,
        attributes: Optional[dict[str, Any]] = None,
        payload_digest: Optional[str] = None,
        payload_mime: Optional[str] = None,
        payload_size: Optional[int] = None,
        payload_summary: Optional[str] = None,
        payload_role: Optional[str] = None,
        payload_evicted: Optional[bool] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> Optional[Span]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def end_span(
        self,
        span_id: str,
        end_time: float,
        status: SpanStatus,
        error: Optional[dict[str, Any]] = None,
    ) -> Optional[Span]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_span(self, span_id: str) -> Optional[Span]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_spans(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[Span]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def put_annotation(self, annotation: Annotation) -> Annotation:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_annotations(
        self,
        session_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> list[Annotation]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def put_payload(
        self, digest: str, data: bytes, mime: str, summary: str = ""
    ) -> PayloadMeta:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_payload(self, digest: str) -> Optional[PayloadRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def has_payload(self, digest: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def gc_payloads(self) -> int:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def put_task_plan(self, plan: TaskPlan) -> TaskPlan:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_task_plan(self, plan_id: str) -> Optional[TaskPlan]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_task_plans_for_session(self, session_id: str) -> list[TaskPlan]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def update_task_status(
        self,
        plan_id: str,
        task_id: str,
        status: TaskStatus,
        bound_span_id: Optional[str] = None,
        *,
        cancel_reason: str = "",
    ) -> Optional[Task]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def append_context_window_sample(self, sample: ContextWindowSample) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_context_window_samples(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        limit_per_agent: int = 200,
    ) -> list[ContextWindowSample]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def stats(self) -> Stats:
        raise NotImplementedError(_NOT_IMPLEMENTED)
