"""The chained run (E10). Cron fires this every 30 minutes; it decides
internally which legs are due.

Every run:      poll all ATS boards -> ingest.
3x/day:         JobSpy sweeps (wall-clock windows in config/schedule.yaml).
1x/day:         filter -> digest -> Telegram.
Every run:      JobSpy stale sweep, per-tier silence check, dead-man's ping.

Concurrency is handled by an flock on a single file: if the previous run is
still alive, this one SKIPS rather than queues (a queue of 30-min runs would
stampede the boards after any slow run).
"""

import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml
from sqlalchemy import func, select

from jobpilot.adapters import ashby, greenhouse, lever
from jobpilot.adapters.jobspy_search import SweepSpec
from jobpilot.adapters.jobspy_search import poll as jobspy_poll
from jobpilot.core import ATS_SOURCES, ingest, mark_jobspy_stale, utcnow
from jobpilot.db import DB_PATH, get_session, init_db
from jobpilot.dedupe import build_alias_lookup
from jobpilot.digest import build_digest, mark_digested, select_for_digest
from jobpilot.filters import FilterConfig, filter_survivors
from jobpilot.models import PollLog
from jobpilot.telegram import Telegram

log = logging.getLogger("jobpilot.pipeline")

ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
UA = {"User-Agent": "Mozilla/5.0 (JobPilot)"}


def state_path() -> Path:
    return Path(f"{DB_PATH}.state.json")


def lock_path() -> Path:
    return Path(f"{DB_PATH}.lock")


@dataclass
class RunReport:
    started_at: datetime
    ats_boards: int = 0
    ats_failures: list[str] = field(default_factory=list)
    new_jobs: int = 0
    duplicates_linked: int = 0
    closed: int = 0
    jobspy_ran: bool = False
    jobspy_failures: list[str] = field(default_factory=list)
    digest_sent: int = 0
    digest_held: int = 0
    staled: int = 0
    alerts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.ats_boards} boards, {self.new_jobs} new, "
            f"{self.duplicates_linked} deduped, {self.closed} closed, "
            f"jobspy={'yes' if self.jobspy_ran else 'skip'}, "
            f"digest={self.digest_sent or 'skip'}, stale={self.staled}, "
            f"failures={len(self.ats_failures) + len(self.jobspy_failures)}"
        )


# ---- wall-clock windows ----

def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}  # corrupt state just means a leg re-fires; never fatal


def save_state(state: dict) -> None:
    state_path().write_text(json.dumps(state, indent=2))


def window_due(name: str, hours: list[int], now: datetime, state: dict) -> str | None:
    """Key of the latest window that has passed today and not yet fired.

    Returns None when the leg is not due. Only the LATEST passed window is
    considered, so a laptop that missed three windows does one catch-up run.
    """
    passed = [h for h in sorted(hours) if now.hour >= h]
    if not passed:
        return None
    key = f"{now:%Y-%m-%d}T{passed[-1]:02d}"
    return key if state.get(name) != key else None


# ---- legs ----

def run_ats(session, companies: list[dict], alias_lookup: dict, timeout: int, report: RunReport) -> None:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=UA) as client:
        for c in companies:
            adapter = ADAPTERS.get(c["ats"])
            if adapter is None:
                report.ats_failures.append(f"{c['name']}: unknown ats {c['ats']!r}")
                continue
            result = adapter.poll(c["name"], c["slug"], client)
            stats = ingest(session, result, c["ats"], c["market"], alias_lookup)
            report.ats_boards += 1
            report.new_jobs += stats.new
            report.duplicates_linked += stats.duplicates_linked
            report.closed += stats.closed
            if not result.success:
                report.ats_failures.append(f"{c['name']}: {result.error}")
            session.commit()


def run_jobspy(session, sweeps: list[dict], search_terms: list[str], alias_lookup: dict,
               budget: int, report: RunReport) -> None:
    for raw in sweeps:
        spec = SweepSpec(
            market=raw["market"],
            sites=raw["sites"],
            location=raw.get("location"),
            country_indeed=raw.get("country_indeed", "india"),
            is_remote=raw.get("is_remote", False),
            results_wanted=raw.get("results_wanted", 25),
            hours_old=raw.get("hours_old", 72),
            budget_seconds=raw.get("budget_seconds", budget),
        )
        # The adapter returns one result per site, in spec.sites order — an
        # empty or failed result carries no rows to read the site name from.
        for site, result in zip(spec.sites, jobspy_poll(spec, search_terms), strict=True):
            stats = ingest(session, result, site, spec.market, alias_lookup)
            report.new_jobs += stats.new
            report.duplicates_linked += stats.duplicates_linked
            if not result.success:
                report.jobspy_failures.append(f"{spec.market}/{site}: {result.error}")
            session.commit()
    report.jobspy_ran = True


def run_digest(session, cfg: FilterConfig, now: datetime, telegram: Telegram,
               report: RunReport, limit: int | None = None) -> None:
    survivors = filter_survivors(session, cfg, now)  # freshest-first
    shown = select_for_digest(survivors, limit)
    payloads = build_digest(survivors, now, limit)
    sent = telegram.send_all(payloads)
    report.digest_sent = sent
    report.digest_held = len(survivors) - len(shown)
    if sent == len(payloads):
        mark_digested(session, shown, now)  # only burn rows we actually delivered
        session.commit()
    else:
        session.rollback()
        report.alerts.append(f"digest partially delivered ({sent}/{len(payloads)}) — rows not marked")


# ---- E11 per-tier silence ----

def tier_of(source: str) -> str:
    return "ats" if source in ATS_SOURCES else "aggregator"


def silent_tiers(session, now: datetime, hours: int) -> list[str]:
    """Tiers that polled but ingested nothing for `hours` — 'ran but blind',
    which a dead-man's ping cannot see (it only detects 'didn't run')."""
    cutoff = now - timedelta(hours=hours)
    rows = session.execute(
        select(PollLog.source, func.sum(PollLog.jobs_seen)).where(PollLog.polled_at >= cutoff).group_by(PollLog.source)
    ).all()
    totals: dict[str, int] = {"ats": 0, "aggregator": 0}
    seen_tiers: set[str] = set()
    for source, total in rows:
        tier = tier_of(source)
        seen_tiers.add(tier)
        totals[tier] += total or 0
    # A tier that never polled in the window is not "silent" here — that is the
    # dead-man's ping's job. Only report tiers that ran and saw nothing.
    return [t for t in sorted(seen_tiers) if totals[t] == 0]


def ping_healthcheck(report: RunReport) -> None:
    url = os.environ.get("HEALTHCHECKS_PING_URL")
    if not url:
        return
    try:
        httpx.post(url, content=report.summary(), timeout=10)
    except Exception as e:  # a dead ping must never fail the run
        log.warning("healthcheck ping failed: %s", e)


# ---- entrypoint ----

def run(now: datetime | None = None, force_jobspy: bool = False, force_digest: bool = False,
        config_dir: str | Path = "config", telegram_factory=None) -> RunReport:
    config_dir = Path(config_dir)
    schedule = yaml.safe_load((config_dir / "schedule.yaml").read_text())
    roles = yaml.safe_load((config_dir / "roles.yaml").read_text())
    companies = yaml.safe_load((config_dir / "companies.yaml").read_text())["companies"]
    cfg = FilterConfig.load(config_dir)

    local_now = now or datetime.now()
    report = RunReport(started_at=local_now)
    state = load_state()
    alias_lookup = build_alias_lookup(companies)

    jobspy_key = jobspy_due(schedule, local_now, state, force_jobspy)
    digest_key = digest_due(schedule, local_now, state, force_digest)

    with get_session() as session:
        run_ats(session, companies, alias_lookup, schedule["ats_timeout_seconds"], report)

        if jobspy_key:
            run_jobspy(session, roles.get("jobspy") or [], roles["search_terms"], alias_lookup,
                       schedule["jobspy_budget_seconds"], report)
            state["jobspy"] = jobspy_key
            save_state(state)

        report.staled = mark_jobspy_stale(session)
        session.commit()

        # Built lazily: an ingest-only run should not need Telegram credentials.
        telegram = telegram_factory or Telegram
        if digest_key:
            run_digest(session, cfg, utcnow(), telegram(), report, schedule.get("digest_max"))
            state["digest"] = digest_key
            save_state(state)

        for tier in silent_tiers(session, utcnow(), schedule["silence_alert_hours"]):
            key = f"{local_now:%Y-%m-%d}:{tier}"
            if state.get("silence_alert") == key:
                continue  # one alert per tier per day, not one per 30-min run
            msg = (f"🔕 {tier} tier polled but ingested 0 rows in the last "
                   f"{schedule['silence_alert_hours']}h — likely broken, not quiet.")
            telegram().send({"text": msg})
            report.alerts.append(msg)
            state["silence_alert"] = key
            save_state(state)

    ping_healthcheck(report)
    return report


def jobspy_due(schedule: dict, now: datetime, state: dict, force: bool) -> str | None:
    return f"forced-{now:%Y-%m-%dT%H%M%S}" if force else window_due("jobspy", schedule["jobspy_hours"], now, state)


def digest_due(schedule: dict, now: datetime, state: dict, force: bool) -> str | None:
    return f"forced-{now:%Y-%m-%dT%H%M%S}" if force else window_due("digest", schedule["digest_hours"], now, state)


def main(force_jobspy: bool = False, force_digest: bool = False) -> int:
    """Returns a shell exit code. 0 = ran (or skipped on lock), 1 = crashed."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from dotenv import load_dotenv

    load_dotenv()
    init_db()

    lock = open(lock_path(), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("previous run still active — skipping (E10: skip, don't queue)")
        return 0

    try:
        report = run(force_jobspy=force_jobspy, force_digest=force_digest)
        log.info("run complete: %s", report.summary())
        for failure in report.ats_failures + report.jobspy_failures:
            log.warning("poll failure: %s", failure)
        return 0
    except Exception:
        log.exception("pipeline run failed")
        return 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
