from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class QaSample(Base):
    """§7: "every run samples 20 scored claims into qa_samples, re-fetches each
    citation, verdicts match/mismatch; mismatch rate >10% pages Duan" — the automated
    stand-in for "Duan reads the output".

    G1.5 deviation from the brief's §3 column name: `citation` rather than
    `citation_url`. Our citations (§4 Stage 3) are inline text — a quote/paraphrase of
    stored mission/program text, or a specific financial figure — not external URLs;
    "re-fetch" here means re-querying our own stored source data (organizations/
    filings/signals), the actual ground truth in this system, not an HTTP fetch.
    """

    __tablename__ = "qa_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    ein: Mapped[str] = mapped_column(String(9), index=True)
    claim: Mapped[str] = mapped_column(Text)
    citation: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(20))  # match | mismatch | unverifiable
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
