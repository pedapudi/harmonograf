"""Unit tests for :class:`harmonograf_client.ClientConfig`.

harmonograf#105 replaced the two implicit env-var reads inside the
client (``HARMONOGRAF_SERVER`` / ``HARMONOGRAF_HOME``) with an
explicit :class:`ClientConfig` dataclass. These tests pin down:

- Default field values.
- The :meth:`ClientConfig.from_environ` factory: populates, falls
  through, ignores empty strings, expands ``~``.
- Integration with :func:`observe` — passing an explicit config routes
  the address through to :class:`Client`.
- Integration with identity — ``home_dir`` feeds the identity
  persistence path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harmonograf_client import ClientConfig, identity, observe
from harmonograf_client.client import Client
from harmonograf_client.config import DEFAULT_SERVER_ADDR

from tests._fixtures import FakeTransport, make_factory


class _FakeRunner:
    """Minimal stand-in for ``goldfive.Runner`` — same shape as
    ``tests/test_observe.py::_FakeRunner``, duplicated here to keep the
    config tests self-contained.
    """

    def __init__(self) -> None:
        self.sinks: list[Any] = []
        self._control: Any = None
        self._close_hooks: list[Any] = []

    def add_sink(self, sink: Any) -> None:
        self.sinks.append(sink)

    def add_close_hook(self, hook: Any) -> None:
        self._close_hooks.append(hook)

    @property
    def control(self) -> Any:
        return self._control

    @control.setter
    def control(self, value: Any) -> None:
        self._control = value

    async def close(self) -> None:
        for hook in self._close_hooks:
            await hook()


# ---------------------------------------------------------------------
# Dataclass defaults + from_environ factory
# ---------------------------------------------------------------------


def test_default_config_values() -> None:
    cfg = ClientConfig()
    assert cfg.server_addr == "127.0.0.1:7531"
    assert cfg.server_addr == DEFAULT_SERVER_ADDR
    assert cfg.home_dir is None


def test_from_environ_reads_both_vars() -> None:
    cfg = ClientConfig.from_environ(
        {"HARMONOGRAF_SERVER": "other:9000", "HARMONOGRAF_HOME": "/tmp/x"}
    )
    assert cfg.server_addr == "other:9000"
    assert cfg.home_dir == Path("/tmp/x")


def test_from_environ_falls_through_when_vars_unset() -> None:
    cfg = ClientConfig.from_environ({})
    assert cfg.server_addr == DEFAULT_SERVER_ADDR
    assert cfg.home_dir is None


def test_from_environ_ignores_empty_strings() -> None:
    cfg = ClientConfig.from_environ(
        {"HARMONOGRAF_SERVER": "", "HARMONOGRAF_HOME": ""}
    )
    assert cfg.server_addr == DEFAULT_SERVER_ADDR
    assert cfg.home_dir is None


def test_from_environ_partial_population() -> None:
    cfg = ClientConfig.from_environ({"HARMONOGRAF_SERVER": "only-addr:1"})
    assert cfg.server_addr == "only-addr:1"
    assert cfg.home_dir is None

    cfg2 = ClientConfig.from_environ({"HARMONOGRAF_HOME": "/tmp/only-home"})
    assert cfg2.server_addr == DEFAULT_SERVER_ADDR
    assert cfg2.home_dir == Path("/tmp/only-home")


def test_from_environ_expands_tilde_in_home_dir(monkeypatch) -> None:
    # Route HOME so ``expanduser`` is deterministic under test.
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    cfg = ClientConfig.from_environ({"HARMONOGRAF_HOME": "~/.harmonograf"})
    assert cfg.home_dir == Path("/tmp/fake-home/.harmonograf")


def test_from_environ_defaults_to_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit mapping, ``from_environ`` reads ``os.environ``."""
    monkeypatch.setenv("HARMONOGRAF_SERVER", "live.env:4242")
    monkeypatch.delenv("HARMONOGRAF_HOME", raising=False)
    cfg = ClientConfig.from_environ()
    assert cfg.server_addr == "live.env:4242"
    assert cfg.home_dir is None


# ---------------------------------------------------------------------
# observe() integration
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_accepts_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing a :class:`ClientConfig` with a custom ``server_addr``
    must route that address through to the constructed :class:`Client`.
    """
    captured: dict[str, Any] = {}
    original_init = Client.__init__

    def spy_init(self: Client, **kwargs: Any) -> None:
        captured.update(kwargs)
        kwargs["_transport_factory"] = make_factory([])
        original_init(self, **kwargs)

    monkeypatch.setattr(Client, "__init__", spy_init)
    monkeypatch.delenv("HARMONOGRAF_SERVER", raising=False)

    runner = _FakeRunner()
    observe(runner, name="cfg", config=ClientConfig(server_addr="explicit:1111"))

    assert captured["server_addr"] == "explicit:1111"
    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_no_config_and_no_env_uses_client_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config, no env, no server_addr => Client's own default applies
    (server_addr kwarg omitted from Client.__init__).
    """
    captured: dict[str, Any] = {}
    original_init = Client.__init__

    def spy_init(self: Client, **kwargs: Any) -> None:
        captured.update(kwargs)
        kwargs["_transport_factory"] = make_factory([])
        original_init(self, **kwargs)

    monkeypatch.setattr(Client, "__init__", spy_init)
    # Explicitly set the env to prove observe() does not read it.
    monkeypatch.setenv("HARMONOGRAF_SERVER", "should.be.ignored:0")

    runner = _FakeRunner()
    observe(runner, name="default-cfg")

    assert "server_addr" not in captured
    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


@pytest.mark.asyncio
async def test_observe_config_home_dir_feeds_identity_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config.home_dir`` must become the Client's identity root.

    Verified end-to-end: after ``observe`` returns, the on-disk identity
    record lives under ``config.home_dir/agents/<name>.json`` and
    nowhere else.
    """
    captured: dict[str, Any] = {}
    original_init = Client.__init__

    def spy_init(self: Client, **kwargs: Any) -> None:
        captured.update(kwargs)
        kwargs["_transport_factory"] = make_factory([])
        original_init(self, **kwargs)

    monkeypatch.setattr(Client, "__init__", spy_init)

    runner = _FakeRunner()
    cfg = ClientConfig(server_addr="x:1", home_dir=tmp_path)
    observe(runner, name="home-agent", config=cfg)

    assert captured["identity_root"] == str(tmp_path)
    assert (tmp_path / "agents" / "home-agent.json").exists()

    await runner.close()
    runner.sinks[0]._client.shutdown(flush_timeout=0.1)


# ---------------------------------------------------------------------
# identity integration
# ---------------------------------------------------------------------


def test_identity_respects_config_home_dir(tmp_path: Path) -> None:
    """:func:`identity.load_or_create` accepts an explicit ``root``;
    ``ClientConfig.home_dir`` is the canonical source for that path.
    """
    cfg = ClientConfig(home_dir=tmp_path)
    ident = identity.load_or_create(
        "cfg-identity", framework="ADK", root=cfg.home_dir
    )
    assert ident.name == "cfg-identity"
    assert (tmp_path / "agents" / "cfg-identity.json").exists()


def test_identity_default_root_ignores_harmonograf_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``identity._default_root`` no longer consults
    ``HARMONOGRAF_HOME`` — the env read moved onto
    :meth:`ClientConfig.from_environ`.
    """
    monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))
    assert identity._default_root() == Path.home() / ".harmonograf"


def test_from_environ_round_trips_through_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy env-driven callers can migrate by piping
    ``ClientConfig.from_environ().home_dir`` into ``load_or_create``.
    """
    monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))
    cfg = ClientConfig.from_environ()
    assert cfg.home_dir == tmp_path
    ident = identity.load_or_create(
        "roundtrip", framework="ADK", root=cfg.home_dir
    )
    assert (tmp_path / "agents" / "roundtrip.json").exists()
    assert ident.name == "roundtrip"
