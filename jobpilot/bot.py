"""Persistent Telegram bot daemon (E2): receives what the pipeline cannot —
callback taps and commands. Long-polling, so it works behind home NAT.

Business logic lives in plain sync functions (testable without Telegram);
the async python-telegram-bot handlers are glue. Every handler ignores
updates from anyone but the owner chat.
"""

import asyncio
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot import config_edit
from jobpilot.core import utcnow
from jobpilot.db import get_session, init_db
from jobpilot.dedupe import norm_company, norm_title
from jobpilot.digest import mark_digested, render_job_message, select_for_digest
from jobpilot.filters import FilterConfig, filter_survivors
from jobpilot.models import Annotation, Application, Job, JobStatus

log = logging.getLogger("jobpilot.bot")

APPLIED_USAGE = (
    "To log an application: /applied Company | Job Title [| reply channel]\n"
    "e.g. /applied Zerodha | Backend Engineer | email\n"
    "Channels: email, linkedin, portal (omit if none).\n"
    "Bare /applied lists what you've applied to."
)
COMPANY_USAGE = (
    "/company list\n"
    "/company add <Name> <greenhouse|lever|ashby> <slug> <india|remote-intl>\n"
    "   e.g. /company add Zerodha lever zerodha india\n"
    "   The board is polled before it is accepted, so a bad slug is rejected.\n"
    "/company rm <Name>\n"
    "/company alias <Name> add|rm <alias>   — feeds cross-source dedupe"
)
ROLE_USAGE = (
    "/role list\n"
    "/role include add <term>     e.g. /role include add golang\n"
    "/role exclude add <term>     e.g. /role exclude add staff\n"
    "/role search add <query>     e.g. /role search add Python Developer\n"
    "/role <kind> rm <term>\n"
    "\n"
    "include/exclude filter what was already fetched (whole ATS boards too).\n"
    "search is what LinkedIn/Indeed are actually ASKED for — different job.\n"
    "Excludes match whole words; search queries keep the case you type."
)
FILTER_USAGE = (
    "/filter block list | add <Company> | rm <Company>\n"
    "/filter salary                       — show floors\n"
    "/filter salary <market> <CUR> <amt>  e.g. /filter salary india INR 1500000\n"
    "/filter salary <market> off\n"
    "/filter location                     — show location rules\n"
    "/filter location <market> include|exclude add|rm <term>\n"
    "\n"
    "Markets: india, remote-intl. Postings with no stated salary always pass."
)
SWEEP_USAGE = (
    "/sweep                                — show both sweeps\n"
    "/sweep <market> sites indeed,linkedin\n"
    "/sweep <market> location Bengaluru, India   (or: none)\n"
    "/sweep <market> is_remote true|false\n"
    "/sweep <market> results_wanted 25\n"
    "/sweep <market> hours_old 72\n"
    "\n"
    "These control what the aggregators are asked for. naukri is refused "
    "(recaptcha-blocked, fails silently) and LinkedIn locations must end in a "
    "real country."
)
LIST_LIMIT = 20
MORE_DEFAULT = 10
MORE_MAX = 30
HEARTBEAT_SECONDS = 300


# ---- business logic (sync, tested) ----

def parse_more_count(text: str) -> int:
    try:
        return max(1, min(MORE_MAX, int(text.strip())))
    except (TypeError, ValueError):
        return MORE_DEFAULT


def next_batch(session: Session, limit: int, now: datetime, config_dir: str = "config") -> tuple[list[Job], int]:
    """The next `limit` queued roles, plus how many stay queued after them.

    Same gates and same market x tier balancing as the daily digest — /more is
    the queue drained on demand, not a different view of it. Rows are marked
    digested by the caller, and only once they have actually been delivered.
    """
    survivors = filter_survivors(session, FilterConfig.load(config_dir), now)
    batch = select_for_digest(survivors, limit)
    return batch, len(survivors) - len(batch)

def record_annotation(session: Session, job_id: int, verdict: str) -> str:
    job = session.get(Job, job_id)
    if job is None:
        return "Unknown job — was the DB reset?"
    existing = session.scalar(select(Annotation).where(Annotation.job_id == job_id))
    if existing is None:
        session.add(Annotation(job_id=job_id, verdict=verdict, created_at=utcnow()))
    elif existing.verdict != verdict:
        existing.verdict = verdict  # changing your mind is allowed; still one row
    session.flush()
    emoji = "👍" if verdict == "up" else "👎"
    return f"{emoji} saved — {job.title} @ {job.company}"


def match_job(session: Session, company: str, title: str) -> Job | None:
    """Best-effort link of a manual application to an ingested row."""
    ckey, tkey = norm_company(company), norm_title(title)
    candidates = session.scalars(
        select(Job).where(Job.duplicate_of.is_(None), Job.status == JobStatus.OPEN)
    ).all()
    best, best_ratio = None, 0.0
    for j in candidates:
        if norm_company(j.company) != ckey:
            continue
        ratio = fuzz.ratio(tkey, norm_title(j.title))
        if ratio >= 85 and ratio > best_ratio:
            best, best_ratio = j, ratio
    return best


def list_applications(session: Session) -> str:
    apps = session.scalars(
        select(Application).order_by(Application.sent_at.desc()).limit(LIST_LIMIT)
    ).all()
    if not apps:
        return "No applications logged yet. Log one with:\n" + APPLIED_USAGE
    total = session.scalar(select(func.count()).select_from(Application))
    lines = [f"📋 Applications ({total} total, latest {len(apps)}):"]
    for a in apps:
        channel = f" via {a.reply_channel}" if a.reply_channel else ""
        lines.append(f"• {a.sent_at:%d %b} — {a.title} @ {a.company} [{a.status}]{channel}")
    return "\n".join(lines)


def record_application(session: Session, text: str) -> str:
    """Send capture (C1): '/applied Company | Title [| channel]'.
    Works for roles JobPilot never ingested — manual applications are
    first-class data from week 1."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return APPLIED_USAGE
    company, title = parts[0], parts[1]
    channel = parts[2].lower() if len(parts) > 2 and parts[2] else None
    job = match_job(session, company, title)
    session.add(
        Application(
            job_id=job.id if job else None,
            company=company,
            title=title,
            sent_at=utcnow(),
            reply_channel=channel,
            status="sent",
        )
    )
    session.flush()
    linked = f" (linked to tracked job #{job.id})" if job else ""
    return f"✅ Application logged: {title} @ {company}{linked}. Timers anchor on now."


# ---- liveness (T3) ----
#
# systemd's Restart=always only sees the process die. The failure that actually
# costs us is quieter: the process stays up while long-polling stops (token
# revoked, updater task dead, network gone). Digests still arrive — they are
# sent by jobpilot/telegram.py, not by this daemon — so the only symptom is
# that buttons do nothing, and 👍 taps are exactly what the weekend-1 gate
# counts. A dead bot would read as "liked nothing this week" and silently burn
# gate weeks. So the bot pings its OWN check, on its own URL.

def scrub(text: str, token: str) -> str:
    """Never let the token reach a third-party ping body. httpx embeds the full
    request URL — token and all — in its error strings, which is the same
    reason its logger is muted in main()."""
    return text.replace(token, "<token>") if token else text


def ping_target(base: str, ok: bool) -> str:
    """healthchecks.io: <url> is a success ping, <url>/fail alerts immediately
    rather than waiting out the grace period."""
    return base.rstrip("/") + ("" if ok else "/fail")


async def liveness(app, token: str = "") -> tuple[bool, str]:
    """Two independent things must hold, because either alone lies.

    `updater.running` alone stays True if the process is up but the token was
    revoked; a successful API call alone proves nothing about whether anything
    is consuming updates. Together they cover the realistic failures.
    """
    updater = getattr(app, "updater", None)
    if updater is None or not updater.running:
        return False, "updater is not polling"
    try:
        me = await app.bot.get_me()
    except Exception as e:
        return False, scrub(f"get_me failed: {type(e).__name__}: {e}", token)
    return True, f"polling as @{me.username}"


async def heartbeat(app, url: str, token: str = "", interval: int = HEARTBEAT_SECONDS,
                    sleep=None, post=None) -> None:
    import httpx

    sleep = sleep or asyncio.sleep
    while True:
        await sleep(interval)
        ok, detail = await liveness(app, token)
        if not ok:
            log.warning("bot liveness check failed: %s", detail)
        try:
            if post is not None:
                await post(ping_target(url, ok), detail)
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(ping_target(url, ok), content=detail)
        except Exception as e:  # a failed ping must never kill the daemon
            log.warning("bot ping failed: %s", scrub(str(e), token))


# ---- telegram glue ----

def build_application(token: str, owner_chat_id: int, ping_url: str | None = None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )

    def owned(update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == owner_chat_id

    async def on_annotation(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not owned(update):
            await query.answer()
            return
        _, job_id, verdict = query.data.split(":")
        with get_session() as session:
            feedback = record_annotation(session, int(job_id), verdict)
            session.commit()
        await query.answer(feedback)
        chosen = "👍ᅠ✓" if verdict == "up" else "👎ᅠ✓"
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(chosen, callback_data=query.data)]])
        )

    async def on_applied(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        text = " ".join(context.args or [])
        with get_session() as session:
            feedback = list_applications(session) if not text.strip() else record_application(session, text)
            session.commit()
        await update.message.reply_text(feedback)

    async def on_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pull the queue on demand. The daily digest is capped, so without
        this a backlog bigger than the cap expires before it is ever shown."""
        if not owned(update):
            return
        limit = parse_more_count(" ".join(context.args or ""))
        now = utcnow()
        with get_session() as session:
            batch, remaining = next_batch(session, limit, now)
            if not batch:
                await update.message.reply_text("📭 Queue empty — nothing waiting to be shown.")
                return
            delivered = []
            for job in batch:
                payload = render_job_message(job, now)
                keyboard = [
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in rowb]
                    for rowb in payload["reply_markup"]["inline_keyboard"]
                ]
                try:
                    await context.bot.send_message(
                        chat_id=owner_chat_id,
                        text=payload["text"],
                        parse_mode=payload["parse_mode"],
                        disable_web_page_preview=True,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    log.exception("send failed for job %s; leaving it queued", job.id)
                    break  # undelivered rows stay queued rather than vanishing
                delivered.append(job)
            mark_digested(session, delivered, now)
            session.commit()
        await update.message.reply_text(
            f"✅ Sent {len(delivered)}. {remaining + len(batch) - len(delivered)} still queued — /more for the next batch."
        )

    async def on_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        args = context.args or []
        action = (args[0].lower() if args else "list")
        if action == "list":
            await update.message.reply_text(config_edit.list_companies())
        elif action == "add":
            if len(args) < 5:
                await update.message.reply_text(COMPANY_USAGE)
                return
            name, ats, slug, market = args[1], args[2], args[3], args[4]
            await update.message.reply_text(f"🔎 Checking {ats}:{slug}…")
            # Network call off the event loop; the board is polled BEFORE the
            # slug is trusted, so a typo cannot sit in config failing silently.
            ok, detail = await asyncio.to_thread(config_edit.verify_board, ats, slug, name)
            if not ok:
                await update.message.reply_text(f"❌ Not added — board check failed: {detail}")
                return
            await update.message.reply_text(
                config_edit.add_company(name, ats, slug, market) + f"\n   Board OK: {detail}"
            )
        elif action == "alias":
            if len(args) < 4:
                await update.message.reply_text(COMPANY_USAGE)
                return
            await update.message.reply_text(
                config_edit.edit_alias(args[1], args[2].lower(), " ".join(args[3:]))
            )
        elif action in ("rm", "remove"):
            if len(args) < 2:
                await update.message.reply_text(COMPANY_USAGE)
                return
            await update.message.reply_text(config_edit.remove_company(" ".join(args[1:])))
        else:
            await update.message.reply_text(COMPANY_USAGE)

    async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        args = context.args or []
        action = (args[0].lower() if args else "list")
        if action == "list":
            await update.message.reply_text(config_edit.list_terms())
        elif action in config_edit.TERM_KINDS and len(args) >= 3:
            op, term = args[1].lower(), " ".join(args[2:])
            if op == "add":
                await update.message.reply_text(config_edit.add_term(action, term))
            elif op in ("rm", "remove"):
                await update.message.reply_text(config_edit.remove_term(action, term))
            else:
                await update.message.reply_text(ROLE_USAGE)
        else:
            await update.message.reply_text(ROLE_USAGE)

    async def on_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        args = context.args or []
        section = (args[0].lower() if args else "")
        if section == "block":
            op = (args[1].lower() if len(args) > 1 else "list")
            if op == "list":
                await update.message.reply_text(config_edit.list_blocklist())
            elif op == "add" and len(args) >= 3:
                await update.message.reply_text(config_edit.block_company(" ".join(args[2:])))
            elif op in ("rm", "remove") and len(args) >= 3:
                await update.message.reply_text(config_edit.unblock_company(" ".join(args[2:])))
            else:
                await update.message.reply_text(FILTER_USAGE)
        elif section == "salary":
            if len(args) == 1:
                await update.message.reply_text(config_edit.show_salary())
            elif len(args) == 3 and args[2].lower() in ("off", "none", "clear"):
                await update.message.reply_text(config_edit.clear_salary_floor(args[1]))
            elif len(args) == 4:
                await update.message.reply_text(
                    config_edit.set_salary_floor(args[1], args[2], args[3])
                )
            else:
                await update.message.reply_text(FILTER_USAGE)
        elif section == "location":
            if len(args) == 1:
                await update.message.reply_text(config_edit.show_location())
            elif len(args) >= 5:
                await update.message.reply_text(
                    config_edit.edit_location(args[1], args[2], args[3], " ".join(args[4:]))
                )
            else:
                await update.message.reply_text(FILTER_USAGE)
        else:
            await update.message.reply_text(FILTER_USAGE)

    async def on_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(config_edit.show_sweeps())
            return
        if len(args) < 3:
            await update.message.reply_text(SWEEP_USAGE)
            return
        await update.message.reply_text(
            config_edit.set_sweep(args[0], args[1], " ".join(args[2:]))
        )

    async def on_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(config_edit.show_settings())
            return
        if len(args) < 2:
            await update.message.reply_text("Usage: /set <key> <value> — /set alone lists them.")
            return
        await update.message.reply_text(config_edit.set_value(args[0], args[1]))

    async def on_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        from jobpilot.gate import build_report, render

        with get_session() as session:
            await update.message.reply_text(render(build_report(session)))

    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        await update.message.reply_text(
            "JobPilot bot alive.\n"
            "• 👍/👎 on a card records what you think of it\n"
            "• /more [n] — pull the next n queued roles (default 10)\n"
            "• /applied — list what you've applied to, or log a new one\n"
            "• /gate — progress toward the weekend-1 exit gate\n"
            "• /company, /role, /filter, /sweep, /set — edit config from here\n"
            "  (every config/*.yaml value is reachable; each command alone shows usage)"
        )

    async def register_commands(application) -> None:
        await application.bot.set_my_commands([
            ("more", "Pull the next queued roles (/more 20 for a bigger batch)"),
            ("applied", "List applications, or log one: Company | Title"),
            ("gate", "Weekend-1 gate progress per market x source tier"),
            ("company", "list / add / rm / alias tracked ATS companies"),
            ("role", "title include+exclude terms, and aggregator search queries"),
            ("filter", "blocklist, salary floors, location rules"),
            ("sweep", "what LinkedIn/Indeed are asked for (sites, location, recency)"),
            ("set", "View or change tuning values (freshness_days, digest_max, ...)"),
            ("start", "What this bot does"),
        ])

    async def start_heartbeat(application) -> None:
        await register_commands(application)
        if not ping_url:
            log.info("no HEALTHCHECKS_BOT_PING_URL — bot liveness ping disabled")
            return
        # post_init runs before polling starts, but heartbeat sleeps first, so
        # the updater is up well before the first check.
        application.bot_data["heartbeat"] = asyncio.create_task(
            heartbeat(application, ping_url, token)
        )
        log.info("bot liveness ping every %ss", HEARTBEAT_SECONDS)

    async def stop_heartbeat(application) -> None:
        task = application.bot_data.get("heartbeat")
        if task is not None:
            task.cancel()

    app = (Application.builder().token(token)
           .post_init(start_heartbeat).post_shutdown(stop_heartbeat).build())
    app.add_handler(CallbackQueryHandler(on_annotation, pattern=r"^ann:\d+:(up|down)$"))
    app.add_handler(CommandHandler("applied", on_applied))
    app.add_handler(CommandHandler("more", on_more))
    app.add_handler(CommandHandler("gate", on_gate))
    app.add_handler(CommandHandler("company", on_company))
    app.add_handler(CommandHandler("role", on_role))
    app.add_handler(CommandHandler("filter", on_filter))
    app.add_handler(CommandHandler("sweep", on_sweep))
    app.add_handler(CommandHandler("set", on_set))
    app.add_handler(CommandHandler("start", on_start))
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token URL
    load_dotenv()
    init_db()
    app = build_application(
        os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        int(os.environ["TELEGRAM_CHAT_ID"]),
        ping_url=os.environ.get("HEALTHCHECKS_BOT_PING_URL"),
    )
    log.info("bot daemon starting (long polling)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
