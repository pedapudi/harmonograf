"""Tests for the frontend RPC surfacing of UserMessageReceived events
(harmonograf user-message UX gap).

Covers the translation of ``DELTA_USER_MESSAGE`` bus deltas into the
``SessionUpdate.user_message`` oneof variant on WatchSession.
"""

from __future__ import annotations

from harmonograf_server.bus import DELTA_USER_MESSAGE, Delta
from harmonograf_server.rpc.frontend import _delta_to_session_update


def _user_message_payload(
    *,
    run_id: str = "run-1",
    sequence: int = 7,
    content: str = "forget solar panels. tell me about solar flares.",
    author: str = "alice",
    mid_turn: bool = False,
    invocation_id: str = "",
    emitted_at: float | None = 1_700_000_000.25,
    recorded_at: float | None = 1_000_000.0,
) -> dict:
    return {
        "run_id": run_id,
        "sequence": sequence,
        "content": content,
        "author": author,
        "mid_turn": mid_turn,
        "invocation_id": invocation_id,
        "emitted_at": emitted_at,
        "recorded_at": recorded_at,
    }


def test_user_message_delta_translates_to_session_update():
    """A DELTA_USER_MESSAGE bus delta translates into the
    ``SessionUpdate.user_message`` oneof variant carrying every
    payload field plus the session_id."""
    delta = Delta(
        session_id="sess_um",
        kind=DELTA_USER_MESSAGE,
        payload=_user_message_payload(),
    )
    update = _delta_to_session_update(delta)
    assert update is not None
    assert update.WhichOneof("kind") == "user_message"
    msg = update.user_message
    assert msg.session_id == "sess_um"
    assert msg.run_id == "run-1"
    assert msg.sequence == 7
    assert msg.content == "forget solar panels. tell me about solar flares."
    assert msg.author == "alice"
    assert msg.mid_turn is False
    assert msg.invocation_id == ""
    assert msg.emitted_at.seconds == 1_700_000_000
    assert msg.emitted_at.nanos == 250_000_000


def test_mid_turn_message_carries_invocation_id():
    delta = Delta(
        session_id="sess_um",
        kind=DELTA_USER_MESSAGE,
        payload=_user_message_payload(
            mid_turn=True,
            invocation_id="inv-9",
            content="interject!",
        ),
    )
    update = _delta_to_session_update(delta)
    assert update.user_message.mid_turn is True
    assert update.user_message.invocation_id == "inv-9"


def test_emitted_at_falls_back_to_recorded_at():
    """When the goldfive-side emitted_at is missing the ingest-side
    recorded_at is stamped onto the wire so the frontend always has
    a timestamp to render the marker against."""
    delta = Delta(
        session_id="sess_um",
        kind=DELTA_USER_MESSAGE,
        payload=_user_message_payload(emitted_at=None, recorded_at=99.5),
    )
    update = _delta_to_session_update(delta)
    msg = update.user_message
    assert msg.emitted_at.seconds == 99
    assert msg.emitted_at.nanos == 500_000_000
