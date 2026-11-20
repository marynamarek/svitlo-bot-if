"""
SvitloBot for Prykarpattyaoblenergo (Ivano-Frankivsk oblast), https://svitlo.oe.if.ua/

Sends to Telegram:
  * the schedule (picture + text) when it appears or changes;
  * an advance warning LEAD_MINUTES before each switch (0 = off);
  * a message at the moment of each switch: "🔴 12:00 Світло вимкнули за графіком".

Runs once and exits, meant to be called every few minutes (GitHub Actions / cron).
Remembers what it already sent in state.json.
Python 3.9+. Pillow is optional (for the picture); without it only text is sent.
"""

import hashlib
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

QUEUE = os.getenv("QUEUE", "4.2")
LEAD_MINUTES = int(os.getenv("LEAD_MINUTES", "30"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))
TEST_DATA = os.getenv("TEST_DATA")      # JSON file instead of the API (for testing)
FAKE_NOW = os.getenv("FAKE_NOW")        # e.g. "2026-11-20T11:59" (for testing)
PREVIEW_DIR = os.getenv("PREVIEW_DIR")  # where to save pictures when there is no token

API_URL = "https://be-svitlo.oe.if.ua/schedule-by-queue?queue=" + urllib.parse.quote(QUEUE)
KYIV = ZoneInfo("Europe/Kyiv")
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


# ---------------- data ----------------
def fetch() -> list:
    if TEST_DATA:
        return json.loads(Path(TEST_DATA).read_text(encoding="utf-8"))
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (svitlo-bot)",
            "Accept": "application/json",
            "Origin": "https://svitlo.oe.if.ua",
            "Referer": "https://svitlo.oe.if.ua/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def to_min(hhmm: str, is_end: bool) -> int:
    h, m = map(int, hhmm.split(":"))
    v = h * 60 + m
    return 1440 if (is_end and v == 0) else v  # "00:00" as an end means midnight


def parse_days(data: list) -> dict:
    """{date: [(start_min, end_min), ...]} with merged intervals."""
    days = {}
    for item in data or []:
        try:
            d = datetime.strptime(item["eventDate"], "%d.%m.%Y").date()
        except (KeyError, ValueError):
            continue
        slots = (item.get("queues") or {}).get(QUEUE) or []
        raw = sorted(
            (to_min(s["from"], False), to_min(s["to"], True))
            for s in slots
            if s.get("from") and s.get("to") and s.get("status", 1) == 1
        )
        merged = []
        for a, b in raw:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        days[d] = {
            "intervals": [tuple(x) for x in merged],
            "updated": item.get("scheduleApprovedSince") or item.get("createdAt") or "",
        }
    return days


def fmt(m: int) -> str:
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


def dur(minutes: float) -> str:
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def day_label(d: date, today: date) -> str:
    word = "сьогодні" if d == today else "завтра" if d == today + timedelta(days=1) else WEEKDAYS[d.weekday()]
    return f"{word}, {d:%d.%m}"


def schedule_text(d: date, info: dict, today: date, header: str) -> str:
    iv = info["intervals"]
    lines = [header, f"<b>Відключення {day_label(d, today)}</b>"]
    if iv:
        lines += [f"    🔴 {fmt(a)} - {fmt(b)}" for a, b in iv]
        lines.append(f"<i>Без світла: {dur(sum(b - a for a, b in iv))}</i>")
    else:
        lines.append("    ✅ відключень немає")
    if info["updated"]:
        lines.append(f"<i>Оновлено {info['updated']}</i>")
    return "\n".join(lines)


def timeline(days: dict) -> list:
    """Absolute outage periods across all known days, merged over midnight."""
    periods = []
    for d in sorted(days):
        base = datetime.combine(d, datetime.min.time(), KYIV)
        for a, b in days[d]["intervals"]:
            s, e = base + timedelta(minutes=a), base + timedelta(minutes=b)
            if periods and s <= periods[-1][1]:
                periods[-1][1] = max(periods[-1][1], e)
            else:
                periods.append([s, e])
    return periods


# ---------------- picture ----------------
def render_png(d: date, info: dict):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    def font(size, bold=False):
        names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
        dirs = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", ""]
        for n in names:
            for dd in dirs:
                try:
                    return ImageFont.truetype(dd + n, size)
                except OSError:
                    pass
        return ImageFont.load_default()

    W, cell, left, top = 1240, 44, 150, 80
    H = top + 120 + 70
    img = Image.new("RGB", (W, H), (246, 247, 251))
    dr = ImageDraw.Draw(img)
    dr.text((24, 22), "Графік відключень (Прикарпаттяобленерго)", font=font(24, True), fill=(20, 20, 30))
    badge = f"Черга: {QUEUE}"
    bw = dr.textlength(badge, font=font(20, True)) + 28
    dr.rounded_rectangle((W - bw - 24, 18, W - 24, 52), 16, fill=(255, 214, 102))
    dr.text((W - bw - 10, 23), badge, font=font(20, True), fill=(30, 30, 30))

    # hours header
    y0 = top
    for h in range(24):
        x = left + h * cell
        dr.rectangle((x, y0, x + cell, y0 + 50), outline=(210, 212, 220), fill=(255, 255, 255))
        dr.text((x + 6, y0 + 8), f"{h:02d}-", font=font(13), fill=(60, 60, 70))
        dr.text((x + 6, y0 + 27), f"{(h + 1) % 24:02d}", font=font(13), fill=(60, 60, 70))
    dr.text((20, y0 + 16), "Години", font=font(16, True), fill=(40, 40, 50))

    # day row (half-hour resolution)
    y1 = y0 + 50
    dr.text((20, y1 + 20), f"{d:%d.%m}", font=font(18, True), fill=(40, 40, 50))
    off = info["intervals"]
    for half in range(48):
        a, b = half * 30, half * 30 + 30
        x = left + (half // 2) * cell + (cell // 2 if half % 2 else 0)
        is_off = any(s < b and e > a for s, e in off)
        dr.rectangle((x, y1, x + cell // 2, y1 + 60), fill=(150, 150, 160) if is_off else (255, 255, 255))
    for h in range(25):
        x = left + h * cell
        dr.line((x, y1, x, y1 + 60), fill=(210, 212, 220))
    dr.rectangle((left, y1, left + 24 * cell, y1 + 60), outline=(210, 212, 220))

    # legend
    ly = y1 + 80
    dr.rectangle((left, ly, left + 24, ly + 24), fill=(255, 255, 255), outline=(200, 200, 210))
    dr.text((left + 32, ly + 3), "Світло є", font=font(16), fill=(60, 60, 70))
    dr.rectangle((left + 160, ly, left + 184, ly + 24), fill=(150, 150, 160))
    dr.text((left + 192, ly + 3), "Світла немає", font=font(16), fill=(60, 60, 70))
    if info["updated"]:
        dr.text((W - 330, ly + 3), f"Оновлено {info['updated']}", font=font(15), fill=(110, 110, 120))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------- telegram ----------------
def _post(method: str, fields: dict, file_bytes=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if file_bytes is None:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data)
    else:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in fields.items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        body.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="schedule.png"\r\n'
            "Content-Type: image/png\r\n\r\n".encode()
        )
        body.write(file_bytes)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=body.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def send(text: str, photo: bytes = None) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("---- (no token, not sent)" + (" [+picture]" if photo else "") + " ----\n" + text + "\n")
        if photo and PREVIEW_DIR:
            p = Path(PREVIEW_DIR) / f"preview_{hashlib.sha1(text.encode()).hexdigest()[:6]}.png"
            p.write_bytes(photo)
        return
    base = {"chat_id": CHAT_ID, "parse_mode": "HTML"}
    if photo:
        _post("sendPhoto", {**base, "caption": text}, photo)
    else:
        _post("sendMessage", {**base, "text": text, "disable_web_page_preview": "true"})


# ---------------- state ----------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# ---------------- main ----------------
def main() -> int:
    now = datetime.fromisoformat(FAKE_NOW).replace(tzinfo=KYIV) if FAKE_NOW else datetime.now(KYIV)
    today = now.date()
    all_days = parse_days(fetch())
    days = {d: v for d, v in all_days.items() if d >= today}
    # yesterday is kept only for the timeline (outage that crosses midnight)
    tl_days = {d: v for d, v in all_days.items() if d >= today - timedelta(days=1)}

    state = load_state()
    seen = state.setdefault("days", {})
    sent = set(state.setdefault("sent", []))
    first_run = not seen

    # 1) schedule appeared / changed
    for d in sorted(days):
        info = days[d]
        fp = hashlib.sha1(json.dumps(info["intervals"]).encode()).hexdigest()[:12]
        old = seen.get(d.isoformat())
        if old == fp:
            continue
        if first_run:
            header = f"👋 Бот запущено, черга <b>{QUEUE}</b>\n"
        elif old is None:
            header = "🆕 З'явився графік\n"
        else:
            header = "🔄 <b>Графік змінився</b>\n"
        send(schedule_text(d, info, today, header), render_png(d, info))
        seen[d.isoformat()] = fp

    # 2) warnings and switch messages
    periods = timeline(tl_days)
    last_known_end = (datetime.combine(max(tl_days), datetime.min.time(), KYIV) + timedelta(days=1)) if tl_days else None
    events = []  # (moment, kind, previous_state_duration, next_moment)
    for i, (s, e) in enumerate(periods):
        prev_end = periods[i - 1][1] if i else None
        events.append((s, "off", (s - prev_end) if prev_end else None, e))
        if e < last_known_end:  # skip "on" at the edge of known data
            nxt = periods[i + 1][0] if i + 1 < len(periods) else None
            events.append((e, "on", e - s, nxt))

    for moment, kind, was, nxt in events:
        mins = (moment - now).total_seconds() / 60
        key_now, key_pre = f"{moment.isoformat()}|{kind}", f"{moment.isoformat()}|{kind}|pre"

        if LEAD_MINUTES and 0 < mins <= LEAD_MINUTES and key_pre not in sent and key_now not in sent:
            verb = "вимкнуть" if kind == "off" else "мають увімкнути"
            icon = "⚠️" if kind == "off" else "🔜"
            send(f"{icon} Через {dur(mins)} світло {verb} (о <b>{moment:%H:%M}</b>)")
            sent.add(key_pre)

        if -20 <= mins <= 0 and key_now not in sent:
            nxt_txt = f"{nxt:%H:%M}" if nxt else None
            if nxt and nxt.date() != moment.date():
                nxt_txt += f" ({nxt:%d.%m})"
            if kind == "off":
                lines = [f"🔴 <b>{moment:%H:%M} Світло вимкнули</b> (за графіком)"]
                if was:
                    lines.append(f"🕓 Воно було {dur(was.total_seconds() / 60)}")
                if nxt_txt:
                    lines.append(f"🗓 Очікуємо за розкладом о <b>{nxt_txt}</b>")
            else:
                lines = [f"🟢 <b>{moment:%H:%M} Світло мають увімкнути</b> (за графіком)",
                         f"🕓 Його не було {dur(was.total_seconds() / 60)}"]
                if nxt_txt:
                    lines.append(f"🗓 Наступне відключення о <b>{nxt_txt}</b>")
                else:
                    lines.append("🗓 Більше відключень за графіком немає")
            send("\n".join(lines))
            sent.add(key_now)

    # tidy
    cutoff = (now - timedelta(days=2)).isoformat()
    state["sent"] = sorted(k for k in sent if k.split("|")[0] >= cutoff)
    state["days"] = {d: fp for d, fp in seen.items() if d >= (today - timedelta(days=2)).isoformat()}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
