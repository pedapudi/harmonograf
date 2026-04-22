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
    log_format: str = "text"  # "text" | "json"
    grace_seconds: float = 5.0
    retention_hours: float = 0.0  # 0 disables retention sweeping
    retention_interval_seconds: float = 300.0
    metrics_interval_seconds: float = 30.0  # 0 disables periodic metrics
    auth_token: str = ""  # empty string disables shared-secret auth

    # Opt-in legacy time-window fallback for plan-revision attribution
    # (pre-strict-id-dedup behaviour from before harmonograf#99). Default
    # 0 disables the fallback — plan revisions without a trigger_event_id
    # become their own intervention card. Set to a positive ms value to
    # allow the aggregator to merge plan-revision rows onto annotations
    # / drifts within that wall-clock window when no strict id is
    # available. See docs/runbooks/plan-revision-dedup.md.
    legacy_plan_attribution_window_ms: float = 0.0

    # ---- heartbeat + payload (ingest.py) -----------------------------
    # Moved from module-level constants in harmonograf_server.ingest
    # (harmonograf#102). Operators tune ``heartbeat_timeout_seconds`` on
    # flaky networks and ``payload_max_bytes`` as a DoS guard / ops knob
    # for large-artifact deployments; the other two are rarely-touched
    # internal defaults and stay constructor-only.
    heartbeat_timeout_seconds: float = 15.0
    heartbeat_check_interval_seconds: float = 5.0
    stuck_threshold_beats: int = 3
    payload_max_bytes: int = 64 * 1024 * 1024

    # ---- RPC behavior (rpc/frontend.py) ------------------------------
    # Moved from module-level constants in harmonograf_server.rpc.frontend
    # (harmonograf#102). ``rpc_watch_window_seconds`` + ``rpc_span_tree_limit``
    # are exposed as CLI flags (operators with long-running sessions or
    # security-sensitive deployments may want to adjust); the chunk size
    # and per-control ack timeout are sensible defaults that stay
    # constructor-only.
    rpc_payload_chunk_bytes: int = 256 * 1024
    rpc_watch_window_seconds: float = 3600.0
    rpc_span_tree_limit: int = 10_000
    rpc_send_control_timeout_seconds: float = 5.0
