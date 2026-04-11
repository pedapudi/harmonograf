"""Generated proto stubs for the harmonograf server.

The files under harmonograf/v1/ are produced by `make proto-python` and
should not be edited by hand. protoc emits absolute imports of the form
`from harmonograf.v1 import types_pb2`; to make those resolve, we
insert this package's directory onto sys.path once on import.
"""
from __future__ import annotations

import os as _os
import sys as _sys

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
