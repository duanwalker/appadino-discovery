"""E2: enrichments stale_likely_moved status + stale_detail

Revision ID: c1e8a4f2d5b9
Revises: b7d4f1a9c3e6
Create Date: 2026-09-21 00:00:00.000000

Two additions carried into E2 from E1's close (STATUS.md open items), neither
of which made it into the brief's v2.3 text itself (confirmed by re-reading
the brief before starting this gate — flagged separately to Duan):

1. A "stale — likely moved" status distinct from not_found. E1's manual
   spot-check found FullEnrich's own verified/deliverable status doesn't mean
   the contact is still at the org being prospected (e.g. a Day One officer
   resolving to a sheppardpratt.org address) — this is real signal, not a
   failure state, and the brief's three-way verified|catch_all|not_found
   bucket had no room for it.
2. `stale_detail`: the mismatched domain/company FullEnrich's response
   revealed, so the dashboard can surface *why* something is flagged stale
   rather than just the bare label.

No separate "real-vs-verified confidence" column: per review, that signal is
computed the same way stale detection is (org-domain comparison, with a
personal-email-domain carve-out to avoid flagging board volunteers who list a
personal address as "moved") — see discovery.clients.enrichment_adapter. It
folds into email_status/stale_detail rather than needing its own column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c1e8a4f2d5b9"
down_revision: Union[str, None] = "b7d4f1a9c3e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("enrichments", sa.Column("stale_detail", sa.Text(), nullable=True))
    op.drop_constraint("ck_enrichments_email_status", "enrichments", type_="check")
    op.create_check_constraint(
        "ck_enrichments_email_status",
        "enrichments",
        "email_status IS NULL OR email_status IN "
        "('verified', 'catch_all', 'not_found', 'stale_likely_moved')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_enrichments_email_status", "enrichments", type_="check")
    op.create_check_constraint(
        "ck_enrichments_email_status",
        "enrichments",
        "email_status IS NULL OR email_status IN ('verified', 'catch_all', 'not_found')",
    )
    op.drop_column("enrichments", "stale_detail")
