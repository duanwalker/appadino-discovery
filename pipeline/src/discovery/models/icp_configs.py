from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class IcpConfig(Base):
    """A versioned ICP config (§3). `config` is the full tenant-variable surface —
    geography tiers, revenue band, excludes, disqualifiers, signal weights, alignment
    keywords, enrichment flags — nothing tenant-specific lives in code (§9, hard rules
    are tests; everything else is a DB update). Only one row per client should have
    active=true at a time; Stage 1 (§4) reads that row to build its recall filter."""

    __tablename__ = "icp_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
