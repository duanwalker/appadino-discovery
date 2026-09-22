"""E2 fix: enrichment_jobs table — atomic double-submit guard

Revision ID: d3f9a1c6e8b2
Revises: c1e8a4f2d5b9
Create Date: 2026-09-22 00:00:00.000000

Fixes a real gap Copilot's independent E2 review found (test_e2_independent.py,
`test_double_click_while_pending_submits_only_once`): two POSTs to the Enrich route
while a job is still pending each submitted to FullEnrich, risking duplicate credit
spend on the shared pooled credit balance. An in-process guard alone isn't a
guarantee — Azure Functions can run multiple instances — so this needs a claim
that's atomic at the database layer, not just per-process.

One row per (client_id, ein) means "a submission is currently in flight for this
org" — nothing more. `UNIQUE (client_id, ein)` is the actual enforcement mechanism:
a concurrent second `INSERT ... ON CONFLICT (client_id, ein) DO NOTHING` can never
win against an existing row, regardless of which Functions instance handles it.
`job_id` is nullable because the claim is taken *before* the provider call returns
one (see function_app.py's `_claim_pending_slot`/`_set_claimed_job_id`); the row is
deleted once the job resolves (function_app.py's `_clear_pending_slot`), so a
completed or abandoned job never permanently blocks a future enrichment attempt for
the same org — except a genuinely abandoned job (crash mid-flight, no completion
ever recorded), which has no cleanup/TTL yet. Flagged as a known follow-up, not
silently ignored, and out of scope for this fix (Copilot's test only covers the
double-submit case).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d3f9a1c6e8b2"
down_revision: Union[str, None] = "c1e8a4f2d5b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "enrichment_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("ein", sa.String(length=9), nullable=False),
        sa.Column("job_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_id", "ein", name="uq_enrichment_jobs_client_ein"),
    )


def downgrade() -> None:
    op.drop_table("enrichment_jobs")
