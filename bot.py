import asyncio
import logging
import platform
import re
from collections import defaultdict
from datetime import datetime, timezone
from html import escape

import asyncpg
import httpx
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from config import BOT_TOKEN, API_BASE_URL, ALLOWED_USER_IDS, PORT, CHANNEL_ID, DATABASE_URL

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
BOT_START_TIME: datetime = datetime.now(timezone.utc)
TOTAL_QUERIES: int = 0
_last_request: dict[int, float] = defaultdict(float)
RATE_LIMIT_SECONDS = 5
API_RETRY_ATTEMPTS = 2
API_RETRY_DELAY    = 3

# DB pool (set on startup)
db_pool: asyncpg.Pool | None = None


# ── Database ──────────────────────────────────────────────────────────────────

async def init_db(pool: asyncpg.Pool):
    """Create tables if they don't exist."""
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id     BIGINT PRIMARY KEY,
            username        TEXT,
            first_name      TEXT,
            joined_at       TIMESTAMPTZ DEFAULT NOW(),
            last_seen       TIMESTAMPTZ DEFAULT NOW(),
            total_lookups   INTEGER DEFAULT 0
        )
    """)
    logger.info("DB tables ready")

async def upsert_user(user):
    """Insert new user or update last_seen on every interaction."""
    if not db_pool:
        return
    try:
        await db_pool.execute("""
            INSERT INTO users (telegram_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE
                SET username   = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_seen  = NOW()
        """, user.id, user.username, user.first_name)
    except Exception as e:
        logger.error("upsert_user failed: %s", e)

async def increment_lookup(user_id: int):
    """Bump lookup counter for a user."""
    if not db_pool:
        return
    try:
        await db_pool.execute("""
            UPDATE users SET total_lookups = total_lookups + 1,
                             last_seen     = NOW()
            WHERE telegram_id = $1
        """, user_id)
    except Exception as e:
        logger.error("increment_lookup failed: %s", e)

async def get_stats() -> dict:
    """Return aggregate stats for /status."""
    if not db_pool:
        return {}
    try:
        row = await db_pool.fetchrow("""
            SELECT COUNT(*)                         AS total_users,
                   COALESCE(SUM(total_lookups), 0)  AS total_lookups,
                   MAX(last_seen)                   AS last_active
            FROM users
        """)
        return dict(row)
    except Exception as e:
        logger.error("get_stats failed: %s", e)
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_email(text: str) -> bool:
    return "@" in text and "." in text.split("@")[-1]

def is_mobile(text: str) -> bool:
    digits = text.replace("+", "").replace(" ", "").replace("-", "")
    return digits.isdigit() and 7 <= len(digits) <= 15

def normalize_mobile(text: str) -> str:
    digits = text.replace("+", "").replace(" ", "").replace("-", "")
    if len(digits) == 10 and digits.isdigit():
        return "91" + digits
    return digits

def fmt(value) -> str:
    return escape(str(value)) if value not in (None, "", [], {}) else "—"

def format_record(record) -> str:
    if not record:
        return "<i>(empty record)</i>"
    if isinstance(record, list) and record and isinstance(record[0], str):
        return "\n".join(f"• {escape(line)}" for line in record)
    if isinstance(record, dict):
        return "\n".join(
            f"• <b>{escape(str(k))}</b>: <code>{fmt(v)}</code>"
            for k, v in record.items()
        )
    return escape(str(record))

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

async def send_html(update: Update, text: str, **kwargs):
    await update.message.reply_text(text, parse_mode="HTML", **kwargs)


# ── Channel membership ────────────────────────────────────────────────────────

async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except BadRequest:
        return False
    except Exception as e:
        logger.error("Membership check failed: %s", e)
        return False

def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
        [InlineKeyboardButton("✅ Verify Membership", callback_data="verify")],
    ])

async def prompt_join(update: Update):
    text = (
        "🔒 <b>Access Restricted</b>\n\n"
        f"You must join {escape(CHANNEL_ID)} to use this bot.\n\n"
        "1️⃣ Click <b>Join Channel</b>\n"
        "2️⃣ Click <b>Verify Membership</b>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=join_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=join_keyboard())

def auth_required(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await upsert_user(user)
        if ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS:
            return await func(update, context)
        if not await is_member(context.bot, user.id):
            await prompt_join(update)
            return
        return await func(update, context)
    return wrapper


# ── API call ──────────────────────────────────────────────────────────────────

async def fetch_record(query: str) -> tuple[list | dict | None, str | None]:
    url     = f"{API_BASE_URL}{query}"
    headers = {"Content-Type": "application/json"}

    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            await asyncio.sleep(1)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)

                if response.status_code == 503:
                    m    = re.search(r"(\d+)\s*[sS]", response.text)
                    wait = m.group(1) if m else "30"
                    if attempt < API_RETRY_ATTEMPTS:
                        await asyncio.sleep(API_RETRY_DELAY)
                        continue
                    return None, f"⚠️ API is under heavy load.\nPlease try again in <b>{wait} seconds</b>."

                response.raise_for_status()
                data = response.json()
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
            return None, f"❌ API error <code>{e.response.status_code}</code>. Please try again later."
        except Exception as e:
            logger.error("API request failed: %s", e)
            return None, "❌ Could not reach the API. Please try again later."

    return None, "❌ API unavailable after retries."


# ── Messages ──────────────────────────────────────────────────────────────────

def welcome_msg(name: str) -> str:
    return (
        f"👋 <b>Welcome, {name}!</b>\n\n"
        "I'm <b>Drogon</b> — your free info bot.\n\n"
        "📖 <b>How to use:</b>\n\n"
        "Just send an email or mobile number — I'll fetch everything from the database.\n\n"
        "<code>user@example.com</code>\n"
        "<code>9876543210</code>\n\n"
        "Commands:\n"
        "  /help   — show this guide"
    )

GUIDANCE = (
    "📖 <b>How to use:</b>\n\n"
    "Send an email or mobile number and I'll fetch everything from the database.\n\n"
    "<code>john@example.com</code>\n"
    "<code>+919876543210</code>"
)


# ── Telegram handlers ─────────────────────────────────────────────────────────

@auth_required
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = escape(update.effective_user.first_name or "User")
    await send_html(update, welcome_msg(name))

@auth_required
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(update, GUIDANCE)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Hidden — only ALLOWED_USER_IDS can see it, silently ignored for others
    if not ALLOWED_USER_IDS or update.effective_user.id not in ALLOWED_USER_IDS:
        return

    uptime  = human_uptime(BOT_START_TIME)
    started = BOT_START_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")
    stats   = await get_stats()

    total_users   = stats.get("total_users",   "—")
    total_lookups = stats.get("total_lookups", "—")
    last_active   = stats.get("last_active")
    last_active_str = last_active.strftime("%Y-%m-%d %H:%M UTC") if last_active else "—"

    await send_html(update,
        "🟢 <b>Drogon Status</b>\n\n"
        f"🕐 <b>Started:</b> <code>{started}</code>\n"
        f"⏱ <b>Uptime:</b> <code>{uptime}</code>\n\n"
        f"👥 <b>Total users:</b> <code>{total_users}</code>\n"
        f"🔍 <b>Total lookups:</b> <code>{total_lookups}</code>\n"
        f"🕓 <b>Last active:</b> <code>{last_active_str}</code>\n\n"
        f"🐍 <b>Python:</b> <code>{platform.python_version()}</code>"
    )

@auth_required
async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_QUERIES
    query = update.message.text.strip()
    uid   = update.effective_user.id

    if not (is_email(query) or is_mobile(query)):
        await send_html(update,
            "⚠️ Send a valid <b>email address</b> or <b>mobile number</b>.\n\n"
            "Examples:\n<code>john@example.com</code>\n<code>+919876543210</code>"
        )
        return

    if is_mobile(query):
        query = normalize_mobile(query)

    now = asyncio.get_event_loop().time()
    if now - _last_request[uid] < RATE_LIMIT_SECONDS:
        wait = int(RATE_LIMIT_SECONDS - (now - _last_request[uid])) + 1
        await send_html(update, f"⏳ Please wait <b>{wait}s</b> before the next request.")
        return
    _last_request[uid] = now

    await context.bot.send_chat_action(update.effective_chat.id, action="typing")
    data, error = await fetch_record(query)

    TOTAL_QUERIES += 1
    await increment_lookup(uid)

    if error:
        await send_html(update, error)
        return
    if not data:
        await send_html(update, f"🔍 No record found for <code>{escape(query)}</code>.")
        return

    if isinstance(data, list) and data and isinstance(data[0], dict):
        context.user_data["results"] = data
        context.user_data["query"]   = query
        await send_page(update, context, data, 0, query)
        return

    await send_html(update,
        f"✅ <b>Record found</b> for <code>{escape(query)}</code>\n"
        f"{'─' * 30}\n"
        f"{format_record(data)}"
    )


async def send_page(update, context, records, page, query=""):
    total = len(records)
    text = (
        f"✅ <b>Result {page + 1} of {total}</b> for <code>{escape(query)}</code>\n"
        f"{'─' * 30}\n"
        f"{format_record(records[page])}"
    )
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page:{page - 1}"))
    if page < total - 1:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page + 1}"))
    markup = InlineKeyboardMarkup([buttons]) if buttons else None

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page    = int(q.data.split(":")[1])
    records = context.user_data.get("results", [])
    query   = context.user_data.get("query", "")
    if records:
        await send_page(update, context, records, page, query)


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    await upsert_user(user)

    if await is_member(context.bot, user.id):
        name = escape(user.first_name or "User")
        await q.edit_message_text(
            f"✅ <b>Verified, {name}!</b>\n\n"
            "You now have full access. Send an email or mobile number to search.\n\n"
            + GUIDANCE,
            parse_mode="HTML",
        )
    else:
        await q.edit_message_text(
            "❌ <b>Not a member yet.</b>\n\n"
            "Please join the channel first, then click <b>Verify</b> again.",
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )


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
    global db_pool

    await run_health_server()

    # Connect to Neon DB
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await init_db(db_pool)
        logger.info("Database connected")
    except Exception as e:
        logger.error("Database connection failed: %s — running without DB", e)

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(CommandHandler("start",  start))
    application.add_handler(CommandHandler("help",   help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(paginate,        pattern=r"^page:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Drogon is running…")
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())