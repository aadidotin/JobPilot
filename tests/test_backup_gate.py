"""Backup round-trip and weekend-1 gate arithmetic.

The backup tests deliberately do a REAL gpg encrypt/decrypt and reopen the
restored file as a database. A backup that has never been restored is not
known to be a backup.
"""

import shutil
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobpilot import backup as backup_mod
from jobpilot.gate import TARGET_THUMBS_UP_PER_WEEK, build_report, render
from jobpilot.models import Annotation, Base, Job, PollLog

NOW = datetime(2026, 8, 3, 12, 0)
_seq = iter(range(10_000))
needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


@pytest.fixture
def live_db(tmp_path):
    """A real on-disk database with a row in it."""
    path = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        s.add(Job(source="greenhouse", external_id="x1", company="Acme", title="Python Developer",
                  market="india", url="https://x.example/1", first_seen=NOW,
                  first_seen_source="api", last_seen=NOW, status="open"))
        s.commit()
    engine.dispose()
    return path


@pytest.fixture
def key(tmp_path):
    return backup_mod.ensure_key(tmp_path / "backup.key")


# ---- backup ----

def test_key_is_generated_once_and_owner_only(tmp_path):
    path = tmp_path / "k.key"
    first = backup_mod.ensure_key(path).read_text()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert backup_mod.ensure_key(path).read_text() == first  # not regenerated


def test_snapshot_of_a_live_db_is_readable(live_db, tmp_path):
    out = backup_mod.snapshot(live_db, tmp_path / "snap.db")
    engine = create_engine(f"sqlite:///{out}")
    with sessionmaker(bind=engine)() as s:
        assert s.scalar(select(Job)).title == "Python Developer"
    engine.dispose()


@needs_gpg
def test_backup_restore_round_trip(live_db, tmp_path, key):
    archive = backup_mod.backup(live_db, tmp_path / "backups", now=NOW, key_path=key)
    assert archive.exists() and archive.suffix == ".gpg"
    assert oct(archive.stat().st_mode)[-3:] == "600"

    restored = backup_mod.restore(archive, tmp_path / "restored.db", key_path=key)
    engine = create_engine(f"sqlite:///{restored}")
    with sessionmaker(bind=engine)() as s:
        assert s.scalar(select(Job)).company == "Acme"
    engine.dispose()


@needs_gpg
def test_archive_is_not_plaintext(live_db, tmp_path, key):
    """The whole point of CEO T4: company names must not be readable at rest."""
    archive = backup_mod.backup(live_db, tmp_path / "backups", now=NOW, key_path=key)
    assert b"Acme" not in archive.read_bytes()


@needs_gpg
def test_wrong_key_cannot_decrypt(live_db, tmp_path, key):
    archive = backup_mod.backup(live_db, tmp_path / "backups", now=NOW, key_path=key)
    other = backup_mod.ensure_key(tmp_path / "other.key")
    with pytest.raises(RuntimeError):
        backup_mod.restore(archive, tmp_path / "nope.db", key_path=other)


def test_prune_keeps_the_newest_n(tmp_path):
    for i in range(20):
        (tmp_path / f"jobpilot-2026080{i//10}T0{i%10}0000.db.gz.gpg").write_text("x")
    removed = backup_mod.prune(tmp_path, keep_days=5)
    survivors = sorted(p.name for p in tmp_path.glob("jobpilot-*.gpg"))
    assert removed == 15 and len(survivors) == 5
    assert survivors[-1].endswith("T090000.db.gz.gpg")  # newest kept


def test_prune_is_count_based_not_age_based(tmp_path):
    """A laptop off for a month must not wake and delete every backup."""
    for i in range(3):
        (tmp_path / f"jobpilot-2020010{i}T000000.db.gz.gpg").write_text("ancient")
    assert backup_mod.prune(tmp_path, keep_days=14) == 0
    assert len(list(tmp_path.glob("*.gpg"))) == 3


# ---- gate ----

def annotate(session, verdict, when, market="india", source="greenhouse"):
    job = Job(source=source, external_id=f"j{next(_seq)}", company="Acme", title="Python Developer",
              market=market, url="https://x.example/1", first_seen=when,
              first_seen_source="api", last_seen=when, status="open", digested_at=when)
    session.add(job)
    session.flush()
    session.add(Annotation(job_id=job.id, verdict=verdict, created_at=when))
    session.flush()
    return job


def test_gate_fails_with_no_annotations(session):
    report = build_report(session, now=NOW)
    assert not report.passes
    assert all(w.up == 0 for w in report.weeks)
    assert "does not pass yet" in render(report)


def test_gate_passes_when_both_weeks_clear_the_bar(session):
    for week_offset in (1, 8):  # one day inside each of the two weeks
        for _ in range(TARGET_THUMBS_UP_PER_WEEK):
            annotate(session, "up", NOW - timedelta(days=week_offset))
    report = build_report(session, now=NOW)
    assert report.passes and "PASSES" in render(report)


def test_one_strong_week_does_not_pass_the_gate(session):
    """16 in one week and 0 in the next is enthusiasm, not sustained signal."""
    for _ in range(TARGET_THUMBS_UP_PER_WEEK * 2):
        annotate(session, "up", NOW - timedelta(days=1))
    report = build_report(session, now=NOW)
    assert not report.passes


def test_thumbs_down_do_not_count_toward_the_target(session):
    for _ in range(TARGET_THUMBS_UP_PER_WEEK):
        annotate(session, "down", NOW - timedelta(days=1))
    report = build_report(session, now=NOW)
    assert not report.passes
    assert report.weeks[-1].down == TARGET_THUMBS_UP_PER_WEEK


def test_counts_split_by_market_and_tier(session):
    annotate(session, "up", NOW - timedelta(days=1), market="india", source="greenhouse")
    annotate(session, "up", NOW - timedelta(days=1), market="india", source="linkedin")
    annotate(session, "up", NOW - timedelta(days=1), market="remote-intl", source="linkedin")
    recent = build_report(session, now=NOW).weeks[-1]
    assert recent.by_cell[("india", "ats")] == 1
    assert recent.by_cell[("india", "aggregator")] == 1
    assert recent.by_cell[("remote-intl", "aggregator")] == 1


def shown_only(session, market, source, when):
    """A role that was delivered but never annotated."""
    job = Job(source=source, external_id=f"s{next(_seq)}", company="Acme", title="Python Developer",
              market=market, url="https://x.example/1", first_seen=when, first_seen_source="api",
              last_seen=when, status="open", digested_at=when)
    session.add(job)
    session.flush()
    return job


def test_starved_cell_is_shown_but_never_liked(session):
    """E6's question: a cell we surfaced roles from that earns no 👍."""
    for _ in range(TARGET_THUMBS_UP_PER_WEEK):
        annotate(session, "up", NOW - timedelta(days=1), market="india", source="linkedin")
    shown_only(session, "india", "greenhouse", NOW - timedelta(days=1))
    report = build_report(session, now=NOW)
    assert ("india", "ats") in report.starved_cells
    assert "rebalance sources" in render(report)


def test_cell_that_delivered_nothing_is_silent_not_starved(session):
    """The India/ATS case: you cannot dislike roles you were never shown."""
    annotate(session, "up", NOW - timedelta(days=1), market="india", source="linkedin")
    report = build_report(session, now=NOW)
    assert ("india", "ats") in report.silent_cells
    assert ("india", "ats") not in report.starved_cells
    text = render(report)
    assert "Delivered NOTHING" in text
    assert "grow config/companies.yaml" in text


def test_funnel_reports_every_cell_even_empty_ones(session):
    annotate(session, "up", NOW - timedelta(days=1), market="india", source="linkedin")
    text = render(build_report(session, now=NOW))
    for market in ("india", "remote-intl"):
        for tier in ("ats", "aggregator"):
            assert f"{market:12} {tier:11}" in text


def test_annotations_outside_the_window_are_ignored(session):
    for _ in range(TARGET_THUMBS_UP_PER_WEEK):
        annotate(session, "up", NOW - timedelta(days=30))
    report = build_report(session, now=NOW)
    assert all(w.up == 0 for w in report.weeks)


def test_polling_history_below_c10_floor_is_flagged(session):
    session.add(PollLog(source="greenhouse", company="Acme", polled_at=NOW - timedelta(days=3),
                        success=True, jobs_seen=10))
    session.flush()
    report = build_report(session, now=NOW)
    assert report.poll_days == 3
    assert "below the C10 floor" in render(report)


def test_mature_polling_history_is_not_flagged(session):
    session.add(PollLog(source="greenhouse", company="Acme", polled_at=NOW - timedelta(days=40),
                        success=True, jobs_seen=10))
    session.flush()
    assert "below the C10 floor" not in render(build_report(session, now=NOW))


def test_no_annotations_reports_nothing_starved(session):
    """With zero signal, every cell is trivially empty — 'rebalance sources'
    would be wrong advice. The answer is to annotate, and the report says so."""
    report = build_report(session, now=NOW)
    assert report.starved_cells == []
    text = render(report)
    assert "rebalance sources" not in text
    assert "No 👍 yet" in text


def test_starvation_is_only_reported_against_real_signal(session):
    annotate(session, "up", NOW - timedelta(days=1), market="india", source="linkedin")
    shown_only(session, "remote-intl", "greenhouse", NOW - timedelta(days=1))
    report = build_report(session, now=NOW)
    assert report.total_up == 1
    assert ("remote-intl", "ats") in report.starved_cells
