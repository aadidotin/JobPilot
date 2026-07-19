"""Digest assembly — filter survivors → Telegram message payloads.

One message per job (own 👍/👎 keyboard, no 4096-char juggling). Payloads are
plain dicts ready for the Bot API sendMessage call; nothing here talks to
Telegram, so the whole digest is testable offline. An empty day still
produces one message — silence must mean "broken", never "no jobs" (E11).
"""

from datetime import datetime
from html import escape

from sqlalchemy.orm import Session

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


def build_digest(jobs: list[Job], now: datetime) -> list[dict]:
    if not jobs:
        return [{"text": f"📭 Digest {now:%d %b}: 0 new roles today. (Pipeline alive — this is a real zero, not a silence.)"}]
    header = {"text": f"📬 Digest {now:%d %b}: {len(jobs)} new role(s)"}
    return [header] + [render_job_message(j, now) for j in jobs]


def mark_digested(session: Session, jobs: list[Job], now: datetime) -> None:
    for job in jobs:
        job.digested_at = now
    session.flush()
