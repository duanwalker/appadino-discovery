from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"


def get_organization(ein: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Per-EIN detail/backfill source (§4 Stage 0) — free, no key required.

    Not called during bulk ingest (Stage 0 populates the national universe from the
    IRS BMF alone; calling this per-EIN for ~1M+ orgs isn't practical or necessary).
    Intended for opportunistic backfill of survivors with missing/stale BMF fields,
    starting once Stage 1 filtering (G1.3) narrows the set down.

    Returns None for an unknown EIN (404) rather than raising, since a missing
    ProPublica record is an expected, non-error outcome for a backfill lookup.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(BASE_URL.format(ein=ein))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
    finally:
        if owns_client:
            client.close()
