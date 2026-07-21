"""Bot business-logic tests (E12: callback handlers, send capture)."""

from datetime import datetime

import asyncio

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jobpilot import bot as bot_module
from jobpilot.bot import (
    APPLIED_USAGE,
    HELP,
    heartbeat,
    list_applications,
    liveness,
    ping_target,
    next_batch,
    note_unauthorized,
    parse_more_count,
    record_annotation,
    record_application,
    render_help,
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


# ---- /help ----

def test_help_lists_every_command_grouped():
    out = render_help()
    for name in HELP:
        assert f"/{name}" in out
    assert "Day to day:" in out and "Editing config" in out


def test_help_topic_returns_that_commands_usage():
    out = render_help("sweep")
    assert "/sweep <market> sites" in out
    assert "naukri" in out  # the guard worth knowing about before you try it


def test_help_topic_tolerates_a_leading_slash_and_case():
    assert render_help("/Filter") == render_help("filter")


def test_help_on_an_unknown_command():
    out = render_help("teleport")
    assert "❌" in out and "/help" in out


def test_help_covers_every_registered_command():
    """The guard that matters: a new CommandHandler with no HELP entry is
    invisible in both /help and Telegram's command menu."""
    from telegram.ext import CommandHandler

    from jobpilot.bot import build_application

    app = build_application("123456:FAKE-TOKEN-VALUE", 1)
    registered = {
        cmd
        for group in app.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
        for cmd in h.commands
    }
    assert registered == set(HELP), (
        f"missing from HELP: {registered - set(HELP)}; "
        f"in HELP but not registered: {set(HELP) - registered}"
    )


def test_ungrouped_commands_still_appear_in_the_overview(monkeypatch):
    """A command added to HELP but to neither group must not vanish."""
    from jobpilot import bot as bot_module

    monkeypatch.setitem(bot_module.HELP, "teleport", ("Go somewhere", "/teleport"))
    assert "/teleport" in render_help()


# ---- access control ----
#
# Telegram has no private-bot setting; anyone who knows the username can send
# to it. Our chat-id check is the entire perimeter.

def test_unauthorized_chat_is_logged_once_not_per_message():
    seen = set()
    assert note_unauthorized(999, seen) is not None
    assert note_unauthorized(999, seen) is None   # no flood from a probe loop
    assert note_unauthorized(1000, seen) is not None


def test_unauthorized_log_set_is_bounded():
    """A spammer cycling chat ids must not grow this without limit."""
    seen = set()
    for i in range(bot_module.UNAUTHORIZED_LOG_CAP * 3):
        note_unauthorized(i, seen)
    assert len(seen) <= bot_module.UNAUTHORIZED_LOG_CAP


class Recorder:
    """Stands in for update.message / update.callback_query; any reply at all
    to a stranger is a failure, so every method records and fails loudly."""

    def __init__(self):
        self.replies = []

    async def reply_text(self, *a, **k):
        self.replies.append(a)

    async def answer(self, *a, **k):
        self.replies.append(a)

    async def edit_message_reply_markup(self, *a, **k):
        self.replies.append(a)


class FakeUpdate:
    def __init__(self, chat_id, data=None):
        self.effective_chat = type("C", (), {"id": chat_id})()
        self.message = Recorder()
        self.callback_query = Recorder()
        self.callback_query.data = data or "ann:1:up"


@pytest.mark.parametrize("chat_id", [0, -1, 12345, 987654321])
def test_no_handler_responds_to_a_stranger(chat_id):
    """Every registered handler, not a sampled few — a new ungated command
    fails here instead of shipping."""
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from jobpilot.bot import build_application

    owner = 555000111
    assert chat_id != owner
    app = build_application(TOKEN, owner)
    handlers = [h for group in app.handlers.values() for h in group
                if isinstance(h, (CommandHandler, CallbackQueryHandler))]
    assert len(handlers) >= 11

    for handler in handlers:
        update = FakeUpdate(chat_id)
        ctx = type("C", (), {"args": [], "bot": None})()
        asyncio.run(handler.callback(update, ctx))
        assert update.message.replies == [], f"{handler} replied to a stranger"


def test_owner_is_not_blocked():
    """The gate must not be so tight it locks out the owner."""
    from telegram.ext import CommandHandler

    from jobpilot.bot import build_application

    owner = 555000111
    app = build_application(TOKEN, owner)
    handler = next(h for group in app.handlers.values() for h in group
                   if isinstance(h, CommandHandler) and "help" in h.commands)
    update = FakeUpdate(owner)
    ctx = type("C", (), {"args": [], "bot": None})()
    asyncio.run(handler.callback(update, ctx))
    assert update.message.replies  # got a real answer
