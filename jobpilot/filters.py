"""Hard filters (PROJECT.md §7 Stage 2 + design deltas).

Cheap gates only — volume control, not ranking. Policy lives in
config/filters.yaml and config/roles.yaml; nothing here is hardcoded.
Edge case pinned by the design: a posting with no parseable salary PASSES
the salary floor (most posts omit salary) and renders unbadged.
"""

from dataclasses import dataclass
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


def passes_title(title: str, cfg: FilterConfig) -> bool:
    t = title.lower()
    return any(k in t for k in cfg.title_include) and not any(k in t for k in cfg.title_exclude)


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
