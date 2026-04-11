"""Harmonograf Python client library.

Emits agent activity spans to a harmonograf server over a bidirectional
gRPC stream. Designed to be non-blocking: agent code never waits on the
network, disk, or a full queue.

Public surface (stable):
    Client              — top-level handle; owns buffer, identity, transport
    attach_adk          — one-liner integration for google.adk Runners
    SpanKind, SpanStatus, Capability — enum mirrors of the wire protocol
"""

from .enums import Capability, SpanKind, SpanStatus

__all__ = [
    "Capability",
    "SpanKind",
    "SpanStatus",
]

__version__ = "0.0.0"
