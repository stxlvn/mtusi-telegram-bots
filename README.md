# MTUSI Telegram bots (БАП2551)

Three small Telegram automations for one МТУСИ study-group chat, sharing one bot
account, one `.env` and one МТУСИ login:

| component | what it does | how it runs |
|---|---|---|
| **`telegram_post.py`** | every morning logs into lk.mtuci.ru, scrapes today's lessons and posts them as one **pinned native Telegram table** (Время / Предмет / Преподаватель / Аудитория) into the announcements chat; unpins yesterday's; posts nothing on empty days | cron, once a day |
| **`attendance_bot.py`** | self-check-in for lessons: at each lesson's start time posts a per-student button list into the subject's forum topic; students tap their name (identity verified against the group's contact list via a userbot); at lesson end posts a present/absent summary. Plus an **owner-only button panel** (`/links` in a DM) for the per-subject conference links | systemd service (long-poll) |
| **`lms_scraper.py`** | walks lms.mtuci.ru (Moodle), pulls each course's Контур.Толк / BBB conference link and pins it (as a **▶️ Подключиться** button) into the matching subject topic; only re-posts when the link changed | cron, once a day |

## Credits

The lk.mtuci.ru login + schedule parser (`src/`) is from
[**TheFoxKD/CalendarMTUSI**](https://github.com/TheFoxKD/CalendarMTUSI) (MIT) — originally
a Google Calendar sync. This project keeps that scraper (with two bugfixes, below) and
replaces the calendar output with the Telegram bots above.

Changes to the vendored code:
- `schedule_scraper.py` `_get_current_date()` — the site's date header switched from
  `"13 ноября 2024"` to numeric `"07.08.2026"`, which broke the parser.
- `auth.py` — the login form fill/submit grabbed an element handle and clicked it,
  which went stale if the Keycloak form re-rendered mid-interaction; now uses
  `page.fill()` / `page.click()` (re-resolve the selector right before acting).
- per-lesson debug screenshots are opt-in (`SCRAPING_DEBUG_SCREENSHOTS`); `LOG_LEVEL`
  is configurable (upstream logged every parsed lesson's raw HTML).
- `src/my_calendar/` (Google Calendar) is unused dead code, kept for reference.

## Setup

```bash
git clone https://github.com/stxlvn/mtusi-telegram-bots.git
cd mtusi-telegram-bots
python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install --with-deps firefox chromium

cp .env.example .env      # fill in МТУСИ creds, bot token, chat id, owner id
```

Everything is env-configured — see [`.env.example`](.env.example). `.env` and the whole
`data/` dir (roster, cookies, state — real personal data) are gitignored. The userbot
session for identity checks + LMS cookies is set up separately, see
[`userbot/README.md`](userbot/README.md).

## Running

```bash
# schedule digest — cron, e.g. 08:00
0 8 * * *  cd /path/mtusi-telegram-bots && ./venv/bin/python telegram_post.py >> data/schedule.log 2>&1

# LMS conference links — cron, e.g. 07:35
35 7 * * * cd /path/mtusi-telegram-bots && ./venv/bin/python lms_scraper.py >> data/lms_scraper.log 2>&1

# attendance bot — systemd service
ExecStart=/path/mtusi-telegram-bots/venv/bin/python /path/mtusi-telegram-bots/attendance_bot.py
```

### lms.mtuci.ru CAPTCHA

Moodle sits behind a slider-CAPTCHA shield that binds clearance to **IP + browser
TLS/JA3 fingerprint** — cookies transplanted from another machine/browser still get
challenged. What works: `lms_scraper.py` drives a Playwright **Firefox** engine with the
exact phone Firefox UA + cookies exported from that phone's Firefox *after solving the
CAPTCHA while the phone is on this server's VPN* (so the shield clears the server's own
IP). The owner refreshes those cookies through the bot's `/links` → **🔄 Обновить cookie
LMS** button; on expiry the bot DMs the owner.

## License

[MIT](LICENSE), inherited from the upstream project.
