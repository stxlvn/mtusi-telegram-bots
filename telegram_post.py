#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from src.core.logging import configure_logging  # noqa: E402
from src.scraping.auth import AuthConfig  # noqa: E402
from src.scraping.schedule_scraper import MTUCIScheduleScraper  # noqa: E402

configure_logging()


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return value


MTUCI_EMAIL = require_env("MTUCI_EMAIL")
MTUCI_PASSWORD = require_env("MTUCI_PASSWORD")
MTUCI_LOGIN_URL = os.environ.get("MTUCI_LOGIN_URL", "https://lk.mtuci.ru/auth/login")
GROUP_LABEL = os.environ.get("GROUP_LABEL", "")

TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(require_env("TELEGRAM_CHAT_ID"))
# Optional: a private chat (e.g. your own) that gets full tracebacks on failure,
# so the group only ever sees a short friendly notice. Leave unset to disable.
DEBUG_CHAT_ID = int(os.environ["DEBUG_CHAT_ID"]) if os.environ.get("DEBUG_CHAT_ID") else None

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(SCRIPT_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
TOPICS_FILE = os.environ.get("TOPICS_FILE", os.path.join(DATA_DIR, "topics.json"))
# {"<subject as lk.mtuci.ru names it>": "<existing topic subject>"}
SUBJECT_ALIASES_FILE = os.environ.get("SUBJECT_ALIASES_FILE",
                                      os.path.join(DATA_DIR, "subject_aliases.json"))
PIN_STATE_FILE = os.environ.get("PIN_STATE_FILE", os.path.join(DATA_DIR, "pinned_schedule.json"))

MSK_OFFSET = timedelta(hours=3)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def msk_now():
    # naive, MSK wall-clock — see bot.py's msk_now() for why (must stay
    # comparable with the scraper's naive lesson start/end datetimes)
    return datetime.now(UTC).replace(tzinfo=None) + MSK_OFFSET


async def fetch_events():
    auth_config = AuthConfig(
        email=MTUCI_EMAIL,
        password=MTUCI_PASSWORD,
        login_url=MTUCI_LOGIN_URL,
    )
    scraper = MTUCIScheduleScraper(auth_config=auth_config, max_retries=5, timeout_ms=60000)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                ),
            )
            page = await context.new_page()
            events = await scraper.parse_schedule(page)
        finally:
            await browser.close()
    return events


async def fetch_events_retrying(max_attempts=3, delay_sec=15):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fetch_events()
        except Exception as e:
            last_error = e
            print(f"fetch_events attempt {attempt}/{max_attempts} failed: {e}", flush=True)
            if attempt < max_attempts:
                await asyncio.sleep(delay_sec)
    raise last_error


def format_time(dt):
    return dt.strftime("%H:%M")


def load_topics():
    if os.path.exists(TOPICS_FILE):
        with open(TOPICS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_topics(topics):
    tmp = TOPICS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(topics, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TOPICS_FILE)


def tg_api(method, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, data=params, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error in {method}: {result}")
    return result["result"]


def topic_subject(subject):
    """Map a schedule subject onto the topic subject it is posted under, for
    when lk.mtuci.ru's name differs from the group's existing topic."""
    if os.path.exists(SUBJECT_ALIASES_FILE):
        with open(SUBJECT_ALIASES_FILE, encoding="utf-8") as f:
            return json.load(f).get(subject, subject)
    return subject


# Not called from main() below — kept as a public helper for other tools that post
# into per-subject topics (e.g. a companion attendance bot).
def ensure_topic(subject):
    subject = topic_subject(subject)
    topics = load_topics()
    if subject in topics:
        return topics[subject]
    result = tg_api("createForumTopic", chat_id=TELEGRAM_CHAT_ID, name=subject[:128])
    thread_id = result["message_thread_id"]
    topics[subject] = thread_id
    save_topics(topics)
    print(f"created topic for '{subject}' -> {thread_id}", flush=True)
    return thread_id


def send_general(text):
    tg_api("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text)


def send_debug(text):
    if DEBUG_CHAT_ID is None:
        return
    try:
        tg_api("sendMessage", chat_id=DEBUG_CHAT_ID, text=text[:4000])
    except Exception as e:
        print(f"send_debug failed: {e}", flush=True)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_daily_digest_rich(day_events, target_date):
    weekday = WEEKDAYS[target_date.weekday()]
    label = f" — {GROUP_LABEL}" if GROUP_LABEL else ""
    header = f"📅 Расписание на {target_date.strftime('%d.%m.%Y')} ({weekday}){label}"

    rows_html = "".join(
        f"<tr><td>{escape_html(format_time(e.start_time))}-{escape_html(format_time(e.end_time))}</td>"
        f"<td>{escape_html(e.subject)}</td>"
        f"<td>{escape_html(e.teacher)}</td>"
        f"<td>{escape_html(str(e.location))}</td></tr>"
        for e in sorted(day_events, key=lambda x: x.start_time)
    )
    html = (
        f"<h3>{escape_html(header)}</h3>"
        "<table><tr><th>Время</th><th>Предмет</th><th>Преподаватель</th><th>Аудитория</th></tr>"
        f"{rows_html}</table>"
    )
    return {"html": html}


def load_pin_state():
    if os.path.exists(PIN_STATE_FILE):
        with open(PIN_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_pin_state(state):
    tmp = PIN_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PIN_STATE_FILE)


def post_and_pin_digest(day_events, target_date):
    pin_state = load_pin_state()
    old_msg_id = pin_state.get("message_id")
    if old_msg_id:
        try:
            tg_api("unpinChatMessage", chat_id=TELEGRAM_CHAT_ID, message_id=old_msg_id)
        except Exception as e:
            print(f"unpin of previous digest failed (probably already unpinned): {e}", flush=True)

    rich_message = build_daily_digest_rich(day_events, target_date)
    result = tg_api("sendRichMessage", chat_id=TELEGRAM_CHAT_ID, rich_message=json.dumps(rich_message))
    msg_id = result["message_id"]
    tg_api("pinChatMessage", chat_id=TELEGRAM_CHAT_ID, message_id=msg_id, disable_notification=True)
    save_pin_state({"message_id": msg_id, "date": target_date.date().isoformat()})
    print(f"posted+pinned daily digest in Обьявления, message_id={msg_id}", flush=True)


def main():
    target_date = msk_now()
    try:
        events = asyncio.run(fetch_events_retrying())
    except Exception as e:
        print(f"fetch_events_retrying failed for {target_date.strftime('%d.%m.%Y')}: {e}", flush=True)
        send_general(
            f"⚠️ ЛК МТУСИ сейчас недоступен, получить расписание на "
            f"{target_date.strftime('%d.%m.%Y')} не удалось."
        )
        send_debug(f"mtusi-schedule-bot: fetch failed for {target_date.strftime('%d.%m.%Y')}\n{e!r}")
        return

    day_events = [e for e in events if e.start_time.date() == target_date.date()]
    if not day_events:
        print("no lessons today, skipping post", flush=True)
        return

    try:
        post_and_pin_digest(day_events, target_date)
    except Exception as e:
        print(f"FAILED to post/pin daily digest: {e}", flush=True)


if __name__ == "__main__":
    main()
