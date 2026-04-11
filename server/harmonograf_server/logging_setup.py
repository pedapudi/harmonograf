"""Logging setup — text (default) and JSON formatters.

JSON mode emits one record per line with a stable set of fields so that
log shippers can ingest it without a parser. Extra fields passed via
logger.info(..., extra={...}) are merged into the root object.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 6),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            try:
                json.dumps(v)
            except (TypeError, ValueError):
                v = repr(v)
            payload[k] = v
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: str, log_format: str) -> None:
    """Install a single stream handler on the root logger."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level))
