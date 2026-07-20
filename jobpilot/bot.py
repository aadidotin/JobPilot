"""Persistent Telegram bot daemon (E2): receives what the pipeline cannot —
callback taps and commands. Long-polling, so it works behind home NAT.

Business logic lives in plain sync functions (testable without Telegram);
the async python-telegram-bot handlers are glue. Every handler ignores
updates from anyone but the owner chat.
"""

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
LIST_LIMIT = 20
MORE_DEFAULT = 10
MORE_MAX = 30


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


# ---- telegram glue ----

def build_application(token: str, owner_chat_id: int):
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

    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not owned(update):
            return
        await update.message.reply_text(
            "JobPilot bot alive.\n"
            "• 👍/👎 on a card records what you think of it\n"
            "• /more [n] — pull the next n queued roles (default 10)\n"
            "• /applied — list what you've applied to, or log a new one"
        )

    async def register_commands(application) -> None:
        await application.bot.set_my_commands([
            ("more", "Pull the next queued roles (/more 20 for a bigger batch)"),
            ("applied", "List applications, or log one: Company | Title"),
            ("start", "What this bot does"),
        ])

    app = Application.builder().token(token).post_init(register_commands).build()
    app.add_handler(CallbackQueryHandler(on_annotation, pattern=r"^ann:\d+:(up|down)$"))
    app.add_handler(CommandHandler("applied", on_applied))
    app.add_handler(CommandHandler("more", on_more))
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
    )
    log.info("bot daemon starting (long polling)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
