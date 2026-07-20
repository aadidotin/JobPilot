"""Pipeline tests — window scheduling, leg wiring, E11 silence, and the
digest's all-or-nothing delivery guarantee. No network: adapters and Telegram
are stubbed, the DB is a temp file.
"""

from datetime import datetime, timedelta

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobpilot import pipeline
from jobpilot.adapters import NormalizedJob, PollResult
from jobpilot.models import Base, Job, PollLog

NOW = datetime(2026, 7, 20, 20, 30)
SCHEDULE = {"jobspy_hours": [9, 13, 19], "digest_hours": [20],
            "ats_timeout_seconds": 30, "jobspy_budget_seconds": 300, "silence_alert_hours": 24}
_seq = iter(range(10_000))


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


class FakeTelegram:
    def __init__(self, fail_after: int | None = None):
        self.sent: list[dict] = []
        self.fail_after = fail_after

    def send(self, payload, client=None):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append(payload)
        return True

    def send_all(self, payloads):
        return sum(1 for p in payloads if self.send(p))


# ---- wall-clock windows ----

def test_window_not_due_before_its_hour():
    assert pipeline.window_due("digest", [20], datetime(2026, 7, 20, 19, 59), {}) is None


def test_window_due_at_its_hour():
    assert pipeline.window_due("digest", [20], NOW, {}) == "2026-07-20T20"


def test_window_fires_once_per_day():
    state = {"digest": "2026-07-20T20"}
    assert pipeline.window_due("digest", [20], NOW, state) is None
    assert pipeline.window_due("digest", [20], NOW + timedelta(days=1), state) == "2026-07-21T20"


def test_missed_windows_collapse_to_one_catchup():
    """Laptop asleep through 09:00 and 13:00 — waking at 14:00 owes ONE sweep."""
    state = {}
    key = pipeline.window_due("jobspy", [9, 13, 19], datetime(2026, 7, 20, 14, 0), state)
    assert key == "2026-07-20T13"
    state["jobspy"] = key
    assert pipeline.window_due("jobspy", [9, 13, 19], datetime(2026, 7, 20, 14, 30), state) is None
    # ...and the 19:00 window still fires normally later that day.
    assert pipeline.window_due("jobspy", [9, 13, 19], datetime(2026, 7, 20, 19, 5), state) == "2026-07-20T19"


def test_forced_legs_bypass_windows():
    early = datetime(2026, 7, 20, 3, 0)
    assert pipeline.jobspy_due(SCHEDULE, early, {}, force=True)
    assert pipeline.digest_due(SCHEDULE, early, {}, force=True)
    assert pipeline.jobspy_due(SCHEDULE, early, {}, force=False) is None


def test_corrupt_state_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "state_path", lambda: tmp_path / "s.json")
    (tmp_path / "s.json").write_text("{not json")
    assert pipeline.load_state() == {}


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "state_path", lambda: tmp_path / "s.json")
    pipeline.save_state({"digest": "2026-07-20T20"})
    assert pipeline.load_state() == {"digest": "2026-07-20T20"}


# ---- ATS leg ----

def nj(**over):
    base = dict(source="greenhouse", external_id=f"g{next(_seq)}", company="Acme",
                title="Python Developer", url="https://x.example/1", location="Bengaluru, India",
                posted_at=datetime(2026, 7, 20, 8, 0))
    return NormalizedJob(**{**base, **over})


def stub_adapter(monkeypatch, result: PollResult):
    class Stub:
        @staticmethod
        def poll(company, slug, client):
            return result

    monkeypatch.setitem(pipeline.ADAPTERS, "greenhouse", Stub)


def test_ats_leg_ingests_and_counts(session, monkeypatch):
    stub_adapter(monkeypatch, PollResult("Acme", [nj(), nj()]))
    report = pipeline.RunReport(started_at=NOW)
    companies = [{"name": "Acme", "ats": "greenhouse", "slug": "acme", "market": "india"}]
    pipeline.run_ats(session, companies, {}, 30, report)
    assert report.ats_boards == 1 and report.new_jobs == 2
    assert report.ats_failures == []
    assert session.scalar(select(Job).where(Job.title == "Python Developer")) is not None


def test_failed_board_is_reported_not_raised(session, monkeypatch):
    stub_adapter(monkeypatch, PollResult("Acme", [], success=False, error="HTTP 500"))
    report = pipeline.RunReport(started_at=NOW)
    companies = [{"name": "Acme", "ats": "greenhouse", "slug": "acme", "market": "india"}]
    pipeline.run_ats(session, companies, {}, 30, report)
    assert report.ats_failures == ["Acme: HTTP 500"]


def test_unknown_ats_does_not_abort_the_run(session, monkeypatch):
    stub_adapter(monkeypatch, PollResult("Acme", [nj()]))
    report = pipeline.RunReport(started_at=NOW)
    companies = [
        {"name": "Weird", "ats": "workday", "slug": "w", "market": "india"},
        {"name": "Acme", "ats": "greenhouse", "slug": "acme", "market": "india"},
    ]
    pipeline.run_ats(session, companies, {}, 30, report)
    assert report.new_jobs == 1  # the good board still ran
    assert "unknown ats" in report.ats_failures[0]


# ---- JobSpy leg ----

def test_jobspy_leg_tags_each_site_correctly(session, monkeypatch):
    def fake_poll(spec, terms):
        return [
            PollResult("india sweep", [nj(source="indeed", external_id="in-1")]),
            PollResult("india sweep", [], success=False, error="429"),
        ]

    monkeypatch.setattr(pipeline, "jobspy_poll", fake_poll)
    report = pipeline.RunReport(started_at=NOW)
    sweeps = [{"market": "india", "sites": ["indeed", "linkedin"], "location": "India"}]
    pipeline.run_jobspy(session, sweeps, ["Python Developer"], {}, 300, report)

    assert report.jobspy_ran and report.new_jobs == 1
    assert report.jobspy_failures == ["india/linkedin: 429"]
    logs = {p.source: p.success for p in session.scalars(select(PollLog))}
    assert logs == {"indeed": True, "linkedin": False}  # per-site poll_log for E11


# ---- digest delivery ----

def make_job(session, **over):
    base = dict(source="greenhouse", external_id=f"j{next(_seq)}", company="Acme",
                title="Python Developer", market="india", location="Bengaluru, India",
                url="https://x.example/1", first_seen=NOW, first_seen_source="api",
                last_seen=NOW, status="open")
    job = Job(**{**base, **over})
    session.add(job)
    session.flush()
    return job


def test_digest_marks_rows_only_after_full_delivery(session):
    from jobpilot.filters import FilterConfig

    cfg = FilterConfig(title_include=["python"], title_exclude=[], freshness_days=7,
                       salary_floor={}, company_blocklist=set(), location={})
    make_job(session)
    session.commit()
    report = pipeline.RunReport(started_at=NOW)
    telegram = FakeTelegram(fail_after=1)  # header lands, the job message does not
    pipeline.run_digest(session, cfg, NOW, telegram, report)

    assert report.digest_sent == 1
    assert "partially delivered" in report.alerts[0]
    assert session.scalar(select(Job)).digested_at is None  # re-offered next run


def test_digest_cap_holds_overflow_for_tomorrow(session):
    """The flood guard: a cold start must not send one message per survivor."""
    from jobpilot.filters import FilterConfig

    cfg = FilterConfig(title_include=["python"], title_exclude=[], freshness_days=7,
                       salary_floor={}, company_blocklist=set(), location={})
    for i in range(10):
        make_job(session, first_seen=NOW - timedelta(hours=i))  # freshest first
    session.commit()
    report = pipeline.RunReport(started_at=NOW)
    telegram = FakeTelegram()
    pipeline.run_digest(session, cfg, NOW, telegram, report, limit=3)

    assert report.digest_sent == 4  # header + 3 jobs
    assert report.digest_held == 7
    assert "7 more held for tomorrow" in telegram.sent[0]["text"]
    digested = session.scalars(select(Job).where(Job.digested_at.isnot(None))).all()
    assert len(digested) == 3  # held rows stay eligible
    assert min(j.first_seen for j in digested) == NOW - timedelta(hours=2)  # freshest 3


def test_digest_cap_does_not_starve_the_ats_tier(session):
    """Regression: aggregator rows are 'observed', so their first_seen is ingest
    time and freshest-first alone hands them the entire capped digest."""
    from jobpilot.filters import FilterConfig

    cfg = FilterConfig(title_include=["python"], title_exclude=[], freshness_days=7,
                       salary_floor={}, company_blocklist=set(), location={})
    for i in range(20):  # aggregator rows, all ingested "now"
        make_job(session, source="linkedin", market="remote-intl",
                 first_seen=NOW, first_seen_source="observed")
    for i in range(20):  # ATS rows carrying real, older posting dates
        make_job(session, source="greenhouse", market="india",
                 first_seen=NOW - timedelta(days=2, hours=i))
    session.commit()
    report = pipeline.RunReport(started_at=NOW)
    pipeline.run_digest(session, cfg, NOW, FakeTelegram(), report, limit=10)

    digested = session.scalars(select(Job).where(Job.digested_at.isnot(None))).all()
    assert {j.source for j in digested} == {"linkedin", "greenhouse"}
    assert len([j for j in digested if j.source == "greenhouse"]) == 5  # even split


def test_digest_marks_rows_on_full_delivery(session):
    from jobpilot.filters import FilterConfig

    cfg = FilterConfig(title_include=["python"], title_exclude=[], freshness_days=7,
                       salary_floor={}, company_blocklist=set(), location={})
    make_job(session)
    session.commit()
    report = pipeline.RunReport(started_at=NOW)
    pipeline.run_digest(session, cfg, NOW, FakeTelegram(), report)
    assert report.digest_sent == 2  # header + one job
    assert session.scalar(select(Job)).digested_at is not None


# ---- E11 silence ----

def log_poll(session, source, jobs_seen, polled_at, success=True):
    session.add(PollLog(source=source, company="x", polled_at=polled_at,
                        success=success, jobs_seen=jobs_seen))
    session.flush()


def test_tier_that_polled_but_saw_nothing_is_silent(session):
    log_poll(session, "greenhouse", 0, NOW)
    log_poll(session, "indeed", 12, NOW)
    assert pipeline.silent_tiers(session, NOW, 24) == ["ats"]


def test_productive_tiers_are_not_silent(session):
    log_poll(session, "greenhouse", 5, NOW)
    log_poll(session, "indeed", 12, NOW)
    assert pipeline.silent_tiers(session, NOW, 24) == []


def test_tier_that_never_polled_is_not_reported_as_silent(session):
    """'Didn't run' belongs to the dead-man's ping; this alert is 'ran blind'."""
    log_poll(session, "greenhouse", 5, NOW)
    assert pipeline.silent_tiers(session, NOW, 24) == []


def test_old_polls_fall_outside_the_window(session):
    log_poll(session, "greenhouse", 99, NOW - timedelta(hours=48))
    log_poll(session, "greenhouse", 0, NOW)
    assert pipeline.silent_tiers(session, NOW, 24) == ["ats"]


def test_tier_mapping():
    assert pipeline.tier_of("greenhouse") == "ats"
    assert pipeline.tier_of("ashby") == "ats"
    assert pipeline.tier_of("linkedin") == "aggregator"
    assert pipeline.tier_of("naukri") == "aggregator"


# ---- config sanity ----

def test_shipped_schedule_config_has_every_key_the_code_reads():
    schedule = yaml.safe_load(open("config/schedule.yaml"))
    for key in ("jobspy_hours", "digest_hours", "ats_timeout_seconds",
                "jobspy_budget_seconds", "silence_alert_hours", "digest_max"):
        assert key in schedule, key
    assert 0 < schedule["digest_max"] <= 50, "an uncapped digest floods the phone"


def test_shipped_roles_config_defines_jobspy_sweeps():
    roles = yaml.safe_load(open("config/roles.yaml"))
    markets = {s["market"] for s in roles["jobspy"]}
    assert markets == {"india", "remote-intl"}
    for sweep in roles["jobspy"]:
        assert sweep["sites"]
