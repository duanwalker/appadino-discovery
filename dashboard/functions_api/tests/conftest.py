"""Shared test fixtures for the Functions API suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _auth_helpers import TEST_CLIENT_ID, TEST_EMAIL, TEST_PROVIDER


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_client_lookup: use the real client_users lookup (real Postgres) instead of the "
        "conftest stub",
    )


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


@pytest.fixture(autouse=True)
def _stub_client_lookup(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route tests built on fake connections would otherwise feed the client_users
    lookup the fake's canned rows. The stub knows exactly one active user
    (TEST_PROVIDER, TEST_EMAIL) → TEST_CLIENT_ID; everyone else is unknown (403).
    Header parsing and the role check still run for real. Tests marked
    `real_client_lookup` (the real-Postgres access-control suite) get the real query."""
    if request.node.get_closest_marker("real_client_lookup"):
        return
    import function_app

    def lookup(identity_provider: str, email: str) -> int | None:
        if (identity_provider, email) == (TEST_PROVIDER, TEST_EMAIL):
            return TEST_CLIENT_ID
        return None

    monkeypatch.setattr(function_app, "_lookup_client_id", lookup)
