from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class Suppression(Base):
    """§4 Stage 4 / §9 rule 7: EIN-resolvable entries suppress outright; name-only
    entries (no EIN available, as is the case for all of ARCHITECT's seed data — see
    §4's suppression lists) are matched fuzzily at Stage 4 and only ever *flagged*,
    never silently dropped.
    """

    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    ein: Mapped[str | None] = mapped_column(String(9))
    org_name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(30))  # client | active_prospect | partner_attribution
    source: Mapped[str | None] = mapped_column(String(255))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # G1.5 addition (not in the brief's §3 sketch): partner_attribution entries "carry
    # the 18-month window metadata" per §4 Stage 4 — the 12% commission context lives
    # in ARCHITECT's world, this just tracks when the attribution window closes.
    partner_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
