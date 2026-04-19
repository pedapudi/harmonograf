"""Shared pytest fixtures for harmonograf_client tests.

Intentionally minimal post-goldfive-migration (issue #4): the legacy
invariant-checker singleton that used to require per-test reset lived
in ``harmonograf_client.invariants``, which was deleted alongside the
orchestration layer.
"""
