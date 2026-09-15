#!/usr/bin/env python3
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(SCRIPT_DIR, "data"))
sys.path.insert(0, SCRIPT_DIR)
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from telegram_post import ensure_topic  # noqa: E402
from telegram_post import fetch_events_retrying  # noqa: E402
from telegram_post import tg_api as schedule_tg_api  # noqa: E402

import lk_attendance  # noqa: E402

LMS_SCRAPER = os.path.join(SCRIPT_DIR, "lms_scraper.py")
LMS_LOG = os.path.join(DATA_DIR, "lms_scraper.log")
LMS_MATERIALS = os.path.join(SCRIPT_DIR, "lms_materials.py")
LMS_MATERIALS_LOG = os.path.join(DATA_DIR, "lms_materials.log")
SCHEDULE_SCRIPT = os.path.join(SCRIPT_DIR, "telegram_post.py")
SCHEDULE_LOG = os.path.join(DATA_DIR, "schedule.log")
VENV_PY = sys.executable

# daily jobs folded into the main loop (no cron); MSK HH:MM triggers
LMS_SCRAPE_AT = (7, 35)
LMS_MATERIALS_AT = (7, 45)
SCHEDULE_POST_AT = (8, 0)
# some conference types (BigBlueButton Cloud) only expose their join link once
# the teacher actually opens the meeting, which doesn't reliably happen right
# at the scheduled start — keep re-checking a still-open lesson's link every
# few minutes instead of trying just once at open time.
CONF_LINK_RECHECK_SEC = 60


def spawn(script, *args, log=None):
    """Run a sibling script detached, so the poll loop keeps serving updates."""
    out = open(log, "a") if log else subprocess.DEVNULL
    subprocess.Popen([VENV_PY, script, *args], cwd=SCRIPT_DIR,
                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def _require_env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return v


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    return {
        "bot_token": _require_env("TELEGRAM_BOT_TOKEN"),
        "chat_id": int(_require_env("TELEGRAM_CHAT_ID")),
        "owner_telegram_id": int(_require_env("OWNER_TELEGRAM_ID")),
        "checkin_keywords": [k.strip().lower() for k in
                             os.environ.get("CHECKIN_KEYWORDS", "+,тут,здесь").split(",") if k.strip()],
        "roster_file": os.path.join(DATA_DIR, "roster.json"),
        "student_map_file": os.path.join(DATA_DIR, "student_map.json"),
        "state_file": os.path.join(DATA_DIR, "state.json"),
        "rollcall_file": os.path.join(DATA_DIR, "rollcall.json"),
        "offset_file": os.path.join(DATA_DIR, "offset.txt"),
        "conf_links_file": os.path.join(DATA_DIR, "conf_links.json"),
        "topics_file": os.path.join(DATA_DIR, "topics.json"),
        "materials_file": os.path.join(DATA_DIR, "lms_materials.json"),
        "participants_file": os.path.join(SCRIPT_DIR, "userbot", "participants.json"),
    }


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def msk_now():
    # naive, MSK wall-clock — matches the scraper's naive lesson start/end
    # datetimes (scraped straight off the site's local-time display), so the
    # two are directly comparable. A tz-aware value here made every lesson
    # open/close comparison raise "can't compare offset-naive and
    # offset-aware datetimes" the moment today's lessons were non-empty.
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=3)


def normalize_fio(s):
    return re.sub(r"\s+", " ", s.strip().lower()).replace("ё", "е")


def match_student(text, roster):
    words = set(normalize_fio(text).split())
    if not words:
        return None
    candidates = [s for s in roster if words <= set(normalize_fio(s["fio"]).split())
                  or set(normalize_fio(s["fio"]).split()) <= words]
    return candidates[0] if len(candidates) == 1 else None


def _norm(s):
    return re.sub(r"[^а-яёa-z0-9 ]", "", s.strip().lower()).replace("ё", "е")


def _word_match(qw, sw):
    if qw == sw:
        return True
    return len(qw) >= 3 and len(sw) >= 3 and (sw.startswith(qw) or qw.startswith(sw))


def match_subject(text, subjects):
    """Fuzzy-match a subject name against the known topic subjects.

    Every word of the query must match (equal or 3+ char prefix) some word
    of the subject; the subject with the most matched words wins.
    """
    q = _norm(text).split()
    if not q:
        return None
    scored = []
    for subj in subjects:
        s = _norm(subj).split()
        matched = sum(1 for qw in q if any(_word_match(qw, sw) for sw in s))
        if matched == len(q):
            scored.append((matched, -len(s), subj))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None
    return scored[0][2]


def load_participants(cfg):
    raw = load_json(cfg["participants_file"], [])
    return {str(p["id"]): p for p in raw}


def identity_matches(cfg, student_fio, user_id, participants):
    if str(user_id) == str(cfg.get("owner_telegram_id")):
        return True
    participant = participants.get(str(user_id))
    if participant is None:
        return False
    contact_name = f"{participant.get('first_name') or ''} {participant.get('last_name') or ''}"
    contact_words = set(normalize_fio(contact_name).split())
    if not contact_words:
        return False
    roster_words = set(normalize_fio(student_fio).split())
    return contact_words <= roster_words


def tg_api(cfg, method, **params):
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/{method}"
    data = {k: v for k, v in params.items() if v is not None}
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error in {method}: {result}")
    return result["result"]


def load_offset(cfg):
    if os.path.exists(cfg["offset_file"]):
        with open(cfg["offset_file"]) as f:
            content = f.read().strip()
            return int(content) if content else 0
    return 0


def save_offset(cfg, offset):
    with open(cfg["offset_file"], "w") as f:
        f.write(str(offset))


FETCH_COOLDOWN_SEC = 1800  # after a failed fetch, don't hammer lk.mtuci.ru again for 30 min


def refresh_today_lessons(cfg, state):
    today = msk_now().date().isoformat()
    force = state.pop("force_refresh", False)
    if not force and state.get("date") == today and state.get("lessons") is not None:
        return state
    last_attempt = state.get("last_attempt")
    if last_attempt is not None:
        elapsed = (msk_now() - datetime.fromisoformat(last_attempt)).total_seconds()
        if elapsed < FETCH_COOLDOWN_SEC:
            return state
    state["last_attempt"] = msk_now().isoformat()
    print(f"refreshing schedule for {today}", flush=True)
    try:
        events = asyncio.run(fetch_events_retrying())
    except Exception as e:
        print(f"schedule fetch failed: {e}, will retry in {FETCH_COOLDOWN_SEC // 60} min", flush=True)
        save_json(cfg["state_file"], state)
        return state
    day_events = [e for e in events if e.start_time.date().isoformat() == today]
    # preserve in-progress/closed lessons across a refetch (a forced /refresh
    # mid-day must not reopen an already-closed lesson or reset an ongoing
    # checkin's present_uids back to empty) — match old entries by identity
    old_by_key = {}
    if state.get("date") == today:
        for lsn in state.get("lessons") or []:
            old_by_key[(lsn["subject"], lsn["start"], lsn["end"])] = lsn
    lessons = []
    for e in sorted(day_events, key=lambda x: x.start_time):
        key = (e.subject, e.start_time.isoformat(), e.end_time.isoformat())
        old = old_by_key.get(key)
        if old is not None:
            lessons.append(old)
        else:
            lessons.append({
                "subject": e.subject,
                "start": e.start_time.isoformat(),
                "end": e.end_time.isoformat(),
                "opened": False,
                "closed": False,
                "thread_id": None,
                "present_uids": [],
            })
    state = {"date": today, "lessons": lessons}
    save_json(cfg["state_file"], state)
    print(f"cached {len(lessons)} lessons for today", flush=True)
    return state


def build_lesson_checkin_keyboard(roster, present_uids):
    present = set(present_uids)
    buttons = []
    for s in roster:
        text = f"✅ {s['fio']}" if s["uid"] in present else s["fio"]
        buttons.append([{"text": text, "callback_data": f"lc:{s['uid']}"}])
    return {"inline_keyboard": buttons}


def open_lesson(cfg, lesson, roster):
    thread_id = ensure_topic(lesson["subject"])
    lesson["thread_id"] = thread_id
    end_time = datetime.fromisoformat(lesson["end"])
    result = schedule_tg_api(
        "sendMessage",
        chat_id=cfg["chat_id"],
        message_thread_id=thread_id,
        text=(
            "🖊 <b>Отметка присутствия</b>\n"
            f"Нажми своё имя в списке ниже до {end_time.strftime('%H:%M')}, чтобы отметиться на паре.\n"
            "Если тебя нет в списке зарегистрированных — сначала нажми своё имя в закреплённом "
            "сообщении в топике «Обьявления»."
        ),
        parse_mode="HTML",
        reply_markup=json.dumps(build_lesson_checkin_keyboard(roster, lesson["present_uids"])),
    )
    lesson["checkin_message_id"] = result["message_id"]
    lesson["opened"] = True
    print(f"opened checkin for '{lesson['subject']}'", flush=True)


def close_lesson(cfg, lesson, roster):
    present = set(lesson["present_uids"])
    present_fio = sorted(s["fio"] for s in roster if s["uid"] in present)
    absent_fio = sorted(s["fio"] for s in roster if s["uid"] not in present)
    lines = [f"📋 <b>Итог по паре «{lesson['subject']}»</b>", ""]
    lines.append(f"✅ Присутствовали ({len(present_fio)}):")
    lines.extend(f"  {fio}" for fio in present_fio) if present_fio else lines.append("  —")
    lines.append("")
    lines.append(f"❌ Отсутствовали ({len(absent_fio)}):")
    lines.extend(f"  {fio}" for fio in absent_fio) if absent_fio else lines.append("  —")
    schedule_tg_api(
        "sendMessage",
        chat_id=cfg["chat_id"],
        message_thread_id=lesson["thread_id"],
        text="\n".join(lines),
        parse_mode="HTML",
    )
    if lesson.get("checkin_message_id"):
        try:
            tg_api(cfg, "deleteMessage", chat_id=cfg["chat_id"],
                   message_id=lesson["checkin_message_id"])
        except Exception as e:
            print(f"delete old checkin message failed: {e}", flush=True)
    lesson["closed"] = True
    print(f"closed checkin for '{lesson['subject']}': {len(present_fio)}/{len(roster)}", flush=True)

    try:
        report = asyncio.run(lk_attendance.submit(
            lesson["subject"], lesson["start"], lesson["end"], list(present)))
    except Exception as e:
        report = {"ok": False, "error": str(e), "marked": [], "not_in_sheet": []}

    if report["ok"]:
        owner_msg = (f"✅ ЛК МТУСИ: отметил {len(report['marked'])}/{len(present)} "
                     f"по «{lesson['subject']}»")
        if report["not_in_sheet"]:
            owner_msg += f"\n⚠️ не нашёл в ведомости: {len(report['not_in_sheet'])} uid"
    else:
        owner_msg = f"⚠️ Не смог отметить посещаемость в ЛК для «{lesson['subject']}»: {report['error']}"
    print(owner_msg.replace("\n", " | "), flush=True)
    if cfg.get("owner_telegram_id"):
        try:
            tg_api(cfg, "sendMessage", chat_id=cfg["owner_telegram_id"], text=owner_msg)
        except Exception:
            pass


def handle_lesson_checkin(cfg, cq, uid, roster, student_map, state):
    user_id = str(cq.get("from", {}).get("id"))
    msg = cq.get("message", {})
    thread_id = msg.get("message_thread_id")

    lesson = next((lsn for lsn in state.get("lessons", [])
                    if lsn["opened"] and not lsn["closed"] and lsn["thread_id"] == thread_id), None)
    if lesson is None:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Отметка уже закрыта или это не та пара.", show_alert=True)
        return

    registered = student_map.get(user_id)
    if registered is None:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Сначала зарегистрируйся: нажми своё имя в закреплённом сообщении в топике «Обьявления».",
               show_alert=True)
        return
    if registered["uid"] != uid:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Это не твоё имя.", show_alert=True)
        return

    if uid not in lesson["present_uids"]:
        lesson["present_uids"].append(uid)
        text = f"Отмечено, {registered['fio']} ✅"
    else:
        lesson["present_uids"].remove(uid)
        text = f"Отметка снята, {registered['fio']}"

    save_json(cfg["state_file"], state)
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if chat_id and message_id:
        try:
            tg_api(cfg, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup=json.dumps(build_lesson_checkin_keyboard(roster, lesson["present_uids"])))
        except Exception as e:
            print(f"editMessageReplyMarkup failed: {e}", flush=True)

    try:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"], text=text)
    except Exception as e:
        print(f"answerCallbackQuery failed: {e}", flush=True)


def handle_callback_query(cfg, cq, rollcall, roster, student_map, participants, state):
    data = cq.get("data", "")
    from_user = cq.get("from", {})
    user_id = str(from_user.get("id"))

    if data.startswith("cl:"):
        if str(user_id) == str(cfg.get("owner_telegram_id")):
            handle_conf_link_callback(cfg, cq, state)
        else:
            tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"])
        return

    if data == "rc_taken":
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Это имя уже занято.", show_alert=True)
        return

    if data.startswith("lc:"):
        handle_lesson_checkin(cfg, cq, data[3:], roster, student_map, state)
        return

    if not data.startswith("rc:"):
        return

    uid = data[3:]
    student = next((s for s in roster if s["uid"] == uid), None)
    if student is None:
        return

    if not identity_matches(cfg, student["fio"], user_id, participants):
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Ты не тот, за кого себя выдаёшь. Это имя тебе не принадлежит.", show_alert=True)
        return

    existing = student_map.get(user_id)
    if existing and existing["uid"] != uid:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text=f"Ты уже отметился как {existing['fio']}. Если ошибка — напиши старосте.",
               show_alert=True)
        return

    claimed_by_other = any(v["uid"] == uid and k != user_id for k, v in student_map.items())
    if claimed_by_other:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
               text="Это имя уже занял кто-то другой. Если ошибка — напиши старосте.", show_alert=True)
        return

    student_map[user_id] = {"uid": student["uid"], "fio": student["fio"]}
    save_json(cfg["student_map_file"], student_map)
    try:
        tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"], text=f"Готово, ты — {student['fio']} ✅")
    except Exception as e:
        print(f"answerCallbackQuery failed: {e}", flush=True)

    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if chat_id and message_id:
        try:
            tg_api(cfg, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup=json.dumps(build_roster_keyboard(roster, student_map)))
        except Exception as e:
            print(f"editMessageReplyMarkup failed: {e}", flush=True)


def build_roster_keyboard(roster, student_map):
    claimed_uids = {v["uid"] for v in student_map.values()}
    buttons = []
    for s in roster:
        if s["uid"] in claimed_uids:
            buttons.append([{"text": f"✅ {s['fio']}", "callback_data": "rc_taken"}])
        else:
            buttons.append([{"text": s["fio"], "callback_data": f"rc:{s['uid']}"}])
    return {"inline_keyboard": buttons}


URL_RE = re.compile(r"https?://\S+")


def load_topic_subjects(cfg):
    return load_json(cfg["topics_file"], {})


def _post_conf_link(cfg, subject, thread_id, url, old_msg_id):
    if old_msg_id:
        for method in ("unpinChatMessage", "deleteMessage"):
            try:
                tg_api(cfg, method, chat_id=cfg["chat_id"], message_id=old_msg_id)
            except Exception:
                pass
    result = tg_api(cfg, "sendMessage", chat_id=cfg["chat_id"], message_thread_id=thread_id,
                    text="🎥 <b>Конференция по предмету</b>", parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=json.dumps({"inline_keyboard": [
                        [{"text": "▶️ Подключиться к конференции", "url": url}]]}))
    msg_id = result["message_id"]
    try:
        tg_api(cfg, "pinChatMessage", chat_id=cfg["chat_id"], message_id=msg_id,
               disable_notification=True)
    except Exception as e:
        print(f"pin conf link failed: {e}", flush=True)
    return msg_id


def set_conf_link(cfg, subject_query, url):
    subjects = load_topic_subjects(cfg)
    subject = match_subject(subject_query, subjects)
    if subject is None:
        known = "\n".join(f"  {s}" for s in subjects)
        return f"Не понял предмет «{subject_query}». Известные предметы (топики):\n{known}"
    links = load_json(cfg["conf_links_file"], {})
    old = links.get(subject, {})
    thread_id = subjects.get(subject) or ensure_topic(subject)
    try:
        msg_id = _post_conf_link(cfg, subject, thread_id, url, old.get("message_id"))
    except Exception as e:
        return f"Не получилось запостить в топик: {e}"
    links[subject] = {"url": url, "thread_id": thread_id, "message_id": msg_id,
                      "updated": msk_now().isoformat()}
    save_json(cfg["conf_links_file"], links)
    return f"Готово. «{subject}» → {url}\nСсылка запощена и закреплена в топике."


def remove_conf_link(cfg, subject_query):
    links = load_json(cfg["conf_links_file"], {})
    subject = match_subject(subject_query, list(links))
    if subject is None:
        return f"Нет сохранённой ссылки для «{subject_query}»."
    old_msg_id = links[subject].get("message_id")
    if old_msg_id:
        for method in ("unpinChatMessage", "deleteMessage"):
            try:
                tg_api(cfg, method, chat_id=cfg["chat_id"], message_id=old_msg_id)
            except Exception:
                pass
    del links[subject]
    save_json(cfg["conf_links_file"], links)
    return f"Удалил ссылку для «{subject}»."


def lms_status(cfg):
    state = load_json(cfg["state_file"], {})
    return "⚠️ ошибка (см. личку)" if state.get("lms_fail_notified") else "ок"


def conf_links_keyboard(subjects, links):
    rows = []
    for i, subj in enumerate(subjects):
        mark = "✅" if subj in links else "➕"
        rows.append([{"text": f"{mark} {subj}"[:64], "callback_data": f"cl:s:{i}"}])
    rows.append([{"text": "▶️ Спарсить LMS сейчас", "callback_data": "cl:scan"}])
    rows.append([{"text": "✖️ Закрыть", "callback_data": "cl:x"}])
    return {"inline_keyboard": rows}


def conf_link_detail_keyboard(idx, has_link):
    rows = [[{"text": "✏️ Задать/изменить ссылку", "callback_data": f"cl:u:{idx}"}]]
    if has_link:
        rows.append([{"text": "🗑 Удалить ссылку", "callback_data": f"cl:d:{idx}"}])
    rows.append([{"text": "⬅️ К списку", "callback_data": "cl:l"}])
    return {"inline_keyboard": rows}


def conf_links_menu_text(links, cfg=None):
    lines = ["🎥 <b>Ссылки на конференции</b>"]
    if cfg is not None:
        lines.append(f"<i>Автопарсер LMS: {lms_status(cfg)}</i>")
    lines.append("")
    if links:
        for s, i in links.items():
            src = " 🤖" if i.get("auto") else ""
            lines.append(f"• <b>{s}</b>{src}\n  {i['url']}")
        lines.append("")
    lines.append("Выбери предмет, чтобы задать/изменить/удалить ссылку вручную,\n"
                 "или «Спарсить LMS сейчас» для автопоиска.")
    return "\n".join(lines)


def handle_conf_link_callback(cfg, cq, state):
    data = cq["data"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    subjects = list(load_topic_subjects(cfg))
    links = load_json(cfg["conf_links_file"], {})

    def ack(text=None):
        try:
            tg_api(cfg, "answerCallbackQuery", callback_query_id=cq["id"], text=text)
        except Exception:
            pass

    def show_list():
        tg_api(cfg, "editMessageText", chat_id=chat_id, message_id=message_id,
               text=conf_links_menu_text(links, cfg), parse_mode="HTML",
               reply_markup=json.dumps(conf_links_keyboard(subjects, links)),
               disable_web_page_preview=True)

    if data in ("cl:l", "cl:menu"):
        show_list()
        ack()
    elif data == "cl:scan":
        try:
            spawn(LMS_SCRAPER, "--report", log=LMS_LOG)
            ack("Запущено — результат придёт в личку через пару минут")
        except Exception as e:
            ack(f"Не вышло: {e}")
    elif data == "cl:x":
        try:
            tg_api(cfg, "deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        ack()
    elif data.startswith("cl:s:"):
        idx = int(data[5:])
        if idx >= len(subjects):
            show_list()
            ack()
            return
        subj = subjects[idx]
        cur = links.get(subj)
        body = f"🎥 <b>{subj}</b>\n\n" + (f"Текущая ссылка:\n{cur['url']}" if cur else "Ссылка не задана.")
        tg_api(cfg, "editMessageText", chat_id=chat_id, message_id=message_id, text=body,
               parse_mode="HTML", reply_markup=json.dumps(conf_link_detail_keyboard(idx, bool(cur))),
               disable_web_page_preview=True)
        ack()
    elif data.startswith("cl:u:"):
        idx = int(data[5:])
        subj = subjects[idx] if idx < len(subjects) else None
        if subj is None:
            show_list()
            ack()
            return
        state["pending_url_subject"] = subj
        save_json(cfg["state_file"], state)
        tg_api(cfg, "editMessageText", chat_id=chat_id, message_id=message_id,
               text=f"Пришли ссылку для <b>{subj}</b> следующим сообщением.\n(или /cancel)",
               parse_mode="HTML")
        ack()
    elif data.startswith("cl:d:"):
        idx = int(data[5:])
        subj = subjects[idx] if idx < len(subjects) else None
        if subj and subj in links:
            remove_conf_link(cfg, subj)
            links = load_json(cfg["conf_links_file"], {})
        show_list()
        ack("Удалено")


def check_conf_links(cfg):
    """Ping stored conf links; return list of (subject, reason) for dead ones."""
    links = load_json(cfg["conf_links_file"], {})
    dead = []
    for subject, info in links.items():
        url = info.get("url")
        if not url:
            continue
        try:
            r = requests.get(url, timeout=12, allow_redirects=True)
            if r.status_code == 404 or r.status_code >= 500:
                dead.append((subject, f"HTTP {r.status_code}"))
        except Exception as e:
            dead.append((subject, type(e).__name__))
    return dead


def handle_owner_command(cfg, msg, roster, student_map, state, rollcall):
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].lower() if text else ""
    claimed_uids = {v["uid"] for v in student_map.values()}
    done = sorted(s["fio"] for s in roster if s["uid"] in claimed_uids)
    todo = sorted(s["fio"] for s in roster if s["uid"] not in claimed_uids)

    pending = state.get("pending_url_subject")
    if pending and cmd not in ("/cancel", "/help", "/start"):
        m = URL_RE.search(text)
        if m:
            state["pending_url_subject"] = None
            save_json(cfg["state_file"], state)
            reply = set_conf_link(cfg, pending, m.group(0))
        else:
            reply = f"Это не похоже на ссылку. Пришли URL для «{pending}» или /cancel."
        tg_api(cfg, "sendMessage", chat_id=msg["chat"]["id"], text=reply, parse_mode="HTML")
        return

    if cmd == "/cancel":
        state["pending_url_subject"] = None
        save_json(cfg["state_file"], state)
        reply = "Отменено."
    elif cmd in ("/start", "/help"):
        reply = (
            "🔧 <b>Управление attendance-bot</b>\n\n"
            "/status — статус регистрации и расписания\n"
            "/roster — кто отметился / не отметился\n"
            "/close — закрыть регистрацию (убрать кнопки, открепить)\n"
            "/refresh — принудительно обновить расписание сейчас\n"
            "/links — ссылки на конференции по предметам (кнопками)"
        )
    elif cmd == "/status":
        lessons = state.get("lessons")
        reply = (
            f"Зарегистрировано: {len(done)}/{len(roster)}\n"
            f"Кеш расписания на: {state.get('date') or '—'}\n"
            f"Пар сегодня в кеше: {len(lessons) if lessons is not None else '—'}\n"
            f"Дайджест расписания постился: {state.get('last_digest') or '—'}\n"
            f"Парсер LMS запускался: {state.get('last_lms_scrape') or '—'}\n"
            f"Автопарсер LMS: {lms_status(cfg)}"
        )
    elif cmd == "/roster":
        reply = f"✅ Отметились ({len(done)}):\n" + ("\n".join(done) if done else "—")
        reply += f"\n\n❌ Не отметились ({len(todo)}):\n" + ("\n".join(todo) if todo else "все отметились")
    elif cmd == "/close":
        msg_id = rollcall.get("registration_message_id")
        if not msg_id:
            reply = "Не знаю id сообщения с регистрацией (registration_message_id не сохранён)."
        else:
            close_text = (
                "🔒 <b>Регистрация закрыта</b>\n"
                f"Отметились {len(done)} из {len(roster)}.\n"
            )
            if todo:
                close_text += "\nНе зарегистрировались:\n" + "\n".join(f"  {f}" for f in todo)
            try:
                tg_api(cfg, "editMessageText", chat_id=cfg["chat_id"], message_id=msg_id,
                       text=close_text, parse_mode="HTML")
                tg_api(cfg, "editMessageReplyMarkup", chat_id=cfg["chat_id"], message_id=msg_id,
                       reply_markup=json.dumps({"inline_keyboard": []}))
                tg_api(cfg, "unpinChatMessage", chat_id=cfg["chat_id"], message_id=msg_id)
                reply = "Готово, регистрация закрыта."
            except Exception as e:
                reply = f"Не получилось: {e}"
    elif cmd == "/refresh":
        state["force_refresh"] = True
        state["last_attempt"] = None
        save_json(cfg["state_file"], state)
        reply = ("Расписание обновится в течение минуты. Уже открытые/закрытые "
                 "пары и текущие отметки не тронет — обновятся только новые или "
                 "изменившиеся пары.")
    elif cmd in ("/links", "/ссылки"):
        subjects = list(load_topic_subjects(cfg))
        links = load_json(cfg["conf_links_file"], {})
        if not subjects:
            reply = "Топиков предметов пока нет (создаются при первой паре по предмету)."
        else:
            tg_api(cfg, "sendMessage", chat_id=msg["chat"]["id"],
                   text=conf_links_menu_text(links, cfg), parse_mode="HTML",
                   reply_markup=json.dumps(conf_links_keyboard(subjects, links)),
                   disable_web_page_preview=True)
            return
    else:
        reply = "Не понял команду. /help"

    tg_api(cfg, "sendMessage", chat_id=msg["chat"]["id"], text=reply, parse_mode="HTML")


def handle_update(cfg, upd, roster, student_map, state, rollcall, participants):
    if "callback_query" in upd:
        handle_callback_query(cfg, upd["callback_query"], rollcall, roster, student_map, participants, state)
        return
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    is_private = chat.get("type") == "private"

    if is_private and str(msg.get("from", {}).get("id")) == str(cfg.get("owner_telegram_id")):
        handle_owner_command(cfg, msg, roster, student_map, state, rollcall)
        return
    if not is_private and chat_id != cfg["chat_id"]:
        return
    text = (msg.get("text") or "").strip()
    user_id = str(msg.get("from", {}).get("id"))
    thread_id = msg.get("message_thread_id")

    if text.lower().startswith("/я"):
        fio_text = text[2:].strip()
        student = match_student(fio_text, roster)
        if student is None:
            tg_api(cfg, "sendMessage", chat_id=chat_id, message_thread_id=thread_id,
                   text="Не нашёл такое ФИО в списке группы. Проверь написание (Фамилия Имя Отчество).",
                   reply_to_message_id=msg["message_id"])
            return
        if not identity_matches(cfg, student["fio"], user_id, participants):
            tg_api(cfg, "sendMessage", chat_id=chat_id, message_thread_id=thread_id,
                   text="Ты не тот, за кого себя выдаёшь. Это имя тебе не принадлежит.",
                   reply_to_message_id=msg["message_id"])
            return
        student_map[user_id] = {"uid": student["uid"], "fio": student["fio"]}
        save_json(cfg["student_map_file"], student_map)
        tg_api(cfg, "sendMessage", chat_id=chat_id, message_thread_id=thread_id,
               text=f"Готово, ты — {student['fio']}. Теперь можно отмечаться на парах командой «+».",
               reply_to_message_id=msg["message_id"])
        return

    if is_private:
        return

    if text.lower() in cfg["checkin_keywords"]:
        student = student_map.get(user_id)
        if student is None:
            tg_api(cfg, "sendMessage", chat_id=chat_id, message_thread_id=thread_id,
                   text="Сначала зарегистрируйся: /я Фамилия Имя Отчество",
                   reply_to_message_id=msg["message_id"])
            return
        for lesson in state.get("lessons", []):
            if lesson["opened"] and not lesson["closed"] and lesson["thread_id"] == thread_id:
                if student["uid"] not in lesson["present_uids"]:
                    lesson["present_uids"].append(student["uid"])
                    save_json(cfg["state_file"], state)
                try:
                    tg_api(cfg, "setMessageReaction", chat_id=chat_id, message_id=msg["message_id"],
                           reaction=json.dumps([{"type": "emoji", "emoji": "👍"}]))
                except Exception:
                    pass
                return


def main():
    cfg = load_config()
    roster = load_json(cfg["roster_file"], [])
    student_map = load_json(cfg["student_map_file"], {})
    state = load_json(cfg["state_file"], {"date": None, "lessons": None})
    rollcall = load_json(cfg["rollcall_file"], {"responses": {}})
    participants = load_participants(cfg)
    offset = load_offset(cfg)

    print(f"bot started, roster={len(roster)} students, participants={len(participants)}", flush=True)

    while True:
        try:
            state = refresh_today_lessons(cfg, state)
            now = msk_now()
            changed = False
            for lesson in state.get("lessons", []):
                start = datetime.fromisoformat(lesson["start"])
                end = datetime.fromisoformat(lesson["end"])
                if not lesson["opened"] and now >= start:
                    open_lesson(cfg, lesson, roster)
                    changed = True
                if lesson["opened"] and not lesson["closed"] and now >= end:
                    close_lesson(cfg, lesson, roster)
                    changed = True
                if lesson["opened"] and not lesson["closed"]:
                    last_check = lesson.get("last_conf_check")
                    due = (last_check is None or
                           (now - datetime.fromisoformat(last_check)).total_seconds()
                           >= CONF_LINK_RECHECK_SEC)
                    if due:
                        lesson["last_conf_check"] = now.isoformat()
                        changed = True
                        try:
                            spawn(LMS_SCRAPER, "--subject", lesson["subject"], log=LMS_LOG)
                        except Exception as e:
                            print(f"spawn per-lesson LMS re-check failed: {e}", flush=True)
            if changed:
                save_json(cfg["state_file"], state)

            today = msk_now().date().isoformat()
            hm = (now.hour, now.minute)

            if state.get("last_lms_scrape") != today and hm >= LMS_SCRAPE_AT and now.hour < 12:
                state["last_lms_scrape"] = today
                save_json(cfg["state_file"], state)
                print("spawning daily LMS scrape", flush=True)
                spawn(LMS_SCRAPER, log=LMS_LOG)

            if state.get("last_lms_materials") != today and hm >= LMS_MATERIALS_AT and now.hour < 12:
                state["last_lms_materials"] = today
                save_json(cfg["state_file"], state)
                print("spawning daily LMS materials post", flush=True)
                spawn(LMS_MATERIALS, log=LMS_MATERIALS_LOG)

            if state.get("last_digest") != today and hm >= SCHEDULE_POST_AT and now.hour < 12:
                state["last_digest"] = today
                save_json(cfg["state_file"], state)
                print("spawning daily schedule digest", flush=True)
                spawn(SCHEDULE_SCRIPT, log=SCHEDULE_LOG)

            if state.get("last_conf_check") != today:
                state["last_conf_check"] = today
                save_json(cfg["state_file"], state)
                dead = check_conf_links(cfg)
                if dead and cfg.get("owner_telegram_id"):
                    body = "\n".join(f"• {s} — {r}" for s, r in dead)
                    tg_api(cfg, "sendMessage", chat_id=cfg["owner_telegram_id"],
                           text=f"⚠️ Возможно, ссылки на конференции недоступны:\n{body}\n\n"
                                f"Обнови через /links → выбери предмет")

            updates = tg_api(cfg, "getUpdates", offset=offset, timeout=20,
                              allowed_updates=json.dumps(["message", "callback_query"]))
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    handle_update(cfg, upd, roster, student_map, state, rollcall, participants)
                except Exception as e:
                    print(f"error handling update: {e}", flush=True)
            if updates:
                save_offset(cfg, offset)
        except Exception as e:
            print(f"main loop error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
