# userbot/

[Telethon](https://docs.telethon.dev/) **user**-сессия (не бот) — вход в реальный
Telegram-аккаунт (староста группы). Нужна для того, что Bot API не умеет:

- получить список участников группы → `participants.json` (telegram id ↔ настоящее имя,
  чтобы `attendance_bot.py` мог проверять, что никто не отмечается под чужим именем);
- один раз пройти слайдер-капчу lms.mtuci.ru в браузере телефона и передать cookie
  в `lms_scraper.py` (щит привязывает «пройденность» к IP + отпечатку браузера, поэтому
  сессию надо создавать из браузера, выходящего в сеть через IP этого сервера —
  например через его WireGuard/AmneziaWG).

В git из этого каталога попадает только этот файл: `.session` даёт полный доступ к
аккаунту, `api_hash` — секрет приложения, `participants.json` — персональные данные.

## Первичная настройка

```bash
python -m venv userbot_venv
./userbot_venv/bin/pip install telethon

./userbot_venv/bin/python - <<'PY'
from telethon.sync import TelegramClient
# api_id / api_hash с https://my.telegram.org
TelegramClient("userbot/session", API_ID, "API_HASH").start(phone="+7...")
PY
```

Один раз выгрузить список участников (и повторять при изменении состава группы):

```python
import asyncio, json
from telethon import TelegramClient
CHAT_ID = -100...   # ваша группа
async def main():
    c = TelegramClient("userbot/session", API_ID, "API_HASH")
    await c.start()
    ps = await c.get_participants(CHAT_ID)
    json.dump([{"id": p.id, "first_name": p.first_name, "last_name": p.last_name,
               "username": p.username, "is_bot": p.bot} for p in ps],
              open("userbot/participants.json", "w"), ensure_ascii=False, indent=2)
asyncio.run(main())
```
