from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class EnrichmentJob(Base):
    """E2 fix: a durable, atomic claim that a submission is currently in flight for
    (client_id, ein) — nothing more. `UNIQUE (client_id, ein)` is the actual
    double-submit guard (see function_app.py's `_claim_pending_slot`); the row is
    deleted once the job resolves, so completion never permanently blocks a future
    enrichment attempt for the same org. `job_id` is nullable because the claim is
    taken before the provider returns one.
    """

    __tablename__ = "enrichment_jobs"
    __table_args__ = (UniqueConstraint("client_id", "ein", name="uq_enrichment_jobs_client_ein"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    ein: Mapped[str] = mapped_column(String(9), index=True)
    job_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
