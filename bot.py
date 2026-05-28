import asyncio
import logging
import os
import platform
from datetime import datetime, timezone

import httpx
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from config import BOT_TOKEN, API_BASE_URL, ALLOWED_USER_IDS, PORT

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Bot start time (for uptime calc) ─────────────────────────────────────────
BOT_START_TIME: datetime = datetime.now(timezone.utc)
TOTAL_QUERIES: int = 0          # simple in-memory counter


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_email(text: str) -> bool:
    return "@" in text and "." in text.split("@")[-1]

def is_mobile(text: str) -> bool:
    digits = text.replace("+", "").replace(" ", "").replace("-", "")
    return digits.isdigit() and 7 <= len(digits) <= 15

def format_record(record: dict) -> str:
    """Pretty-print every key-value pair returned by the API."""
    if not record:
        return "_(empty record)_"
    lines = []
    for key, value in record.items():
        if value in (None, "", [], {}):
            value = "—"
        lines.append(f"• *{key}*: `{value}`")
    return "\n".join(lines)

def human_uptime(start: datetime) -> str:
    """Return a human-readable uptime string."""
    delta = datetime.now(timezone.utc) - start
    days    = delta.days
    hours   = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    seconds = delta.seconds % 60
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

def auth_required(func):
    """Decorator: block users not in ALLOWED_USER_IDS (skip if list is empty)."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_USER_IDS:
            uid = update.effective_user.id
            if uid not in ALLOWED_USER_IDS:
                await update.message.reply_text("⛔ You are not authorised to use this bot.")
                return
        return await func(update, context)
    return wrapper


# ── REST API call ─────────────────────────────────────────────────────────────

async def fetch_record(query: str, query_type: str) -> dict | None:
    headers = {"Content-Type": "application/json"}

    # OPTION A – GET with query params (default)
    params = {query_type: query}
    url = f"{API_BASE_URL}/search"

    # OPTION B – POST with JSON body (uncomment to switch)
    # url = f"{API_BASE_URL}/search"
    # payload = {query_type: query}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params, headers=headers)
            # response = await client.post(url, json=payload, headers=headers)  # OPTION B
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("API HTTP error %s: %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("API request failed: %s", e)
        return None


# ── Telegram handlers ─────────────────────────────────────────────────────────

@auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to the Lookup Bot!*\n\n"
        "Send me an *email address* or *mobile number* and I'll fetch all related details.\n\n"
        "Examples:\n"
        "  `john@example.com`\n"
        "  `+919876543210`\n\n"
        "Commands:\n"
        "  /start  — show this help\n"
        "  /help   — show this help\n"
        "  /status — bot uptime & stats"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


@auth_required
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram /status command — mirrors the /health web endpoint."""
    uptime  = human_uptime(BOT_START_TIME)
    started = BOT_START_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        "🟢 *Bot is running*\n\n"
        f"🕐 *Started at:* `{started}`\n"
        f"⏱ *Uptime:* `{uptime}`\n"
        f"🔍 *Queries served:* `{TOTAL_QUERIES}`\n"
        f"🐍 *Python:* `{platform.python_version()}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_QUERIES
    query = update.message.text.strip()

    if is_email(query):
        query_type = "email"
        label = "📧 Email"
    elif is_mobile(query):
        query_type = "mobile"
        label = "📱 Mobile"
    else:
        await update.message.reply_text(
            "⚠️ Please send a valid *email address* or *mobile number*.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, action="typing")
    record = await fetch_record(query, query_type)
    TOTAL_QUERIES += 1

    if record is None:
        await update.message.reply_text(
            "❌ API error or network issue. Please try again later.",
            parse_mode="Markdown",
        )
        return

    if isinstance(record, list):
        if not record:
            await update.message.reply_text(
                f"🔍 No records found for `{query}`.", parse_mode="Markdown"
            )
            return
        context.user_data["results"] = record
        context.user_data["page"] = 0
        await send_page(update, context, record, 0, query)
        return

    if not record:
        await update.message.reply_text(
            f"🔍 No record found for `{query}`.", parse_mode="Markdown"
        )
        return

    reply = (
        f"✅ *Record found* ({label}: `{query}`)\n"
        f"{'─' * 30}\n"
        f"{format_record(record)}"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")


async def send_page(update, context, records, page, query=""):
    total  = len(records)
    record = records[page]
    text = (
        f"✅ *Result {page + 1} of {total}* for `{query or '?'}`\n"
        f"{'─' * 30}\n"
        f"{format_record(record)}"
    )
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page:{page - 1}"))
    if page < total - 1:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page + 1}"))
    markup = InlineKeyboardMarkup([buttons]) if buttons else None

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    records = context.user_data.get("results", [])
    if records:
        await send_page(update, context, records, page)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Send /help to see usage.")


# ── Health-check HTTP server (for UptimeRobot / Render) ──────────────────────

async def health_handler(request: web.Request) -> web.Response:
    """
    GET /health  →  JSON with uptime, start time, query count, status.
    UptimeRobot pings this URL. As long as it returns HTTP 200 the bot
    stays alive on Render's free tier (which sleeps idle services).
    """
    uptime_str = human_uptime(BOT_START_TIME)
    payload = {
        "status":       "ok",
        "bot":          "Lookup Bot",
        "started_at":   BOT_START_TIME.isoformat(),
        "uptime":       uptime_str,
        "queries_total": TOTAL_QUERIES,
        "python":       platform.python_version(),
        "platform":     platform.system(),
    }
    return web.json_response(payload)


async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/",       health_handler)   # root also returns 200
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server listening on port %s", PORT)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    # Start health-check server first (Render needs a port to bind quickly)
    await run_health_server()

    # Build and start the Telegram bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",  start))
    application.add_handler(CommandHandler("help",   help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CallbackQueryHandler(paginate, pattern=r"^page:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Telegram bot is running…")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        # Keep running forever
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
