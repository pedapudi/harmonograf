"""Replay a goldfive ``events.jsonl`` file into a harmonograf server.

Harmonograf normally terminates a live goldfive event stream over gRPC
(see :class:`HarmonografSink`). But a finished run is also a *file*:
goldfive's recorder writes one ``goldfive.v1.Event`` per line to an
``events.jsonl`` next to the run. Tools that produce those files — most
notably zicato, which runs multi-agent systems under goldfive and keeps
each run's events under ``.zicato/epochs/.../runs/<run>/events.jsonl`` —
want to point an operator at the harmonograf timeline for one run
*after* it has completed.

This module is that bridge: it reads an ``events.jsonl`` from disk,
parses each line, and feeds the events through the *exact same*
:class:`HarmonografSink` the live path uses. No new wire surface, no new
server endpoint — replay is just a non-live producer. The session id is
preserved from the events themselves, so the run is addressable in the
harmonograf UI at ``/session/<run-id>`` and a dashboard can deep-link to
it (see ``docs/zicato-handoff.md``).

Two on-disk event shapes are accepted, matching what
:meth:`HarmonografSink.emit` already handles:

* **proto-JSON** — the protobuf JSON mapping of ``goldfive.v1.Event``
  (camelCase keys, payload oneof at top level). Parsed with
  ``json_format.Parse(..., ignore_unknown_fields=True)`` so an
  ``events.jsonl`` written by a *newer* goldfive than the harmonograf
  submodule is pinned to still replays — unknown event kinds are
  dropped, known ones render.
* **dict envelope** — a JSON object with top-level ``kind`` + ``payload``
  keys (snake_case). Goldfive emits a handful of operator-observability
  events this way (``refine_attempted``, ``refine_failed``, ...).
  Passed through to the sink verbatim; the sink owns the dict→proto
  translation.

Usage::

    harmonograf-replay path/to/events.jsonl
    harmonograf-replay --server 127.0.0.1:7531 --title "my run" run/events.jsonl

Then open the harmonograf frontend and the replayed session appears in
the picker, addressable by the run id the file carries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

from .client import Client
from .sink import HarmonografSink

logger = logging.getLogger(__name__)


class ReplayStats:
    """Tally of what a replay run did, for the operator-facing summary."""

    def __init__(self) -> None:
        self.proto_events = 0
        self.dict_events = 0
        self.skipped_unparseable = 0
        self.skipped_empty_payload = 0

    @property
    def emitted(self) -> int:
        return self.proto_events + self.dict_events

    def __str__(self) -> str:
        return (
            f"emitted={self.emitted} "
            f"(proto={self.proto_events}, dict={self.dict_events}) "
            f"skipped={self.skipped_unparseable + self.skipped_empty_payload} "
            f"(unparseable={self.skipped_unparseable}, "
            f"empty_payload={self.skipped_empty_payload})"
        )


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(line_number, parsed_object)`` for every non-blank line.

    Blank lines are skipped silently (a trailing newline is common).
    A line that is not valid JSON is logged at WARNING and skipped — a
    single corrupt line must not abort the replay of a long run.
    """
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("skipping line %d: invalid JSON (%s)", lineno, exc)
                continue
            if not isinstance(obj, dict):
                logger.warning("skipping line %d: not a JSON object", lineno)
                continue
            yield lineno, obj


def _is_dict_envelope(obj: dict[str, Any]) -> bool:
    """True when ``obj`` is a goldfive dict envelope (``kind`` + ``payload``).

    Distinct from the proto-JSON shape, where the payload oneof variant
    is itself a top-level camelCase key and there is no literal ``kind``
    field. :meth:`HarmonografSink.emit` routes the two shapes apart the
    same way — this mirrors that test so replay classifies before the
    sink does and can keep an accurate per-shape tally.
    """
    return (
        "kind" in obj
        and "payload" in obj
        and isinstance(obj.get("payload"), dict)
    )


async def replay_events(
    path: Path,
    sink: HarmonografSink,
    *,
    stats: ReplayStats | None = None,
) -> ReplayStats:
    """Parse ``path`` and feed every event through ``sink``.

    The events are emitted in file order — goldfive writes ``events.jsonl``
    in ``sequence`` order, so file order is replay order. The sink reuses
    the client's buffer + transport, so emission is non-blocking; the
    caller drains the buffer via :meth:`Client.shutdown`.
    """
    from goldfive.pb.goldfive.v1 import events_pb2 as goldfive_events_pb2
    from google.protobuf import json_format

    stats = stats or ReplayStats()
    for lineno, obj in _iter_jsonl(path):
        if _is_dict_envelope(obj):
            # Operator-observability dict envelope — the sink owns the
            # dict→proto translation (refine_attempted, refine_failed).
            await sink.emit(obj)
            stats.dict_events += 1
            continue

        event = goldfive_events_pb2.Event()
        try:
            # ignore_unknown_fields keeps a file written by a newer
            # goldfive than this submodule replayable: unknown event
            # kinds parse to an Event with no payload oneof set and are
            # dropped just below, known kinds still render.
            json_format.Parse(json.dumps(obj), event, ignore_unknown_fields=True)
        except json_format.ParseError as exc:
            logger.warning("skipping line %d: not a goldfive Event (%s)", lineno, exc)
            stats.skipped_unparseable += 1
            continue

        if event.WhichOneof("payload") is None:
            # Either a genuinely empty envelope or an event whose only
            # payload field was unknown to this goldfive submodule and
            # got dropped by ignore_unknown_fields. Nothing to render.
            stats.skipped_empty_payload += 1
            continue

        await sink.emit(event)
        stats.proto_events += 1

    return stats


async def _run(args: argparse.Namespace) -> int:
    path = Path(args.events_file).expanduser()
    if not path.is_file():
        logger.error("no such file: %s", path)
        return 2

    # Default the session title to the run directory name — for a zicato
    # run that is the run slug (e.g. ``transformers_lay_audience``), which
    # is far more useful in the session picker than the bare run id.
    title = args.title or f"replay: {path.parent.name}"

    client = Client(
        name=args.agent_name,
        framework="CUSTOM",
        server_addr=args.server,
        session_title=title,
        token=args.token or None,
    )
    sink = HarmonografSink(client)
    try:
        stats = await replay_events(path, sink)
    finally:
        await sink.close()
        # Drain the transport buffer before exit so the server has the
        # whole run before this process dies.
        client.shutdown(flush_timeout=args.flush_timeout)

    session_id = client.session_id or "(unassigned)"
    logger.info("replay complete: %s", stats)
    print(f"replayed {path}")
    print(f"  {stats}")
    print(f"  session: {session_id}")
    print(f"  open:    harmonograf frontend → /session/{session_id}")
    if stats.emitted == 0:
        logger.warning("no events emitted — is %s a goldfive events.jsonl?", path)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harmonograf-replay",
        description=(
            "Replay a goldfive events.jsonl file into a harmonograf server "
            "so a finished run renders as a session in the harmonograf UI."
        ),
    )
    p.add_argument(
        "events_file",
        help="path to a goldfive events.jsonl (one goldfive.v1.Event per line)",
    )
    p.add_argument(
        "--server",
        default="127.0.0.1:7531",
        help="harmonograf server gRPC address (default: 127.0.0.1:7531)",
    )
    p.add_argument(
        "--title",
        default="",
        help=(
            "session title shown in the picker; defaults to "
            "'replay: <run-dir-name>'"
        ),
    )
    p.add_argument(
        "--agent-name",
        default="zicato-replay",
        help=(
            "display name for the replay client's own agent row "
            "(default: zicato-replay)"
        ),
    )
    p.add_argument(
        "--token",
        default="",
        help="bearer token, if the target server was started with --auth-token",
    )
    p.add_argument(
        "--flush-timeout",
        type=float,
        default=15.0,
        help=(
            "seconds to wait for the transport buffer to drain before exit; "
            "raise for very large event files (default: 15.0)"
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
