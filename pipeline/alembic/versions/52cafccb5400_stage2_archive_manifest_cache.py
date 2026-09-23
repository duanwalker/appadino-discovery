"""Stage 2 performance fix: archive_manifest local-cache table

Revision ID: 52cafccb5400
Revises: d3f9a1c6e8b2
Create Date: 2026-09-22 00:00:00.000000

Today, `fetch_filing_xml()` (extract_signals.py) opens a brand-new `RemoteZip`
connection per filing — with up to ~238K filings nationally sharing ~40 monthly IRS
archives, that's tens of thousands of redundant remote-zip opens against
apps.irs.gov for data that barely changes. Fix: download each archive once to a
persistent Azure Files mount and read from the local copy thereafter.

One row per (year, month, suffix) shard — that's the shard's own natural identity
(what `discover_zip_urls()` already enumerates it by), so it's the primary key
rather than a surrogate id; every caller looks this up by year/month/suffix, never
by an opaque row id. Rows are treated as immutable once written (see
`sync_archive_manifest()`): a shard already on disk is never re-downloaded, only
sanity-checked (`size_bytes` vs. a fresh HEAD's Content-Length) on each run.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "52cafccb5400"
down_revision: Union[str, None] = "d3f9a1c6e8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "archive_manifest",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("month", sa.Integer(), primary_key=True),
        sa.Column("suffix", sa.String(length=1), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("archive_manifest")
