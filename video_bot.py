#!/usr/bin/env python3
import contextlib
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "video_bot_config.json")

HASHTAG_RE = re.compile(r"#\S+")


def tg_api(tg_conf, method, **params):
    url = f"{tg_conf['api_base']}/bot{tg_conf['bot_token']}/{method}"
    for attempt in range(6):
        resp = requests.post(url, data=params, timeout=30)
        result = resp.json()
        if result.get("ok"):
            return result["result"]
        if result.get("error_code") == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 3)
            time.sleep(retry_after + 0.5)
            continue
        raise RuntimeError(f"Telegram error in {method}: {result}")
    raise RuntimeError(f"Telegram error in {method}: repeated 429, giving up")


@contextlib.contextmanager
def chat_action(tg_conf, action, interval_sec=3):
    """Keep a chat action ("отправляет видео…") visible in the topic for the
    whole block, until the video message is actually posted. Telegram drops an
    action after ~5s, so it is re-sent in the background; failures are ignored
    so the status can never break a send."""
    url = f"{tg_conf['api_base']}/bot{tg_conf['bot_token']}/sendChatAction"
    params = {
        "chat_id": tg_conf["chat_id"],
        "message_thread_id": tg_conf["thread_id"],
        "action": action,
    }
    stop = threading.Event()

    def refresh():
        while not stop.is_set():
            try:
                requests.post(url, data=params, timeout=10)
            except Exception:
                pass
            stop.wait(interval_sec)

    thread = threading.Thread(target=refresh, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=15)


def fetch_message_text(tg_conf, message_id):
    fwd = tg_api(
        tg_conf, "forwardMessage",
        chat_id=tg_conf["chat_id"],
        message_thread_id=tg_conf["thread_id"],
        from_chat_id=tg_conf["chat_id"],
        message_id=message_id,
        disable_notification=True,
    )
    fwd_id = fwd["message_id"]
    try:
        return fwd.get("text", "")
    finally:
        try:
            tg_api(tg_conf, "deleteMessage", chat_id=tg_conf["chat_id"], message_id=fwd_id)
        except Exception:
            pass


def parse_tag_map(text):
    tag_map = {}
    pending_subject = None
    lines = text.split("\n")[2:]  # skip "Навигация по записям" header + blank line
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        hashtags = HASHTAG_RE.findall(line)
        if hashtags:
            hash_pos = line.index("#")
            subject_part = line[:hash_pos].strip()
            subject = subject_part if subject_part else pending_subject
            if subject:
                tag_map.setdefault(subject.lower(), []).extend(hashtags)
                pending_subject = subject
        else:
            pending_subject = line
    return tag_map


def auto_tag(subject):
    slug = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", subject).lower()
    return f"#{slug}" if slug else ""


def resolve_tags(subject, live_tag_map, fallback_tag_map):
    tags = live_tag_map.get(subject.lower())
    if tags:
        return tags
    tags = fallback_tag_map.get(subject)
    if tags:
        return tags
    auto = auto_tag(subject)
    return [auto] if auto else []


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["yandex_token"] = os.environ["YANDEX_DISK_TOKEN"]
    cfg["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    cfg["telegram"]["chat_id"] = int(os.environ["TELEGRAM_CHAT_ID"])
    return cfg


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def yandex_get(token, url_path, params):
    url = "https://cloud-api.yandex.net" + url_path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def yandex_put(token, url_path, params):
    url = "https://cloud-api.yandex.net" + url_path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {token}"}, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp) if resp.length else {}


def ensure_public_url(token, disk_path, existing_url=None):
    if existing_url:
        return existing_url
    yandex_put(token, "/v1/disk/resources/publish", {"path": disk_path})
    info = yandex_get(token, "/v1/disk/resources", {"path": disk_path})
    return info.get("public_url")


def list_video_files(token, root_path):
    root_prefix = "disk:" + root_path
    items = []
    offset = 0
    limit = 200
    while True:
        d = yandex_get(token, "/v1/disk/resources/files", {
            "media_type": "video", "limit": limit, "offset": offset, "sort": "-modified",
        })
        batch = d.get("items", [])
        if not batch:
            break
        items.extend(batch)
        offset += limit
        if len(batch) < limit:
            break
    return [it for it in items if it["path"].startswith(root_prefix + "/")]


def subject_from_path(path, root_path):
    rel = path[len("disk:" + root_path):].lstrip("/")
    return rel.split("/")[0]


def semester_sort_key(path, root_path):
    rel = path[len("disk:" + root_path):].lstrip("/")
    parts = rel.split("/")
    if len(parts) >= 3:
        m = re.match(r"(\d+)_курс_(\d+)_семестр", parts[1])
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return (999, 999)


def semester_from_path(path, root_path):
    rel = path[len("disk:" + root_path):].lstrip("/")
    parts = rel.split("/")
    if len(parts) >= 3:
        return parts[1].replace("_", " ")
    return ""


def download_yandex_file(token, disk_path, dest_path):
    d = yandex_get(token, "/v1/disk/resources/download", {"path": disk_path})
    href = d["href"]
    req = urllib.request.Request(href)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def make_thumbnail(src_path, thumb_path):
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "1", "-i", src_path, "-vframes", "1",
         "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
         thumb_path],
        capture_output=True, check=True,
    )


def probe_dimensions(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=s=x:p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def compress_video(src_path, dest_path, target_size_mb):
    duration = ffprobe_duration(src_path)
    target_bits = target_size_mb * 1024 * 1024 * 8 * 0.96
    audio_kbps = 128
    video_kbps = max(int(target_bits / 1000 / duration) - audio_kbps, 300)
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path,
         "-c:v", "libx264", "-preset", "veryfast",
         "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps*1.2)}k",
         "-bufsize", f"{video_kbps*2}k",
         "-c:a", "aac", "-b:a", f"{audio_kbps}k",
         "-movflags", "+faststart",
         dest_path],
        capture_output=True, check=True,
    )


import html as _html

DATE_RE = re.compile(r"(\d{2})(\d{2})(\d{4})")
PART_RE = re.compile(r"[чЧ](\d+(?:\.\d+)?)")
PRACTICE_RE = re.compile(r"ПРАКТИКА(\d*)", re.IGNORECASE)


def humanize_semester(semester):
    m = re.match(r"(\d+)\s*курс\s*(\d+)\s*семестр", semester, re.IGNORECASE)
    if m:
        return f"{m.group(1)} курс, {m.group(2)} семестр"
    return semester


def parse_filename_meta(name):
    date_str = None
    m = DATE_RE.search(name)
    if m:
        date_str = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

    kind = "Лекция"
    m = PRACTICE_RE.search(name)
    if m:
        kind = "Практика" + (f" {m.group(1)}" if m.group(1) else "")

    part = None
    m = PART_RE.search(name)
    if m:
        part = f"часть {m.group(1)}"

    return date_str, kind, part


def build_caption(subject, semester, filename, tags, yandex_url=None):
    name = os.path.splitext(filename)[0]
    date_str, kind, part = parse_filename_meta(name)
    semester_h = humanize_semester(semester) if semester else ""

    lines = [f"🎓 <b>{_html.escape(subject)}</b>"]
    if semester_h:
        lines.append(f"🗓 {_html.escape(semester_h)}")

    info_bits = [kind]
    if date_str:
        info_bits.append(date_str)
    if part:
        info_bits.append(part)
    lines.append("📹 " + " · ".join(_html.escape(b) for b in info_bits))

    if yandex_url:
        lines.append("")
        lines.append(f'☁️ <a href="{_html.escape(yandex_url)}">Оригинал на Яндекс.Диске</a>')

    if tags:
        lines.append("")
        lines.append(" ".join(tags))
    return "\n".join(lines)


def send_video(tg_conf, video_path, thumb_path, caption, duration, width, height):
    api_base = tg_conf["api_base"]
    token = tg_conf["bot_token"]
    url = f"{api_base}/bot{token}/sendVideo"
    data = {
        "chat_id": str(tg_conf["chat_id"]),
        "message_thread_id": str(tg_conf["thread_id"]),
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true",
        "duration": str(int(duration)),
        "width": str(width),
        "height": str(height),
    }
    files = {"video": open(video_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumbnail"] = open(thumb_path, "rb")
    try:
        resp = requests.post(url, data=data, files=files, timeout=3600)
    finally:
        for f in files.values():
            f.close()
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")
    return result


def process_item(cfg, token, item, state, live_tag_map):
    resource_id = item["resource_id"]
    if resource_id in state:
        return

    disk_path = item["path"][len("disk:"):]
    filename = os.path.basename(disk_path)
    subject = subject_from_path(item["path"], cfg["root_path"])
    semester = semester_from_path(item["path"], cfg["root_path"])
    tags = resolve_tags(subject, live_tag_map, cfg["tag_map"])

    tmp_dir = cfg["tmp_dir"]
    raw_path = os.path.join(tmp_dir, f"raw_{resource_id[:16]}_{filename}")
    thumb_path = os.path.join(tmp_dir, f"thumb_{resource_id[:16]}.jpg")
    upload_path = raw_path

    print(f"[{filename}] downloading ({item['size']/1024/1024:.1f} MB)...", flush=True)
    download_yandex_file(token, disk_path, raw_path)

    size_mb = os.path.getsize(raw_path) / 1024 / 1024
    if size_mb > cfg["compress_threshold_mb"]:
        print(f"[{filename}] {size_mb:.0f} MB > threshold, compressing to ~{cfg['target_size_mb']} MB...", flush=True)
        compressed_path = os.path.join(tmp_dir, f"cmp_{resource_id[:16]}_{filename}")
        compress_video(raw_path, compressed_path, cfg["target_size_mb"])
        upload_path = compressed_path

    duration = ffprobe_duration(upload_path)
    width, height = probe_dimensions(upload_path)
    make_thumbnail(upload_path, thumb_path)

    yandex_url = ensure_public_url(token, disk_path, item.get("public_url"))
    caption = build_caption(subject, semester, filename, tags, yandex_url)
    print(f"[{filename}] uploading to Telegram...", flush=True)
    result = send_video(cfg["telegram"], upload_path, thumb_path, caption, duration, width, height)
    message_id = result["result"]["message_id"]
    print(f"[{filename}] done, message_id={message_id}", flush=True)

    state[resource_id] = {"path": disk_path, "sent_at": int(time.time()), "message_id": message_id}
    save_state(cfg["state_file"], state)

    for p in {raw_path, upload_path, thumb_path}:
        if os.path.exists(p):
            os.remove(p)


def run_once(cfg):
    token = cfg["yandex_token"]
    state = load_state(cfg["state_file"])
    items = list_video_files(token, cfg["root_path"])
    new_items = [it for it in items if it["resource_id"] not in state]
    new_items.sort(key=lambda it: (
        semester_sort_key(it["path"], cfg["root_path"]),
        it.get("created", ""),
    ))
    if not new_items:
        print("no new videos", flush=True)
        return
    print(f"{len(new_items)} new video(s) found", flush=True)

    live_tag_map = {}
    try:
        source_id = cfg.get("tags_source_message_id")
        if source_id:
            text = fetch_message_text(cfg["telegram"], source_id)
            live_tag_map = parse_tag_map(text)
    except Exception:
        print("WARNING: failed to fetch live tag map, falling back to config tag_map", flush=True)
        traceback.print_exc()

    for item in new_items:
        try:
            with chat_action(cfg["telegram"], "upload_video"):
                process_item(cfg, token, item, state, live_tag_map)
        except Exception:
            print(f"ERROR processing {item['path']}:", flush=True)
            traceback.print_exc()


def main():
    """Single pass — spawned periodically by mtusi-schedule-bot's own main
    loop (see VIDEO_BOT_SCRIPT there) rather than looping/sleeping here, so
    the whole suite runs under one systemd service instead of two."""
    cfg = load_config()
    os.makedirs(cfg["tmp_dir"], exist_ok=True)
    run_once(cfg)


if __name__ == "__main__":
    main()
