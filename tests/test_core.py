"""Pipeline-core tests (E12): seeding provenance, status-flip guard,
dedupe with state migration + tiebreaks. In-memory SQLite per test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobpilot.adapters import NormalizedJob, PollResult
from jobpilot.core import ingest, mark_jobspy_stale, utcnow
from jobpilot.dedupe import build_alias_lookup
from jobpilot.models import Annotation, Application, Base, Job


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def nj(source="greenhouse", external_id="1", company="Acme", title="Backend Engineer",
       location="Remote", posted_at=None, **kw):
    return NormalizedJob(source=source, external_id=external_id, company=company,
                         title=title, url=f"https://x.example/{external_id}",
                         location=location, posted_at=posted_at, **kw)


def poll(jobs, company="Acme", success=True, error=None):
    return PollResult(company=company, jobs=jobs, success=success, error=error)


def get_job(session, external_id, source="greenhouse"):
    return session.scalar(select(Job).where(Job.external_id == external_id, Job.source == source))


# ---- first_seen seeding (E1) ----

def test_api_timestamp_seeds_api_provenance(session):
    ts = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    ingest(session, poll([nj(posted_at=ts)]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.first_seen_source == "api"
    assert job.first_seen == datetime(2026, 7, 15, 10, 0)


def test_no_timestamp_seeds_observed_now(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.first_seen_source == "observed"
    assert abs((job.first_seen - utcnow()).total_seconds()) < 5


def test_reseen_job_updates_last_seen_not_first_seen(session):
    ts = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    ingest(session, poll([nj(posted_at=ts)]), "greenhouse", "remote-intl")
    first = get_job(session, "1").first_seen
    ingest(session, poll([nj(posted_at=ts, title="Backend Engineer (updated)")]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.first_seen == first
    assert job.title == "Backend Engineer (updated)"


# ---- status-flip guard (E1) ----

def test_absent_once_stays_open(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.status == "open" and job.absent_polls == 1


def test_absent_twice_closes(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    stats = ingest(session, poll([]), "greenhouse", "remote-intl")
    assert get_job(session, "1").status == "closed"
    assert stats.closed == 1


def test_failed_poll_never_flips(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    for _ in range(5):
        ingest(session, poll([], success=False, error="Cloudflare 1020"), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.status == "open" and job.absent_polls == 0


def test_absent_then_present_resets_counter(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.status == "open" and job.absent_polls == 1


def test_closed_job_reappearing_reopens(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    ingest(session, poll([]), "greenhouse", "remote-intl")
    assert get_job(session, "1").status == "closed"
    ingest(session, poll([nj()]), "greenhouse", "remote-intl")
    job = get_job(session, "1")
    assert job.status == "open" and job.absent_polls == 0


def test_flip_guard_scoped_to_board(session):
    ingest(session, poll([nj()], company="Acme"), "greenhouse", "remote-intl")
    ingest(session, poll([nj(external_id="9", company="Umbrella")], company="Umbrella"), "greenhouse", "remote-intl")
    for _ in range(2):
        ingest(session, poll([], company="Umbrella"), "greenhouse", "remote-intl")
    assert get_job(session, "1").status == "open"  # Acme board untouched
    assert get_job(session, "9").status == "closed"


# ---- JobSpy lifecycle (E1 source-tier scope) ----

def test_jobspy_rows_never_flip_on_polls(session):
    ingest(session, poll([nj(source="linkedin", external_id="li-1")]), "linkedin", "india")
    for _ in range(3):
        ingest(session, poll([]), "linkedin", "india")
    assert get_job(session, "li-1", "linkedin").status == "open"


def test_jobspy_stale_after_14_days(session):
    ingest(session, poll([nj(source="linkedin", external_id="li-1")]), "linkedin", "india")
    assert mark_jobspy_stale(session, now=utcnow() + timedelta(days=15)) == 1
    assert get_job(session, "li-1", "linkedin").status == "stale"
    assert mark_jobspy_stale(session, now=utcnow() + timedelta(days=13)) == 0


# ---- dedupe (E9) ----

ALIASES = build_alias_lookup([{"name": "Acme", "aliases": ["Acme Inc", "Acme Software"]}])


def _ats_and_linkedin(session, first, second):
    """Insert two matching rows in the given order; return (ats, linkedin)."""
    order = {"ats": poll([nj(posted_at=datetime(2026, 7, 15, tzinfo=UTC))]),
             "li": poll([nj(source="linkedin", external_id="li-1", title="Sr. Backend Engineer")])}
    ingest(session, order[first], "greenhouse" if first == "ats" else "linkedin",
           "remote-intl", ALIASES)
    ingest(session, order[second], "greenhouse" if second == "ats" else "linkedin",
           "remote-intl", ALIASES)
    return get_job(session, "1"), get_job(session, "li-1", "linkedin")


def test_dedupe_ats_canonical_when_jobspy_arrives_second(session):
    ats, li = _ats_and_linkedin(session, "ats", "li")
    assert li.duplicate_of == ats.id and ats.duplicate_of is None


def test_dedupe_ats_canonical_when_jobspy_arrived_first(session):
    ats, li = _ats_and_linkedin(session, "li", "ats")
    assert li.duplicate_of == ats.id and ats.duplicate_of is None


def test_canonical_flip_migrates_annotation(session):
    ingest(session, poll([nj(source="linkedin", external_id="li-1")]), "linkedin", "remote-intl", ALIASES)
    li = get_job(session, "li-1", "linkedin")
    session.add(Annotation(job_id=li.id, verdict="up", created_at=utcnow()))
    session.flush()
    ingest(session, poll([nj()]), "greenhouse", "remote-intl", ALIASES)
    ats = get_job(session, "1")
    ann = session.scalar(select(Annotation))
    assert ann.job_id == ats.id


def test_canonical_flip_migrates_application(session):
    ingest(session, poll([nj(source="naukri", external_id="nk-1", company="Acme Software")]), "naukri", "india", ALIASES)
    nk = get_job(session, "nk-1", "naukri")
    session.add(Application(job_id=nk.id, company="Acme", title="Backend Engineer", sent_at=utcnow()))
    session.flush()
    ingest(session, poll([nj()]), "greenhouse", "india", ALIASES)
    app = session.scalar(select(Application))
    assert app.job_id == get_job(session, "1").id


def test_alias_matching_links_different_company_names(session):
    ingest(session, poll([nj(source="naukri", external_id="nk-1", company="Acme Software")]), "naukri", "india", ALIASES)
    ingest(session, poll([nj()]), "greenhouse", "india", ALIASES)
    assert get_job(session, "nk-1", "naukri").duplicate_of == get_job(session, "1").id


def test_incompatible_locations_block_merge(session):
    ingest(session, poll([nj(location="Bangalore")]), "greenhouse", "india", ALIASES)
    ingest(session, poll([nj(source="linkedin", external_id="li-1", location="New York")]), "linkedin", "india", ALIASES)
    assert get_job(session, "li-1", "linkedin").duplicate_of is None


def test_different_titles_do_not_merge(session):
    ingest(session, poll([nj()]), "greenhouse", "india", ALIASES)
    ingest(session, poll([nj(source="linkedin", external_id="li-1", title="Product Designer")]), "linkedin", "india", ALIASES)
    assert get_job(session, "li-1", "linkedin").duplicate_of is None


def test_api_first_seen_never_backdated_by_jobspy(session):
    old = datetime(2026, 6, 1, tzinfo=UTC)
    api_ts = datetime(2026, 7, 15, tzinfo=UTC)
    ingest(session, poll([nj(source="linkedin", external_id="li-1", posted_at=old)]), "linkedin", "remote-intl", ALIASES)
    ingest(session, poll([nj(posted_at=api_ts)]), "greenhouse", "remote-intl", ALIASES)
    ats = get_job(session, "1")
    assert ats.first_seen == datetime(2026, 7, 15) and ats.first_seen_source == "api"


def test_observed_canonical_adopts_api_timestamp_and_provenance(session):
    ingest(session, poll([nj()]), "greenhouse", "remote-intl", ALIASES)  # no timestamp -> observed
    api_ts = datetime(2026, 7, 10, tzinfo=UTC)
    ingest(session, poll([nj(source="lever", external_id="lv-1", posted_at=api_ts)]), "lever", "remote-intl", ALIASES)
    canonical = session.scalar(select(Job).where(Job.duplicate_of.is_(None)))
    assert canonical.first_seen == datetime(2026, 7, 10)
    assert canonical.first_seen_source == "api"


def test_tiebreak_prefers_higher_ratio(session):
    ingest(session, poll([nj(external_id="1", title="Backend Engineer"),
                          nj(external_id="2", title="Backend Engineer, Platform")]),
           "greenhouse", "remote-intl", ALIASES)
    ingest(session, poll([nj(source="linkedin", external_id="li-1", title="Backend Engineer")]), "linkedin", "remote-intl", ALIASES)
    assert get_job(session, "li-1", "linkedin").duplicate_of == get_job(session, "1").id
