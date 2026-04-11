"""Unit tests for identity persistence."""

from __future__ import annotations

import json

import pytest

from harmonograf_client import identity


def test_creates_then_reloads_same_id(tmp_path):
    ident1 = identity.load_or_create(
        "research-agent", framework="ADK", root=tmp_path
    )
    ident2 = identity.load_or_create(
        "research-agent", framework="ADK", root=tmp_path
    )
    assert ident1.agent_id == ident2.agent_id
    assert ident1.agent_id.startswith("research-agent-")


def test_stored_file_is_readable_json(tmp_path):
    identity.load_or_create("a1", framework="ADK", root=tmp_path)
    path = tmp_path / "agents" / "a1.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["name"] == "a1"
    assert data["framework"] == "ADK"
    assert "agent_id" in data


def test_framework_version_updates_are_persisted(tmp_path):
    identity.load_or_create("a1", framework="ADK", framework_version="0.1", root=tmp_path)
    ident2 = identity.load_or_create(
        "a1", framework="ADK", framework_version="0.2", root=tmp_path
    )
    assert ident2.framework_version == "0.2"
    reread = identity.load_or_create("a1", root=tmp_path)
    assert reread.framework_version == "0.2"


def test_metadata_is_merged_not_replaced(tmp_path):
    identity.load_or_create(
        "a1", framework="ADK", metadata={"team": "alpha"}, root=tmp_path
    )
    ident = identity.load_or_create(
        "a1", framework="ADK", metadata={"host": "box1"}, root=tmp_path
    )
    assert ident.metadata == {"team": "alpha", "host": "box1"}


def test_rejects_invalid_names(tmp_path):
    with pytest.raises(ValueError):
        identity.load_or_create("bad/name", root=tmp_path)
    with pytest.raises(ValueError):
        identity.load_or_create("", root=tmp_path)
    with pytest.raises(ValueError):
        identity.load_or_create("a" * 129, root=tmp_path)


def test_distinct_names_get_distinct_ids(tmp_path):
    a = identity.load_or_create("agent-one", root=tmp_path)
    b = identity.load_or_create("agent-two", root=tmp_path)
    assert a.agent_id != b.agent_id


def test_honors_harmonograf_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))
    ident = identity.load_or_create("envtest", framework="ADK")
    assert (tmp_path / "agents" / "envtest.json").exists()
    assert ident.name == "envtest"
