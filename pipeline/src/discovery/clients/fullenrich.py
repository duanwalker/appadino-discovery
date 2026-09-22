from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://app.fullenrich.com/api/v2"

# Confirmed against https://docs.fullenrich.com/api/v2/contact/enrich/bulk/get (2026-09-19).
TERMINAL_STATUSES = {"FINISHED", "CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT", "UNKNOWN"}


def get_credits(api_key: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """GET /account/credits — trial/plan balance check, called before spending."""
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(f"{BASE_URL}/account/credits", headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
    finally:
        if owns_client:
            client.close()


def start_bulk_enrichment(
    api_key: str,
    name: str,
    contacts: list[dict[str, Any]],
    client: httpx.Client | None = None,
) -> str:
    """POST /contact/enrich/bulk. Returns the enrichment_id.

    No webhook_url is passed: this is a one-off CLI script with no public endpoint to
    receive one, so the caller polls instead. FullEnrich's own docs call polling "not
    recommended" (vs. webhooks) — noted as an API-ergonomics finding for gate E1.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.post(
            f"{BASE_URL}/contact/enrich/bulk",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"name": name, "data": contacts},
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        enrichment_id: str = result["enrichment_id"]
        return enrichment_id
    finally:
        if owns_client:
            client.close()


def get_bulk_enrichment(
    api_key: str,
    enrichment_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /contact/enrich/bulk/{id} — a single, non-looping check. Callers that want
    to block until a terminal status use poll_bulk_enrichment (below); callers that
    want to bound their own wait (e.g. a request-scoped HTTP handler that must return
    before a gateway timeout) call this directly and inspect `status` themselves."""
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(
            f"{BASE_URL}/contact/enrich/bulk/{enrichment_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
    finally:
        if owns_client:
            client.close()


def poll_bulk_enrichment(
    api_key: str,
    enrichment_id: str,
    poll_interval_s: float = 10.0,
    timeout_s: float = 600.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /contact/enrich/bulk/{id} repeatedly until the batch reaches a terminal
    status (see TERMINAL_STATUSES), or raise TimeoutError past timeout_s."""
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            result = get_bulk_enrichment(api_key, enrichment_id, client=client)
            status = result.get("status")
            if status in TERMINAL_STATUSES:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"enrichment {enrichment_id} still '{status}' after {timeout_s}s")
            time.sleep(poll_interval_s)
    finally:
        if owns_client:
            client.close()
