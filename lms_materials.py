#!/usr/bin/env python3
"""Post subject materials (lecture files, folders of files) from lms.mtuci.ru
into the matching Telegram subject topics.

Reuses lms_scraper.py's browser-login-then-requests session (same RU proxy,
same flaky-interstitial retry). Course pages are scanned for two activity
types: `mod_resource` (one file — redirects straight to its pluginfile.php
URL) and `mod_folder` (a page listing several pluginfile.php file links).
Already-posted files are tracked in data/lms_materials.json keyed by the
file's pluginfile.php path (stable across runs unless the teacher replaces
the file), so re-runs only post what's new.

bot.py's main loop spawns this once a day; also runnable standalone.
"""
import os
import re
import sys
import time
from urllib.parse import unquote, urlsplit

import requests

import lms_scraper as ls

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import bot  # noqa: E402
from telegram_post import ensure_topic  # noqa: E402

ACTIVITY_RE = re.compile(
    r'<a[^>]+href="(https://lms\.mtuci\.ru/(?:lms/)?mod/(\w+)/view\.php\?id=\d+)"'
    r'[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,80})', re.I)
FOLDER_FILE_RE = re.compile(
    r'href="(https://lms\.mtuci\.ru/pluginfile\.php/[^"]+)"[^>]*>([^<]{2,120})', re.I)

MAX_FILE_BYTES = 45 * 1024 * 1024  # Bot API document upload cap is ~50MB


def _material_key(url):
    """Stable id for a file: its pluginfile.php path, query string dropped
    (?forcedownload=1 etc. varies, the path doesn't unless the file changes)."""
    return urlsplit(url).path


def _filename_from_url(url):
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def list_materials(sess, course_view_url):
    """One course-page fetch -> [(label, file_url, filename), ...] for every
    resource/folder activity, files resolved to their direct download URL."""
    r = sess.get(course_view_url, timeout=30)
    materials = []
    seen_mod_urls = set()
    for mod_url, modtype, label in ACTIVITY_RE.findall(r.text):
        if modtype not in ("resource", "folder") or mod_url in seen_mod_urls:
            continue
        seen_mod_urls.add(mod_url)
        label = label.strip()
        try:
            if modtype == "resource":
                fr = sess.get(mod_url, timeout=60, allow_redirects=True)
                if "pluginfile.php" not in fr.url:
                    continue
                materials.append((label, fr.url, _filename_from_url(fr.url)))
            else:  # folder
                fr = sess.get(mod_url, timeout=30)
                for furl, fname in dict.fromkeys(FOLDER_FILE_RE.findall(fr.text)):
                    materials.append((f"{label} — {fname.strip()}", furl, fname.strip()))
        except Exception as e:
            print(f"activity '{label}' ({modtype}) failed: {e}", flush=True)
    return materials


def _send_document(cfg, thread_id, caption, filename, content):
    """Bulk-posting many documents back to back hits Telegram's flood control
    (429) reliably — retry on its own retry_after instead of just giving up."""
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendDocument"
    for attempt in range(5):
        resp = requests.post(
            url,
            data={"chat_id": cfg["chat_id"], "message_thread_id": thread_id,
                  "caption": caption[:1024], "parse_mode": "HTML"},
            files={"document": (filename, content)},
            timeout=120,
        )
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
            time.sleep(retry_after + 0.5)
            continue
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"sendDocument failed: {result}")
        return result["result"]["message_id"]
    raise RuntimeError("sendDocument: repeated 429, giving up")


def scrape_and_post(cfg):
    sess = ls.login()
    topics = bot.load_topic_subjects(cfg)
    subjects = list(topics)
    posted = bot.load_json(cfg["materials_file"], {})
    group = os.environ.get("GROUP_LABEL", "")

    by_course = {}  # subject -> best course_name (prefer one naming our group)
    for course_name, viewurl in ls.list_courses(sess):
        clean = re.sub(r"\s*\([^)]*\)", "", course_name).strip()
        subj = bot.match_subject(clean, subjects)
        if not subj:
            continue
        cur = by_course.get(subj)
        if cur is None or (group and group in course_name and group not in cur[0]):
            by_course[subj] = (course_name, viewurl)

    new_count = 0
    for subj, (course_name, viewurl) in by_course.items():
        try:
            materials = list_materials(sess, viewurl)
        except Exception as e:
            print(f"course '{course_name}' materials scan failed: {e}", flush=True)
            continue
        subj_posted = posted.setdefault(subj, {})
        for label, file_url, filename in materials:
            key = _material_key(file_url)
            if key in subj_posted:
                continue
            try:
                fr = sess.get(file_url, timeout=120)
                fr.raise_for_status()
                if len(fr.content) > MAX_FILE_BYTES:
                    print(f"skip '{label}': {len(fr.content)} bytes, over the cap", flush=True)
                    subj_posted[key] = {"filename": filename, "skipped": "too large"}
                    continue
                thread_id = topics.get(subj) or ensure_topic(subj)
                msg_id = _send_document(cfg, thread_id, f"📎 <b>{label}</b>", filename, fr.content)
                subj_posted[key] = {"filename": filename, "message_id": msg_id,
                                    "updated": bot.msk_now().isoformat()}
                new_count += 1
                print(f"posted '{label}' -> {subj}", flush=True)
                time.sleep(1.5)  # pace bulk sends, avoid tripping flood control
            except Exception as e:
                print(f"material '{label}' ({subj}) failed: {e}", flush=True)
        bot.save_json(cfg["materials_file"], posted)
    return new_count


def main():
    cfg = bot.load_config()
    cfg["materials_file"] = os.path.join(bot.DATA_DIR, "lms_materials.json")
    try:
        new_count = scrape_and_post(cfg)
    except ls.LMSError as e:
        print(f"LMS materials scrape failed: {e}", flush=True)
        ls.notify_owner(cfg, f"⚠️ Не смог зайти в LMS за материалами: {e}")
        return
    print(f"done, {new_count} new materials posted", flush=True)
    if new_count and cfg.get("owner_telegram_id"):
        ls.notify_owner(cfg, f"📎 Автопарсер LMS: разослал {new_count} новых материалов по темам.")


if __name__ == "__main__":
    main()
