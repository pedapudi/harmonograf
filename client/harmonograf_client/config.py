"""Client-side configuration.

Mirrors the shape of server :class:`ServerConfig`: a plain dataclass
constructed explicitly by callers (or via helpers that map from
argparse / environment variables). Keeps identity and connection
parameters off the ambient process environment so tests and embedded
usages don't need to monkeypatch ``os.environ``.

Historically two env vars sneaked into the client:

- ``HARMONOGRAF_SERVER`` — transport server address, read by
  :func:`harmonograf_client.observe`.
- ``HARMONOGRAF_HOME`` — identity root directory, read by
  :func:`harmonograf_client.identity._default_root`.

Both are now surfaced as explicit :class:`ClientConfig` fields.
Callers that still want the legacy env-driven behaviour can opt in via
:meth:`ClientConfig.from_environ`; everything else should pass the
config explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


# The client's default transport target. Matches the server's default
# gRPC listener (:class:`ServerConfig.grpc_port`). Exposed as a module
# constant so tests and callers can reference it instead of hard-coding
# the string.
DEFAULT_SERVER_ADDR: str = "127.0.0.1:7531"


@dataclass
class ClientConfig:
    """Explicit client configuration.

    Attributes
    ----------
    server_addr:
        Address of the harmonograf server the client connects to, in
        ``host:port`` form (no scheme). Defaults to
        :data:`DEFAULT_SERVER_ADDR` (``"127.0.0.1:7531"``) which matches
        the local-dev server.
    home_dir:
        Local identity root — parent of the ``agents/`` directory that
        stores persisted :class:`AgentIdentity` records. ``None`` means
        use the platform default (``~/.harmonograf``); callers with
        test isolation or multi-tenant needs pass an explicit
        :class:`Path`.

    Notes
    -----
    There's room to grow this dataclass (TLS options, bearer-token
    mirror of :class:`ServerConfig.auth_token`, default heartbeat
    interval, etc.). Keep the surface lean for now — adding a field
    later is a non-breaking change, renaming one isn't.
    """

    server_addr: str = DEFAULT_SERVER_ADDR
    home_dir: Optional[Path] = None

    @classmethod
    def from_environ(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> "ClientConfig":
        """Factory that reads the legacy ``HARMONOGRAF_*`` env vars.

        Intended for backwards compatibility only: callers that have
        been setting ``HARMONOGRAF_SERVER`` / ``HARMONOGRAF_HOME`` in
        their shell / systemd unit / CI workflow can migrate by
        replacing::

            # old — implicit env read inside observe() / identity
            observe(runner, name="agent")

        with::

            # new — explicit, zero ambient-env magic
            from harmonograf_client import ClientConfig, observe
            observe(runner, name="agent", config=ClientConfig.from_environ())

        New code should construct :class:`ClientConfig` directly with
        the values it has — ``ClientConfig(server_addr="...", ...)``.

        Parameters
        ----------
        env:
            Mapping to read variables from. Defaults to ``os.environ``
            (read lazily here, not at import time, so the helper sees
            runtime env updates). Pass an explicit dict in tests.

        Returns
        -------
        A :class:`ClientConfig` with fields populated from the provided
        environment, falling back to the dataclass defaults for any
        var that's unset or empty.
        """
        import os

        e: Mapping[str, str] = env if env is not None else os.environ
        raw_addr = e.get("HARMONOGRAF_SERVER") or ""
        raw_home = e.get("HARMONOGRAF_HOME") or ""
        return cls(
            server_addr=raw_addr or cls.server_addr,
            home_dir=Path(raw_home).expanduser() if raw_home else None,
        )
