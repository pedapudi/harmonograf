from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class SubscribeControlRequest(_message.Message):
    __slots__ = ("session_id", "agent_id", "stream_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    agent_id: str
    stream_id: str
    def __init__(self, session_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., stream_id: _Optional[str] = ...) -> None: ...
