"""seed ARCHITECT client and default icp_config

Revision ID: 1cded851bc4e
Revises: 4539971ffa35
Create Date: 2026-09-16 02:05:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1cded851bc4e"
down_revision: Union[str, None] = "4539971ffa35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# §4 Stage 1 recall filter defaults, written out explicitly here (rather than left to
# rely on discovery.stages.filter.DEFAULT_RECALL_FILTER) so they're visible and
# reviewable/tunable straight from the DB, per "config is data, not code" (§9).
# recall_filter mirrors the code defaults; the remaining keys are placeholders for
# gates that read them later (G1.4 scoring, G1.5 triggers) and are not yet consumed.
ARCHITECT_ICP_CONFIG = {
    "recall_filter": {
        "revenue_floor": 500_000,
        "revenue_ceiling": 10_000_000,
        "exclude_foundation_codes": ["00", "02", "03", "04", "12", "13", "14"],
        "exclude_ntee_prefixes": ["B4", "B5", "E2", "Y"],
        "require_filing_on_record": True,
    },
    "geography_tiers": {
        # Ranking weights (§4) — applied at scoring/ranking, not as a Stage 1 wall.
        "priority_metros": ["Charlotte", "Cincinnati"],
    },
    "govt_funding_heavy_pct": 0.40,  # §4 Stage 2 soft-flag threshold; read by G1.4
    "signal_weights": {},  # G1.4
    "alignment_keywords": [],  # G1.4
    "enrichment_enabled": False,
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
        .values(name="ARCHITECT Philanthropic Collective", status="active", created_at=sa.func.now())
        .returning(clients.c.id)
    )
    client_id = result.scalar_one()

    conn.execute(
        icp_configs.insert().values(
            client_id=client_id,
            version=1,
            config=ARCHITECT_ICP_CONFIG,
            active=True,
            created_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM icp_configs WHERE client_id IN "
            "(SELECT id FROM clients WHERE name = 'ARCHITECT Philanthropic Collective')"
        )
    )
    conn.execute(sa.text("DELETE FROM clients WHERE name = 'ARCHITECT Philanthropic Collective'"))
