from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

BASE_URL = "https://app.fullenrich.com/api/v2"
API_KEY_ENV = "FULLENRICH_API_KEY"

# Confirmed against https://docs.fullenrich.com/api/v2/contact/enrich/bulk/get (2026-09-19,
# re-checked 2026-10-29: non-terminal statuses are CREATED and IN_PROGRESS).
TERMINAL_STATUSES = {"FINISHED", "CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT", "UNKNOWN"}

# Every terminal status except FINISHED means FullEnrich gave up on the batch. Shown
# to the reviewer as "FullEnrich could not complete this lookup: <reason>".
FAILED_STATUS_REASONS: dict[str, str] = {
    "CANCELED": "the lookup was canceled",
    "CREDITS_INSUFFICIENT": "credits insufficient",
    "RATE_LIMIT": "rate limit reached",
    "UNKNOWN": "unknown error at FullEnrich",
}

# Documented 400 from GET /contact/enrich/bulk/{id} while the batch is still running
# ("Enrichment not ready, try again in 30 seconds") — a not-yet-done, not a failure.
IN_PROGRESS_ERROR_CODE = "error.enrichment.in_progress"

ERROR_BODY_LOG_LIMIT = 500

# "Bearer <token>" only. The bare word is fine: FullEnrich's documented 401 message
# itself says "do not have prefix 'bearer'", and that is worth keeping in logs.
_BEARER_TOKEN_RE = re.compile(r"\bbearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
REDACTED = "[REDACTED]"


def redact_credentials(text: str) -> str:
    """The one place log text from FullEnrich passes through: removes the API key
    value (when configured) and any "Bearer <token>", e.g. a provider error that
    echoes the request's Authorization header back."""
    api_key = os.environ.get(API_KEY_ENV)
    if api_key:
        text = text.replace(api_key, REDACTED)
    return _BEARER_TOKEN_RE.sub(f"Bearer {REDACTED}", text)


def error_code(response: httpx.Response) -> str | None:
    """The `code` from FullEnrich's documented error body ({"code", "message"}), or
    None when the body isn't that shape (e.g. the 402 result-shaped body)."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def error_body_excerpt(response: httpx.Response) -> str:
    """Response body only, redacted and then truncated for logs (redacting first, so
    a key cut in half at the limit can't leak its first half). Never log the
    request: its headers carry the API key."""
    return redact_credentials(response.text)[:ERROR_BODY_LOG_LIMIT]


def describe_failure(exc: Exception) -> str:
    """Log text for a failed call. Transport errors (timeouts, resets) and malformed
    JSON have no response, so never assume `exc.response` exists."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {error_body_excerpt(exc.response)}"
    return redact_credentials(f"{type(exc).__name__}: {exc}")[:ERROR_BODY_LOG_LIMIT]


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
