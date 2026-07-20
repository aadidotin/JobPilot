"""Hard filters (PROJECT.md §7 Stage 2 + design deltas).

Cheap gates only — volume control, not ranking. Policy lives in
config/filters.yaml and config/roles.yaml; nothing here is hardcoded.
Edge case pinned by the design: a posting with no parseable salary PASSES
the salary floor (most posts omit salary) and renders unbadged.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.dedupe import norm_company
from jobpilot.models import Job, JobStatus


@dataclass
class FilterConfig:
    title_include: list[str]
    title_exclude: list[str]
    freshness_days: int
    salary_floor: dict  # market -> {currency, amount} | None
    company_blocklist: set[str]  # normalized
    location: dict  # market -> {include_any: [...], exclude: [...]}

    @classmethod
    def load(cls, config_dir: str | Path = "config") -> "FilterConfig":
        config_dir = Path(config_dir)
        roles = yaml.safe_load((config_dir / "roles.yaml").read_text())
        f = yaml.safe_load((config_dir / "filters.yaml").read_text())
        return cls(
            title_include=[t.lower() for t in roles["title_include"]],
            title_exclude=[t.lower() for t in roles["title_exclude"]],
            freshness_days=f["freshness_days"],
            salary_floor=f.get("salary_floor") or {},
            company_blocklist={norm_company(c) for c in f.get("company_blocklist") or []},
            location=f.get("location") or {},
        )


@lru_cache(maxsize=8)  # rebuilt per config, not per job title
def _exclude_pattern(terms: tuple[str, ...]) -> re.Pattern | None:
    """Excludes match on word boundaries; includes stay plain substrings.

    The asymmetry is deliberate. A loose include only costs you an extra card
    in the digest, which the excludes then get a shot at. A loose exclude
    silently deletes a real job and you never learn it existed — 'intern' was
    swallowing "Full Stack Internal Tooling". Boundaries are written by hand
    rather than with \\b so terms ending in punctuation (.net, c++) still work.
    """
    if not terms:
        return None
    return re.compile("|".join(_bounded(t) for t in terms))


def _bounded(term: str) -> str:
    """Guard only the sides that actually end in a word character, so '.net'
    still matches inside 'asp.net' while 'intern' stays out of 'internal'."""
    head = r"(?<!\w)" if term[:1].isalnum() else ""
    tail = r"(?!\w)" if term[-1:].isalnum() else ""
    return head + re.escape(term) + tail


def passes_title(title: str, cfg: FilterConfig) -> bool:
    t = title.lower()
    if not any(k in t for k in cfg.title_include):
        return False
    pattern = _exclude_pattern(tuple(cfg.title_exclude))
    return not (pattern and pattern.search(t))


def passes_location(location: str | None, market: str, cfg: FilterConfig) -> bool:
    rules = cfg.location.get(market)
    if not rules:
        return True
    if location is None:
        return True  # unknown location is the scorer's problem, not a hard reject
    loc = location.lower()
    include = rules.get("include_any") or []
    if include and not any(k in loc for k in include):
        return False
    return not any(k in loc for k in rules.get("exclude") or [])


def passes_salary(job: Job, cfg: FilterConfig) -> bool:
    floor = cfg.salary_floor.get(job.market)
    if not floor:
        return True
    stated = job.salary_max or job.salary_min
    if stated is None:
        return True  # no stated salary passes, renders unbadged
    if job.salary_currency and job.salary_currency.upper() != floor["currency"].upper():
        return True  # cross-currency comparison is not a hard gate's job
    return stated >= floor["amount"]


def passes_company(job: Job, cfg: FilterConfig) -> bool:
    return norm_company(job.company) not in cfg.company_blocklist


def filter_survivors(session: Session, cfg: FilterConfig, now: datetime) -> list[Job]:
    """Open, canonical, fresh, not-yet-digested rows that pass every gate."""
    cutoff = now - timedelta(days=cfg.freshness_days)
    candidates = session.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.OPEN,
            Job.duplicate_of.is_(None),
            Job.digested_at.is_(None),
            Job.first_seen >= cutoff,
        )
        .order_by(Job.first_seen.desc())
    ).all()
    return [
        j
        for j in candidates
        if passes_title(j.title, cfg)
        and passes_location(j.location, j.market, cfg)
        and passes_salary(j, cfg)
        and passes_company(j, cfg)
    ]
