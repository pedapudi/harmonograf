import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from harmonograf.v1 import types_pb2 as _types_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Hello(_message.Message):
    __slots__ = ("agent_id", "session_id", "name", "framework", "framework_version", "capabilities", "metadata", "resume_token", "session_title")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_VERSION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    RESUME_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    session_id: str
    name: str
    framework: _types_pb2.Framework
    framework_version: str
    capabilities: _containers.RepeatedScalarFieldContainer[_types_pb2.Capability]
    metadata: _containers.ScalarMap[str, str]
    resume_token: str
    session_title: str
    def __init__(self, agent_id: _Optional[str] = ..., session_id: _Optional[str] = ..., name: _Optional[str] = ..., framework: _Optional[_Union[_types_pb2.Framework, str]] = ..., framework_version: _Optional[str] = ..., capabilities: _Optional[_Iterable[_Union[_types_pb2.Capability, str]]] = ..., metadata: _Optional[_Mapping[str, str]] = ..., resume_token: _Optional[str] = ..., session_title: _Optional[str] = ...) -> None: ...

class SpanStart(_message.Message):
    __slots__ = ("span",)
    SPAN_FIELD_NUMBER: _ClassVar[int]
    span: _types_pb2.Span
    def __init__(self, span: _Optional[_Union[_types_pb2.Span, _Mapping]] = ...) -> None: ...

class SpanUpdate(_message.Message):
    __slots__ = ("span_id", "attributes", "status", "payload_refs")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _types_pb2.AttributeValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_types_pb2.AttributeValue, _Mapping]] = ...) -> None: ...
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REFS_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    attributes: _containers.MessageMap[str, _types_pb2.AttributeValue]
    status: _types_pb2.SpanStatus
    payload_refs: _containers.RepeatedCompositeFieldContainer[_types_pb2.PayloadRef]
    def __init__(self, span_id: _Optional[str] = ..., attributes: _Optional[_Mapping[str, _types_pb2.AttributeValue]] = ..., status: _Optional[_Union[_types_pb2.SpanStatus, str]] = ..., payload_refs: _Optional[_Iterable[_Union[_types_pb2.PayloadRef, _Mapping]]] = ...) -> None: ...

class SpanEnd(_message.Message):
    __slots__ = ("span_id", "end_time", "status", "error", "attributes", "payload_refs")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _types_pb2.AttributeValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_types_pb2.AttributeValue, _Mapping]] = ...) -> None: ...
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    END_TIME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REFS_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    end_time: _timestamp_pb2.Timestamp
    status: _types_pb2.SpanStatus
    error: _types_pb2.ErrorInfo
    attributes: _containers.MessageMap[str, _types_pb2.AttributeValue]
    payload_refs: _containers.RepeatedCompositeFieldContainer[_types_pb2.PayloadRef]
    def __init__(self, span_id: _Optional[str] = ..., end_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., status: _Optional[_Union[_types_pb2.SpanStatus, str]] = ..., error: _Optional[_Union[_types_pb2.ErrorInfo, _Mapping]] = ..., attributes: _Optional[_Mapping[str, _types_pb2.AttributeValue]] = ..., payload_refs: _Optional[_Iterable[_Union[_types_pb2.PayloadRef, _Mapping]]] = ...) -> None: ...

class PayloadUpload(_message.Message):
    __slots__ = ("digest", "total_size", "mime", "chunk", "last", "evicted")
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    TOTAL_SIZE_FIELD_NUMBER: _ClassVar[int]
    MIME_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    EVICTED_FIELD_NUMBER: _ClassVar[int]
    digest: str
    total_size: int
    mime: str
    chunk: bytes
    last: bool
    evicted: bool
    def __init__(self, digest: _Optional[str] = ..., total_size: _Optional[int] = ..., mime: _Optional[str] = ..., chunk: _Optional[bytes] = ..., last: bool = ..., evicted: bool = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("buffered_events", "dropped_events", "dropped_spans_critical", "buffered_payload_bytes", "payloads_evicted", "cpu_self_pct", "client_time", "progress_counter", "current_activity")
    BUFFERED_EVENTS_FIELD_NUMBER: _ClassVar[int]
    DROPPED_EVENTS_FIELD_NUMBER: _ClassVar[int]
    DROPPED_SPANS_CRITICAL_FIELD_NUMBER: _ClassVar[int]
    BUFFERED_PAYLOAD_BYTES_FIELD_NUMBER: _ClassVar[int]
    PAYLOADS_EVICTED_FIELD_NUMBER: _ClassVar[int]
    CPU_SELF_PCT_FIELD_NUMBER: _ClassVar[int]
    CLIENT_TIME_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_COUNTER_FIELD_NUMBER: _ClassVar[int]
    CURRENT_ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    buffered_events: int
    dropped_events: int
    dropped_spans_critical: int
    buffered_payload_bytes: int
    payloads_evicted: int
    cpu_self_pct: float
    client_time: _timestamp_pb2.Timestamp
    progress_counter: int
    current_activity: str
    def __init__(self, buffered_events: _Optional[int] = ..., dropped_events: _Optional[int] = ..., dropped_spans_critical: _Optional[int] = ..., buffered_payload_bytes: _Optional[int] = ..., payloads_evicted: _Optional[int] = ..., cpu_self_pct: _Optional[float] = ..., client_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., progress_counter: _Optional[int] = ..., current_activity: _Optional[str] = ...) -> None: ...

class Goodbye(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class TelemetryUp(_message.Message):
    __slots__ = ("hello", "span_start", "span_update", "span_end", "payload", "heartbeat", "control_ack", "goodbye")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    SPAN_START_FIELD_NUMBER: _ClassVar[int]
    SPAN_UPDATE_FIELD_NUMBER: _ClassVar[int]
    SPAN_END_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    CONTROL_ACK_FIELD_NUMBER: _ClassVar[int]
    GOODBYE_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    span_start: SpanStart
    span_update: SpanUpdate
    span_end: SpanEnd
    payload: PayloadUpload
    heartbeat: Heartbeat
    control_ack: _types_pb2.ControlAck
    goodbye: Goodbye
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., span_start: _Optional[_Union[SpanStart, _Mapping]] = ..., span_update: _Optional[_Union[SpanUpdate, _Mapping]] = ..., span_end: _Optional[_Union[SpanEnd, _Mapping]] = ..., payload: _Optional[_Union[PayloadUpload, _Mapping]] = ..., heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ..., control_ack: _Optional[_Union[_types_pb2.ControlAck, _Mapping]] = ..., goodbye: _Optional[_Union[Goodbye, _Mapping]] = ...) -> None: ...

class Welcome(_message.Message):
    __slots__ = ("accepted", "assigned_session_id", "assigned_stream_id", "server_time", "flags", "rejection_reason")
    class FlagsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIME_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    REJECTION_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    assigned_session_id: str
    assigned_stream_id: str
    server_time: _timestamp_pb2.Timestamp
    flags: _containers.ScalarMap[str, str]
    rejection_reason: str
    def __init__(self, accepted: bool = ..., assigned_session_id: _Optional[str] = ..., assigned_stream_id: _Optional[str] = ..., server_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., flags: _Optional[_Mapping[str, str]] = ..., rejection_reason: _Optional[str] = ...) -> None: ...

class PayloadRequest(_message.Message):
    __slots__ = ("digest",)
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    digest: str
    def __init__(self, digest: _Optional[str] = ...) -> None: ...

class FlowControl(_message.Message):
    __slots__ = ("action",)
    class Action(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        ACTION_UNSPECIFIED: _ClassVar[FlowControl.Action]
        ACTION_SLOW: _ClassVar[FlowControl.Action]
        ACTION_RESUME: _ClassVar[FlowControl.Action]
    ACTION_UNSPECIFIED: FlowControl.Action
    ACTION_SLOW: FlowControl.Action
    ACTION_RESUME: FlowControl.Action
    ACTION_FIELD_NUMBER: _ClassVar[int]
    action: FlowControl.Action
    def __init__(self, action: _Optional[_Union[FlowControl.Action, str]] = ...) -> None: ...

class ServerGoodbye(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class TelemetryDown(_message.Message):
    __slots__ = ("welcome", "payload_request", "flow_control", "server_goodbye")
    WELCOME_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REQUEST_FIELD_NUMBER: _ClassVar[int]
    FLOW_CONTROL_FIELD_NUMBER: _ClassVar[int]
    SERVER_GOODBYE_FIELD_NUMBER: _ClassVar[int]
    welcome: Welcome
    payload_request: PayloadRequest
    flow_control: FlowControl
    server_goodbye: ServerGoodbye
    def __init__(self, welcome: _Optional[_Union[Welcome, _Mapping]] = ..., payload_request: _Optional[_Union[PayloadRequest, _Mapping]] = ..., flow_control: _Optional[_Union[FlowControl, _Mapping]] = ..., server_goodbye: _Optional[_Union[ServerGoodbye, _Mapping]] = ...) -> None: ...
