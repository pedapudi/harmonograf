"""Tests for ``harmonograf_client.replay`` — goldfive events.jsonl replay.

The replay path turns a finished run's ``events.jsonl`` (one
``goldfive.v1.Event`` per line, as written by goldfive's recorder /
zicato) into a harmonograf session by feeding every event through the
same :class:`HarmonografSink` the live gRPC path uses.

These tests drive :func:`replay_events` directly against a
:class:`Client` backed by a :class:`FakeTransport`, so no server is
needed. They pin three invariants:

* both on-disk shapes — proto-JSON and dict envelope — are accepted;
* an event kind unknown to the pinned goldfive submodule is skipped,
  not fatal (forward-compat via ``ignore_unknown_fields``);
* a corrupt JSON line is skipped, not fatal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.replay import (
    ReplayStats,
    _is_dict_envelope,
    _run,
    build_parser,
    replay_events,
)
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


CLIENT_AGENT_ID = "zicato-replay-abc123"


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="zicato-replay",
        agent_id=CLIENT_AGENT_ID,
        session_id="run-xyz",
        framework="CUSTOM",
        buffer_size=256,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _write_jsonl(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- shape classification --------------------------------------------------


class TestIsDictEnvelope:
    def test_dict_envelope_recognised(self) -> None:
        assert _is_dict_envelope({"kind": "refine_attempted", "payload": {}})

    def test_proto_json_not_a_dict_envelope(self) -> None:
        # proto-JSON puts the payload oneof variant at the top level and
        # has no literal ``kind`` key.
        assert not _is_dict_envelope(
            {"runId": "r", "runStarted": {"runId": "r"}}
        )

    def test_kind_without_dict_payload_not_envelope(self) -> None:
        assert not _is_dict_envelope({"kind": "x", "payload": "not-a-dict"})


# ---- proto-JSON events -----------------------------------------------------


class TestProtoJsonReplay:
    @pytest.mark.asyncio
    async def test_run_started_proto_json_emitted(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        path = _write_jsonl(
            tmp_path,
            [
                json.dumps(
                    {
                        "eventId": "e0",
                        "runId": "run-xyz",
                        "sequence": "1",
                        "sessionId": "run-xyz",
                        "runStarted": {
                            "runId": "run-xyz",
                            "goalSummary": "do a thing",
                        },
                    }
                )
            ],
        )
        stats = await replay_events(path, sink)
        assert stats.proto_events == 1
        assert stats.dict_events == 0
        assert stats.emitted == 1

        env = _drain(client)[0]
        assert env.kind is EnvelopeKind.GOLDFIVE_EVENT
        assert env.payload.WhichOneof("payload") == "run_started"

    @pytest.mark.asyncio
    async def test_unknown_event_kind_skipped_not_fatal(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        """A line whose only payload field is unknown to the pinned
        goldfive submodule parses to an Event with no payload oneof and
        is counted as skipped — the replay continues."""
        path = _write_jsonl(
            tmp_path,
            [
                json.dumps(
                    {
                        "eventId": "e1",
                        "runId": "run-xyz",
                        "sequence": "2",
                        # A made-up future event kind. ignore_unknown_fields
                        # drops it; WhichOneof('payload') is then None.
                        "someFutureEventKind": {"foo": "bar"},
                    }
                ),
                json.dumps(
                    {
                        "eventId": "e2",
                        "runId": "run-xyz",
                        "sequence": "3",
                        "runCompleted": {"runId": "run-xyz"},
                    }
                ),
            ],
        )
        stats = await replay_events(path, sink)
        assert stats.skipped_empty_payload == 1
        assert stats.proto_events == 1  # run_completed still emitted

        env = _drain(client)[0]
        assert env.payload.WhichOneof("payload") == "run_completed"


# ---- dict-envelope events --------------------------------------------------


class TestDictEnvelopeReplay:
    @pytest.mark.asyncio
    async def test_refine_attempted_dict_envelope_emitted(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        path = _write_jsonl(
            tmp_path,
            [
                json.dumps(
                    {
                        "kind": "refine_attempted",
                        "run_id": "run-xyz",
                        "sequence": 7,
                        "session_id": "run-xyz",
                        "emitted_at": {"seconds": 1778889023, "nanos": 0},
                        "payload": {
                            "attempt_id": "att-1",
                            "drift_id": "drift-1",
                            "trigger_kind": "capability_mismatch",
                            "trigger_severity": "critical",
                            "current_task_id": "t-draft",
                            "current_agent_id": "reviewer_agent",
                        },
                    }
                )
            ],
        )
        stats = await replay_events(path, sink)
        assert stats.dict_events == 1
        assert stats.proto_events == 0

        env = _drain(client)[0]
        # The sink translates the dict envelope to a RefineAttempted proto.
        assert env.kind is EnvelopeKind.REFINE_ATTEMPTED
        assert env.payload.attempt_id == "att-1"
        # Agent id is canonicalized bare -> compound by the sink.
        assert env.payload.current_agent_id == f"{CLIENT_AGENT_ID}:reviewer_agent"


# ---- mixed + resilience ----------------------------------------------------


class TestMixedAndResilience:
    @pytest.mark.asyncio
    async def test_blank_and_corrupt_lines_skipped(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text(
            "\n".join(
                [
                    "",  # blank
                    "{not json",  # corrupt
                    "[1, 2, 3]",  # JSON but not an object
                    json.dumps(
                        {
                            "eventId": "e0",
                            "runId": "run-xyz",
                            "sequence": "1",
                            "runStarted": {"runId": "run-xyz"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        stats = await replay_events(path, sink)
        # Corrupt + non-object lines are skipped silently; only the valid
        # event is emitted.
        assert stats.proto_events == 1
        assert stats.emitted == 1

    @pytest.mark.asyncio
    async def test_json_object_that_is_not_a_goldfive_event_skipped(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        """A line that is a valid JSON object but not a parseable
        ``goldfive.v1.Event`` (a type mismatch ``ignore_unknown_fields``
        cannot paper over) raises ``json_format.ParseError`` — it must be
        counted as ``skipped_unparseable`` and the replay must continue.

        This is a distinct path from the corrupt-JSON skip (which never
        reaches the proto parser) and was previously untested."""
        path = _write_jsonl(
            tmp_path,
            [
                # ``sequence`` is a scalar field on Event; handing it a
                # nested object is a type error proto-JSON cannot parse.
                json.dumps(
                    {
                        "eventId": "e0",
                        "sequence": {"not": "a scalar"},
                        "runStarted": {"runId": "run-xyz"},
                    }
                ),
                json.dumps(
                    {
                        "eventId": "e1",
                        "runId": "run-xyz",
                        "sequence": "2",
                        "runCompleted": {"runId": "run-xyz"},
                    }
                ),
            ],
        )
        stats = await replay_events(path, sink)
        assert stats.skipped_unparseable == 1
        assert stats.proto_events == 1  # the well-formed event still lands
        assert stats.emitted == 1

        env = _drain(client)[0]
        assert env.payload.WhichOneof("payload") == "run_completed"

    @pytest.mark.asyncio
    async def test_mixed_proto_and_dict_in_one_file(
        self, tmp_path: Path, sink: HarmonografSink, client: Client
    ) -> None:
        path = _write_jsonl(
            tmp_path,
            [
                json.dumps(
                    {
                        "eventId": "e0",
                        "runId": "run-xyz",
                        "sequence": "1",
                        "runStarted": {"runId": "run-xyz"},
                    }
                ),
                json.dumps(
                    {
                        "kind": "refine_attempted",
                        "run_id": "run-xyz",
                        "sequence": 2,
                        "session_id": "run-xyz",
                        "payload": {"attempt_id": "att-1"},
                    }
                ),
                json.dumps(
                    {
                        "eventId": "e2",
                        "runId": "run-xyz",
                        "sequence": "3",
                        "runCompleted": {"runId": "run-xyz"},
                    }
                ),
            ],
        )
        stats = await replay_events(path, sink)
        assert stats.proto_events == 2
        assert stats.dict_events == 1
        assert stats.emitted == 3


# ---- _run exit codes -------------------------------------------------------


class TestRunExitCodes:
    """``_run`` is the CLI body; its exit codes are the contract a zicato
    hook (or an operator) keys on. These drive ``_run`` directly with a
    parsed args namespace. The error cases below never reach the network
    (missing / empty file) or point at a guaranteed-dead port, so no
    harmonograf server is needed."""

    @pytest.mark.asyncio
    async def test_missing_file_returns_2(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(
            [str(tmp_path / "does-not-exist.jsonl")]
        )
        assert await _run(args) == 2

    @pytest.mark.asyncio
    async def test_directory_argument_returns_2(self, tmp_path: Path) -> None:
        # A directory is not a file — ``path.is_file()`` is False, so the
        # CLI rejects it cleanly instead of raising IsADirectoryError on
        # open().
        args = build_parser().parse_args([str(tmp_path)])
        assert await _run(args) == 2

    @pytest.mark.asyncio
    async def test_empty_file_returns_1(self, tmp_path: Path) -> None:
        # Nothing is emitted, so nothing can be buffered/undelivered —
        # ``_run`` reaches the ``emitted == 0`` branch and returns 1
        # regardless of whether a server is reachable.
        path = tmp_path / "events.jsonl"
        path.write_text("", encoding="utf-8")
        args = build_parser().parse_args([str(path)])
        assert await _run(args) == 1

    @pytest.mark.asyncio
    async def test_blank_only_file_returns_1(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text("\n\n   \n\n", encoding="utf-8")
        args = build_parser().parse_args([str(path)])
        assert await _run(args) == 1

    @pytest.mark.asyncio
    async def test_undelivered_run_returns_3(self, tmp_path: Path) -> None:
        """Events that parse but never reach the server (here: a dead
        port) must exit 3, not 0 — a green exit on a dropped run would
        let a caller treat it as ingested."""
        path = _write_jsonl(
            tmp_path,
            [
                json.dumps(
                    {
                        "eventId": "e0",
                        "runId": "run-xyz",
                        "sequence": "1",
                        "sessionId": "run-xyz",
                        "runStarted": {"runId": "run-xyz"},
                    }
                )
            ],
        )
        # Port 1 is privileged and never has a harmonograf server; the
        # short flush timeout keeps the test fast.
        args = build_parser().parse_args(
            [str(path), "--server", "127.0.0.1:1", "--flush-timeout", "0.5"]
        )
        assert await _run(args) == 3


# ---- ReplayStats -----------------------------------------------------------


def test_replay_stats_str_is_operator_readable() -> None:
    stats = ReplayStats()
    stats.proto_events = 107
    stats.dict_events = 9
    stats.skipped_unparseable = 0
    stats.skipped_empty_payload = 13
    text = str(stats)
    assert "emitted=116" in text
    assert "proto=107" in text
    assert "dict=9" in text
    assert "empty_payload=13" in text
