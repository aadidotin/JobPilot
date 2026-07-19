"""Bot business-logic tests (E12: callback handlers, send capture)."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jobpilot.bot import APPLIED_USAGE, list_applications, record_annotation, record_application
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
