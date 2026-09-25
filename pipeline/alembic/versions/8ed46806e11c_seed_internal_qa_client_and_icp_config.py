"""seed internal QA client and permissive icp_config

Revision ID: 8ed46806e11c
Revises: 52cafccb5400
Create Date: 2026-09-24 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8ed46806e11c"
down_revision: Union[str, None] = "52cafccb5400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Second tenant, but not a customer: a scratch client for clicking through
# dashboard states (approve, enrich, suppression flags, etc.) end to end. Not
# ARCHITECT (client_id=2, no longer proceeding as a customer) and not the future
# Appadino dogfood tenant (G3.x, separate). Name is deliberately unmistakable
# for a real customer if someone lists clients later — `clients` has no slug
# column, so the "obviously scratch" marker lives in `name` itself.
QA_CLIENT_NAME = "INTERNAL QA — Appadino Discovery Pipeline (not a customer)"

# Thresholds mirror discovery.stages.filter.DEFAULT_RECALL_FILTER for the
# legal-form/data-validity gates (501(c)(3) public charity, filing on record) —
# loosening those would pull in odd test data, not more real prospects. Revenue
# and geography are the two knobs actually meant to be permissive here, per the
# G1.x brief: revenue span widened to ~admit anything, and priority_metros left
# empty since geography is a ranking weight, not a Stage 1 wall (see filter.py).
QA_ICP_CONFIG = {
    "recall_filter": {
        "revenue_floor": 0,
        "revenue_ceiling": 100_000_000,
        "exclude_foundation_codes": ["00", "02", "03", "04", "12", "13", "14"],
        "exclude_ntee_prefixes": ["B4", "B5", "E2", "Y"],
        "require_filing_on_record": True,
    },
    "geography_tiers": {
        "priority_metros": [],
    },
    "govt_funding_heavy_pct": 0.40,
    "signal_weights": {},
    "alignment_keywords": [],
    "enrichment_enabled": True,
    # TODO: ARCHITECT's (client_id=2) fullenrich_subaccount_id is itself unset
    # (None) — nothing exists yet to reuse. Set this once a real FullEnrich
    # sub-account is provisioned for this tenant. Until then, hard rule 8's
    # can_enrich() guard (discovery/stages/enrich.py) blocks enrichment calls
    # even with enrichment_enabled=True above, so this is safe to leave unset.
    "fullenrich_subaccount_id": None,
}


def upgrade() -> None:
    clients = sa.table(
        "clients",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    icp_configs = sa.table(
        "icp_configs",
        sa.column("id", sa.Integer),
        sa.column("client_id", sa.Integer),
        sa.column("version", sa.Integer),
        sa.column("config", postgresql.JSONB),
        sa.column("active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )

    conn = op.get_bind()
    result = conn.execute(
        clients.insert()
        .values(name=QA_CLIENT_NAME, status="active", created_at=sa.func.now())
        .returning(clients.c.id)
    )
    client_id = result.scalar_one()

    conn.execute(
        icp_configs.insert().values(
            client_id=client_id,
            version=1,
            config=QA_ICP_CONFIG,
            active=True,
            created_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM icp_configs WHERE client_id IN "
            "(SELECT id FROM clients WHERE name = :name)"
        ),
        {"name": QA_CLIENT_NAME},
    )
    conn.execute(sa.text("DELETE FROM clients WHERE name = :name"), {"name": QA_CLIENT_NAME})
