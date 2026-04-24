import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from harmonograf.v1 import types_pb2 as _types_pb2
from harmonograf.v1 import telemetry_pb2 as _telemetry_pb2
from goldfive.v1 import control_pb2 as _control_pb2
from goldfive.v1 import events_pb2 as _events_pb2
from goldfive.v1 import types_pb2 as _types_pb2_1
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionSummary(_message.Message):
    __slots__ = ("id", "title", "created_at", "ended_at", "status", "agent_count", "attention_count", "last_activity")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    AGENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    ATTENTION_COUNT_FIELD_NUMBER: _ClassVar[int]
    LAST_ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    created_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    status: _types_pb2.SessionStatus
    agent_count: int
    attention_count: int
    last_activity: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[_types_pb2.SessionStatus, str]] = ..., agent_count: _Optional[int] = ..., attention_count: _Optional[int] = ..., last_activity: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ListSessionsRequest(_message.Message):
    __slots__ = ("status_filter", "search", "limit", "offset")
    STATUS_FILTER_FIELD_NUMBER: _ClassVar[int]
    SEARCH_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    status_filter: _types_pb2.SessionStatus
    search: str
    limit: int
    offset: int
    def __init__(self, status_filter: _Optional[_Union[_types_pb2.SessionStatus, str]] = ..., search: _Optional[str] = ..., limit: _Optional[int] = ..., offset: _Optional[int] = ...) -> None: ...

class ListSessionsResponse(_message.Message):
    __slots__ = ("sessions", "total_count")
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_COUNT_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[SessionSummary]
    total_count: int
    def __init__(self, sessions: _Optional[_Iterable[_Union[SessionSummary, _Mapping]]] = ..., total_count: _Optional[int] = ...) -> None: ...

class WatchSessionRequest(_message.Message):
    __slots__ = ("session_id", "window_start", "window_end")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WINDOW_START_FIELD_NUMBER: _ClassVar[int]
    WINDOW_END_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    window_start: _timestamp_pb2.Timestamp
    window_end: _timestamp_pb2.Timestamp
    def __init__(self, session_id: _Optional[str] = ..., window_start: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., window_end: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionUpdate(_message.Message):
    __slots__ = ("session", "agent", "initial_span", "initial_annotation", "burst_complete", "new_span", "updated_span", "ended_span", "new_annotation", "agent_joined", "agent_left", "agent_status_changed", "session_ended", "payload_available", "task_report", "context_window_sample", "goldfive_event", "refine_attempted", "refine_failed")
    SESSION_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    INITIAL_SPAN_FIELD_NUMBER: _ClassVar[int]
    INITIAL_ANNOTATION_FIELD_NUMBER: _ClassVar[int]
    BURST_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    NEW_SPAN_FIELD_NUMBER: _ClassVar[int]
    UPDATED_SPAN_FIELD_NUMBER: _ClassVar[int]
    ENDED_SPAN_FIELD_NUMBER: _ClassVar[int]
    NEW_ANNOTATION_FIELD_NUMBER: _ClassVar[int]
    AGENT_JOINED_FIELD_NUMBER: _ClassVar[int]
    AGENT_LEFT_FIELD_NUMBER: _ClassVar[int]
    AGENT_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    SESSION_ENDED_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    TASK_REPORT_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_WINDOW_SAMPLE_FIELD_NUMBER: _ClassVar[int]
    GOLDFIVE_EVENT_FIELD_NUMBER: _ClassVar[int]
    REFINE_ATTEMPTED_FIELD_NUMBER: _ClassVar[int]
    REFINE_FAILED_FIELD_NUMBER: _ClassVar[int]
    session: _types_pb2.Session
    agent: _types_pb2.Agent
    initial_span: _types_pb2.Span
    initial_annotation: _types_pb2.Annotation
    burst_complete: InitialBurstComplete
    new_span: NewSpan
    updated_span: UpdatedSpan
    ended_span: EndedSpan
    new_annotation: NewAnnotation
    agent_joined: AgentJoined
    agent_left: AgentLeft
    agent_status_changed: AgentStatusChanged
    session_ended: SessionEnded
    payload_available: PayloadAvailable
    task_report: TaskReport
    context_window_sample: ContextWindowSample
    goldfive_event: _events_pb2.Event
    refine_attempted: _telemetry_pb2.RefineAttempted
    refine_failed: _telemetry_pb2.RefineFailed
    def __init__(self, session: _Optional[_Union[_types_pb2.Session, _Mapping]] = ..., agent: _Optional[_Union[_types_pb2.Agent, _Mapping]] = ..., initial_span: _Optional[_Union[_types_pb2.Span, _Mapping]] = ..., initial_annotation: _Optional[_Union[_types_pb2.Annotation, _Mapping]] = ..., burst_complete: _Optional[_Union[InitialBurstComplete, _Mapping]] = ..., new_span: _Optional[_Union[NewSpan, _Mapping]] = ..., updated_span: _Optional[_Union[UpdatedSpan, _Mapping]] = ..., ended_span: _Optional[_Union[EndedSpan, _Mapping]] = ..., new_annotation: _Optional[_Union[NewAnnotation, _Mapping]] = ..., agent_joined: _Optional[_Union[AgentJoined, _Mapping]] = ..., agent_left: _Optional[_Union[AgentLeft, _Mapping]] = ..., agent_status_changed: _Optional[_Union[AgentStatusChanged, _Mapping]] = ..., session_ended: _Optional[_Union[SessionEnded, _Mapping]] = ..., payload_available: _Optional[_Union[PayloadAvailable, _Mapping]] = ..., task_report: _Optional[_Union[TaskReport, _Mapping]] = ..., context_window_sample: _Optional[_Union[ContextWindowSample, _Mapping]] = ..., goldfive_event: _Optional[_Union[_events_pb2.Event, _Mapping]] = ..., refine_attempted: _Optional[_Union[_telemetry_pb2.RefineAttempted, _Mapping]] = ..., refine_failed: _Optional[_Union[_telemetry_pb2.RefineFailed, _Mapping]] = ...) -> None: ...

class ContextWindowSample(_message.Message):
    __slots__ = ("agent_id", "recorded_at", "tokens", "limit_tokens")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    RECORDED_AT_FIELD_NUMBER: _ClassVar[int]
    TOKENS_FIELD_NUMBER: _ClassVar[int]
    LIMIT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    recorded_at: _timestamp_pb2.Timestamp
    tokens: int
    limit_tokens: int
    def __init__(self, agent_id: _Optional[str] = ..., recorded_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., tokens: _Optional[int] = ..., limit_tokens: _Optional[int] = ...) -> None: ...

class InitialBurstComplete(_message.Message):
    __slots__ = ("spans_sent", "agents_sent")
    SPANS_SENT_FIELD_NUMBER: _ClassVar[int]
    AGENTS_SENT_FIELD_NUMBER: _ClassVar[int]
    spans_sent: int
    agents_sent: int
    def __init__(self, spans_sent: _Optional[int] = ..., agents_sent: _Optional[int] = ...) -> None: ...

class NewSpan(_message.Message):
    __slots__ = ("span",)
    SPAN_FIELD_NUMBER: _ClassVar[int]
    span: _types_pb2.Span
    def __init__(self, span: _Optional[_Union[_types_pb2.Span, _Mapping]] = ...) -> None: ...

class UpdatedSpan(_message.Message):
    __slots__ = ("span_id", "status", "attributes", "payload_refs")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _types_pb2.AttributeValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_types_pb2.AttributeValue, _Mapping]] = ...) -> None: ...
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REFS_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    status: _types_pb2.SpanStatus
    attributes: _containers.MessageMap[str, _types_pb2.AttributeValue]
    payload_refs: _containers.RepeatedCompositeFieldContainer[_types_pb2.PayloadRef]
    def __init__(self, span_id: _Optional[str] = ..., status: _Optional[_Union[_types_pb2.SpanStatus, str]] = ..., attributes: _Optional[_Mapping[str, _types_pb2.AttributeValue]] = ..., payload_refs: _Optional[_Iterable[_Union[_types_pb2.PayloadRef, _Mapping]]] = ...) -> None: ...

class EndedSpan(_message.Message):
    __slots__ = ("span_id", "end_time", "status", "error", "payload_refs")
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    END_TIME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REFS_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    end_time: _timestamp_pb2.Timestamp
    status: _types_pb2.SpanStatus
    error: _types_pb2.ErrorInfo
    payload_refs: _containers.RepeatedCompositeFieldContainer[_types_pb2.PayloadRef]
    def __init__(self, span_id: _Optional[str] = ..., end_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[_types_pb2.SpanStatus, str]] = ..., error: _Optional[_Union[_types_pb2.ErrorInfo, _Mapping]] = ..., payload_refs: _Optional[_Iterable[_Union[_types_pb2.PayloadRef, _Mapping]]] = ...) -> None: ...

class NewAnnotation(_message.Message):
    __slots__ = ("annotation",)
    ANNOTATION_FIELD_NUMBER: _ClassVar[int]
    annotation: _types_pb2.Annotation
    def __init__(self, annotation: _Optional[_Union[_types_pb2.Annotation, _Mapping]] = ...) -> None: ...

class AgentJoined(_message.Message):
    __slots__ = ("agent",)
    AGENT_FIELD_NUMBER: _ClassVar[int]
    agent: _types_pb2.Agent
    def __init__(self, agent: _Optional[_Union[_types_pb2.Agent, _Mapping]] = ...) -> None: ...

class AgentLeft(_message.Message):
    __slots__ = ("agent_id", "stream_id")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    stream_id: str
    def __init__(self, agent_id: _Optional[str] = ..., stream_id: _Optional[str] = ...) -> None: ...

class AgentStatusChanged(_message.Message):
    __slots__ = ("agent_id", "status", "buffered_events", "dropped_events", "current_activity", "stuck", "progress_counter")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    BUFFERED_EVENTS_FIELD_NUMBER: _ClassVar[int]
    DROPPED_EVENTS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    STUCK_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_COUNTER_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    status: _types_pb2.AgentStatus
    buffered_events: int
    dropped_events: int
    current_activity: str
    stuck: bool
    progress_counter: int
    def __init__(self, agent_id: _Optional[str] = ..., status: _Optional[_Union[_types_pb2.AgentStatus, str]] = ..., buffered_events: _Optional[int] = ..., dropped_events: _Optional[int] = ..., current_activity: _Optional[str] = ..., stuck: bool = ..., progress_counter: _Optional[int] = ...) -> None: ...

class SessionEnded(_message.Message):
    __slots__ = ("ended_at", "final_status")
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    FINAL_STATUS_FIELD_NUMBER: _ClassVar[int]
    ended_at: _timestamp_pb2.Timestamp
    final_status: _types_pb2.SessionStatus
    def __init__(self, ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., final_status: _Optional[_Union[_types_pb2.SessionStatus, str]] = ...) -> None: ...

class PayloadAvailable(_message.Message):
    __slots__ = ("digest",)
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    digest: str
    def __init__(self, digest: _Optional[str] = ...) -> None: ...

class TaskReport(_message.Message):
    __slots__ = ("agent_id", "report", "invocation_span_id", "recorded_at")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    REPORT_FIELD_NUMBER: _ClassVar[int]
    INVOCATION_SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    RECORDED_AT_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    report: str
    invocation_span_id: str
    recorded_at: _timestamp_pb2.Timestamp
    def __init__(self, agent_id: _Optional[str] = ..., report: _Optional[str] = ..., invocation_span_id: _Optional[str] = ..., recorded_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class GetPayloadRequest(_message.Message):
    __slots__ = ("digest", "summary_only")
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_ONLY_FIELD_NUMBER: _ClassVar[int]
    digest: str
    summary_only: bool
    def __init__(self, digest: _Optional[str] = ..., summary_only: bool = ...) -> None: ...

class PayloadChunk(_message.Message):
    __slots__ = ("digest", "total_size", "mime", "summary", "chunk", "last", "not_found")
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    TOTAL_SIZE_FIELD_NUMBER: _ClassVar[int]
    MIME_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    NOT_FOUND_FIELD_NUMBER: _ClassVar[int]
    digest: str
    total_size: int
    mime: str
    summary: str
    chunk: bytes
    last: bool
    not_found: bool
    def __init__(self, digest: _Optional[str] = ..., total_size: _Optional[int] = ..., mime: _Optional[str] = ..., summary: _Optional[str] = ..., chunk: _Optional[bytes] = ..., last: bool = ..., not_found: bool = ...) -> None: ...

class GetSpanTreeRequest(_message.Message):
    __slots__ = ("session_id", "agent_ids", "window_start", "window_end", "limit")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_IDS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_START_FIELD_NUMBER: _ClassVar[int]
    WINDOW_END_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    agent_ids: _containers.RepeatedScalarFieldContainer[str]
    window_start: _timestamp_pb2.Timestamp
    window_end: _timestamp_pb2.Timestamp
    limit: int
    def __init__(self, session_id: _Optional[str] = ..., agent_ids: _Optional[_Iterable[str]] = ..., window_start: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., window_end: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., limit: _Optional[int] = ...) -> None: ...

class GetSpanTreeResponse(_message.Message):
    __slots__ = ("spans", "truncated")
    SPANS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    spans: _containers.RepeatedCompositeFieldContainer[_types_pb2.Span]
    truncated: bool
    def __init__(self, spans: _Optional[_Iterable[_Union[_types_pb2.Span, _Mapping]]] = ..., truncated: bool = ...) -> None: ...

class PostAnnotationRequest(_message.Message):
    __slots__ = ("session_id", "target", "kind", "body", "author", "ack_timeout_ms")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    ACK_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    target: _types_pb2.AnnotationTarget
    kind: _types_pb2.AnnotationKind
    body: str
    author: str
    ack_timeout_ms: int
    def __init__(self, session_id: _Optional[str] = ..., target: _Optional[_Union[_types_pb2.AnnotationTarget, _Mapping]] = ..., kind: _Optional[_Union[_types_pb2.AnnotationKind, str]] = ..., body: _Optional[str] = ..., author: _Optional[str] = ..., ack_timeout_ms: _Optional[int] = ...) -> None: ...

class PostAnnotationResponse(_message.Message):
    __slots__ = ("annotation", "delivery", "delivery_detail")
    ANNOTATION_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_DETAIL_FIELD_NUMBER: _ClassVar[int]
    annotation: _types_pb2.Annotation
    delivery: _control_pb2.ControlAckResult
    delivery_detail: str
    def __init__(self, annotation: _Optional[_Union[_types_pb2.Annotation, _Mapping]] = ..., delivery: _Optional[_Union[_control_pb2.ControlAckResult, str]] = ..., delivery_detail: _Optional[str] = ...) -> None: ...

class SendControlRequest(_message.Message):
    __slots__ = ("session_id", "event", "ack_timeout_ms", "require_all_acks")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    ACK_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRE_ALL_ACKS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    event: _control_pb2.ControlEvent
    ack_timeout_ms: int
    require_all_acks: bool
    def __init__(self, session_id: _Optional[str] = ..., event: _Optional[_Union[_control_pb2.ControlEvent, _Mapping]] = ..., ack_timeout_ms: _Optional[int] = ..., require_all_acks: bool = ...) -> None: ...

class SendControlResponse(_message.Message):
    __slots__ = ("control_id", "result", "acks")
    CONTROL_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ACKS_FIELD_NUMBER: _ClassVar[int]
    control_id: str
    result: _control_pb2.ControlAckResult
    acks: _containers.RepeatedCompositeFieldContainer[StreamAck]
    def __init__(self, control_id: _Optional[str] = ..., result: _Optional[_Union[_control_pb2.ControlAckResult, str]] = ..., acks: _Optional[_Iterable[_Union[StreamAck, _Mapping]]] = ...) -> None: ...

class StreamAck(_message.Message):
    __slots__ = ("stream_id", "result", "detail", "acked_at")
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    ACKED_AT_FIELD_NUMBER: _ClassVar[int]
    stream_id: str
    result: _control_pb2.ControlAckResult
    detail: str
    acked_at: _timestamp_pb2.Timestamp
    def __init__(self, stream_id: _Optional[str] = ..., result: _Optional[_Union[_control_pb2.ControlAckResult, str]] = ..., detail: _Optional[str] = ..., acked_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class DeleteSessionRequest(_message.Message):
    __slots__ = ("session_id", "force")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    force: bool
    def __init__(self, session_id: _Optional[str] = ..., force: bool = ...) -> None: ...

class DeleteSessionResponse(_message.Message):
    __slots__ = ("deleted", "reason_if_not", "spans_removed", "annotations_removed", "payload_bytes_freed")
    DELETED_FIELD_NUMBER: _ClassVar[int]
    REASON_IF_NOT_FIELD_NUMBER: _ClassVar[int]
    SPANS_REMOVED_FIELD_NUMBER: _ClassVar[int]
    ANNOTATIONS_REMOVED_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_BYTES_FREED_FIELD_NUMBER: _ClassVar[int]
    deleted: bool
    reason_if_not: str
    spans_removed: int
    annotations_removed: int
    payload_bytes_freed: int
    def __init__(self, deleted: bool = ..., reason_if_not: _Optional[str] = ..., spans_removed: _Optional[int] = ..., annotations_removed: _Optional[int] = ..., payload_bytes_freed: _Optional[int] = ...) -> None: ...

class ListInterventionsRequest(_message.Message):
    __slots__ = ("session_id",)
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    def __init__(self, session_id: _Optional[str] = ...) -> None: ...

class ListInterventionsResponse(_message.Message):
    __slots__ = ("interventions",)
    INTERVENTIONS_FIELD_NUMBER: _ClassVar[int]
    interventions: _containers.RepeatedCompositeFieldContainer[_types_pb2.Intervention]
    def __init__(self, interventions: _Optional[_Iterable[_Union[_types_pb2.Intervention, _Mapping]]] = ...) -> None: ...

class GetStatsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetStatsResponse(_message.Message):
    __slots__ = ("session_count", "live_session_count", "agent_count", "span_count", "annotation_count", "payload_count", "payload_bytes", "disk_bytes", "data_dir", "active_telemetry_streams", "active_control_streams")
    SESSION_COUNT_FIELD_NUMBER: _ClassVar[int]
    LIVE_SESSION_COUNT_FIELD_NUMBER: _ClassVar[int]
    AGENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    SPAN_COUNT_FIELD_NUMBER: _ClassVar[int]
    ANNOTATION_COUNT_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_COUNT_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_BYTES_FIELD_NUMBER: _ClassVar[int]
    DISK_BYTES_FIELD_NUMBER: _ClassVar[int]
    DATA_DIR_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_TELEMETRY_STREAMS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CONTROL_STREAMS_FIELD_NUMBER: _ClassVar[int]
    session_count: int
    live_session_count: int
    agent_count: int
    span_count: int
    annotation_count: int
    payload_count: int
    payload_bytes: int
    disk_bytes: int
    data_dir: str
    active_telemetry_streams: int
    active_control_streams: int
    def __init__(self, session_count: _Optional[int] = ..., live_session_count: _Optional[int] = ..., agent_count: _Optional[int] = ..., span_count: _Optional[int] = ..., annotation_count: _Optional[int] = ..., payload_count: _Optional[int] = ..., payload_bytes: _Optional[int] = ..., disk_bytes: _Optional[int] = ..., data_dir: _Optional[str] = ..., active_telemetry_streams: _Optional[int] = ..., active_control_streams: _Optional[int] = ...) -> None: ...

class GetSessionPlanHistoryRequest(_message.Message):
    __slots__ = ("session_id",)
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    def __init__(self, session_id: _Optional[str] = ...) -> None: ...

class GetSessionPlanHistoryResponse(_message.Message):
    __slots__ = ("revisions",)
    REVISIONS_FIELD_NUMBER: _ClassVar[int]
    revisions: _containers.RepeatedCompositeFieldContainer[PlanRevision]
    def __init__(self, revisions: _Optional[_Iterable[_Union[PlanRevision, _Mapping]]] = ...) -> None: ...

class PlanRevision(_message.Message):
    __slots__ = ("plan", "revision_number", "revision_reason", "revision_kind", "revision_trigger_event_id", "emitted_at")
    PLAN_FIELD_NUMBER: _ClassVar[int]
    REVISION_NUMBER_FIELD_NUMBER: _ClassVar[int]
    REVISION_REASON_FIELD_NUMBER: _ClassVar[int]
    REVISION_KIND_FIELD_NUMBER: _ClassVar[int]
    REVISION_TRIGGER_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    EMITTED_AT_FIELD_NUMBER: _ClassVar[int]
    plan: _types_pb2_1.Plan
    revision_number: int
    revision_reason: str
    revision_kind: str
    revision_trigger_event_id: str
    emitted_at: _timestamp_pb2.Timestamp
    def __init__(self, plan: _Optional[_Union[_types_pb2_1.Plan, _Mapping]] = ..., revision_number: _Optional[int] = ..., revision_reason: _Optional[str] = ..., revision_kind: _Optional[str] = ..., revision_trigger_event_id: _Optional[str] = ..., emitted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
