"""Harmonograf Python client library.

Emits agent activity spans to a harmonograf server over a bidirectional
gRPC stream and ships goldfive orchestration events via
:class:`HarmonografSink`. Designed to be non-blocking: agent code never
waits on the network, disk, or a full queue.

Post-goldfive-migration (issue #4), the client is an observability-only
library. Plan-driven orchestration, drift detection, steering, and
reporting tools all live in ``goldfive``. Use a
:class:`goldfive.Runner` with a :class:`HarmonografSink` for telemetry
and (optionally) a :class:`HarmonografTelemetryPlugin` installed on the
ADK app for per-span observability::

    from goldfive import Runner, SequentialExecutor, LLMPlanner
    from goldfive.adapters.adk import ADKAdapter
    from harmonograf_client import Client, HarmonografSink

    client = Client(name="research", server_addr="127.0.0.1:7531")
    runner = Runner(
        agent=ADKAdapter(root_agent),
        planner=LLMPlanner(call_llm=...),
        executor=SequentialExecutor(),
        sinks=[HarmonografSink(client)],
    )
    await runner.run("user request")

Public surface (stable):
    Client                      — top-level handle; owns buffer, identity, transport
    HarmonografSink             — goldfive.EventSink adapter
    HarmonografTelemetryPlugin  — optional ADK plugin emitting lifecycle spans
    SpanKind, SpanStatus, Capability — enum mirrors of the wire protocol
    ControlAckSpec              — control-handler return type
"""

from __future__ import annotations

from .client import Client
from .config import ClientConfig
from .enums import Capability, SpanKind, SpanStatus
from .observe import observe
from .sink import HarmonografSink
from .telemetry_plugin import HarmonografTelemetryPlugin
from .transport import ControlAckSpec

__all__ = [
    "Capability",
    "Client",
    "ClientConfig",
    "ControlAckSpec",
    "HarmonografSink",
    "HarmonografTelemetryPlugin",
    "SpanKind",
    "SpanStatus",
    "observe",
]

__version__ = "0.0.0"
