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
from .client import Client
from .enums import Capability, SpanKind, SpanStatus
from .transport import ControlAckSpec

__all__ = [
    "AdkAdapter",
    "Capability",
    "Client",
    "ControlAckSpec",
    "SpanKind",
    "SpanStatus",
    "attach_adk",
    "make_adk_plugin",
]

__version__ = "0.0.0"
