#!/usr/bin/env python3
"""Scrape lms.mtuci.ru (Moodle) for per-course Контур.Толк / BBB conference links
and publish them into the matching Telegram subject topics.

lms.mtuci.ru sits behind a slider-CAPTCHA shield that binds clearance to
IP + browser fingerprint, so it can only be reached with a real Firefox engine
using cookies exported from a browser that solved the CAPTCHA while routed
through this server's IP (dim-wiluite VPN). Session is refreshed by the owner via
the bot's /lms command. On expiry the owner is DM'd.
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import attendance_bot as bot  # noqa: E402

SESSION_FILE = bot.LMS_SESSION_FILE
COURSES_URL = "https://lms.mtuci.ru/lms/my/courses.php"
KTALK_RE = re.compile(
    r"https://[a-z0-9.-]*ktalk\.ru/[A-Za-z0-9._-]+"
    r"|https://[a-z0-9.-]*zoom\.[a-z]+/j/[0-9]+(?:\?pwd=[A-Za-z0-9._-]+)?"
    r"|https://[a-z0-9.-]*bbb[a-z0-9.-]*/[A-Za-z0-9/_?=&.-]+"
)


def notify_owner(cfg, text):
    if cfg.get("owner_telegram_id"):
        try:
            bot.tg_api(cfg, "sendMessage", chat_id=cfg["owner_telegram_id"], text=text)
        except Exception as e:
            print(f"notify_owner failed: {e}", flush=True)


async def scrape(session):
    found = {}  # course_name -> conference url
    async with async_playwright() as pw:
        br = await pw.firefox.launch(headless=True)
        ctx = await br.new_context(user_agent=session["ua"],
                                   viewport={"width": 1280, "height": 2200})
        await ctx.add_cookies(session["cookies"])
        page = await ctx.new_page()
        await page.goto(COURSES_URL, wait_until="networkidle", timeout=45000)

        if "captcha" in (await page.title()).lower() or "/login/" in page.url:
            await br.close()
            return None  # session expired

        courses = await page.eval_on_selector_all(
            "a[href*='/course/view.php?id=']",
            """els => {
                const m = {};
                for (const e of els) {
                    const t = (e.getAttribute('aria-label') || e.textContent || '').trim();
                    if (t && t !== 'Название курса' && !t.includes('избранным'))
                        m[e.href.split('#')[0]] = t;
                }
                return Object.entries(m);
            }""")

        for href, name in courses:
            try:
                await page.goto(href, wait_until="networkidle", timeout=45000)
                act = await page.eval_on_selector_all(
                    "a[href*='/mod/konturtalk/'], a[href*='/mod/bigbluebuttonbn/'], "
                    "a[href*='/mod/zoom/'], a[href*='/mod/url/']",
                    "els => [...new Set(els.map(e => e.href))]")
                if not act:
                    continue
                await page.goto(act[0], wait_until="networkidle", timeout=45000)
                html = await page.content()
                m = KTALK_RE.search(html)
                if m:
                    found[name] = m.group(0)
                else:
                    # a /mod/url/ activity redirects straight to the meeting
                    if "/mod/url/" in act[0] and KTALK_RE.search(page.url):
                        found[name] = page.url
            except Exception as e:
                print(f"course '{name}' failed: {e}", flush=True)
        await br.close()
    return found


def main():
    cfg = bot.load_config()
    session = bot.load_json(SESSION_FILE, None)
    if not session:
        print("no lms_session.json", flush=True)
        return

    result = asyncio.run(scrape(session))
    if result is None:
        print("LMS session expired", flush=True)
        notify_owner(cfg, "⚠️ LMS-сессия истекла — автопарсер конференций не может зайти.\n"
                          "Обнови cookie: /lms в личке боту.")
        return

    subjects = list(bot.load_topic_subjects(cfg))
    links = bot.load_json(cfg["conf_links_file"], {})
    updated = []
    for course_name, url in result.items():
        clean = re.sub(r"\s*\([^)]*\)", "", course_name).strip()
        subj = bot.match_subject(clean, subjects)
        if not subj:
            print(f"no topic match for course '{course_name}'", flush=True)
            continue
        if links.get(subj, {}).get("url") == url:
            continue
        msg = bot.set_conf_link(cfg, subj, url)
        cur = bot.load_json(cfg["conf_links_file"], {})
        if subj in cur:
            cur[subj]["auto"] = True
            bot.save_json(cfg["conf_links_file"], cur)
        updated.append(f"{subj}: {url}")
        print(msg, flush=True)

    print(f"done, {len(result)} conf links found, {len(updated)} updated", flush=True)
    if updated:
        notify_owner(cfg, "🎥 Автопарсер LMS обновил ссылки:\n" + "\n".join(updated))
    elif "--report" in sys.argv:
        notify_owner(cfg, f"🎥 Прогнал парсер LMS: нашёл {len(result)} ссылок, изменений нет.")


if __name__ == "__main__":
    main()
