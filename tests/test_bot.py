"""Bot business-logic tests (E12: callback handlers, send capture)."""

from datetime import datetime

import asyncio

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jobpilot.bot import (
    APPLIED_USAGE,
    heartbeat,
    list_applications,
    liveness,
    ping_target,
    next_batch,
    parse_more_count,
    record_annotation,
    record_application,
    scrub,
)
from jobpilot.models import Annotation, Application, Base, Job

NOW = datetime(2026, 7, 20, 12, 0)
_seq = iter(range(10_000))


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def job(session, company="Acme", title="Backend Engineer"):
    j = Job(source="greenhouse", external_id=f"j{next(_seq)}", company=company,
            title=title, market="india", url="https://x.example/1",
            first_seen=NOW, first_seen_source="api", last_seen=NOW, status="open")
    session.add(j)
    session.flush()
    return j


def ann_count(session):
    return session.scalar(select(func.count()).select_from(Annotation))


# ---- annotations ----

def test_annotation_written_once(session):
    j = job(session)
    msg = record_annotation(session, j.id, "up")
    assert "👍" in msg and j.title in msg
    assert ann_count(session) == 1


def test_double_tap_stays_one_row(session):
    j = job(session)
    record_annotation(session, j.id, "up")
    record_annotation(session, j.id, "up")
    assert ann_count(session) == 1


def test_changed_verdict_updates_same_row(session):
    j = job(session)
    record_annotation(session, j.id, "up")
    record_annotation(session, j.id, "down")
    assert ann_count(session) == 1
    assert session.scalar(select(Annotation)).verdict == "down"


def test_unknown_job_id_is_graceful(session):
    assert "Unknown job" in record_annotation(session, 999, "up")
    assert ann_count(session) == 0


# ---- /applied send capture (C1) ----

def test_applied_minimal(session):
    msg = record_application(session, "Zerodha | Backend Engineer")
    app = session.scalar(select(Application))
    assert "✅" in msg
    assert app.company == "Zerodha" and app.title == "Backend Engineer"
    assert app.job_id is None and app.reply_channel is None
    assert app.status == "sent" and app.sent_at is not None


def test_applied_with_channel(session):
    record_application(session, "Zerodha | Backend Engineer | Email")
    assert session.scalar(select(Application)).reply_channel == "email"


def test_applied_links_to_ingested_job(session):
    j = job(session, company="Acme", title="Senior Backend Engineer")
    msg = record_application(session, "Acme Inc | Backend Engineer")
    app = session.scalar(select(Application))
    assert app.job_id == j.id and "linked" in msg


def test_applied_does_not_link_wrong_company(session):
    job(session, company="Umbrella", title="Backend Engineer")
    record_application(session, "Acme | Backend Engineer")
    assert session.scalar(select(Application)).job_id is None


def test_list_applications_empty(session):
    assert "No applications logged yet" in list_applications(session)


def test_list_applications_newest_first(session):
    record_application(session, "Acme | Backend Engineer | email")
    record_application(session, "Umbrella | Python Developer")
    out = list_applications(session)
    assert "2 total" in out
    assert out.index("Umbrella") < out.index("Acme")  # newest first
    assert "via email" in out and "[sent]" in out


def test_applied_bad_format_shows_usage(session):
    for bad in ("", "just a company", "| title only"):
        assert record_application(session, bad) == APPLIED_USAGE
    assert session.scalar(select(func.count()).select_from(Application)) == 0


# ---- /more: draining the queue on demand ----

def test_parse_more_count_defaults_and_clamps():
    assert parse_more_count("") == 10
    assert parse_more_count("garbage") == 10
    assert parse_more_count("3") == 3
    assert parse_more_count(" 7 ") == 7
    assert parse_more_count("999") == 30   # clamped to MORE_MAX
    assert parse_more_count("0") == 1
    assert parse_more_count("-5") == 1


def queued_job(session, **over):
    base = dict(source="greenhouse", external_id=f"q{next(_seq)}", company="Acme",
                title="Python Developer", market="india", location="Bengaluru, India",
                url="https://x.example/1", first_seen=NOW, first_seen_source="api",
                last_seen=NOW, status="open")
    j = Job(**{**base, **over})
    session.add(j)
    session.flush()
    return j


def test_next_batch_returns_limit_and_remaining(session):
    for _ in range(12):
        queued_job(session)
    batch, remaining = next_batch(session, 5, NOW)
    assert len(batch) == 5 and remaining == 7


def test_next_batch_skips_already_digested(session):
    shown = queued_job(session)
    shown.digested_at = NOW
    queued_job(session)
    batch, remaining = next_batch(session, 10, NOW)
    assert len(batch) == 1 and remaining == 0
    assert batch[0].id != shown.id


def test_next_batch_empty_queue(session):
    batch, remaining = next_batch(session, 10, NOW)
    assert batch == [] and remaining == 0


# ---- liveness ping (T3) ----
#
# Driven with asyncio.run rather than pytest-asyncio: one heartbeat is not
# worth a new test dependency.

TOKEN = "123456:FAKE-TOKEN-VALUE"


class FakeUpdater:
    def __init__(self, running=True):
        self.running = running


class FakeBot:
    def __init__(self, username="jobpilot_bot", error=None):
        self.username = username
        self.error = error

    async def get_me(self):
        if self.error:
            raise self.error
        return type("Me", (), {"username": self.username})()


class FakeApp:
    def __init__(self, updater=FakeUpdater(), bot=None):
        self.updater = updater
        self.bot = bot or FakeBot()


def test_scrub_removes_the_token():
    leaky = f"ConnectError: POST https://api.telegram.org/bot{TOKEN}/getMe"
    assert TOKEN not in scrub(leaky, TOKEN)
    assert "<token>" in scrub(leaky, TOKEN)


def test_ping_target_uses_fail_endpoint_when_unhealthy():
    assert ping_target("https://hc-ping.com/uuid", True) == "https://hc-ping.com/uuid"
    assert ping_target("https://hc-ping.com/uuid/", False) == "https://hc-ping.com/uuid/fail"


def test_liveness_healthy():
    ok, detail = asyncio.run(liveness(FakeApp(), TOKEN))
    assert ok and "jobpilot_bot" in detail


def test_liveness_fails_when_updater_stopped():
    ok, detail = asyncio.run(liveness(FakeApp(updater=FakeUpdater(running=False)), TOKEN))
    assert not ok and "not polling" in detail


def test_liveness_fails_when_api_rejects_the_token():
    """The failure systemd cannot see: process up, token dead."""
    app = FakeApp(bot=FakeBot(error=RuntimeError(f"Unauthorized for bot{TOKEN}")))
    ok, detail = asyncio.run(liveness(app, TOKEN))
    assert not ok
    assert TOKEN not in detail  # must not leak into the ping body


def _run_heartbeat(app, url, pings, rounds=2, post=None):
    calls = {"n": 0}

    async def sleep(_):
        calls["n"] += 1
        if calls["n"] > rounds:
            raise asyncio.CancelledError

    async def record(target, detail):
        pings.append((target, detail))

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await heartbeat(app, url, TOKEN, sleep=sleep, post=post or record)

    asyncio.run(go())


def test_heartbeat_pings_success_while_healthy():
    pings = []
    _run_heartbeat(FakeApp(), "https://hc-ping.com/uuid", pings)
    assert len(pings) == 2
    assert all(target == "https://hc-ping.com/uuid" for target, _ in pings)


def test_heartbeat_pings_fail_when_dead():
    pings = []
    _run_heartbeat(FakeApp(updater=FakeUpdater(running=False)), "https://hc-ping.com/uuid", pings)
    assert all(target.endswith("/fail") for target, _ in pings)


def test_heartbeat_survives_a_ping_outage():
    """The monitor being down must never take the bot down with it."""
    pings = []

    async def flaky(target, detail):
        pings.append(target)
        raise OSError("network unreachable")

    _run_heartbeat(FakeApp(), "https://hc-ping.com/uuid", pings, rounds=3, post=flaky)
    assert len(pings) == 3  # kept looping through every failure
