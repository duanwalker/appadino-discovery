"""v1 item 5: client_users table, the login-to-customer allowlist

Revision ID: a5c2e7d9f1b3
Revises: f1abb24b67b7
Create Date: 2026-11-12 00:00:00.000000

One row per person allowed into the deployed dashboard (ACCESS-CONTROL-PROPOSAL.md,
Option A). The Functions API reads the Static Web Apps login header, then finds the
caller here by (identity_provider, lower(email)); the row's client_id is the only
place a request's tenant comes from. No row, or active = false, means 403.

Uniqueness is on lower(email) so "Lauren@x.org" and "lauren@x.org" can never map to
two different clients. Turning a row's `active` off is the immediate off-switch, and
it takes effect on the very next request, whatever SWA's session cookie still says.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a5c2e7d9f1b3"
down_revision: Union[str, None] = "f1abb24b67b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("identity_provider", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(email) <> ''", name="ck_client_users_email_not_blank"),
        sa.CheckConstraint("btrim(identity_provider) <> ''", name="ck_client_users_provider_not_blank"),
    )
    op.create_index(
        "uq_client_users_provider_email",
        "client_users",
        ["identity_provider", sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_client_users_provider_email", table_name="client_users")
    op.drop_table("client_users")
