"""Agent identity persistence.

An agent's ``agent_id`` must survive restarts so the Gantt chart can
reclaim the same row when the process comes back. The id is chosen
by the client (§2.2), persisted under::

    ~/.harmonograf/agents/{name}.json

Files are keyed by the human-readable ``name`` the caller passes to
:func:`load_or_create`. The stored record also carries framework
metadata so a reconnect can reuse it even if the caller only hands
over the name.

The file format is deliberately tiny — one JSON object, human-readable,
rewritable by humans who want to hand-edit ids during debugging.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _default_root() -> Path:
    override = os.environ.get("HARMONOGRAF_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".harmonograf"


@dataclasses.dataclass
class AgentIdentity:
    agent_id: str
    name: str
    framework: str
    framework_version: str = ""
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> "AgentIdentity":
        return cls(
            agent_id=str(obj["agent_id"]),
            name=str(obj["name"]),
            framework=str(obj.get("framework", "CUSTOM")),
            framework_version=str(obj.get("framework_version", "")),
            metadata=dict(obj.get("metadata") or {}),
        )


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid agent name {name!r}: must match [a-zA-Z0-9_-]{{1,128}}"
        )


def identity_path(name: str, root: Path | None = None) -> Path:
    validate_name(name)
    base = root or _default_root()
    return base / "agents" / f"{name}.json"


def load_or_create(
    name: str,
    framework: str = "CUSTOM",
    framework_version: str = "",
    metadata: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> AgentIdentity:
    """Load the identity for ``name``, creating it on first use.

    On subsequent calls the stored ``agent_id`` is returned unchanged.
    ``framework`` / ``framework_version`` / ``metadata`` are refreshed
    on every call so version bumps of the agent binary propagate.
    """
    path = identity_path(name, root=root)
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        ident = AgentIdentity.from_json(data)
        dirty = False
        if framework and ident.framework != framework:
            ident.framework = framework
            dirty = True
        if framework_version and ident.framework_version != framework_version:
            ident.framework_version = framework_version
            dirty = True
        if metadata:
            merged = {**ident.metadata, **dict(metadata)}
            if merged != ident.metadata:
                ident.metadata = merged
                dirty = True
        if dirty:
            _write(path, ident)
        return ident

    ident = AgentIdentity(
        agent_id=f"{name}-{uuid.uuid4().hex[:12]}",
        name=name,
        framework=framework,
        framework_version=framework_version,
        metadata=dict(metadata or {}),
    )
    _write(path, ident)
    return ident


def _write(path: Path, ident: AgentIdentity) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(ident.to_json(), f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
