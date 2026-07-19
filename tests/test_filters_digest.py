"""Filter + digest tests (E12): salary edge case, freshness, location rules,
digest rendering incl. unbadged-salary and empty-day message."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobpilot.digest import build_digest, mark_digested, render_job_message
from jobpilot.filters import (
    FilterConfig,
    filter_survivors,
    passes_location,
    passes_salary,
    passes_title,
)
from jobpilot.models import Base, FirstSeenSource, Job, JobStatus

NOW = datetime(2026, 7, 20, 12, 0)
_seq = iter(range(10_000))


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


@pytest.fixture
def cfg():
    c = FilterConfig.load("config")  # the real config files are the fixture
    c.salary_floor = {"india": {"currency": "INR", "amount": 1_500_000}}
    return c


def job(session, *, title="Backend Engineer", market="remote-intl", location="Remote",
        first_seen=NOW - timedelta(days=1), provenance=FirstSeenSource.API,
        status=JobStatus.OPEN, external_id=None, **kw):
    j = Job(source="greenhouse", external_id=external_id or f"j{next(_seq)}",
            company="Acme", title=title, market=market, location=location,
            url="https://x.example/1", first_seen=first_seen, first_seen_source=provenance,
            last_seen=NOW, status=status, **kw)
    session.add(j)
    session.flush()
    return j


# ---- individual gates ----

def test_title_gate_uses_roles_yaml(cfg):
    assert passes_title("Senior Backend Engineer", cfg)
    assert not passes_title("Engineering Manager", cfg)
    assert not passes_title("Software Engineer, React Native", cfg)


def test_location_india_rules(cfg):
    assert passes_location("Bengaluru", "india", cfg)
    assert passes_location("Remote", "india", cfg)
    assert not passes_location("Dubai, United Arab Emirates", "india", cfg)
    assert passes_location(None, "india", cfg)


def test_location_remote_intl_rules(cfg):
    assert passes_location("Remote (Europe)", "remote-intl", cfg)
    assert not passes_location("Amsterdam, Netherlands", "remote-intl", cfg)


def test_salary_floor_edge_cases(session, cfg):
    no_salary = job(session, market="india")
    below = job(session, market="india", salary_max=900_000, salary_currency="INR")
    above = job(session, market="india", salary_min=2_000_000, salary_currency="INR")
    other_currency = job(session, market="india", salary_max=50_000, salary_currency="USD")
    assert passes_salary(no_salary, cfg)  # missing salary PASSES (design edge case)
    assert not passes_salary(below, cfg)
    assert passes_salary(above, cfg)
    assert passes_salary(other_currency, cfg)  # cross-currency never hard-rejects


# ---- survivor query ----

def test_survivors_exclude_stale_closed_dup_old_digested(session, cfg):
    keep = job(session)
    job(session, status=JobStatus.CLOSED)
    job(session, first_seen=NOW - timedelta(days=10))  # too old
    dup_target = job(session)
    dup = job(session)
    dup.duplicate_of = dup_target.id
    done = job(session)
    done.digested_at = NOW - timedelta(days=1)
    session.flush()
    ids = {j.id for j in filter_survivors(session, cfg, NOW)}
    assert keep.id in ids and dup_target.id in ids
    assert len(ids) == 2


def test_survivors_are_freshest_first(session, cfg):
    older = job(session, first_seen=NOW - timedelta(days=3))
    newer = job(session, first_seen=NOW - timedelta(hours=2))
    got = filter_survivors(session, cfg, NOW)
    assert [j.id for j in got] == [newer.id, older.id]


# ---- digest rendering ----

def test_job_message_has_buttons_and_market_tag(session):
    j = job(session, market="india", location="Pune")
    msg = render_job_message(j, NOW)
    assert "Backend Engineer" in msg["text"] and "india" in msg["text"]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"ann:{j.id}:up"
    assert buttons[1]["callback_data"] == f"ann:{j.id}:down"


def test_salary_badge_and_unbadged(session):
    with_salary = job(session, salary_min=100_000, salary_max=140_000, salary_currency="USD")
    without = job(session)
    assert "💰 USD 100,000–140,000" in render_job_message(with_salary, NOW)["text"]
    assert "💰" not in render_job_message(without, NOW)["text"]


def test_age_line_api_vs_observed(session):
    api = job(session, first_seen=NOW - timedelta(hours=3))
    observed = job(session, provenance=FirstSeenSource.OBSERVED)
    assert "posted 3h ago" in render_job_message(api, NOW)["text"]
    assert "age unknown" in render_job_message(observed, NOW)["text"]


def test_html_is_escaped(session):
    j = job(session, title="C++ <senior> Engineer & Co")
    assert "<senior>" not in render_job_message(j, NOW)["text"]
    assert "&lt;senior&gt;" in render_job_message(j, NOW)["text"]


def test_empty_digest_sends_explicit_zero_message():
    msgs = build_digest([], NOW)
    assert len(msgs) == 1 and "0 new roles" in msgs[0]["text"]


def test_digest_has_header_plus_one_message_per_job(session):
    jobs = [job(session), job(session)]
    msgs = build_digest(jobs, NOW)
    assert len(msgs) == 3 and "2 new role(s)" in msgs[0]["text"]


def test_mark_digested_removes_from_next_run(session, cfg):
    j = job(session)
    survivors = filter_survivors(session, cfg, NOW)
    assert [s.id for s in survivors] == [j.id]
    mark_digested(session, survivors, NOW)
    assert filter_survivors(session, cfg, NOW) == []
