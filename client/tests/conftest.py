import pytest

from harmonograf_client.invariants import reset_default_checker


@pytest.fixture(autouse=True)
def _reset_invariant_checker():
    # The monotonic-state invariant checker is a process-wide singleton
    # that tracks per-(hsession_id, task_id) status history across calls
    # so it can catch illegal transitions within a single run. Tests
    # routinely reuse ids like "hsess-inv-1" / "t1" across independent
    # scenarios, which would otherwise leak state and trip false
    # COMPLETED → FAILED / FAILED → RUNNING violations.
    reset_default_checker()
    yield
    reset_default_checker()
