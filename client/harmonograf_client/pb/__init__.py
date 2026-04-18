"""Generated proto stubs for the harmonograf client library.

The files under harmonograf/v1/ are produced by `make proto-python` and
should not be edited by hand. protoc emits absolute imports of the form
`from harmonograf.v1 import types_pb2`; to make those resolve, we
insert this package's directory onto sys.path once on import.

The generated ``telemetry_pb2`` also imports ``goldfive.v1.events_pb2``.
That module lives inside the installed ``goldfive`` package under
``goldfive/pb/goldfive/v1/`` and is grafted onto the outer ``goldfive``
package's ``__path__`` by ``goldfive.pb``'s init. We trigger that import
here so ``from goldfive.v1 import events_pb2`` resolves when protoc's
generated code runs.
"""
from __future__ import annotations

import os as _os
import sys as _sys

import goldfive.pb as _goldfive_pb  # noqa: F401 — grafts goldfive.v1 onto goldfive

_pb_dir = _os.path.dirname(_os.path.abspath(__file__))
if _pb_dir not in _sys.path:
    _sys.path.insert(0, _pb_dir)

from harmonograf.v1 import (  # noqa: E402
    control_pb2,
    control_pb2_grpc,
    frontend_pb2,
    service_pb2,
    service_pb2_grpc,
    telemetry_pb2,
    telemetry_pb2_grpc,
    types_pb2,
)

__all__ = [
    "control_pb2",
    "control_pb2_grpc",
    "frontend_pb2",
    "service_pb2",
    "service_pb2_grpc",
    "telemetry_pb2",
    "telemetry_pb2_grpc",
    "types_pb2",
]
