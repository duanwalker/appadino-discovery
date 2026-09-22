"""Shared test fixtures for the Functions API suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_pending_jobs_cache() -> Iterator[None]:
    """`function_app._PENDING_JOBS` is process-global by design (a same-instance
    fast path for the E2 double-submit guard, see function_app.py) — without this,
    a job left "pending" by one test (e.g. test_pending_after_budget_returns_202...)
    would leak into the next test using the same (client_id, ein) and silently
    short-circuit it before it ever reaches the DB/provider mocks that test sets up.
    Copilot's own double-click test relies on the cache persisting *within* a single
    test's two sequential calls, which this fixture doesn't affect — it only resets
    the cache *between* tests."""
    import function_app

    function_app._PENDING_JOBS.clear()
    yield
    function_app._PENDING_JOBS.clear()
