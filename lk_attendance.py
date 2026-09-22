#!/usr/bin/env python3
"""Submit attendance marks to the official lk.mtuci.ru attendance sheet
(«Управление посещаемостью», curator view) for one lesson.

Reverse-engineered 2026-09-14, the day real ведомости first appeared for the
group. Everything is the same 1C-style RPC lk.mtuci.ru uses everywhere:
POST /ilk/x/getProcessor {"processor": "<name>", ...params}

- getData_CurrentArrayEducationAttendance  -> list today's open sheets
- getData_ArrayScoreAttendance             -> one sheet's student roster
                                               (row["Обучающийся"]["uid"] ==
                                               roster.json's uid — same
                                               ФизическиеЛица catalog, verified)
- update_ScoreToLineAttendance             -> mark ONE student present

lk.mtuci.ru is not behind the anti-bot shield lms.mtuci.ru has, so this logs
in directly from this server (no RU proxy needed) via the same Playwright
Keycloak auth the schedule scraper uses.

bot.py spawns this as `lk_attendance.py --submit <subject> <start> <end>
<uids-json>` (30 min before a lesson ends and again at close) and it DMs the
owner the result.
"""
import asyncio
import html
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from playwright.async_api import async_playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from src.scraping.auth import AuthConfig, MTUCIAuthenticator  # noqa: E402

MTUCI_EMAIL = os.environ.get("MTUCI_EMAIL")
MTUCI_PASSWORD = os.environ.get("MTUCI_PASSWORD")
MTUCI_LOGIN_URL = os.environ.get("MTUCI_LOGIN_URL", "https://lk.mtuci.ru/auth/login")


class AttendanceError(Exception):
    pass


async def _call(page, processor, extra=None):
    payload = {"processor": processor, "referrer": "/student/attendance-headman"}
    if extra:
        payload.update(extra)
    result = await page.evaluate(
        """
        async (payload) => {
            const r = await fetch('/ilk/x/getProcessor', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
                credentials: 'include'
            });
            return await r.json();
        }
        """,
        payload,
    )
    if result.get("state") != "ok":
        raise AttendanceError(f"{processor} failed: {result}")
    return result


def _time_label(start_iso, end_iso):
    """lk.mtuci.ru labels lesson slots like '09.30-11.00'."""
    s = datetime.fromisoformat(start_iso).strftime("%H.%M")
    e = datetime.fromisoformat(end_iso).strftime("%H.%M")
    return f"{s}-{e}"


def _ref_name(value):
    """A cell is usually a {"name": ...} ref, but may come as rendered HTML."""
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, str):
        return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
    return None


@asynccontextmanager
async def _authenticated_page(pw):
    """One browser+login, shared by every entry point below (submit, sheet
    listing) so the launch/context/auth setup lives in exactly one place."""
    browser = await pw.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
    try:
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = await ctx.new_page()
        auth_cfg = AuthConfig(email=MTUCI_EMAIL, password=MTUCI_PASSWORD,
                              login_url=MTUCI_LOGIN_URL)
        # authenticate() returns None on success and *raises* on failure —
        # it does not return a truthy/falsy verdict, so just let it raise
        await MTUCIAuthenticator(auth_cfg).authenticate(page)
        yield page
    finally:
        await browser.close()


async def _iter_attendance_pages(page):
    """Yield each page's ТаблицаДанных rows across
    getData_CurrentArrayEducationAttendance's full pagination."""
    page_num, page_count = 0, 1
    while page_num < page_count:
        result = await _call(page, "getData_CurrentArrayEducationAttendance",
                             {"ФильтрУслуг": 1, "ЭтоКуратор": True, "НомерСтраницы": page_num})
        answer = result["data"]["Ответ"]
        page_count = int((answer.get("Пагинатор") or {}).get("КоличествоСтраниц") or 1)
        yield answer["ТаблицаДанных"]
        page_num += 1


async def _find_sheet(page, subject, start_iso, end_iso):
    day = datetime.fromisoformat(start_iso).strftime("%Y%m%d")
    label = _time_label(start_iso, end_iso)
    async for rows in _iter_attendance_pages(page):
        for row in rows:
            if _ref_name(row.get("Дисциплина")) != subject:
                continue
            if _ref_name(row.get("ХарактеристикаЗанятия")) != label:
                continue
            if not str(row.get("ДатаЗанятия", "")).startswith(day):
                continue
            for cmd in row.get("data", {}).get("command", []):
                reg = cmd.get("ПараметрыКоманды", {}).get("РегистраторВедомости")
                if reg:
                    return reg
    return None


async def _sheet_rows(page, reg):
    result = await _call(page, "getData_ArrayScoreAttendance", {"РегистраторВедомости": reg})
    return result["data"]["Ответ"]["ТаблицаДанных"]


async def _mark(page, reg, row_num, student_ref):
    payload = {
        "РегистраторВедомости": reg,
        "НомерСтроки": row_num,
        "Обучающийся": student_ref,
        "Отметка": True,
        "Примечание": "",
    }
    await _call(page, "update_ScoreToLineAttendance", payload)


async def _submit_once(subject, start_iso, end_iso, present_uids):
    """One attempt: log in, find the sheet, mark present_uids. Raises on any
    failure (auth included) — submit() below is what retries."""
    report = {"ok": False, "marked": [], "not_in_sheet": [], "error": None}
    async with async_playwright() as pw:
        async with _authenticated_page(pw) as page:
            reg = await _find_sheet(page, subject, start_iso, end_iso)
            if not reg:
                raise AttendanceError(f"no open ведомость for '{subject}' "
                                      f"{_time_label(start_iso, end_iso)}")

            rows = await _sheet_rows(page, reg)
            by_uid = {r["Обучающийся"]["uid"]: r for r in rows}

            for uid in present_uids:
                row = by_uid.get(uid)
                if row is None:
                    report["not_in_sheet"].append(uid)
                    continue
                await _mark(page, reg, row["НомерСтроки"], row["Обучающийся"])
                report["marked"].append(row["Обучающийся"]["name"])
            report["ok"] = True
            return report


async def list_open_sheets():
    """List every currently-open ведомость as (subject, date, time label)
    triples, one login, no marking — used for the hourly awareness pass so
    the bot notices a sheet has appeared without doing a full per-lesson
    login/search at every check."""
    if not MTUCI_EMAIL or not MTUCI_PASSWORD:
        return []
    async with async_playwright() as pw:
        async with _authenticated_page(pw) as page:
            rows_out = []
            async for rows in _iter_attendance_pages(page):
                for row in rows:
                    rows_out.append({
                        "subject": _ref_name(row.get("Дисциплина")),
                        "label": _ref_name(row.get("ХарактеристикаЗанятия")),
                        "date": str(row.get("ДатаЗанятия", "")),
                    })
            return rows_out


async def submit(subject, start_iso, end_iso, present_uids, attempts=3, delay_sec=10):
    """Mark present_uids (== roster.json uid == ФизическиеЛица uid) present in
    the matching official sheet. Returns a report dict, never raises past
    this point — caller decides how to surface failures.

    lk.mtuci.ru's own Keycloak validation is occasionally flaky the same way
    lms.mtuci.ru's login is (see lms_scraper.py) — confirmed NOT an account
    lockout (a plain login right after a failure succeeds fine), so retry the
    whole browser session a few times before giving up."""
    report = {"ok": False, "marked": [], "not_in_sheet": [], "error": None}
    if not MTUCI_EMAIL or not MTUCI_PASSWORD:
        report["error"] = "MTUCI_EMAIL/MTUCI_PASSWORD not set"
        return report
    if not present_uids:
        report["ok"] = True
        return report
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await _submit_once(subject, start_iso, end_iso, present_uids)
        except Exception as e:
            last_error = e
            print(f"lk_attendance attempt {attempt}/{attempts} failed: {e}", flush=True)
            if attempt < attempts:
                await asyncio.sleep(delay_sec)
    report["error"] = str(last_error)
    return report


def main():
    import json
    import bot  # loads .env; imported here, not at top, since bot imports this module

    global MTUCI_EMAIL, MTUCI_PASSWORD, MTUCI_LOGIN_URL
    MTUCI_EMAIL = os.environ.get("MTUCI_EMAIL")
    MTUCI_PASSWORD = os.environ.get("MTUCI_PASSWORD")
    MTUCI_LOGIN_URL = os.environ.get("MTUCI_LOGIN_URL", MTUCI_LOGIN_URL)

    if sys.argv[1:2] != ["--submit"] or len(sys.argv) != 6:
        sys.exit("usage: lk_attendance.py --submit <subject> <start_iso> <end_iso> <uids-json>")
    subject, start_iso, end_iso, uids_json = sys.argv[2:6]
    present = json.loads(uids_json)

    report = asyncio.run(submit(subject, start_iso, end_iso, present))
    if report["ok"]:
        msg = f"✅ ЛК МТУСИ: отметил {len(report['marked'])}/{len(present)} по «{subject}»"
        if report["not_in_sheet"]:
            msg += f"\n⚠️ не нашёл в ведомости: {len(report['not_in_sheet'])} uid"
    else:
        msg = f"⚠️ Не смог отметить посещаемость в ЛК для «{subject}»: {report['error']}"
    print(msg.replace("\n", " | "), flush=True)

    cfg = bot.load_config()
    if cfg.get("owner_telegram_id"):
        bot.tg_api(cfg, "sendMessage", chat_id=cfg["owner_telegram_id"], text=msg)


if __name__ == "__main__":
    main()
