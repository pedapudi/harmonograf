"""End-to-end ADK + harmonograf handshake — disabled during Phase C of
the goldfive migration (issue #4).

The original suite drove ``attach_adk`` + ``_harmonograf_session_id_for_adk``
to assert span + control telemetry round-trips through the server. Both
helpers were deleted when ``harmonograf_client.adk`` was stripped to a
thin :class:`HarmonografTelemetryPlugin` (issue #4, Phase C).

Coverage of the new wiring lives in:

* ``tests/e2e/test_goldfive_end_to_end.py`` — goldfive Runner +
  HarmonografSink happy-path ingest.
* ``tests/e2e/test_presentation_agent.py`` — ADK presentation demo on
  the new stack (mocked LLM).

Restoring the adk_hello scenarios (control ack round-trip, long-running
tool awaiting_human) against the new :class:`HarmonografTelemetryPlugin`
is Phase D cleanup work.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "Phase C of goldfive migration (issue #4) deleted attach_adk; "
    "rewire these scenarios against HarmonografTelemetryPlugin in Phase D.",
    allow_module_level=True,
)
