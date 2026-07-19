"""Greenhouse board adapter.

Gotchas handled (from research + E-series):
- content comes HTML-escaped → html.unescape before storing.
- A deleted/renamed board can return HTTP 200 with an error payload instead of
  jobs → treated as a failed poll, never an empty board.
- first_seen seeding prefers first_published; updated_at moves on edits and
  would corrupt true posting age, so it is only a fallback.
"""

import html
from datetime import datetime

import httpx

from jobpilot.adapters import NormalizedJob, PollResult

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
UA = {"User-Agent": "Mozilla/5.0 (JobPilot)"}


def poll(company: str, slug: str, client: httpx.Client) -> PollResult:
    try:
        resp = client.get(BOARD_URL.format(slug=slug), headers=UA)
        resp.raise_for_status()
        payload = resp.json()
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            return PollResult(company, success=False, error=f"no jobs list in payload: {str(payload)[:200]}")
        jobs = [_normalize(company, j) for j in raw_jobs]
        return PollResult(company, jobs)
    except Exception as e:
        return PollResult(company, success=False, error=f"{type(e).__name__}: {e}")


def _normalize(company: str, j: dict) -> NormalizedJob:
    content = j.get("content")
    location = (j.get("location") or {}).get("name")
    ts = j.get("first_published") or j.get("updated_at")
    return NormalizedJob(
        source="greenhouse",
        external_id=str(j["id"]),
        company=company,
        title=j["title"],
        url=j["absolute_url"],
        location=location,
        description=html.unescape(content) if content else None,
        description_partial=content is None,
        posted_at=datetime.fromisoformat(ts) if ts else None,
    )
