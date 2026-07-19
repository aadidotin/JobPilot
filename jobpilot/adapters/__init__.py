"""Adapter contract (amendment E5): dumb adapters, smart core.

An adapter does fetch + parse + normalize ONLY and returns a PollResult.
It never touches the database. If it cannot guarantee a complete read
(timeout, truncation mid-pagination, error payload), it returns
success=False and the core skips status flips for that board.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NormalizedJob:
    source: str
    external_id: str
    company: str
    title: str
    url: str
    location: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    description: str | None = None
    description_partial: bool = False
    posted_at: datetime | None = None  # board's own timestamp → 'api' provenance; None → 'observed'


@dataclass
class PollResult:
    company: str
    jobs: list[NormalizedJob] = field(default_factory=list)
    success: bool = True
    error: str | None = None
