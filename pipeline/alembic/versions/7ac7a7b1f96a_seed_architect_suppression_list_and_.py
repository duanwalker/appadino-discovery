"""seed ARCHITECT suppression list and trigger_angles config

Revision ID: 7ac7a7b1f96a
Revises: 08e29df9a3e2
Create Date: 2026-09-16 15:45:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "7ac7a7b1f96a"
down_revision: Union[str, None] = "08e29df9a3e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# §4 Stage 4 — no EINs given for any of these; all name-only, matched fuzzily at
# Stage 4 and flagged (§9 rule 7), never silently dropped.
PAST_CURRENT_CLIENTS = [
    "Butterfly Dreamz",
    "Project OutPour",
    "Our Tribe Cincy",
    "Blue Bowtie Foundation",
    "Queen City Cocoa B.E.A.N.S.",
    "Common Cause",
    "WEMH",
    "She Dreams in Color",
    "LPCCD",
    "CCIP",
    "Clinton Hill Community Action",
    "CBAC",
]
ACTIVE_PROSPECTS = [
    "LBFE Cincinnati",
    "The Partnership Fund",
    "Spring Clean",
    "CCT Center for Community Transitions",
    "Sanford Institute/National University",
]

# §4 Stage 5: 3 of the 4 filing-derived triggers have a default angle; the 4th
# (first_filing_above_floor) is a genuine gap in what Lauren provided — left null
# rather than invented, per Duan's explicit instruction during G1.5.
TRIGGER_ANGLES = {
    "new_ed": "first-100-days",
    "dd_departure": "Fractional/Interim DD",
    "transformational_revenue_jump": "absorb-and-build",
    "first_filing_above_floor": None,
}


def upgrade() -> None:
    conn = op.get_bind()

    client_id = conn.execute(
        sa.text("SELECT id FROM clients WHERE name = 'ARCHITECT Philanthropic Collective'")
    ).scalar_one()

    suppression = sa.table(
        "suppression",
        sa.column("client_id", sa.Integer),
        sa.column("org_name", sa.String),
        sa.column("kind", sa.String),
        sa.column("source", sa.String),
        sa.column("added_at", sa.DateTime),
    )
    rows = [
        {"client_id": client_id, "org_name": name, "kind": "client", "source": "ARCHITECT seed data (brief §4 Stage 4)"}
        for name in PAST_CURRENT_CLIENTS
    ] + [
        {
            "client_id": client_id,
            "org_name": name,
            "kind": "active_prospect",
            "source": "ARCHITECT seed data (brief §4 Stage 4)",
        }
        for name in ACTIVE_PROSPECTS
    ]
    conn.execute(suppression.insert().values(added_at=sa.func.now()), rows)

    config = conn.execute(
        sa.text("SELECT config FROM icp_configs WHERE client_id = :client_id AND active = true"),
        {"client_id": client_id},
    ).scalar_one()
    config["trigger_angles"] = TRIGGER_ANGLES
    update_stmt = sa.text(
        "UPDATE icp_configs SET config = :config WHERE client_id = :client_id AND active = true"
    ).bindparams(sa.bindparam("config", type_=JSONB))
    conn.execute(update_stmt, {"config": config, "client_id": client_id})


def downgrade() -> None:
    conn = op.get_bind()
    client_id = conn.execute(
        sa.text("SELECT id FROM clients WHERE name = 'ARCHITECT Philanthropic Collective'")
    ).scalar_one()
    conn.execute(
        sa.text("DELETE FROM suppression WHERE client_id = :client_id AND source = 'ARCHITECT seed data (brief §4 Stage 4)'"),
        {"client_id": client_id},
    )
    conn.execute(
        sa.text("UPDATE icp_configs SET config = config - 'trigger_angles' WHERE client_id = :client_id AND active = true"),
        {"client_id": client_id},
    )
