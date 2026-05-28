# ─────────────────────────────────────────────
#  config.py  –  edit this before deploying
# ─────────────────────────────────────────────
import os

# 1. Telegram bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# 2. Your REST API base URL (no trailing slash, ends with search=)
API_BASE_URL = os.getenv("API_BASE_URL", "https://your-api.com/v1/search=")

# 3. Whitelist of Telegram user IDs (empty = allow everyone who is in channel)
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = (
    [int(x.strip()) for x in _raw_ids.split(",") if x.strip()]
    if _raw_ids else []
)

# 4. Port for health-check server (Render sets this automatically)
PORT: int = int(os.getenv("PORT", "8080"))

# 5. Your Telegram channel username (with @) or numeric ID
#    Example: "@mychannel"  or  "-1001234567890"
#    Bot MUST be an admin of this channel to check membership.
CHANNEL_ID = os.getenv("CHANNEL_ID", "@yourchannel")
