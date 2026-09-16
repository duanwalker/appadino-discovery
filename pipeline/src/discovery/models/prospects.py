from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Prospect(Base):
    """§3/§4 Stage 6 (Publish). One row per (client_id, ein) — upserted, status starts
    at 'new'. Only disqualified=false, Sonnet-scored, non-suppressed survivors are
    published here at all (§9: a disqualified org is "never surfaced as a prospect").

    G1.5 additions beyond the brief's §3 sketch (assigned_trigger only): trigger_angle
    (resolved from icp_configs.config.trigger_angles at publish time — null when no
    mapping exists, e.g. first_filing_above_floor, rather than inventing one) and
    trigger_evidence (the citable facts behind the trigger — every claim needs a
    citation, triggers are no exception).
    """

    __tablename__ = "prospects"
    __table_args__ = (UniqueConstraint("client_id", "ein", name="uq_prospects_client_ein"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    ein: Mapped[str] = mapped_column(String(9), index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    assigned_trigger: Mapped[str | None] = mapped_column(String(50))
    trigger_angle: Mapped[str | None] = mapped_column(String(255))
    trigger_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gap_rank: Mapped[float | None] = mapped_column(Numeric)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
