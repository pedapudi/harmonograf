"""Server configuration.

Kept as a simple dataclass so tests can construct a config by hand
without going through argparse.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    grpc_port: int = 7531
    web_port: int = 7532
    store_backend: str = "sqlite"  # "memory" | "sqlite"
    data_dir: str = "~/.harmonograf/data"
    log_level: str = "INFO"
    grace_seconds: float = 5.0
    retention_hours: float = 0.0  # 0 disables retention sweeping
    retention_interval_seconds: float = 300.0
