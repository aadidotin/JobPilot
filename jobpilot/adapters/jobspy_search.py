"""JobSpy aggregator adapter — the breadth tier (LinkedIn / Indeed / Naukri).

Same dumb-adapter contract as the ATS adapters (E5): fetch, parse, normalize,
return PollResult. Two things differ from a board poll, both deliberate:

- One sweep fans out to several sites, so it returns one PollResult PER SITE.
  That keeps poll_log rows per-source, which is what E11's per-tier silence
  alerts read. `company` carries the sweep label, not a real employer.
- posted_at is always None, so every row lands as 'observed' provenance.
  Aggregator dates are day-granular and frequently wrong (reposts refresh
  them), and E9 forbids an observed date from backdating an api one. Recency
  is instead enforced at query time via hours_old.
"""

import math
import time
from dataclasses import dataclass, field

from jobpilot.adapters import NormalizedJob, PollResult

DEFAULT_RESULTS_WANTED = 25
DEFAULT_HOURS_OLD = 72
DEFAULT_BUDGET_SECONDS = 300

# Indeed abbreviates the country ("KA, IN"); our location filters spell it out.
COUNTRY_SUFFIX = {", IN": ", India", ", US": ", United States", ", GB": ", United Kingdom"}


@dataclass
class SweepSpec:
    """One market's aggregator sweep."""

    market: str
    sites: list[str]
    location: str | None = None
    country_indeed: str = "india"
    is_remote: bool = False
    results_wanted: int = DEFAULT_RESULTS_WANTED
    hours_old: int = DEFAULT_HOURS_OLD
    budget_seconds: int = DEFAULT_BUDGET_SECONDS
    search_terms: list[str] = field(default_factory=list)


def poll(spec: SweepSpec, search_terms: list[str] | None = None) -> list[PollResult]:
    """Run one sweep. Returns a PollResult per site, successful or not.

    A site that raises is reported as a failed poll for that site alone —
    LinkedIn rate-limiting must not discard Indeed's results.
    """
    terms = search_terms or spec.search_terms
    by_site: dict[str, list[NormalizedJob]] = {s: [] for s in spec.sites}
    errors: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    deadline = time.monotonic() + spec.budget_seconds

    for term in terms:
        for site in spec.sites:
            if site in errors:
                continue  # already failed this sweep; don't hammer it per-term
            if time.monotonic() > deadline:
                # E10: the 30-min run must not be held open by a slow scraper.
                # Cooperative, not preemptive — a killed worker thread would
                # keep running and block interpreter exit. Partial results are
                # kept, but the sweep is a FAILED poll so nothing status-flips.
                errors[site] = f"budget of {spec.budget_seconds}s exhausted"
                continue
            try:
                rows = _scrape(spec, site, term)
            except Exception as e:
                errors[site] = f"{type(e).__name__}: {e}"
                continue
            for row in rows:
                key = (site, row.external_id)
                if key in seen:
                    continue  # same posting matched by two search terms
                seen.add(key)
                by_site[site].append(row)

    label = f"{spec.market} sweep"
    return [
        PollResult(
            company=label,
            jobs=by_site[site],
            success=site not in errors,
            error=errors.get(site),
        )
        for site in spec.sites
    ]


def _scrape(spec: SweepSpec, site: str, term: str) -> list[NormalizedJob]:
    from jobspy import scrape_jobs

    df = scrape_jobs(
        site_name=[site],
        search_term=term,
        location=spec.location,
        is_remote=spec.is_remote,
        country_indeed=spec.country_indeed,
        results_wanted=spec.results_wanted,
        hours_old=spec.hours_old,
        description_format="markdown",
        enforce_annual_salary=True,  # filters.yaml floors are annual
        verbose=0,
    )
    if df is None or df.empty:
        return []
    return [j for j in (_normalize(site, r) for r in df.to_dict("records")) if j is not None]


def _clean(value) -> str | None:
    """DataFrame cells arrive as NaN/NaT/None for anything the site omitted."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _location(row: dict) -> str | None:
    loc = _clean(row.get("location"))
    if loc:
        for abbrev, full in COUNTRY_SUFFIX.items():
            if loc.endswith(abbrev):
                loc = loc[: -len(abbrev)] + full
                break
    if row.get("is_remote") is True:
        return f"Remote ({loc})" if loc else "Remote"
    return loc


def _normalize(site: str, row: dict) -> NormalizedJob | None:
    external_id = _clean(row.get("id"))
    title = _clean(row.get("title"))
    url = _clean(row.get("job_url"))
    company = _clean(row.get("company"))
    if not (external_id and title and url and company):
        return None  # unattributed rows can't dedupe or be applied to
    description = _clean(row.get("description"))
    return NormalizedJob(
        source=site,
        external_id=external_id,
        company=company,
        title=title,
        url=url,
        location=_location(row),
        salary_min=_int_or_none(row.get("min_amount")),
        salary_max=_int_or_none(row.get("max_amount")),
        salary_currency=_clean(row.get("currency")),
        description=description,
        description_partial=description is None,
        posted_at=None,  # always 'observed' — see module docstring
    )
