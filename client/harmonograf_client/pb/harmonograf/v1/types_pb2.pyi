import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SESSION_STATUS_UNSPECIFIED: _ClassVar[SessionStatus]
    SESSION_STATUS_LIVE: _ClassVar[SessionStatus]
    SESSION_STATUS_COMPLETED: _ClassVar[SessionStatus]
    SESSION_STATUS_ABORTED: _ClassVar[SessionStatus]

class AgentStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AGENT_STATUS_UNSPECIFIED: _ClassVar[AgentStatus]
    AGENT_STATUS_CONNECTED: _ClassVar[AgentStatus]
    AGENT_STATUS_DISCONNECTED: _ClassVar[AgentStatus]
    AGENT_STATUS_CRASHED: _ClassVar[AgentStatus]

class Framework(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FRAMEWORK_UNSPECIFIED: _ClassVar[Framework]
    FRAMEWORK_CUSTOM: _ClassVar[Framework]
    FRAMEWORK_ADK: _ClassVar[Framework]

class Capability(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAPABILITY_UNSPECIFIED: _ClassVar[Capability]
    CAPABILITY_PAUSE_RESUME: _ClassVar[Capability]
    CAPABILITY_CANCEL: _ClassVar[Capability]
    CAPABILITY_REWIND: _ClassVar[Capability]
    CAPABILITY_STEERING: _ClassVar[Capability]
    CAPABILITY_HUMAN_IN_LOOP: _ClassVar[Capability]
    CAPABILITY_INTERCEPT_TRANSFER: _ClassVar[Capability]

class SpanKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPAN_KIND_UNSPECIFIED: _ClassVar[SpanKind]
    SPAN_KIND_INVOCATION: _ClassVar[SpanKind]
    SPAN_KIND_LLM_CALL: _ClassVar[SpanKind]
    SPAN_KIND_TOOL_CALL: _ClassVar[SpanKind]
    SPAN_KIND_USER_MESSAGE: _ClassVar[SpanKind]
    SPAN_KIND_AGENT_MESSAGE: _ClassVar[SpanKind]
    SPAN_KIND_TRANSFER: _ClassVar[SpanKind]
    SPAN_KIND_WAIT_FOR_HUMAN: _ClassVar[SpanKind]
    SPAN_KIND_PLANNED: _ClassVar[SpanKind]
    SPAN_KIND_CUSTOM: _ClassVar[SpanKind]

class SpanStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPAN_STATUS_UNSPECIFIED: _ClassVar[SpanStatus]
    SPAN_STATUS_PENDING: _ClassVar[SpanStatus]
    SPAN_STATUS_RUNNING: _ClassVar[SpanStatus]
    SPAN_STATUS_COMPLETED: _ClassVar[SpanStatus]
    SPAN_STATUS_FAILED: _ClassVar[SpanStatus]
    SPAN_STATUS_CANCELLED: _ClassVar[SpanStatus]
    SPAN_STATUS_AWAITING_HUMAN: _ClassVar[SpanStatus]

class LinkRelation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LINK_RELATION_UNSPECIFIED: _ClassVar[LinkRelation]
    LINK_RELATION_INVOKED: _ClassVar[LinkRelation]
    LINK_RELATION_WAITING_ON: _ClassVar[LinkRelation]
    LINK_RELATION_TRIGGERED_BY: _ClassVar[LinkRelation]
    LINK_RELATION_FOLLOWS: _ClassVar[LinkRelation]
    LINK_RELATION_REPLACES: _ClassVar[LinkRelation]

class AnnotationKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ANNOTATION_KIND_UNSPECIFIED: _ClassVar[AnnotationKind]
    ANNOTATION_KIND_COMMENT: _ClassVar[AnnotationKind]
    ANNOTATION_KIND_STEERING: _ClassVar[AnnotationKind]
    ANNOTATION_KIND_HUMAN_RESPONSE: _ClassVar[AnnotationKind]

class ControlKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONTROL_KIND_UNSPECIFIED: _ClassVar[ControlKind]
    CONTROL_KIND_PAUSE: _ClassVar[ControlKind]
    CONTROL_KIND_RESUME: _ClassVar[ControlKind]
    CONTROL_KIND_CANCEL: _ClassVar[ControlKind]
    CONTROL_KIND_REWIND_TO: _ClassVar[ControlKind]
    CONTROL_KIND_INJECT_MESSAGE: _ClassVar[ControlKind]
    CONTROL_KIND_APPROVE: _ClassVar[ControlKind]
    CONTROL_KIND_REJECT: _ClassVar[ControlKind]
    CONTROL_KIND_INTERCEPT_TRANSFER: _ClassVar[ControlKind]
    CONTROL_KIND_STEER: _ClassVar[ControlKind]
    CONTROL_KIND_STATUS_QUERY: _ClassVar[ControlKind]

class ControlAckResult(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONTROL_ACK_RESULT_UNSPECIFIED: _ClassVar[ControlAckResult]
    CONTROL_ACK_RESULT_SUCCESS: _ClassVar[ControlAckResult]
    CONTROL_ACK_RESULT_FAILURE: _ClassVar[ControlAckResult]
    CONTROL_ACK_RESULT_UNSUPPORTED: _ClassVar[ControlAckResult]
SESSION_STATUS_UNSPECIFIED: SessionStatus
SESSION_STATUS_LIVE: SessionStatus
SESSION_STATUS_COMPLETED: SessionStatus
SESSION_STATUS_ABORTED: SessionStatus
AGENT_STATUS_UNSPECIFIED: AgentStatus
AGENT_STATUS_CONNECTED: AgentStatus
AGENT_STATUS_DISCONNECTED: AgentStatus
AGENT_STATUS_CRASHED: AgentStatus
FRAMEWORK_UNSPECIFIED: Framework
FRAMEWORK_CUSTOM: Framework
FRAMEWORK_ADK: Framework
CAPABILITY_UNSPECIFIED: Capability
CAPABILITY_PAUSE_RESUME: Capability
CAPABILITY_CANCEL: Capability
CAPABILITY_REWIND: Capability
CAPABILITY_STEERING: Capability
CAPABILITY_HUMAN_IN_LOOP: Capability
CAPABILITY_INTERCEPT_TRANSFER: Capability
SPAN_KIND_UNSPECIFIED: SpanKind
SPAN_KIND_INVOCATION: SpanKind
SPAN_KIND_LLM_CALL: SpanKind
SPAN_KIND_TOOL_CALL: SpanKind
SPAN_KIND_USER_MESSAGE: SpanKind
SPAN_KIND_AGENT_MESSAGE: SpanKind
SPAN_KIND_TRANSFER: SpanKind
SPAN_KIND_WAIT_FOR_HUMAN: SpanKind
SPAN_KIND_PLANNED: SpanKind
SPAN_KIND_CUSTOM: SpanKind
SPAN_STATUS_UNSPECIFIED: SpanStatus
SPAN_STATUS_PENDING: SpanStatus
SPAN_STATUS_RUNNING: SpanStatus
SPAN_STATUS_COMPLETED: SpanStatus
SPAN_STATUS_FAILED: SpanStatus
SPAN_STATUS_CANCELLED: SpanStatus
SPAN_STATUS_AWAITING_HUMAN: SpanStatus
LINK_RELATION_UNSPECIFIED: LinkRelation
LINK_RELATION_INVOKED: LinkRelation
LINK_RELATION_WAITING_ON: LinkRelation
LINK_RELATION_TRIGGERED_BY: LinkRelation
LINK_RELATION_FOLLOWS: LinkRelation
LINK_RELATION_REPLACES: LinkRelation
ANNOTATION_KIND_UNSPECIFIED: AnnotationKind
ANNOTATION_KIND_COMMENT: AnnotationKind
ANNOTATION_KIND_STEERING: AnnotationKind
ANNOTATION_KIND_HUMAN_RESPONSE: AnnotationKind
CONTROL_KIND_UNSPECIFIED: ControlKind
CONTROL_KIND_PAUSE: ControlKind
CONTROL_KIND_RESUME: ControlKind
CONTROL_KIND_CANCEL: ControlKind
CONTROL_KIND_REWIND_TO: ControlKind
CONTROL_KIND_INJECT_MESSAGE: ControlKind
CONTROL_KIND_APPROVE: ControlKind
CONTROL_KIND_REJECT: ControlKind
CONTROL_KIND_INTERCEPT_TRANSFER: ControlKind
CONTROL_KIND_STEER: ControlKind
CONTROL_KIND_STATUS_QUERY: ControlKind
CONTROL_ACK_RESULT_UNSPECIFIED: ControlAckResult
CONTROL_ACK_RESULT_SUCCESS: ControlAckResult
CONTROL_ACK_RESULT_FAILURE: ControlAckResult
CONTROL_ACK_RESULT_UNSUPPORTED: ControlAckResult

class Session(_message.Message):
    __slots__ = ("id", "title", "created_at", "ended_at", "status", "agent_ids", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    AGENT_IDS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    created_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    status: SessionStatus
    agent_ids: _containers.RepeatedScalarFieldContainer[str]
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[SessionStatus, str]] = ..., agent_ids: _Optional[_Iterable[str]] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Agent(_message.Message):
    __slots__ = ("id", "session_id", "name", "framework", "framework_version", "capabilities", "metadata", "connected_at", "last_heartbeat", "status")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_VERSION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    name: str
    framework: Framework
    framework_version: str
    capabilities: _containers.RepeatedScalarFieldContainer[Capability]
    metadata: _containers.ScalarMap[str, str]
    connected_at: _timestamp_pb2.Timestamp
    last_heartbeat: _timestamp_pb2.Timestamp
    status: AgentStatus
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., name: _Optional[str] = ..., framework: _Optional[_Union[Framework, str]] = ..., framework_version: _Optional[str] = ..., capabilities: _Optional[_Iterable[_Union[Capability, str]]] = ..., metadata: _Optional[_Mapping[str, str]] = ..., connected_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., last_heartbeat: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[AgentStatus, str]] = ...) -> None: ...

class SpanLink(_message.Message):
    __slots__ = ("target_span_id", "target_agent_id", "relation")
    TARGET_SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    RELATION_FIELD_NUMBER: _ClassVar[int]
    target_span_id: str
    target_agent_id: str
    relation: LinkRelation
    def __init__(self, target_span_id: _Optional[str] = ..., target_agent_id: _Optional[str] = ..., relation: _Optional[_Union[LinkRelation, str]] = ...) -> None: ...

class AttributeValue(_message.Message):
    __slots__ = ("string_value", "int_value", "double_value", "bool_value", "bytes_value", "array_value")
    STRING_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT_VALUE_FIELD_NUMBER: _ClassVar[int]
    DOUBLE_VALUE_FIELD_NUMBER: _ClassVar[int]
    BOOL_VALUE_FIELD_NUMBER: _ClassVar[int]
    BYTES_VALUE_FIELD_NUMBER: _ClassVar[int]
    ARRAY_VALUE_FIELD_NUMBER: _ClassVar[int]
    string_value: str
    int_value: int
    double_value: float
    bool_value: bool
    bytes_value: bytes
    array_value: AttributeArray
    def __init__(self, string_value: _Optional[str] = ..., int_value: _Optional[int] = ..., double_value: _Optional[float] = ..., bool_value: bool = ..., bytes_value: _Optional[bytes] = ..., array_value: _Optional[_Union[AttributeArray, _Mapping]] = ...) -> None: ...

class AttributeArray(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedCompositeFieldContainer[AttributeValue]
    def __init__(self, values: _Optional[_Iterable[_Union[AttributeValue, _Mapping]]] = ...) -> None: ...

class PayloadRef(_message.Message):
    __slots__ = ("digest", "size", "mime", "summary", "role", "evicted")
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MIME_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    EVICTED_FIELD_NUMBER: _ClassVar[int]
    digest: str
    size: int
    mime: str
    summary: str
    role: str
    evicted: bool
    def __init__(self, digest: _Optional[str] = ..., size: _Optional[int] = ..., mime: _Optional[str] = ..., summary: _Optional[str] = ..., role: _Optional[str] = ..., evicted: bool = ...) -> None: ...

class ErrorInfo(_message.Message):
    __slots__ = ("type", "message", "stack")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    STACK_FIELD_NUMBER: _ClassVar[int]
    type: str
    message: str
    stack: str
    def __init__(self, type: _Optional[str] = ..., message: _Optional[str] = ..., stack: _Optional[str] = ...) -> None: ...

class Span(_message.Message):
    __slots__ = ("id", "session_id", "agent_id", "parent_span_id", "kind", "kind_string", "status", "name", "start_time", "end_time", "attributes", "payload_refs", "links", "error")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: AttributeValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[AttributeValue, _Mapping]] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    KIND_STRING_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    END_TIME_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REFS_FIELD_NUMBER: _ClassVar[int]
    LINKS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    agent_id: str
    parent_span_id: str
    kind: SpanKind
    kind_string: str
    status: SpanStatus
    name: str
    start_time: _timestamp_pb2.Timestamp
    end_time: _timestamp_pb2.Timestamp
    attributes: _containers.MessageMap[str, AttributeValue]
    payload_refs: _containers.RepeatedCompositeFieldContainer[PayloadRef]
    links: _containers.RepeatedCompositeFieldContainer[SpanLink]
    error: ErrorInfo
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., parent_span_id: _Optional[str] = ..., kind: _Optional[_Union[SpanKind, str]] = ..., kind_string: _Optional[str] = ..., status: _Optional[_Union[SpanStatus, str]] = ..., name: _Optional[str] = ..., start_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., end_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., attributes: _Optional[_Mapping[str, AttributeValue]] = ..., payload_refs: _Optional[_Iterable[_Union[PayloadRef, _Mapping]]] = ..., links: _Optional[_Iterable[_Union[SpanLink, _Mapping]]] = ..., error: _Optional[_Union[ErrorInfo, _Mapping]] = ...) -> None: ...

class AnnotationTarget(_message.Message):
    __slots__ = ("span_id", "agent_time")
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_TIME_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    agent_time: AgentTimePoint
    def __init__(self, span_id: _Optional[str] = ..., agent_time: _Optional[_Union[AgentTimePoint, _Mapping]] = ...) -> None: ...

class AgentTimePoint(_message.Message):
    __slots__ = ("agent_id", "at")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AT_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    at: _timestamp_pb2.Timestamp
    def __init__(self, agent_id: _Optional[str] = ..., at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class Annotation(_message.Message):
    __slots__ = ("id", "session_id", "target", "author", "created_at", "kind", "body", "delivered_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    DELIVERED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    target: AnnotationTarget
    author: str
    created_at: _timestamp_pb2.Timestamp
    kind: AnnotationKind
    body: str
    delivered_at: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., target: _Optional[_Union[AnnotationTarget, _Mapping]] = ..., author: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., kind: _Optional[_Union[AnnotationKind, str]] = ..., body: _Optional[str] = ..., delivered_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ControlTarget(_message.Message):
    __slots__ = ("agent_id", "span_id")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    span_id: str
    def __init__(self, agent_id: _Optional[str] = ..., span_id: _Optional[str] = ...) -> None: ...

class ControlEvent(_message.Message):
    __slots__ = ("id", "issued_at", "target", "kind", "payload")
    ID_FIELD_NUMBER: _ClassVar[int]
    ISSUED_AT_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    id: str
    issued_at: _timestamp_pb2.Timestamp
    target: ControlTarget
    kind: ControlKind
    payload: bytes
    def __init__(self, id: _Optional[str] = ..., issued_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., target: _Optional[_Union[ControlTarget, _Mapping]] = ..., kind: _Optional[_Union[ControlKind, str]] = ..., payload: _Optional[bytes] = ...) -> None: ...

class ControlAck(_message.Message):
    __slots__ = ("control_id", "result", "detail", "acked_at")
    CONTROL_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    ACKED_AT_FIELD_NUMBER: _ClassVar[int]
    control_id: str
    result: ControlAckResult
    detail: str
    acked_at: _timestamp_pb2.Timestamp
    def __init__(self, control_id: _Optional[str] = ..., result: _Optional[_Union[ControlAckResult, str]] = ..., detail: _Optional[str] = ..., acked_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
