"""G2.x: prospects.suppression_flag column, status CHECK constraint

Revision ID: 4a2e9c1f7b3d
Revises: 7ac7a7b1f96a
Create Date: 2026-09-18 00:00:00.000000

G2.x groundwork fix, not a dashboard feature itself: `prospects.notes` was
dual-purpose (pipeline-written fuzzy-suppression message + intended home for
Lauren's human review comments) and the publish upsert unconditionally
overwrote it on every re-run (`notes = EXCLUDED.notes`), which would have
silently destroyed any human-entered note on the next scheduled run (§7
weekly score refresh). Splitting the fuzzy-suppression message into its own
column makes `notes` purely human-owned; the publish stage's upsert no
longer touches it (see stages/publish.py).

No data migration needed: the current 154-row client_id=2 dataset has 0
fuzzy-flagged prospects (STATUS.md, G1.5 post-close fix), so `notes` is NULL
everywhere today — nothing to move into the new column.

status CHECK constraint scoped to the four states §5's dashboard workflow
actually uses (new|reviewed|approved|rejected) — not the brief's §3 sketch's
six (which also lists contacted|responded, belonging to the Phase 2 Outreach
queue, explicitly stubbed/out of scope for G2.x).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "4a2e9c1f7b3d"
down_revision: Union[str, None] = "7ac7a7b1f96a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("prospects", sa.Column("suppression_flag", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_prospects_status",
        "prospects",
        "status IN ('new', 'reviewed', 'approved', 'rejected')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_prospects_status", "prospects", type_="check")
    op.drop_column("prospects", "suppression_flag")
