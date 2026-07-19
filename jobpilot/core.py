"""Pipeline core — the smart half of the E5 contract.

Exclusively owns: upsert on (source, external_id), first_seen seeding with
provenance, the status-flip guard, cross-source dedupe, and poll_log rows.
Adapters never touch the database; nothing else writes job state.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.adapters import NormalizedJob, PollResult
from jobpilot.dedupe import find_duplicate, link_duplicates
from jobpilot.models import FirstSeenSource, Job, JobStatus, PollLog

ATS_SOURCES = {"greenhouse", "lever", "ashby"}
ABSENT_POLLS_TO_CLOSE = 2  # E1: 2 consecutive successful polls
JOBSPY_STALE_DAYS = 14  # E1: JobSpy rows soft-close only; no board to re-poll


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


@dataclass
class IngestStats:
    company: str
    source: str
    new: int = 0
    updated: int = 0
    closed: int = 0
    duplicates_linked: int = 0
    success: bool = True
    error: str | None = None
    new_job_ids: list[int] = field(default_factory=list)


def ingest(
    session: Session,
    result: PollResult,
    source: str,
    market: str,
    alias_lookup: dict[str, str] | None = None,
) -> IngestStats:
    """Apply one PollResult. Upserts always happen (partial data is still
    data); status flips happen only for ATS boards and only when the poll
    succeeded — a failed poll must never close anything.
    """
    now = utcnow()
    stats = IngestStats(result.company, source, success=result.success, error=result.error)
    alias_lookup = alias_lookup or {}

    seen_ids: set[str] = set()
    for nj in result.jobs:
        seen_ids.add(nj.external_id)
        row = session.scalar(
            select(Job).where(Job.source == nj.source, Job.external_id == nj.external_id)
        )
        if row is None:
            row = _insert(session, nj, market, now)
            stats.new += 1
            stats.new_job_ids.append(row.id)
            other = find_duplicate(session, row, alias_lookup)
            if other is not None:
                link_duplicates(session, row, other)
                stats.duplicates_linked += 1
        else:
            _refresh(row, nj, now)
            stats.updated += 1

    if result.success and source in ATS_SOURCES:
        stats.closed = _apply_flip_guard(session, source, result.company, seen_ids)

    session.add(
        PollLog(
            source=source,
            company=result.company,
            polled_at=now,
            success=result.success,
            jobs_seen=len(result.jobs),
            error=result.error,
        )
    )
    session.flush()
    return stats


def _insert(session: Session, nj: NormalizedJob, market: str, now: datetime) -> Job:
    if nj.posted_at is not None:
        first_seen, provenance = _to_naive_utc(nj.posted_at), FirstSeenSource.API
    else:
        first_seen, provenance = now, FirstSeenSource.OBSERVED
    row = Job(
        source=nj.source,
        external_id=nj.external_id,
        company=nj.company,
        title=nj.title,
        location=nj.location,
        market=market,
        url=nj.url,
        salary_min=nj.salary_min,
        salary_max=nj.salary_max,
        salary_currency=nj.salary_currency,
        description=nj.description,
        description_partial=nj.description_partial,
        first_seen=first_seen,
        first_seen_source=provenance,
        last_seen=now,
        status=JobStatus.OPEN,
        absent_polls=0,
    )
    session.add(row)
    session.flush()
    return row


def _refresh(row: Job, nj: NormalizedJob, now: datetime) -> None:
    row.last_seen = now
    row.absent_polls = 0
    row.title = nj.title
    row.location = nj.location
    row.url = nj.url
    if nj.description and not nj.description_partial:
        row.description = nj.description
        row.description_partial = False
    if row.status in (JobStatus.CLOSED, JobStatus.STALE):
        row.status = JobStatus.OPEN  # closed→reopened; feeds repost detection


def _apply_flip_guard(session: Session, source: str, company: str, seen_ids: set[str]) -> int:
    """Absence accounting for ONE successful poll of one board (E1)."""
    closed = 0
    open_rows = session.scalars(
        select(Job).where(Job.source == source, Job.company == company, Job.status == JobStatus.OPEN)
    )
    for row in open_rows:
        if row.external_id in seen_ids:
            continue
        row.absent_polls += 1
        if row.absent_polls >= ABSENT_POLLS_TO_CLOSE:
            row.status = JobStatus.CLOSED
            closed += 1
    return closed


def mark_jobspy_stale(session: Session, now: datetime | None = None) -> int:
    """JobSpy rows never auto-close (no board to re-poll); unseen for 14 days
    → 'stale', which keeps digests clean but feeds no diff-engine signal.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=JOBSPY_STALE_DAYS)
    stale = 0
    rows = session.scalars(
        select(Job).where(
            Job.source.notin_(ATS_SOURCES),
            Job.status == JobStatus.OPEN,
            Job.last_seen < cutoff,
        )
    )
    for row in rows:
        row.status = JobStatus.STALE
        stale += 1
    return stale
