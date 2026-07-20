"""Weekend-1 exit gate (eng T7, amendments E6 + C10).

The gate: >=8 👍-annotated roles per week, sustained over two weeks, BEFORE
any scoring or drafting code exists. Reported per market x source tier,
because a headline number hides the failure the amendment actually cares
about — if the ATS tier under-delivers India-eligible roles, the fix is
rebalancing sources, not writing intelligence code on a starved funnel.

Nothing here judges the jobs. It counts what you said about them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.core import ATS_SOURCES, utcnow
from jobpilot.models import Annotation, Job, PollLog

TARGET_THUMBS_UP_PER_WEEK = 8
GATE_WEEKS = 2
MIN_POLL_DAYS = 21  # C10 signal-maturity floor for diff-engine claims

# The funnel E6 requires reporting on. Fixed rather than derived, because a
# cell that delivered NOTHING must still appear — deriving the grid from the
# data would hide exactly the starvation this report exists to surface.
MARKETS = ("india", "remote-intl")
TIERS = ("ats", "aggregator")


def tier_of(source: str) -> str:
    return "ats" if source in ATS_SOURCES else "aggregator"


@dataclass
class WeekSlice:
    label: str
    start: datetime
    end: datetime
    up: int = 0
    down: int = 0
    by_cell: dict[tuple[str, str], int] = field(default_factory=dict)  # (market, tier) -> 👍

    @property
    def passes(self) -> bool:
        return self.up >= TARGET_THUMBS_UP_PER_WEEK


@dataclass
class GateReport:
    weeks: list[WeekSlice]
    digested: int
    poll_days: int
    markets: list[str]
    tiers: list[str]
    shown_by_cell: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def cells(self) -> list[tuple[str, str]]:
        return [(m, t) for m in self.markets for t in self.tiers]

    @property
    def up_by_cell(self) -> dict[tuple[str, str], int]:
        totals: dict[tuple[str, str], int] = {}
        for week in self.weeks:
            for cell, count in week.by_cell.items():
                totals[cell] = totals.get(cell, 0) + count
        return totals

    @property
    def silent_cells(self) -> list[tuple[str, str]]:
        """Delivered nothing at all. Not a taste problem — a sourcing problem,
        and invisible to any measure that only counts thumbs."""
        return [c for c in self.cells if not self.shown_by_cell.get(c)]

    @property
    def passes(self) -> bool:
        """Every week must clear the bar — 16 in one week and 0 in the next is
        a burst of enthusiasm, not a sustained signal."""
        return bool(self.weeks) and all(w.passes for w in self.weeks)

    @property
    def total_up(self) -> int:
        return sum(w.up for w in self.weeks)

    @property
    def starved_cells(self) -> list[tuple[str, str]]:
        """Showed you roles, earned no 👍 — a taste/quality problem.

        Only meaningful once something has been annotated: with zero signal
        every cell is trivially empty, and 'rebalance sources' would be wrong
        advice. A cell that showed nothing is silent, not starved.
        """
        if not self.total_up:
            return []
        up = self.up_by_cell
        return [c for c in self.cells if self.shown_by_cell.get(c) and not up.get(c)]


def build_report(session: Session, now: datetime | None = None, weeks: int = GATE_WEEKS) -> GateReport:
    now = now or utcnow()
    rows = session.execute(
        select(Annotation.verdict, Annotation.created_at, Job.market, Job.source)
        .join(Job, Job.id == Annotation.job_id)
        .where(Annotation.created_at >= now - timedelta(weeks=weeks))
    ).all()

    slices: list[WeekSlice] = []
    for i in range(weeks):  # week 0 = most recent
        end = now - timedelta(weeks=i)
        slices.append(WeekSlice(label=f"week of {end - timedelta(weeks=1):%d %b}",
                                start=end - timedelta(weeks=1), end=end))

    for verdict, created_at, market, source in rows:
        tier = tier_of(source)
        for week in slices:
            if week.start <= created_at < week.end:
                if verdict == "up":
                    week.up += 1
                    week.by_cell[(market, tier)] = week.by_cell.get((market, tier), 0) + 1
                else:
                    week.down += 1
                break

    # The grid comes from what was SHOWN, not from what was annotated. Deriving
    # it from annotations would make starvation undetectable: a tier you never
    # thumbed would simply never appear as a cell, which is the exact failure
    # E6 asks this report to catch.
    shown_by_cell: dict[tuple[str, str], int] = {}
    for market, source, count in session.execute(
        select(Job.market, Job.source, func.count())
        .where(Job.digested_at.isnot(None))
        .group_by(Job.market, Job.source)
    ).all():
        cell = (market, tier_of(source))
        shown_by_cell[cell] = shown_by_cell.get(cell, 0) + count
    digested_count = sum(shown_by_cell.values())

    first_poll = session.scalar(select(PollLog.polled_at).order_by(PollLog.polled_at).limit(1))
    poll_days = (now - first_poll).days if first_poll else 0

    return GateReport(
        weeks=list(reversed(slices)),  # oldest first, reads like a timeline
        digested=digested_count,
        poll_days=poll_days,
        markets=list(MARKETS),
        tiers=list(TIERS),
        shown_by_cell=shown_by_cell,
    )


def render(report: GateReport) -> str:
    verdict = "PASSES" if report.passes else "does not pass yet"
    lines = [
        f"Weekend-1 gate: {verdict}",
        f"Target: >={TARGET_THUMBS_UP_PER_WEEK} 👍/week for {len(report.weeks)} consecutive weeks.",
        f"{report.digested} roles shown, {report.poll_days} days of polling history.",
        "",
    ]
    for week in report.weeks:
        mark = "✅" if week.passes else "❌"
        lines.append(f"{mark} {week.label}: {week.up} 👍 / {week.down} 👎")
        for (market, tier), count in sorted(week.by_cell.items()):
            lines.append(f"      {market:12} {tier:11} {count} 👍")
        if not week.by_cell:
            lines.append("      (no annotations)")

    lines += ["", "Funnel (roles shown → 👍, whole window):"]
    up = report.up_by_cell
    for cell in report.cells:
        market, tier = cell
        lines.append(f"  {market:12} {tier:11} {report.shown_by_cell.get(cell, 0):4} shown "
                     f"→ {up.get(cell, 0)} 👍")

    if report.silent_cells:
        lines += ["", "Delivered NOTHING — a sourcing problem, not a taste one:"]
        lines += [f"  - {m} / {t}" for m, t in report.silent_cells]
        if ("india", "ats") in report.silent_cells:
            lines.append("  Fix for india/ats: grow config/companies.yaml (E6 rebalance).")

    if not report.total_up:
        lines += ["", f"No 👍 yet. Tap the buttons on digest cards (or /more) — "
                      f"{report.digested} roles have been shown so far."]
    elif report.starved_cells:
        lines += ["", "Shown but never 👍 — E6 says rebalance sources before weekend 2:"]
        lines += [f"  - {m} / {t}" for m, t in report.starved_cells]

    if report.poll_days < MIN_POLL_DAYS:
        lines += ["", f"Note: {report.poll_days}/{MIN_POLL_DAYS} days of polling history — "
                      "below the C10 floor, so diff-engine signals stay hidden."]
    return "\n".join(lines)
