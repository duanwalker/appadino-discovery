from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Signal(Base):
    """Derived, per-(ein, tax_year) signals computed from a survivor's filings (§3, §4
    Stage 2) — gov_funding_pct, revenue_composition, dd_present, fundraising_spend_ratio,
    org_age, revenue_trend. Not tenant-scoped: derived purely from public filing data,
    shared across any client whose ICP happens to select the same org."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("ein", "tax_year", name="uq_signals_ein_tax_year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ein: Mapped[str] = mapped_column(String(9), index=True)
    tax_year: Mapped[int] = mapped_column(Integer)
    signal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
