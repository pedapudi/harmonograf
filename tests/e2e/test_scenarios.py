"""End-to-end scenarios — disabled during Phase A of the goldfive
migration (issue #2).

These tests drive the full pipeline through ``Client.submit_plan`` and
the ``TelemetryUp.task_plan`` / ``task_status_update`` variants, all
removed in Phase A. Plan and task state now travel inside
``TelemetryUp.goldfive_event``; Phase B/C rewires the server ingest and
the presentation_agent around a goldfive Runner + HarmonografSink and
restores these scenarios.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "Submit_plan wire path removed in Phase A of goldfive migration "
    "(issue #2); re-enable in Phase B/C against goldfive Runner "
    "+ HarmonografSink.",
    allow_module_level=True,
)
