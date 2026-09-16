from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Score(Base):
    """Stage 3 AI scoring output (§3, §4). One row per (client_id, ein, icp_version,
    stage) — the Haiku pass writes a cheap pre-score row (stage='haiku') used only to
    decide who proceeds; the Sonnet pass writes the full five-signal row
    (stage='sonnet') that the dashboard actually surfaces.

    gap_rank is nullable and NOT computed by this stage: §4 lists "compute gap_rank
    ordering" under Stage 6 (Publish), which is G1.5, not G1.4 — left for that gate
    rather than inventing an unspecified ranking formula now.
    """

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("client_id", "ein", "icp_version", "stage", name="uq_scores_client_ein_version_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    ein: Mapped[str] = mapped_column(String(9), index=True)
    icp_version: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(10))
    values_signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    alignment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    capacity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gap_rank: Mapped[float | None] = mapped_column(Numeric)
    disqualified: Mapped[bool] = mapped_column(Boolean, default=False)
    dq_reason: Mapped[str | None] = mapped_column(Text)
    soft_flags: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
