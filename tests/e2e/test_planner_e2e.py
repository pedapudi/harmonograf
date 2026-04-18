"""End-to-end planner pipeline — disabled during Phase A of the goldfive
migration (issue #2).

The wire-level ``Client.submit_plan`` / ``TelemetryUp.task_plan`` path
this suite exercised is gone; plan and task state now travel inside
``TelemetryUp.goldfive_event``. Phase B rewires the server's ingest
around that new dispatch and restores this suite against a goldfive
Runner + HarmonografSink.
"""
from __future__ import annotations

import pytest

pytest.skip(
    "Client.submit_plan removed in Phase A of goldfive migration (issue #2); "
    "restore in Phase B around goldfive Runner + HarmonografSink.",
    allow_module_level=True,
)
