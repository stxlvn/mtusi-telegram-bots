#!/usr/bin/env python3
"""Scrape lms.mtuci.ru (Moodle) for per-course Контур.Толк / BBB conference links
and publish them into the matching Telegram subject topics.

lms.mtuci.ru sits behind an anti-bot shield that hits non-Russian IPs with a
slider CAPTCHA. This scraper routes through a Russian SOCKS proxy (``LMS_PROXY``);
from a RU IP there is no CAPTCHA, so a plain HTTP session logs in through the
shared MTUCI Keycloak SSO (``MTUCI_EMAIL`` / ``MTUCI_PASSWORD``) and reads the
course list via Moodle's AJAX endpoint. No browser, no cookie file to maintain.

bot.py's main loop spawns this once a day; also runnable standalone.
"""
import html
import os
import re
import sys

import requests

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


def _submit_autoform(sess, text):
    """POST the JS-less auto-submit form — Keycloak's OIDC ``form_post`` response
    that carries the auth code back to lms.mtuci.ru/auth/oidc/."""
    fm = re.search(r"<form[^>]*action=\"([^\"]+)\"[^>]*>(.*?)</form>", text, re.S | re.I)
    if not fm:
        raise LMSError("Keycloak did not return a form_post response (login rejected?)")
    action = html.unescape(fm.group(1))
    fields = {}
    for inp in re.findall(r"<input[^>]+>", fm.group(2), re.I):
        n = re.search(r"name=\"([^\"]+)\"", inp, re.I)
        v = re.search(r"value=\"([^\"]*)\"", inp, re.I)
        if n and n.group(1).lower() != "continue":
            fields[n.group(1)] = html.unescape(v.group(1)) if v else ""
    return sess.post(action, data=fields, timeout=30)


def login():
    email = os.environ.get("MTUCI_EMAIL")
    password = os.environ.get("MTUCI_PASSWORD")
    if not email or not password:
        raise LMSError("MTUCI_EMAIL / MTUCI_PASSWORD not set")

    sess = requests.Session()
    sess.proxies = {"http": PROXY, "https": PROXY}
    sess.headers["User-Agent"] = UA

    try:
        r = sess.get(LOGIN_URL, timeout=30)
    except requests.RequestException as e:
        raise LMSError(f"proxy/network unreachable ({e.__class__.__name__}) — "
                       f"check LMS_PROXY / lms-proxy.service")
    if "<title>Captcha</title>" in r.text:
        raise LMSError("CAPTCHA")

    m = re.search(r"<form[^>]+id=\"kc-form-login\"[^>]+action=\"([^\"]+)\"", r.text)
    if not m:
        raise LMSError(f"no Keycloak login form at {r.url}")
    action = html.unescape(m.group(1))
    r2 = sess.post(action,
                   data={"username": email, "password": password,
                         "credentialId": "", "login": "Войти"},
                   headers={"Referer": r.url}, timeout=30)
    if "Form_Post" not in r2.text and "auth/oidc" not in r2.text:
        raise LMSError("SSO login rejected — check MTUCI_PASSWORD")
    _submit_autoform(sess, r2.text)
    if "MoodleSession" not in sess.cookies:
        raise LMSError("no MoodleSession cookie after SSO")
    return sess


def list_courses(sess):
    r = sess.get(DASH_URL, timeout=30)
    mk = re.search(r"\"sesskey\":\"(\w+)\"", r.text)
    if not mk:
        raise LMSError("no sesskey on dashboard (not logged in?)")
    payload = [{"index": 0, "methodname": COURSELIST_METHOD,
                "args": {"offset": 0, "limit": 0,
                         "classification": "all", "sort": "fullname"}}]
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
