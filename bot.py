import asyncio
import logging
import platform
from collections import defaultdict
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

# ── State ─────────────────────────────────────────────────────────────────────
BOT_START_TIME: datetime = datetime.now(timezone.utc)
TOTAL_QUERIES: int = 0

# Rate limit: track last request time per user (simple in-memory)
_last_request: dict[int, float] = defaultdict(float)
RATE_LIMIT_SECONDS = 5          # min gap between requests per user
API_RETRY_ATTEMPTS = 2          # retry once on 503
API_RETRY_DELAY    = 3          # seconds between retries


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_email(text: str) -> bool:
    return "@" in text and "." in text.split("@")[-1]

def is_mobile(text: str) -> bool:
    digits = text.replace("+", "").replace(" ", "").replace("-", "")
    return digits.isdigit() and 7 <= len(digits) <= 15

def format_record(record) -> str:
    """Handle dict, list of strings, or list of dicts."""
    if not record:
        return "_(empty record)_"

    # List of "Key: value" strings returned by some APIs
    if isinstance(record, list) and record and isinstance(record[0], str):
        return "\n".join(f"• {line}" for line in record)

    # Standard dict
    if isinstance(record, dict):
        lines = []
        for key, value in record.items():
            if value in (None, "", [], {}):
                value = "—"
            lines.append(f"• *{key}*: `{value}`")
        return "\n".join(lines)

    return str(record)

def human_uptime(start: datetime) -> str:
    delta   = datetime.now(timezone.utc) - start
    days    = delta.days
    hours   = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    seconds = delta.seconds % 60
    parts   = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

def auth_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_USER_IDS:
            if update.effective_user.id not in ALLOWED_USER_IDS:
                await update.message.reply_text("⛔ You are not authorised to use this bot.")
                return
        return await func(update, context)
    return wrapper

GUIDANCE = (
    "📖 *How to use:*\n\n"
    "Send an *email* or *mobile number* and I'll fetch everything from the database.\n\n"
    "`john@example.com`\n"
    "`+919876543210`\n\n"
    "Commands:\n"
    "  /help   — show this guide\n"
    "  /status — bot uptime & stats"
)


# ── API call ──────────────────────────────────────────────────────────────────

async def fetch_record(query: str) -> tuple[list | dict | None, str | None]:
    """
    Returns (data, error_message).
    URL format: {API_BASE_URL}/search={query}
    Retries once on 503. Returns friendly error strings on failure.
    """
    url     = f"{API_BASE_URL}/search={query}"
    headers = {"Content-Type": "application/json"}

    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            # Small delay before every API call to be gentle on the server
            await asyncio.sleep(1)

            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
            ) as client:
                response = await client.get(url, headers=headers)

                # DDoS lockdown
                if response.status_code == 503:
                    body = response.text
                    wait = "30"
                    # Try to extract wait seconds from message if present
                    import re
                    m = re.search(r"(\d+)\s*[sS]", body)
                    if m:
                        wait = m.group(1)
                    if attempt < API_RETRY_ATTEMPTS:
                        logger.warning("503 on attempt %d, retrying in %ds…", attempt, API_RETRY_DELAY)
                        await asyncio.sleep(API_RETRY_DELAY)
                        continue
                    return None, f"⚠️ API is under heavy load (DDoS protection). Please try again in *{wait} seconds*."

                response.raise_for_status()
                data = response.json()

                # Unwrap {"data": [...]} envelope if present
                if isinstance(data, dict) and "data" in data:
                    return data["data"], None
                return data, None

        except httpx.TimeoutException:
            if attempt < API_RETRY_ATTEMPTS:
                await asyncio.sleep(API_RETRY_DELAY)
                continue
            return None, "⏱ Request timed out. Please try again."

        except httpx.HTTPStatusError as e:
            logger.error("API HTTP %s: %s", e.response.status_code, e.response.text[:200])
            return None, f"❌ API returned error {e.response.status_code}. Please try again later."

        except Exception as e:
            logger.error("API request failed: %s", e)
            return None, "❌ Could not reach the API. Please try again later."

    return None, "❌ API unavailable after retries. Please try again later."


# ── Telegram handlers ─────────────────────────────────────────────────────────

@auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    text = (
        f"👋 *Welcome, {name}!*\n\n"
        "I'm *Drogon* 🐉 — your personal database lookup bot.\n\n"
        + GUIDANCE
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GUIDANCE, parse_mode="Markdown")


@auth_required
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime  = human_uptime(BOT_START_TIME)
    started = BOT_START_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        "🟢 *Drogon is running*\n\n"
        f"🕐 *Started:* `{started}`\n"
        f"⏱ *Uptime:* `{uptime}`\n"
        f"🔍 *Queries served:* `{TOTAL_QUERIES}`\n"
        f"🐍 *Python:* `{platform.python_version()}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_QUERIES
    query = update.message.text.strip()
    uid   = update.effective_user.id

    # Validate input
    if not (is_email(query) or is_mobile(query)):
        await update.message.reply_text(
            "⚠️ Send a valid *email address* or *mobile number*.\n\n"
            "Examples:\n`john@example.com`\n`+919876543210`",
            parse_mode="Markdown",
        )
        return

    # Per-user rate limit
    now = asyncio.get_event_loop().time()
    gap = now - _last_request[uid]
    if gap < RATE_LIMIT_SECONDS:
        wait = int(RATE_LIMIT_SECONDS - gap) + 1
        await update.message.reply_text(
            f"⏳ Please wait *{wait}s* before sending another request.",
            parse_mode="Markdown",
        )
        return
    _last_request[uid] = now

    # Show typing while fetching
    await context.bot.send_chat_action(update.effective_chat.id, action="typing")

    data, error = await fetch_record(query)
    TOTAL_QUERIES += 1

    # API error
    if error:
        await update.message.reply_text(error, parse_mode="Markdown")
        return

    # No results
    if not data:
        await update.message.reply_text(
            f"🔍 No record found for `{query}`.",
            parse_mode="Markdown",
        )
        return

    # Multiple records → paginate
    if isinstance(data, list) and data and isinstance(data[0], dict):
        context.user_data["results"] = data
        context.user_data["query"]   = query
        await send_page(update, context, data, 0, query)
        return

    # Single result (dict or list of strings)
    reply = (
        f"✅ *Record found* for `{query}`\n"
        f"{'─' * 30}\n"
        f"{format_record(data)}"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")


async def send_page(update, context, records, page, query=""):
    total  = len(records)
    record = records[page]
    text = (
        f"✅ *Result {page + 1} of {total}* for `{query}`\n"
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
    page    = int(q.data.split(":")[1])
    records = context.user_data.get("results", [])
    query   = context.user_data.get("query", "")
    if records:
        await send_page(update, context, records, page, query)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Use /help to see usage.")


# ── Health-check HTTP server ──────────────────────────────────────────────────

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status":        "ok",
        "bot":           "Drogon",
        "started_at":    BOT_START_TIME.isoformat(),
        "uptime":        human_uptime(BOT_START_TIME),
        "queries_total": TOTAL_QUERIES,
        "python":        platform.python_version(),
        "platform":      platform.system(),
    })


async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/",       health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Health server listening on port %s", PORT)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await run_health_server()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)   # prevents duplicate processing
        .build()
    )

    application.add_handler(CommandHandler("start",  start))
    application.add_handler(CommandHandler("help",   help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CallbackQueryHandler(paginate, pattern=r"^page:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Drogon is running…")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,   # drop queued msgs from when bot was offline
            allowed_updates=["message", "callback_query"],
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
