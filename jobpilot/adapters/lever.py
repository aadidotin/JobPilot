"""Lever postings adapter.

Gotcha handled (from research): Lever caps a response at 250 postings. Any
full page (exactly `limit` results) means more may exist → paginate with the
skip offset until a short page returns. A pagination failure mid-walk is an
incomplete read → success=False so the core never flips statuses off it (E5).
"""

from datetime import UTC, datetime

import httpx

from jobpilot.adapters import NormalizedJob, PollResult

BOARD_URL = "https://api.lever.co/v0/postings/{slug}"
UA = {"User-Agent": "Mozilla/5.0 (JobPilot)"}
PAGE_SIZE = 250
MAX_PAGES = 40  # 10k postings; beyond this assume something is wrong


def poll(company: str, slug: str, client: httpx.Client) -> PollResult:
    jobs: list[NormalizedJob] = []
    try:
        for page in range(MAX_PAGES):
            resp = client.get(
                BOARD_URL.format(slug=slug),
                params={"mode": "json", "limit": PAGE_SIZE, "skip": page * PAGE_SIZE},
                headers=UA,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                return PollResult(company, success=False, error=f"unexpected payload: {str(payload)[:200]}")
            jobs.extend(_normalize(company, j) for j in payload)
            if len(payload) < PAGE_SIZE:
                return PollResult(company, jobs)
        return PollResult(company, jobs, success=False, error=f"pagination exceeded {MAX_PAGES} pages")
    except Exception as e:
        return PollResult(company, jobs, success=False, error=f"{type(e).__name__}: {e}")


def _normalize(company: str, j: dict) -> NormalizedJob:
    cats = j.get("categories") or {}
    location = cats.get("location") or j.get("country")
    if j.get("workplaceType") == "remote" and location and "remote" not in location.lower():
        location = f"Remote ({location})"
    created = j.get("createdAt")
    return NormalizedJob(
        source="lever",
        external_id=j["id"],
        company=company,
        title=j["text"],
        url=j["hostedUrl"],
        location=location,
        description=j.get("descriptionPlain") or j.get("descriptionBodyPlain"),
        description_partial=not (j.get("descriptionPlain") or j.get("descriptionBodyPlain")),
        posted_at=datetime.fromtimestamp(created / 1000, tz=UTC) if created else None,
    )
