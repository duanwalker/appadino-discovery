"""set placeholder fullenrich_subaccount_id for internal QA client

Revision ID: f1abb24b67b7
Revises: 8ed46806e11c
Create Date: 2026-09-25 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f1abb24b67b7"
down_revision: Union[str, None] = "8ed46806e11c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# NOT a real FullEnrich sub-account — deliberately fake, so it's obvious on sight if
# it ever leaked into a support ticket or log line. Confirmed safe: fullenrich_adapter.
# FullEnrichProvider stores subaccount_id but never puts it on the wire yet (no
# Sub-Account-Id header exists in FullEnrich's v2 API — see fullenrich_adapter.py's
# own comment and STATUS.md). So this value only satisfies can_enrich()'s "tenant has
# a subaccount configured" check (discovery/stages/enrich.py) for QA tenant
# clicksthrough; it cannot cause a real API call to hit the wrong sub-account.
# TODO: replace with a real sub-account id once Hugo provisions one for this tenant,
# or once the Sub-Account-Id header itself is wired up — whichever comes first.
PLACEHOLDER_SUBACCOUNT_ID = "INTERNAL-QA-PLACEHOLDER-NOT-REAL"

QA_CLIENT_NAME = "INTERNAL QA — Appadino Discovery Pipeline (not a customer)"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE icp_configs
            SET config = jsonb_set(config, '{fullenrich_subaccount_id}', to_jsonb(CAST(:subaccount_id AS text)))
            WHERE client_id = (SELECT id FROM clients WHERE name = :client_name)
              AND active = true
            """
        ),
        {"subaccount_id": PLACEHOLDER_SUBACCOUNT_ID, "client_name": QA_CLIENT_NAME},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE icp_configs
            SET config = jsonb_set(config, '{fullenrich_subaccount_id}', 'null'::jsonb)
            WHERE client_id = (SELECT id FROM clients WHERE name = :client_name)
              AND active = true
            """
        ),
        {"client_name": QA_CLIENT_NAME},
    )
