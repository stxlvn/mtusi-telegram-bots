#!/usr/bin/env python3
"""Scrape lms.mtuci.ru (Moodle) for per-course Контур.Толк / BBB conference links
and publish them into the matching Telegram subject topics.

lms.mtuci.ru sits behind an anti-bot shield that hits non-Russian IPs with a
slider CAPTCHA. This scraper routes through a Russian SOCKS proxy (``LMS_PROXY``);
from a RU IP there is no CAPTCHA. The login step still needs a real (JS-capable)
browser though — lk.mtuci.ru's Keycloak entry sometimes serves a transient
JS-redirect interstitial (a blank "noindex" spinner page) before the actual
login form, which a plain HTTP client can never get past. So: log in once via
a minimal Playwright sequence through the proxy, then hand the resulting
cookies to a plain ``requests`` session for the rest — course listing via
Moodle's AJAX endpoint, course/module pages.

bot.py's main loop spawns this once a day; also runnable standalone.
"""
import asyncio
import os
import re
import sys
import time

import requests
from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import bot  # noqa: E402

LMS = "https://lms.mtuci.ru"
LOGIN_URL = f"{LMS}/login/index.php"
DASH_URL = f"{LMS}/lms/my/"
COURSELIST_METHOD = "core_course_get_enrolled_courses_by_timeline_classification"

PROXY = os.environ.get("LMS_PROXY", "socks5h://127.0.0.1:1081")
UA = os.environ.get(
    "LMS_UA",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")

KTALK_RE = re.compile(
    r"https://[a-z0-9.-]*ktalk\.ru/[A-Za-z0-9._-]+"
    r"|https://[a-z0-9.-]*zoom\.[a-z]+/j/[0-9]+(?:\?pwd=[A-Za-z0-9._-]+)?"
    r"|https://[a-z0-9.-]*bbb[a-z0-9.-]*/[A-Za-z0-9/_?=&.-]+", re.I)
MOD_RE = re.compile(
    r"https://lms\.mtuci\.ru/(?:lms/)?mod/(?:konturtalk|bigbluebuttonbn|zoom|url)"
    r"/view\.php\?id=\d+", re.I)


class LMSError(Exception):
    pass


def notify_owner(cfg, text):
    if cfg.get("owner_telegram_id"):
        try:
            bot.tg_api(cfg, "sendMessage", chat_id=cfg["owner_telegram_id"], text=text)
        except Exception as e:
            print(f"notify_owner failed: {e}", flush=True)


async def _browser_login_cookies():
    """Log in via a real browser routed through the RU proxy and hand back its
    cookie jar. Deliberately NOT using the shared MTUCIAuthenticator here: the
    LMS SSO flow bounces through several more redirects than lk.mtuci.ru's own
    login (Keycloak's JS-redirect interstitial, then its OIDC form_post
    auto-submit hop back to lms.mtuci.ru/auth/oidc/), and the authenticator's
    DOM-querying validation step raced one of those navigations and blew up
    with "Execution context was destroyed". So: only navigation-safe waits
    here (wait_for_selector / wait_for_url), no querying mid-redirect — the
    real verdict is just whether MoodleSession shows up in the cookie jar."""
    email = os.environ.get("MTUCI_EMAIL")
    password = os.environ.get("MTUCI_PASSWORD")
    if not email or not password:
        raise LMSError("MTUCI_EMAIL / MTUCI_PASSWORD not set")

    proxy_server = PROXY.replace("socks5h://", "socks5://")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, proxy={"server": proxy_server},
            args=["--no-sandbox", "--disable-setuid-sandbox"])
        try:
            ctx = await browser.new_context(user_agent=UA)
            page = await ctx.new_page()
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                raise LMSError(f"proxy/network unreachable ({e.__class__.__name__}) — "
                               f"check LMS_PROXY / lms-proxy.service") from e
            if "captcha" in (await page.title()).lower():
                raise LMSError("CAPTCHA")

            try:
                await page.wait_for_selector("#username", state="visible", timeout=20000)
            except Exception as e:
                raise LMSError(f"no login form appeared ({e.__class__.__name__})") from e

            await page.fill("#username", email)
            await page.fill("#password", password)
            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                    await page.click("#login-submit-button, #kc-login, button[type=submit]")
            except Exception:
                pass  # not every hop counts as a tracked "navigation" — fine either way

            # Keycloak's OIDC form_post interstitial auto-submits itself via an
            # inline <script>; just wait to land back on lms.mtuci.ru, don't poke the DOM
            try:
                await page.wait_for_url(lambda u: "lms.mtuci.ru" in u and "/login/" not in u,
                                        timeout=20000)
            except Exception:
                pass  # cookie check below is the real verdict either way

            return await ctx.cookies()
        finally:
            await browser.close()


def login(attempts=3, delay_sec=10):
    """The interstitial before the Keycloak form is flaky — sometimes gone in
    2s, sometimes never resolves within a generous timeout. Observed to
    usually succeed on a fresh attempt, so just retry the whole login."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            cookies = asyncio.run(_browser_login_cookies())
            sess = requests.Session()
            sess.proxies = {"http": PROXY, "https": PROXY}
            sess.headers["User-Agent"] = UA
            for c in cookies:
                sess.cookies.set(c["name"], c["value"], domain=c["domain"],
                                 path=c.get("path", "/"))
            if "MoodleSession" not in sess.cookies:
                raise LMSError("no MoodleSession cookie after SSO")
            return sess
        except LMSError as e:
            if str(e) == "CAPTCHA":
                raise  # a real shield block — retrying won't help
            last_error = e
            print(f"login attempt {attempt}/{attempts} failed: {e}", flush=True)
            if attempt < attempts:
                time.sleep(delay_sec)
    raise last_error


def list_courses(sess):
    r = sess.get(DASH_URL, timeout=30)
    mk = re.search(r"\"sesskey\":\"(\w+)\"", r.text)
    if not mk:
        raise LMSError("no sesskey on dashboard (not logged in?)")
    # "inprogress" (not "all") — Moodle itself then excludes past-semester
    # leftover course instances (e.g. a prior term's duplicate "Высшая
    # математика" that's still enrolled but long over), instead of relying
    # only on the GROUP_LABEL name-preference below to sort it out.
    payload = [{"index": 0, "methodname": COURSELIST_METHOD,
                "args": {"offset": 0, "limit": 0,
                         "classification": "inprogress", "sort": "fullname"}}]
    w = sess.post(f"{LMS}/lib/ajax/service.php?sesskey={mk.group(1)}"
                  f"&info={COURSELIST_METHOD}", json=payload, timeout=40)
    data = w.json()[0]
    if data.get("error"):
        raise LMSError(f"course list error: {data.get('exception', data)}")
    return [(c["fullname"], c["viewurl"]) for c in data["data"]["courses"]]


def course_conf_link(sess, viewurl):
    r = sess.get(viewurl, timeout=30)
    for mod_url in dict.fromkeys(m.group(0) for m in MOD_RE.finditer(r.text)):
        mr = sess.get(mod_url, timeout=30)
        k = KTALK_RE.search(mr.text)
        if k:
            return k.group(0)
        if "/mod/url/" in mod_url and KTALK_RE.search(mr.url):
            return mr.url
    return None


def scrape():
    sess = login()
    found = {}  # course_fullname -> conference url
    for name, viewurl in list_courses(sess):
        try:
            url = course_conf_link(sess, viewurl)
            if url:
                found[name] = url
        except Exception as e:
            print(f"course '{name}' failed: {e}", flush=True)
    return found


def main():
    cfg = bot.load_config()
    try:
        result = scrape()
    except LMSError as e:
        print(f"LMS scrape failed: {e}", flush=True)
        state = bot.load_json(cfg["state_file"], {})
        if not state.get("lms_fail_notified"):
            if str(e) == "CAPTCHA":
                msg = ("⚠️ LMS отдаёт капчу — прокси до РФ (LMS_PROXY) не работает.\n"
                       "Проверь на сервере: <code>systemctl status lms-proxy</code>")
            else:
                msg = f"⚠️ Не смог зайти в LMS: {e}"
            notify_owner(cfg, msg)
            state["lms_fail_notified"] = True
            bot.save_json(cfg["state_file"], state)
        return

    state = bot.load_json(cfg["state_file"], {})
    if state.get("lms_fail_notified"):
        state["lms_fail_notified"] = None
        bot.save_json(cfg["state_file"], state)

    group = os.environ.get("GROUP_LABEL", "")
    subjects = list(bot.load_topic_subjects(cfg))
    links = bot.load_json(cfg["conf_links_file"], {})

    # a subject can have several LMS courses (per-stream copies) with different
    # links — keep the one whose raw course name names our group.
    picked = {}  # subject -> (course_name, url)
    for course_name, url in result.items():
        clean = re.sub(r"\s*\([^)]*\)", "", course_name).strip()
        subj = bot.match_subject(clean, subjects)
        if not subj:
            print(f"no topic match for course '{course_name}'", flush=True)
            continue
        cur = picked.get(subj)
        if cur is None or (group and group in course_name and group not in cur[0]):
            picked[subj] = (course_name, url)

    updated = []
    for subj, (course_name, url) in picked.items():
        if links.get(subj, {}).get("url") == url:
            continue
        msg = bot.set_conf_link(cfg, subj, url)
        cur = bot.load_json(cfg["conf_links_file"], {})
        if subj in cur:
            cur[subj]["auto"] = True
            bot.save_json(cfg["conf_links_file"], cur)
        updated.append(f"{subj}: {url}")
        print(msg, flush=True)

    print(f"done, {len(picked)} conf links found, {len(updated)} updated", flush=True)
    if updated:
        notify_owner(cfg, "🎥 Автопарсер LMS обновил ссылки:\n" + "\n".join(updated))
    elif "--report" in sys.argv:
        notify_owner(cfg, f"🎥 Прогнал парсер LMS: нашёл {len(picked)} ссылок, "
                          f"изменений нет.")


if __name__ == "__main__":
    main()
