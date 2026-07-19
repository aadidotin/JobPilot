"""Schema per the approved design (2026-07-19): diff-engine columns from day 1,
applications table from weekend 1 (amendment C1 — every post-send feature
anchors on sent_at).
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"  # ATS rows only: absent for 2 consecutive successful polls
    STALE = "stale"  # JobSpy rows only: unseen for 14 days (soft-close, feeds no signal)


class FirstSeenSource(StrEnum):
    API = "api"  # seeded from the board's own posting timestamp — freshness-trustworthy
    OBSERVED = "observed"  # first seen by our poller — age unknown, never instant-pings


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # greenhouse|lever|ashby|linkedin|indeed|naukri
    external_id: Mapped[str] = mapped_column(String(255))
    company: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    market: Mapped[str] = mapped_column(String(16))  # india | remote-intl
    url: Mapped[str] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    description: Mapped[str | None] = mapped_column(Text)  # raw payload IS the JD archive (C7)
    description_partial: Mapped[bool] = mapped_column(default=False)

    # Diff engine
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    first_seen_source: Mapped[str] = mapped_column(String(8))  # FirstSeenSource
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(8), default=JobStatus.OPEN)  # JobStatus
    absent_polls: Mapped[int] = mapped_column(default=0)  # consecutive successful polls absent
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))

    # Intelligence (weekend 2+; columns exist so weekend-1 rows carry forward)
    score: Mapped[int | None] = mapped_column(Integer)
    missing_requirements: Mapped[str | None] = mapped_column(Text)  # JSON list, C8 rollup input


class Annotation(Base):
    """👍/👎 digest taps — the scorer's future golden set."""

    __tablename__ = "annotations"
    __table_args__ = (UniqueConstraint("job_id"),)  # double-tap safe

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    verdict: Mapped[str] = mapped_column(String(4))  # up | down
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Application(Base):
    """Send capture (C1). Rows come from /applied (manual track) or ✅ Sent (weekend 3+).
    job_id is nullable: /applied must work for roles JobPilot never ingested.
    """

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    company: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    reply_channel: Mapped[str | None] = mapped_column(String(32))  # email|linkedin|portal|none (C5)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    # sent|viewed|replied|interview|offer|rejected|ghosted
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)


class PollLog(Base):
    """One row per board per pipeline run. Feeds the status-flip guard
    (flip only after successful polls), E11 per-tier silence alerts, and the
    C10 ≥21-day signal-maturity gate.
    """

    __tablename__ = "poll_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    company: Mapped[str] = mapped_column(String(255))
    polled_at: Mapped[datetime] = mapped_column(DateTime)
    success: Mapped[bool] = mapped_column()
    jobs_seen: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
