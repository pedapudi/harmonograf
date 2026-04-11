"""Pure-Python mirrors of the wire protocol enums.

These exist so application code (and the ADK adapter) can reference
span kinds, statuses, and capabilities without importing generated
protobuf code. The generated pb enums are wired up in transport.py
and converted at the boundary.
"""

from __future__ import annotations

import enum


class SpanKind(enum.Enum):
    INVOCATION = "INVOCATION"
    LLM_CALL = "LLM_CALL"
    TOOL_CALL = "TOOL_CALL"
    USER_MESSAGE = "USER_MESSAGE"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    TRANSFER = "TRANSFER"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    PLANNED = "PLANNED"
    CUSTOM = "CUSTOM"


class SpanStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_HUMAN = "AWAITING_HUMAN"


class Capability(enum.Enum):
    PAUSE_RESUME = "PAUSE_RESUME"
    CANCEL = "CANCEL"
    REWIND = "REWIND"
    STEERING = "STEERING"
    HUMAN_IN_LOOP = "HUMAN_IN_LOOP"
    INTERCEPT_TRANSFER = "INTERCEPT_TRANSFER"
