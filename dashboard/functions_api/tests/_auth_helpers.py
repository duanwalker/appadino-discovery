"""Builds Static Web Apps login headers for tests (v1 item 5).

Unit tests that use fake DB connections get `customer_headers()` plus the conftest
stub of `function_app._lookup_client_id`, which maps TEST_EMAIL to TEST_CLIENT_ID.
The real-Postgres access-control tests opt out of that stub and use real
client_users rows instead.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import Any

TEST_PROVIDER = "aad"
TEST_EMAIL = "lauren@appadino.test"
TEST_CLIENT_ID = 2
DEFAULT_ROLES = ("anonymous", "authenticated", "customer")


def encode_principal(payload: Any) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def customer_headers(
    email: str = TEST_EMAIL,
    *,
    provider: str = TEST_PROVIDER,
    roles: Iterable[str] = DEFAULT_ROLES,
) -> dict[str, str]:
    principal = {
        "identityProvider": provider,
        "userId": f"user-{email}",
        "userDetails": email,
        "userRoles": list(roles),
    }
    return {"x-ms-client-principal": encode_principal(principal)}
