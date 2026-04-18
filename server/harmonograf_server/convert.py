"""Boundary conversions between generated protobuf messages and the
storage-layer dataclasses. The storage layer must not import protobuf,
so every translation lives here.
"""

from __future__ import annotations

from typing import Any, Optional

from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.pb import types_pb2  # imports goldfive.pb via pb/__init__.py
from harmonograf_server.pb import telemetry_pb2
from goldfive.v1 import types_pb2 as goldfive_types_pb2  # noqa: E402
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    Framework,
    LinkRelation,
    Span,
    SpanKind,
    SpanLink,
    SpanStatus,
    TaskStatus,
)


# --- scalar / timestamp -----------------------------------------------------


def ts_to_float(ts: Timestamp) -> float:
    if ts is None:
        return 0.0
    return ts.seconds + ts.nanos / 1e9


def float_to_ts(value: Optional[float]) -> Optional[Timestamp]:
    if value is None:
        return None
    ts = Timestamp()
    ts.seconds = int(value)
    ts.nanos = int((value - int(value)) * 1e9)
    return ts


# --- enums ------------------------------------------------------------------

_PB_TO_FRAMEWORK = {
    types_pb2.FRAMEWORK_UNSPECIFIED: Framework.UNKNOWN,
    types_pb2.FRAMEWORK_CUSTOM: Framework.CUSTOM,
    types_pb2.FRAMEWORK_ADK: Framework.ADK,
}

_PB_TO_CAPABILITY = {
    types_pb2.CAPABILITY_PAUSE_RESUME: Capability.PAUSE_RESUME,
    types_pb2.CAPABILITY_CANCEL: Capability.CANCEL,
    types_pb2.CAPABILITY_REWIND: Capability.REWIND,
    types_pb2.CAPABILITY_STEERING: Capability.STEERING,
    types_pb2.CAPABILITY_HUMAN_IN_LOOP: Capability.HUMAN_IN_LOOP,
    types_pb2.CAPABILITY_INTERCEPT_TRANSFER: Capability.INTERCEPT_TRANSFER,
}

_PB_TO_SPAN_KIND = {
    types_pb2.SPAN_KIND_UNSPECIFIED: SpanKind.CUSTOM,
    types_pb2.SPAN_KIND_INVOCATION: SpanKind.INVOCATION,
    types_pb2.SPAN_KIND_LLM_CALL: SpanKind.LLM_CALL,
    types_pb2.SPAN_KIND_TOOL_CALL: SpanKind.TOOL_CALL,
    types_pb2.SPAN_KIND_USER_MESSAGE: SpanKind.USER_MESSAGE,
    types_pb2.SPAN_KIND_AGENT_MESSAGE: SpanKind.AGENT_MESSAGE,
    types_pb2.SPAN_KIND_TRANSFER: SpanKind.TRANSFER,
    types_pb2.SPAN_KIND_WAIT_FOR_HUMAN: SpanKind.WAIT_FOR_HUMAN,
    types_pb2.SPAN_KIND_PLANNED: SpanKind.PLANNED,
    types_pb2.SPAN_KIND_CUSTOM: SpanKind.CUSTOM,
}

_PB_TO_SPAN_STATUS = {
    types_pb2.SPAN_STATUS_UNSPECIFIED: SpanStatus.RUNNING,
    types_pb2.SPAN_STATUS_PENDING: SpanStatus.PENDING,
    types_pb2.SPAN_STATUS_RUNNING: SpanStatus.RUNNING,
    types_pb2.SPAN_STATUS_COMPLETED: SpanStatus.COMPLETED,
    types_pb2.SPAN_STATUS_FAILED: SpanStatus.FAILED,
    types_pb2.SPAN_STATUS_CANCELLED: SpanStatus.CANCELLED,
    types_pb2.SPAN_STATUS_AWAITING_HUMAN: SpanStatus.AWAITING_HUMAN,
}

_PB_TO_LINK_RELATION = {
    types_pb2.LINK_RELATION_UNSPECIFIED: LinkRelation.FOLLOWS,
    types_pb2.LINK_RELATION_INVOKED: LinkRelation.INVOKED,
    types_pb2.LINK_RELATION_WAITING_ON: LinkRelation.WAITING_ON,
    types_pb2.LINK_RELATION_TRIGGERED_BY: LinkRelation.TRIGGERED_BY,
    types_pb2.LINK_RELATION_FOLLOWS: LinkRelation.FOLLOWS,
    types_pb2.LINK_RELATION_REPLACES: LinkRelation.REPLACES,
}

_PB_TO_ANNOTATION_KIND = {
    types_pb2.ANNOTATION_KIND_COMMENT: AnnotationKind.COMMENT,
    types_pb2.ANNOTATION_KIND_STEERING: AnnotationKind.STEERING,
    types_pb2.ANNOTATION_KIND_HUMAN_RESPONSE: AnnotationKind.HUMAN_RESPONSE,
}

_PB_TO_AGENT_STATUS = {
    types_pb2.AGENT_STATUS_UNSPECIFIED: AgentStatus.CONNECTED,
    types_pb2.AGENT_STATUS_CONNECTED: AgentStatus.CONNECTED,
    types_pb2.AGENT_STATUS_DISCONNECTED: AgentStatus.DISCONNECTED,
    types_pb2.AGENT_STATUS_CRASHED: AgentStatus.CRASHED,
}


# inverse maps for serialization back to proto
_FRAMEWORK_TO_PB = {v: k for k, v in _PB_TO_FRAMEWORK.items()}
_CAPABILITY_TO_PB = {v: k for k, v in _PB_TO_CAPABILITY.items()}
_SPAN_KIND_TO_PB = {
    SpanKind.INVOCATION: types_pb2.SPAN_KIND_INVOCATION,
    SpanKind.LLM_CALL: types_pb2.SPAN_KIND_LLM_CALL,
    SpanKind.TOOL_CALL: types_pb2.SPAN_KIND_TOOL_CALL,
    SpanKind.USER_MESSAGE: types_pb2.SPAN_KIND_USER_MESSAGE,
    SpanKind.AGENT_MESSAGE: types_pb2.SPAN_KIND_AGENT_MESSAGE,
    SpanKind.TRANSFER: types_pb2.SPAN_KIND_TRANSFER,
    SpanKind.WAIT_FOR_HUMAN: types_pb2.SPAN_KIND_WAIT_FOR_HUMAN,
    SpanKind.PLANNED: types_pb2.SPAN_KIND_PLANNED,
    SpanKind.CUSTOM: types_pb2.SPAN_KIND_CUSTOM,
}
_SPAN_STATUS_TO_PB = {
    SpanStatus.PENDING: types_pb2.SPAN_STATUS_PENDING,
    SpanStatus.RUNNING: types_pb2.SPAN_STATUS_RUNNING,
    SpanStatus.COMPLETED: types_pb2.SPAN_STATUS_COMPLETED,
    SpanStatus.FAILED: types_pb2.SPAN_STATUS_FAILED,
    SpanStatus.CANCELLED: types_pb2.SPAN_STATUS_CANCELLED,
    SpanStatus.AWAITING_HUMAN: types_pb2.SPAN_STATUS_AWAITING_HUMAN,
}
_LINK_RELATION_TO_PB = {v: k for k, v in _PB_TO_LINK_RELATION.items() if v is not LinkRelation.FOLLOWS}
_LINK_RELATION_TO_PB[LinkRelation.FOLLOWS] = types_pb2.LINK_RELATION_FOLLOWS
_ANNOTATION_KIND_TO_PB = {v: k for k, v in _PB_TO_ANNOTATION_KIND.items()}
_AGENT_STATUS_TO_PB = {
    AgentStatus.CONNECTED: types_pb2.AGENT_STATUS_CONNECTED,
    AgentStatus.DISCONNECTED: types_pb2.AGENT_STATUS_DISCONNECTED,
    AgentStatus.CRASHED: types_pb2.AGENT_STATUS_CRASHED,
}

# TaskStatus lives in goldfive after the Phase A migration (issue #2). The
# enum is a StrEnum whose values match goldfive.v1.TaskStatus name suffixes,
# so we derive the mapping programmatically rather than listing each pair.
_PB_TO_TASK_STATUS = {
    goldfive_types_pb2.TASK_STATUS_UNSPECIFIED: TaskStatus.PENDING,
    goldfive_types_pb2.TASK_STATUS_PENDING: TaskStatus.PENDING,
    goldfive_types_pb2.TASK_STATUS_RUNNING: TaskStatus.RUNNING,
    goldfive_types_pb2.TASK_STATUS_COMPLETED: TaskStatus.COMPLETED,
    goldfive_types_pb2.TASK_STATUS_FAILED: TaskStatus.FAILED,
    goldfive_types_pb2.TASK_STATUS_CANCELLED: TaskStatus.CANCELLED,
    goldfive_types_pb2.TASK_STATUS_BLOCKED: TaskStatus.BLOCKED,
}
_TASK_STATUS_TO_PB = {v: k for k, v in _PB_TO_TASK_STATUS.items() if v is not TaskStatus.PENDING}
_TASK_STATUS_TO_PB[TaskStatus.PENDING] = goldfive_types_pb2.TASK_STATUS_PENDING


def task_status_from_pb(pb_status: int) -> TaskStatus:
    return _PB_TO_TASK_STATUS.get(pb_status, TaskStatus.PENDING)


def task_status_to_pb(status: TaskStatus) -> int:
    return _TASK_STATUS_TO_PB.get(status, goldfive_types_pb2.TASK_STATUS_PENDING)


# --- attribute values -------------------------------------------------------


def attr_value_to_py(av: types_pb2.AttributeValue) -> Any:
    which = av.WhichOneof("value") if av.DESCRIPTOR.oneofs_by_name else None
    # oneof in generated AttributeValue is implicit (fields in the same slot)
    # Fall back to checking set fields.
    if av.HasField("string_value") if "string_value" in av.DESCRIPTOR.fields_by_name else False:
        return av.string_value
    # Simpler path: test each scalar.
    if av.string_value:
        return av.string_value
    if av.int_value:
        return av.int_value
    if av.double_value:
        return av.double_value
    if av.bool_value:
        return av.bool_value
    if av.bytes_value:
        return av.bytes_value
    if len(av.array_value.values) > 0:
        return [attr_value_to_py(x) for x in av.array_value.values]
    return None


def attr_map_to_dict(attrs) -> dict[str, Any]:
    return {k: attr_value_to_py(v) for k, v in attrs.items()}


def py_to_attr_value(value: Any) -> types_pb2.AttributeValue:
    av = types_pb2.AttributeValue()
    if value is None:
        return av
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    elif isinstance(value, float):
        av.double_value = value
    elif isinstance(value, bytes):
        av.bytes_value = value
    elif isinstance(value, str):
        av.string_value = value
    elif isinstance(value, (list, tuple)):
        for item in value:
            av.array_value.values.append(py_to_attr_value(item))
    else:
        av.string_value = str(value)
    return av


# --- hello / agent ----------------------------------------------------------


def hello_to_agent(
    hello: telemetry_pb2.Hello,
    *,
    session_id: str,
    connected_at: float,
    last_heartbeat: float,
) -> Agent:
    return Agent(
        id=hello.agent_id,
        session_id=session_id,
        name=hello.name or hello.agent_id,
        framework=_PB_TO_FRAMEWORK.get(hello.framework, Framework.UNKNOWN),
        framework_version=hello.framework_version,
        capabilities=[
            _PB_TO_CAPABILITY[c] for c in hello.capabilities if c in _PB_TO_CAPABILITY
        ],
        metadata=dict(hello.metadata),
        connected_at=connected_at,
        last_heartbeat=last_heartbeat,
        status=AgentStatus.CONNECTED,
    )


# --- spans ------------------------------------------------------------------


def pb_span_to_storage(pb: types_pb2.Span, *, agent_id: str, session_id: str) -> Span:
    links = [
        SpanLink(
            target_span_id=ln.target_span_id,
            target_agent_id=ln.target_agent_id,
            relation=_PB_TO_LINK_RELATION.get(ln.relation, LinkRelation.FOLLOWS),
        )
        for ln in pb.links
    ]
    end_time: Optional[float] = None
    if pb.HasField("end_time"):
        end_time = ts_to_float(pb.end_time)
    start_time = ts_to_float(pb.start_time) if pb.HasField("start_time") else 0.0
    payload_digest: Optional[str] = None
    payload_mime = ""
    payload_size = 0
    payload_summary = ""
    payload_role = ""
    payload_evicted = False
    if len(pb.payload_refs):
        ref = pb.payload_refs[0]
        payload_digest = ref.digest
        payload_mime = ref.mime
        payload_size = ref.size
        payload_summary = ref.summary
        payload_role = ref.role
        payload_evicted = ref.evicted
    error = None
    if pb.HasField("error"):
        error = {"type": pb.error.type, "message": pb.error.message, "stack": pb.error.stack}
    return Span(
        id=pb.id,
        session_id=session_id or pb.session_id,
        agent_id=agent_id or pb.agent_id,
        parent_span_id=pb.parent_span_id or None,
        kind=_PB_TO_SPAN_KIND.get(pb.kind, SpanKind.CUSTOM),
        kind_string=pb.kind_string or None,
        status=_PB_TO_SPAN_STATUS.get(pb.status, SpanStatus.RUNNING),
        name=pb.name,
        start_time=start_time,
        end_time=end_time,
        attributes=attr_map_to_dict(pb.attributes),
        payload_digest=payload_digest,
        payload_mime=payload_mime,
        payload_size=payload_size,
        payload_summary=payload_summary,
        payload_role=payload_role,
        payload_evicted=payload_evicted,
        links=links,
        error=error,
    )


def storage_span_to_pb(span: Span) -> types_pb2.Span:
    pb = types_pb2.Span(
        id=span.id,
        session_id=span.session_id,
        agent_id=span.agent_id,
        parent_span_id=span.parent_span_id or "",
        kind=_SPAN_KIND_TO_PB.get(span.kind, types_pb2.SPAN_KIND_CUSTOM),
        kind_string=span.kind_string or "",
        status=_SPAN_STATUS_TO_PB.get(span.status, types_pb2.SPAN_STATUS_RUNNING),
        name=span.name,
    )
    start_ts = float_to_ts(span.start_time)
    if start_ts is not None:
        pb.start_time.CopyFrom(start_ts)
    if span.end_time is not None:
        end_ts = float_to_ts(span.end_time)
        if end_ts is not None:
            pb.end_time.CopyFrom(end_ts)
    for k, v in (span.attributes or {}).items():
        pb.attributes[k].CopyFrom(py_to_attr_value(v))
    for ln in span.links:
        pb.links.add(
            target_span_id=ln.target_span_id,
            target_agent_id=ln.target_agent_id,
            relation=_LINK_RELATION_TO_PB.get(ln.relation, types_pb2.LINK_RELATION_FOLLOWS),
        )
    if span.payload_digest:
        pb.payload_refs.add(
            digest=span.payload_digest,
            size=span.payload_size,
            mime=span.payload_mime,
            summary=span.payload_summary,
            role=span.payload_role,
            evicted=span.payload_evicted,
        )
    if span.error:
        pb.error.type = span.error.get("type", "")
        pb.error.message = span.error.get("message", "")
        pb.error.stack = span.error.get("stack", "")
    return pb


def span_status_from_pb(pb_status: int) -> SpanStatus:
    return _PB_TO_SPAN_STATUS.get(pb_status, SpanStatus.RUNNING)


# --- task plans -------------------------------------------------------------
#
# Task/TaskPlan/UpdatedTaskStatus pb conversions were removed in Phase A of
# the goldfive migration (issue #2). Plan + task state now rides inside
# TelemetryUp.goldfive_event (goldfive.v1.Event); the ingest-side
# dispatcher and its storage wiring land in Phase B.


def storage_agent_to_pb(agent: Agent) -> types_pb2.Agent:
    pb = types_pb2.Agent(
        id=agent.id,
        session_id=agent.session_id,
        name=agent.name,
        framework=_FRAMEWORK_TO_PB.get(agent.framework, types_pb2.FRAMEWORK_UNSPECIFIED),
        framework_version=agent.framework_version,
        capabilities=[_CAPABILITY_TO_PB[c] for c in agent.capabilities if c in _CAPABILITY_TO_PB],
        metadata=agent.metadata,
        status=_AGENT_STATUS_TO_PB.get(agent.status, types_pb2.AGENT_STATUS_CONNECTED),
    )
    ts = float_to_ts(agent.connected_at)
    if ts is not None:
        pb.connected_at.CopyFrom(ts)
    ts = float_to_ts(agent.last_heartbeat)
    if ts is not None:
        pb.last_heartbeat.CopyFrom(ts)
    return pb
