"""E1: enrichments table

Revision ID: b7d4f1a9c3e6
Revises: 4a2e9c1f7b3d
Create Date: 2026-09-19 00:00:00.000000

Pulls forward the §5.5 `enrichments` table schema ahead of the E2 gate decision,
specifically to support pipeline/scripts/e1_fullenrich_spike.py — a standalone,
manually-invoked validation script (never wired into discovery.cli or the `runs`
table) that enriches ~20-25 status='approved' prospects via FullEnrich and records
the go/no-go metrics for gate E1 (see STATUS.md). If E1 doesn't clear the adoption
threshold, this table just sits unused until reconsidered; no downstream code reads
from it yet (the dashboard's contact columns are a Phase 2 concern).

email_status is intentionally coarser than FullEnrich's own v2 API status enum
(DELIVERABLE, HIGH_PROBABILITY, CATCH_ALL, INVALID, INVALID_DOMAIN) — the brief's
§5.5 sketch only names three buckets (verified|catch_all|not_found), so the spike
script maps FullEnrich's five-way enum down to those three. See that script's
docstring for the exact mapping and why it's flagged as a judgment call.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7d4f1a9c3e6"
down_revision: Union[str, None] = "4a2e9c1f7b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "enrichments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("ein", sa.String(length=9), nullable=False),
        sa.Column("prospect_id", sa.Integer(), sa.ForeignKey("prospects.id"), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        sa.Column("contact_title", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("email_status", sa.String(length=20), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("linkedin_url", sa.String(length=500), nullable=True),
        sa.Column("provider_confidence", sa.Integer(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("credits_spent", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_enrichments_ein", "enrichments", ["ein"])
    op.create_check_constraint(
        "ck_enrichments_email_status",
        "enrichments",
        "email_status IS NULL OR email_status IN ('verified', 'catch_all', 'not_found')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_enrichments_email_status", "enrichments", type_="check")
    op.drop_index("ix_enrichments_ein", table_name="enrichments")
    op.drop_table("enrichments")
