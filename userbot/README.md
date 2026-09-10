# userbot/

A [Telethon](https://docs.telethon.dev/) **user** session (not a bot) — logged into
a real Telegram account (the group's headman). It's used for the things the Bot API
can't do:

- reading the group's participant list → `participants.json` (telegram id ↔ real name,
  so `attendance_bot.py` can verify nobody registers under someone else's name);
- solving the lms.mtuci.ru slider-CAPTCHA in a phone browser and handing the cookies
  to `lms_scraper.py` (the shield binds clearance to IP + browser fingerprint, so the
  session must be created from a browser routed through this server's IP — e.g. its
  own WireGuard/AmneziaWG endpoint).

Nothing in this directory is committed except this file: the `.session` grants full
access to that account, `api_hash` is an app secret, and `participants.json` is PII.

## One-time setup

```bash
python -m venv userbot_venv
./userbot_venv/bin/pip install telethon

./userbot_venv/bin/python - <<'PY'
from telethon.sync import TelegramClient
# api_id / api_hash from https://my.telegram.org
TelegramClient("userbot/session", API_ID, "API_HASH").start(phone="+7...")
PY
```

Then dump the participant list once (and re-run whenever the roster changes):

```python
import asyncio, json
from telethon import TelegramClient
CHAT_ID = -100...   # your group
async def main():
    c = TelegramClient("userbot/session", API_ID, "API_HASH")
    await c.start()
    ps = await c.get_participants(CHAT_ID)
    json.dump([{"id": p.id, "first_name": p.first_name, "last_name": p.last_name,
               "username": p.username, "is_bot": p.bot} for p in ps],
              open("userbot/participants.json", "w"), ensure_ascii=False, indent=2)
asyncio.run(main())
```
