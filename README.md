# Drogon 🐉
Telegram bot that looks up records from your API by email or mobile number.

## Setup

```bash
pip install -r requirements.txt
python bot.py
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `API_BASE_URL` | ✅ | Your API base URL e.g. `https://your-api.com/v1` |
| `ALLOWED_USER_IDS` | ❌ | Comma-separated Telegram user IDs e.g. `123456,789012` — leave empty to allow everyone |
| `PORT` | ❌ | Health server port (default `8080`, Render sets this automatically) |

## Usage

Send an email or mobile number directly in chat:
```
john@example.com
+919876543210
```

## Commands
| Command | Description |
|---|---|
| `/start` | Show help |
| `/status` | Bot uptime & query count |

## Health Check
```
GET /health
```
Point UptimeRobot at `https://your-app.onrender.com/health` — ping every **5 minutes**.
