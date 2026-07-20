"""Digest assembly — filter survivors → Telegram message payloads.

One message per job (own 👍/👎 keyboard, no 4096-char juggling). Payloads are
plain dicts ready for the Bot API sendMessage call; nothing here talks to
Telegram, so the whole digest is testable offline. An empty day still
produces one message — silence must mean "broken", never "no jobs" (E11).
"""

from datetime import datetime
from html import escape

from sqlalchemy.orm import Session

from jobpilot.core import ATS_SOURCES
from jobpilot.models import FirstSeenSource, Job

MARKET_TAG = {"india": "🇮🇳 india", "remote-intl": "🌍 remote-intl"}


def age_line(job: Job, now: datetime) -> str:
    if job.first_seen_source != FirstSeenSource.API:
        return "age unknown"
    delta = now - job.first_seen
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "posted <1h ago"
    if hours < 48:
        return f"posted {hours}h ago"
    return f"posted {delta.days}d ago"


def salary_badge(job: Job) -> str | None:
    if job.salary_min is None and job.salary_max is None:
        return None
    cur = job.salary_currency or ""
    if job.salary_min and job.salary_max and job.salary_min != job.salary_max:
        return f"💰 {cur} {job.salary_min:,}–{job.salary_max:,}".strip()
    return f"💰 {cur} {(job.salary_max or job.salary_min):,}".strip()


def render_job_message(job: Job, now: datetime) -> dict:
    """sendMessage payload (HTML parse mode) with annotation buttons."""
    parts = [
        f"<b>{escape(job.title)}</b> — {escape(job.company)}",
        f"{MARKET_TAG.get(job.market, job.market)} | {escape(job.location or 'location unknown')}",
        f"{age_line(job, now)} | via {job.source}",
    ]
    badge = salary_badge(job)
    if badge:
        parts.append(badge)
    parts.append(f'<a href="{escape(job.url)}">Open posting</a>')
    return {
        "text": "\n".join(parts),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "👍", "callback_data": f"ann:{job.id}:up"},
                {"text": "👎", "callback_data": f"ann:{job.id}:down"},
            ]]
        },
    }


def select_for_digest(jobs: list[Job], limit: int | None) -> list[Job]:
    """Fill the cap round-robin across market x source tier, freshest-first
    inside each bucket.

    Plain freshest-first would hand the whole digest to one bucket: aggregator
    rows are 'observed' provenance, so their first_seen is ingest time, which
    always outranks an ATS row carrying its true (older) posting date. The ATS
    tier is the backbone and would never appear. E6 also measures the weekend-1
    gate per market x tier, which a single-bucket digest cannot feed.
    """
    if not limit or len(jobs) <= limit:
        return jobs
    buckets: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:  # already freshest-first, so each bucket inherits that order
        tier = "ats" if job.source in ATS_SOURCES else "aggregator"
        buckets.setdefault((job.market, tier), []).append(job)

    picked: list[Job] = []
    queues = [iter(b) for _, b in sorted(buckets.items())]
    while len(picked) < limit and queues:
        for queue in list(queues):
            if len(picked) == limit:
                break
            job = next(queue, None)
            if job is None:
                queues.remove(queue)
            else:
                picked.append(job)
    return picked


def build_digest(jobs: list[Job], now: datetime, limit: int | None = None) -> list[dict]:
    """Payloads for one digest. `jobs` must arrive freshest-first: anything past
    `limit` is held back, not dropped, and leads tomorrow's digest.
    """
    if not jobs:
        return [{"text": f"📭 Digest {now:%d %b}: 0 new roles today. (Pipeline alive — this is a real zero, not a silence.)"}]
    shown = select_for_digest(jobs, limit)
    held = len(jobs) - len(shown)
    header = f"📬 Digest {now:%d %b}: {len(shown)} new role(s)"
    if held:
        header += f" — {held} more held for tomorrow (cap {limit})"
    return [{"text": header}] + [render_job_message(j, now) for j in shown]


def mark_digested(session: Session, jobs: list[Job], now: datetime) -> None:
    for job in jobs:
        job.digested_at = now
    session.flush()
