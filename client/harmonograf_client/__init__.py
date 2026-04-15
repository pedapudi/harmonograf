"""Harmonograf Python client library.

Emits agent activity spans to a harmonograf server over a bidirectional
gRPC stream. Designed to be non-blocking: agent code never waits on the
network, disk, or a full queue.

Public surface (stable):
    Client              — top-level handle; owns buffer, identity, transport
    attach_adk          — one-liner integration for google.adk Runners
    SpanKind, SpanStatus, Capability — enum mirrors of the wire protocol
"""

from .adk import AdkAdapter, attach_adk, make_adk_plugin
from .agent import HarmonografAgent, make_harmonograf_agent
from .client import Client
from .enums import Capability, SpanKind, SpanStatus
from .planner import (
    LLMPlanner,
    PassthroughPlanner,
    Plan,
    PlannerHelper,
    Task,
    TaskEdge,
    make_default_adk_call_llm,
)
from .runner import HarmonografRunner, make_harmonograf_runner
from .transport import ControlAckSpec

__all__ = [
    "AdkAdapter",
    "Capability",
    "Client",
    "ControlAckSpec",
    "HarmonografAgent",
    "HarmonografRunner",
    "LLMPlanner",
    "PassthroughPlanner",
    "Plan",
    "PlannerHelper",
    "SpanKind",
    "SpanStatus",
    "Task",
    "TaskEdge",
    "attach_adk",
    "make_adk_plugin",
    "make_default_adk_call_llm",
    "make_harmonograf_agent",
    "make_harmonograf_runner",
]

__version__ = "0.0.0"
