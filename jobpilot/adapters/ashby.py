"""Ashby job-board adapter.

Notes from live-API capture (2026-07-19): the timestamp field is publishedAt
(the design doc said publishedDate — wrong, verified against real payloads);
unlisted postings appear in the feed with isListed=false and must be skipped.
"""

from datetime import datetime

import httpx

from jobpilot.adapters import NormalizedJob, PollResult

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
UA = {"User-Agent": "Mozilla/5.0 (JobPilot)"}


def poll(company: str, slug: str, client: httpx.Client) -> PollResult:
    try:
        resp = client.get(BOARD_URL.format(slug=slug), headers=UA)
        resp.raise_for_status()
        payload = resp.json()
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            return PollResult(company, success=False, error=f"no jobs list in payload: {str(payload)[:200]}")
        jobs = [_normalize(company, j) for j in raw_jobs if j.get("isListed", True)]
        return PollResult(company, jobs)
    except Exception as e:
        return PollResult(company, success=False, error=f"{type(e).__name__}: {e}")


def _normalize(company: str, j: dict) -> NormalizedJob:
    location = j.get("location")
    if j.get("isRemote") and location and "remote" not in location.lower():
        location = f"Remote ({location})"
    ts = j.get("publishedAt")
    return NormalizedJob(
        source="ashby",
        external_id=str(j["id"]),
        company=company,
        title=j["title"],
        url=j.get("jobUrl") or j.get("applyUrl"),
        location=location,
        description=j.get("descriptionPlain"),
        description_partial=not j.get("descriptionPlain"),
        posted_at=datetime.fromisoformat(ts) if ts else None,
    )
