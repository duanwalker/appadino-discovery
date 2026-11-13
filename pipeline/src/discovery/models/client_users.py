from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class ClientUser(Base):
    """v1 item 5: one person allowed into the deployed dashboard, and the customer
    they belong to. The Functions API derives a request's client_id from this row
    only (looked up by identity_provider + lower(email) from the Static Web Apps
    login header), never from the request. `active = false` cuts the person off on
    their next request. See migration a5c2e7d9f1b3."""

    __tablename__ = "client_users"
    __table_args__ = (
        Index("uq_client_users_provider_email", "identity_provider", text("lower(email)"), unique=True),
        CheckConstraint("btrim(email) <> ''", name="ck_client_users_email_not_blank"),
        CheckConstraint("btrim(identity_provider) <> ''", name="ck_client_users_provider_not_blank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_provider: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(320))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
