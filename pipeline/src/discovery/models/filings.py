from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Filing(Base):
    """One indexed 990/990-EZ filing per (ein, object_id) — §4 Stage 0 populates
    identity/location columns (object_id, xml_object_url) for the latest 2 filings per
    org; §4 Stage 2 (G1.3) fills in the extracted financial columns and sets extracted_at.

    Deviation from the brief's §3 sketch: adds `object_id`. The IRS's AWS Open Data
    S3 bucket for individually-addressable 990 XML files was deprecated in Dec 2021;
    filings are now only distributed bundled in monthly ZIP archives per submission
    year. `xml_object_url` stores the directory containing those archives for the
    filing's submission year; `object_id` is the key needed to locate the specific
    filing once G1.3 resolves which archive contains it.
    """

    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("ein", "object_id", name="uq_filings_ein_object_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ein: Mapped[str] = mapped_column(String(9), index=True)
    tax_year: Mapped[int] = mapped_column(Integer)
    form_type: Mapped[str] = mapped_column(String(10))
    object_id: Mapped[str] = mapped_column(String(30))
    xml_object_url: Mapped[str | None] = mapped_column(String(500))
    revenue_total: Mapped[int | None] = mapped_column(Numeric)
    contributions: Mapped[int | None] = mapped_column(Numeric)
    program_revenue: Mapped[int | None] = mapped_column(Numeric)
    govt_grants: Mapped[int | None] = mapped_column(Numeric)
    fundraising_expense: Mapped[int | None] = mapped_column(Numeric)
    officers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
