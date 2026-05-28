# ─────────────────────────────────────────────
#  config.py  –  edit this before deploying
# ─────────────────────────────────────────────
import os

# 1. Telegram bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# 2. Your REST API base URL (no trailing slash, ends with search=)
API_BASE_URL = os.getenv("API_BASE_URL", "https://your-api.com/v1/search=")

# 3. Whitelist of Telegram user IDs (empty = allow everyone in channel)
#    Comma-separated: "123456789,987654321"
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = (
    [int(x.strip()) for x in _raw_ids.split(",") if x.strip()]
    if _raw_ids else []
)

# 4. Port (Render sets this automatically)
PORT: int = int(os.getenv("PORT", "8080"))

# 5. Your Telegram channel (with @) — bot must be admin of this channel
CHANNEL_ID = os.getenv("CHANNEL_ID", "@yourchannel")

# 6. Neon PostgreSQL connection string
#    Get from: neon.tech → your project → Connection string
#    Format: postgresql://user:password@host/dbname?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@host/dbname?sslmode=require")