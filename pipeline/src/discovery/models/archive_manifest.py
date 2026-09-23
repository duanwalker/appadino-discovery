from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from discovery.db import Base


class ArchiveManifest(Base):
    """Stage 2 archive cache manifest — one row per IRS 990 monthly ZIP archive shard
    already downloaded to the local/mounted cache (see extract_signals.sync_archive_manifest).
    Composite natural key (year, month, suffix), the shard's own identity, rather than a
    surrogate id — every caller looks this up by year/month/suffix, never by row id. Rows
    are immutable once written; a shard already on disk is never re-downloaded.
    """

    __tablename__ = "archive_manifest"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)
    suffix: Mapped[str] = mapped_column(String(1), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
