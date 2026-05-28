# ─────────────────────────────────────────────
#  config.py  –  edit this before deploying
# ─────────────────────────────────────────────
import os

# 1. Telegram bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# 2. Your REST API base URL (no trailing slash)
API_BASE_URL = os.getenv("API_BASE_URL", "https://your-api.com/v1")

# 3. Whitelist of Telegram user IDs (empty list = allow everyone)
#    Set env var as comma-separated: "123456789,987654321"
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = (
    [int(x.strip()) for x in _raw_ids.split(",") if x.strip()]
    if _raw_ids else []
)

# 4. Port for the health-check HTTP server
#    Render injects $PORT automatically; default 8080 for local dev
PORT: int = int(os.getenv("PORT", "8080"))
