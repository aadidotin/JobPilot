"""Cross-source dedupe (amendment E9).

Runs on every insert, both directions across any source pair. The same role
often arrives twice — a company's ATS board and a LinkedIn/Naukri mirror —
and linking them is what prevents double pings and double applications.

Canonical preference: ATS > LinkedIn > Indeed > Naukri, then earliest
first_seen. When the canonical flips (ATS twin arrives after a JobSpy row),
annotations and applications re-point to the new canonical row.
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobpilot.models import Annotation, Application, FirstSeenSource, Job

SOURCE_RANK = {"greenhouse": 0, "lever": 0, "ashby": 0, "linkedin": 1, "indeed": 2, "naukri": 3, "glassdoor": 4}
TITLE_MATCH_THRESHOLD = 90

_SENIORITY = re.compile(r"\b(senior|sr|junior|jr|staff|principal|lead|intermediate|i{1,3})\b")
_COMPANY_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co)\b")
_PUNCT = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")


def norm_title(title: str) -> str:
    t = _PUNCT.sub(" ", title.lower())
    t = _SENIORITY.sub(" ", t)
    return _WS.sub(" ", t).strip()


def norm_company(name: str) -> str:
    c = _PUNCT.sub(" ", name.lower())
    c = _COMPANY_SUFFIX.sub(" ", c)
    return _WS.sub(" ", c).strip()


def build_alias_lookup(companies: list[dict]) -> dict[str, str]:
    """normalized alias/name -> normalized canonical name, from companies.yaml."""
    lookup: dict[str, str] = {}
    for c in companies:
        canonical = norm_company(c["name"])
        lookup[canonical] = canonical
        for alias in c.get("aliases") or []:
            lookup[norm_company(alias)] = canonical
    return lookup


def _company_key(name: str, alias_lookup: dict[str, str]) -> str:
    n = norm_company(name)
    return alias_lookup.get(n, n)


def _locations_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return True
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a in b or b in a or "remote" in a or "remote" in b


@dataclass
class Match:
    other: Job
    ratio: float


def find_duplicate(session: Session, job: Job, alias_lookup: dict[str, str]) -> Job | None:
    """Best canonical-row match for `job` in OTHER sources, or None."""
    key = _company_key(job.company, alias_lookup)
    title = norm_title(job.title)
    candidates = session.scalars(
        select(Job).where(Job.duplicate_of.is_(None), Job.source != job.source, Job.id != job.id)
    ).all()
    matches: list[Match] = []
    for other in candidates:
        if _company_key(other.company, alias_lookup) != key:
            continue
        if not _locations_compatible(job.location, other.location):
            continue
        ratio = fuzz.ratio(title, norm_title(other.title))
        if ratio >= TITLE_MATCH_THRESHOLD:
            matches.append(Match(other, ratio))
    if not matches:
        return None
    matches.sort(key=lambda m: (-m.ratio, m.other.first_seen))
    return matches[0].other


def _rank(job: Job) -> tuple:
    return (SOURCE_RANK.get(job.source, 9), job.first_seen)


def link_duplicates(session: Session, a: Job, b: Job) -> Job:
    """Link the pair: lower rank wins canonical. Returns the canonical row.

    Migrates state (annotations, applications, duplicate chains) onto the
    canonical and merges first_seen under the provenance rule: 'api' wins
    outright; an observed date never backdates an API-timestamped row.
    """
    canonical, duplicate = (a, b) if _rank(a) <= _rank(b) else (b, a)

    duplicate.duplicate_of = canonical.id
    for chained in session.scalars(select(Job).where(Job.duplicate_of == duplicate.id)):
        chained.duplicate_of = canonical.id

    if canonical.first_seen_source == FirstSeenSource.API:
        pass  # api provenance is authoritative
    elif duplicate.first_seen_source == FirstSeenSource.API:
        canonical.first_seen = duplicate.first_seen
        canonical.first_seen_source = FirstSeenSource.API
    elif duplicate.first_seen < canonical.first_seen:
        canonical.first_seen = duplicate.first_seen

    if session.scalar(select(Annotation).where(Annotation.job_id == canonical.id)) is None:
        for ann in session.scalars(select(Annotation).where(Annotation.job_id == duplicate.id)):
            ann.job_id = canonical.id
    for app in session.scalars(select(Application).where(Application.job_id == duplicate.id)):
        app.job_id = canonical.id

    return canonical
