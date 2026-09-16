from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Organization(Base):
    """The shared, national nonprofit universe — not tenant-scoped. Populated from the
    IRS Business Master File (§4 Stage 0). NTEE is retained for recall shaping only and
    must never be used as a scoring input (§9 rule 6)."""

    __tablename__ = "organizations"

    ein: Mapped[str] = mapped_column(String(9), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    state: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(100))
    ntee: Mapped[str | None] = mapped_column(String(10))
    ruling_year: Mapped[int | None]
    revenue_latest: Mapped[int | None] = mapped_column(Numeric)
    foundation_code: Mapped[str | None] = mapped_column(String(2))
    bmf_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # G1.4 addition (not in the brief's §3 sketch): the 990's own Item 5 "Website
    # address" disclosure, extracted alongside mission/program text in Stage 2. A
    # citable homepage link for the dashboard only — never fetched, never a scoring
    # input (§4 Stage 3 uses 990 mission/program text only; see STATUS.md).
    website: Mapped[str | None] = mapped_column(String(500))
