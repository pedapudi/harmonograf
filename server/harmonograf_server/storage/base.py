"""Storage abstraction and shared data model.

These dataclasses are the in-memory shape every backend round-trips. They are
intentionally independent of generated protobuf classes so the storage layer
can land before proto codegen (task #2). The ingest layer (#4) is responsible
for translating between proto messages and these types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Union

# Orchestration types moved to goldfive in the Phase A migration (issue #2).
# Harmonograf re-exports the goldfive dataclasses here so storage signatures
# stay stable for callers, while the underlying truth lives in goldfive.
from goldfive.types import Task as Task  # noqa: F401
from goldfive.types import TaskEdge as TaskEdge  # noqa: F401
from goldfive.types import TaskStatus as TaskStatus  # noqa: F401


class SessionStatus(str, Enum):
    LIVE = "LIVE"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class AgentStatus(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    CRASHED = "CRASHED"


class SpanKind(str, Enum):
    INVOCATION = "INVOCATION"
    LLM_CALL = "LLM_CALL"
    TOOL_CALL = "TOOL_CALL"
    USER_MESSAGE = "USER_MESSAGE"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    TRANSFER = "TRANSFER"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    PLANNED = "PLANNED"
    CUSTOM = "CUSTOM"


class SpanStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_HUMAN = "AWAITING_HUMAN"


class LinkRelation(str, Enum):
    INVOKED = "INVOKED"
    WAITING_ON = "WAITING_ON"
    TRIGGERED_BY = "TRIGGERED_BY"
    FOLLOWS = "FOLLOWS"
    REPLACES = "REPLACES"


class AnnotationKind(str, Enum):
    COMMENT = "COMMENT"
    STEERING = "STEERING"
    HUMAN_RESPONSE = "HUMAN_RESPONSE"


class Capability(str, Enum):
    PAUSE_RESUME = "PAUSE_RESUME"
    CANCEL = "CANCEL"
    REWIND = "REWIND"
    STEERING = "STEERING"
    HUMAN_IN_LOOP = "HUMAN_IN_LOOP"
    INTERCEPT_TRANSFER = "INTERCEPT_TRANSFER"


class Framework(str, Enum):
    ADK = "ADK"
    CUSTOM = "CUSTOM"
    UNKNOWN = "UNKNOWN"


# --- entities ---------------------------------------------------------------


@dataclass
class Session:
    id: str
    title: str
    created_at: float
    ended_at: Optional[float] = None
    status: SessionStatus = SessionStatus.LIVE
    agent_ids: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Agent:
    id: str
    session_id: str
    name: str
    framework: Framework = Framework.UNKNOWN
    framework_version: str = ""
    capabilities: list[Capability] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    connected_at: float = 0.0
    last_heartbeat: float = 0.0
    status: AgentStatus = AgentStatus.CONNECTED


@dataclass
class SpanLink:
    target_span_id: str
    target_agent_id: str
    relation: LinkRelation


@dataclass
class Span:
    id: str
    session_id: str
    agent_id: str
    kind: SpanKind
    name: str
    start_time: float
    parent_span_id: Optional[str] = None
    status: SpanStatus = SpanStatus.RUNNING
    end_time: Optional[float] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    payload_digest: Optional[str] = None
    payload_mime: str = ""
    payload_size: int = 0
    payload_summary: str = ""
    payload_role: str = ""
    payload_evicted: bool = False
    links: list[SpanLink] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None
    kind_string: Optional[str] = None  # for CUSTOM, the framework label


@dataclass
class AnnotationTarget:
    """Either a single span or an (agent_id, time_range) on the timeline."""

    span_id: Optional[str] = None
    agent_id: Optional[str] = None
    time_start: Optional[float] = None
    time_end: Optional[float] = None


@dataclass
class Annotation:
    id: str
    session_id: str
    target: AnnotationTarget
    author: str
    created_at: float
    kind: AnnotationKind
    body: str
    delivered_at: Optional[float] = None


@dataclass
class PayloadMeta:
    digest: str
    size: int
    mime: str
    summary: str = ""


@dataclass
class PayloadRecord:
    meta: PayloadMeta
    bytes_: bytes


# TaskPlan composes goldfive tasks/edges with harmonograf's session-scoped
# bookkeeping (session_id, invocation_span_id, planner_agent_id, created_at).
# Goldfive's own Plan has run_id / goal_ids instead; we keep both concerns
# separate so goldfive can evolve without touching harmonograf's storage
# schema. See docs/goldfive-migration-plan.md §1.3.
@dataclass
class TaskPlan:
    id: str
    session_id: str
    created_at: float
    invocation_span_id: str = ""
    planner_agent_id: str = ""
    summary: str = ""
    tasks: list[Task] = field(default_factory=list)
    edges: list[TaskEdge] = field(default_factory=list)
    revision_reason: str = ""
    revision_kind: str = ""
    revision_severity: str = ""
    revision_index: int = 0


@dataclass
class ContextWindowSample:
    session_id: str
    agent_id: str
    recorded_at: float
    tokens: int
    limit_tokens: int


@dataclass
class Stats:
    session_count: int
    agent_count: int
    span_count: int
    payload_count: int
    payload_bytes: int
    disk_usage_bytes: int


# --- store interface --------------------------------------------------------


class Store(ABC):
    """Async storage abstraction. All backends implement this."""

    # lifecycle ------------------------------------------------------------
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # sessions -------------------------------------------------------------
    @abstractmethod
    async def create_session(self, session: Session) -> Session: ...

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[Session]: ...

    @abstractmethod
    async def list_sessions(
        self,
        status: Optional[SessionStatus] = None,
        limit: Optional[int] = None,
    ) -> list[Session]: ...

    @abstractmethod
    async def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[SessionStatus] = None,
        ended_at: Optional[float] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Optional[Session]: ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool: ...

    # agents ---------------------------------------------------------------
    @abstractmethod
    async def register_agent(self, agent: Agent) -> Agent: ...

    @abstractmethod
    async def get_agent(self, session_id: str, agent_id: str) -> Optional[Agent]: ...

    @abstractmethod
    async def list_agents_for_session(self, session_id: str) -> list[Agent]: ...

    @abstractmethod
    async def update_agent_status(
        self,
        session_id: str,
        agent_id: str,
        status: AgentStatus,
        last_heartbeat: Optional[float] = None,
    ) -> None: ...

    # spans ----------------------------------------------------------------
    @abstractmethod
    async def append_span(self, span: Span) -> Span:
        """Insert a span. If a span with the same id already exists, this is a
        no-op (idempotent — supports reconnect/replay)."""

    @abstractmethod
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
    ) -> Optional[Span]: ...

    @abstractmethod
    async def end_span(
        self,
        span_id: str,
        end_time: float,
        status: SpanStatus,
        error: Optional[dict[str, Any]] = None,
    ) -> Optional[Span]: ...

    @abstractmethod
    async def get_span(self, span_id: str) -> Optional[Span]: ...

    @abstractmethod
    async def get_spans(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[Span]: ...

    # annotations ----------------------------------------------------------
    @abstractmethod
    async def put_annotation(self, annotation: Annotation) -> Annotation: ...

    @abstractmethod
    async def list_annotations(
        self,
        session_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> list[Annotation]: ...

    # payloads -------------------------------------------------------------
    @abstractmethod
    async def put_payload(
        self, digest: str, data: bytes, mime: str, summary: str = ""
    ) -> PayloadMeta: ...

    @abstractmethod
    async def get_payload(self, digest: str) -> Optional[PayloadRecord]: ...

    @abstractmethod
    async def has_payload(self, digest: str) -> bool: ...

    @abstractmethod
    async def gc_payloads(self) -> int:
        """Remove content-addressed payloads no span references.

        Returns the number of payloads evicted. Safe to call at any time;
        intended as a belt-and-suspenders sweep after delete_session in case
        a backend's per-delete accounting leaks a reference.
        """

    # readiness ------------------------------------------------------------
    async def ping(self) -> bool:
        """Trivial readiness probe for /readyz.

        Backends that want to actually touch their underlying storage
        (e.g. sqlite executes ``SELECT 1``) should override. The default
        implementation always reports ready.
        """
        return True

    # task plans -----------------------------------------------------------
    @abstractmethod
    async def put_task_plan(self, plan: TaskPlan) -> TaskPlan: ...

    @abstractmethod
    async def get_task_plan(self, plan_id: str) -> Optional[TaskPlan]: ...

    @abstractmethod
    async def list_task_plans_for_session(
        self, session_id: str
    ) -> list[TaskPlan]: ...

    @abstractmethod
    async def update_task_status(
        self,
        plan_id: str,
        task_id: str,
        status: TaskStatus,
        bound_span_id: Optional[str] = None,
    ) -> Optional[Task]: ...

    # context window samples ----------------------------------------------
    @abstractmethod
    async def append_context_window_sample(
        self, sample: ContextWindowSample
    ) -> None: ...

    @abstractmethod
    async def list_context_window_samples(
        self,
        session_id: str,
        agent_id: Optional[str] = None,
        limit_per_agent: int = 200,
    ) -> list[ContextWindowSample]: ...

    # stats ----------------------------------------------------------------
    @abstractmethod
    async def stats(self) -> Stats: ...
