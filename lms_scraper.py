#!/usr/bin/env python3
"""Scrape lms.mtuci.ru (Moodle) for per-course Контур.Толк / BBB conference links
and publish them into the matching Telegram subject topics.

lms.mtuci.ru sits behind a slider-CAPTCHA shield that binds clearance to
IP + browser fingerprint, so it can only be reached with a real Firefox engine
using cookies exported from a browser that solved the CAPTCHA while routed
through this server's own IP (its VPN endpoint). Cookies are refreshed by the
owner through bot.py (/links -> "Обновить cookie LMS"); on expiry the owner is DM'd.

Not a standalone job — bot.py's main loop spawns this once a day and on demand.
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import bot  # noqa: E402

SESSION_FILE = bot.LMS_SESSION_FILE
COURSES_URL = "https://lms.mtuci.ru/lms/my/courses.php"
LOGIN_URL = "https://lms.mtuci.ru/login/index.php"
# names of the anti-bot shield cookies — clearance is bound to IP + TLS fingerprint
SHIELD_COOKIE_RE = re.compile(r"^(__cap|__hash|__lhash|__ddg|__cf)")
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


def _persist_jar(session, cookies):
    """Save the full cookie jar back (keeps shield cookies like __lhash_ rolling
    forward, plus the fresh MoodleSession after an SSO re-login)."""
    keep = [{"name": c["name"], "value": c["value"],
             "domain": c["domain"], "path": c.get("path", "/")}
            for c in cookies
            if c["domain"].endswith("mtuci.ru")
            and (c["name"] == "MoodleSession" or SHIELD_COOKIE_RE.match(c["name"]))]
    if keep:
        session["cookies"] = keep
        session["updated"] = bot.msk_now().isoformat()
        bot.save_json(SESSION_FILE, session)


async def _is_captcha(page):
    return "captcha" in (await page.title()).lower()


async def _sso_login(page):
    """The MoodleSession idle-timed out but shield clearance still holds: we land
    on the shared MTUCI Keycloak form. Log back in with the MTUCI credentials
    (same realm/creds as the lk.mtuci.ru schedule scraper)."""
    email = os.environ.get("MTUCI_EMAIL")
    password = os.environ.get("MTUCI_PASSWORD")
    if not email or not password:
        print("no MTUCI_EMAIL/PASSWORD for SSO re-login", flush=True)
        return False
    try:
        await page.wait_for_selector("#username", state="visible", timeout=15000)
        await page.fill("#username", email)
        await page.fill("#password", password)
        await page.click("#login-submit-button, #kc-login, button[type=submit]",
                         timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception as e:
        print(f"SSO re-login failed: {e}", flush=True)
        return False
    ok = "lms.mtuci.ru" in page.url and "/login/" not in page.url
    print(f"SSO re-login {'ok' if ok else 'did not stick'} -> {page.url}", flush=True)
    return ok


async def scrape(session):
    found = {}  # course_name -> conference url
    async with async_playwright() as pw:
        br = await pw.firefox.launch(headless=True)
        ua = session.get("ua") or os.environ.get(
            "LMS_UA", "Mozilla/5.0 (Android 12; Mobile; rv:155.0) Gecko/155.0 Firefox/155.0")
        ctx = await br.new_context(user_agent=ua,
                                   viewport={"width": 1280, "height": 2200})
        await ctx.add_cookies(session["cookies"])
        page = await ctx.new_page()
        await page.goto(COURSES_URL, wait_until="networkidle", timeout=45000)

        if await _is_captcha(page):
            await br.close()
            return None  # shield clearance lost (IP changed / fingerprint) — needs a human

        if "/login/" in page.url or "bvzauth" in page.url or "lk.mtuci.ru" in page.url:
            if not await _sso_login(page):
                await br.close()
                return None
            await page.goto(COURSES_URL, wait_until="networkidle", timeout=45000)
            if await _is_captcha(page) or "/login/" in page.url:
                await br.close()
                return None

        _persist_jar(session, await ctx.cookies())

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
        _persist_jar(session, await ctx.cookies())
        await br.close()
    return found


async def keepalive(session):
    """Cheap warm-up: hit an authenticated page, re-login via SSO if the Moodle
    session idled out, roll the cookie jar forward. Returns True while the shield
    still clears this host, False when a human needs to re-solve the CAPTCHA."""
    async with async_playwright() as pw:
        br = await pw.firefox.launch(headless=True)
        ua = session.get("ua") or os.environ.get(
            "LMS_UA", "Mozilla/5.0 (Android 12; Mobile; rv:155.0) Gecko/155.0 Firefox/155.0")
        ctx = await br.new_context(user_agent=ua, viewport={"width": 1280, "height": 900})
        await ctx.add_cookies(session["cookies"])
        page = await ctx.new_page()
        try:
            await page.goto("https://lms.mtuci.ru/lms/my/",
                            wait_until="networkidle", timeout=45000)
            if await _is_captcha(page):
                return False
            if "/login/" in page.url or "bvzauth" in page.url or "lk.mtuci.ru" in page.url:
                if not await _sso_login(page):
                    return False
            _persist_jar(session, await ctx.cookies())
            return True
        finally:
            await br.close()


CAPTCHA_MSG = (
    "⚠️ LMS не пускает: капча не пройдена для IP этого сервера "
    "(обычно после перезагрузки сервера — сменился IP).\n\n"
    "Нужно один раз пройти слайдер вручную:\n"
    "1. Подключи телефон к VPN этого сервера.\n"
    "2. Открой lms.mtuci.ru в своём браузере, пройди капчу, залогинься.\n"
    "3. /links → 🔄 Обновить cookie LMS → вставь cookie-файл.\n\n"
    "Пароль от МТУСИ бот вводит сам — обновлять cookie нужно только при смене IP."
)


def _captcha_notify_once(cfg):
    """DM the owner the CAPTCHA instructions at most once per outage."""
    state = bot.load_json(cfg["state_file"], {})
    if not state.get("lms_captcha_notified"):
        notify_owner(cfg, CAPTCHA_MSG)
        state["lms_captcha_notified"] = True
        bot.save_json(cfg["state_file"], state)


def _clear_captcha_flag(cfg):
    state = bot.load_json(cfg["state_file"], {})
    if state.get("lms_captcha_notified"):
        state["lms_captcha_notified"] = None
        bot.save_json(cfg["state_file"], state)


def main():
    cfg = bot.load_config()
    session = bot.load_json(SESSION_FILE, None)
    if not session:
        print("no lms_session.json", flush=True)
        if "--keepalive" not in sys.argv:
            _captcha_notify_once(cfg)
        return

    if "--keepalive" in sys.argv:
        ok = asyncio.run(keepalive(session))
        print(f"keepalive: {'ok' if ok else 'CAPTCHA lost'}", flush=True)
        if ok:
            _clear_captcha_flag(cfg)
        else:
            _captcha_notify_once(cfg)
        return

    result = asyncio.run(scrape(session))
    if result is None:
        print("LMS session expired", flush=True)
        _captcha_notify_once(cfg)
        return
    _clear_captcha_flag(cfg)

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
