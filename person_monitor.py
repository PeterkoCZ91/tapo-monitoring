#!/usr/bin/env python3
"""
Tapo C560WS person monitor
22:30-05:30: ONVIF event poll -> RTSP snapshot -> Groq -> Telegram
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from night_window import is_night


def parse_face_id_names(value: str) -> dict:
    """Parse FACE_ID_NAMES='123:petr,456:jana' into a string-keyed map."""
    names = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARNING: ignoruji FACE_ID_NAMES polozku bez dvojtecky: {pair!r}")
            continue
        face_id, name = (part.strip() for part in pair.split(":", 1))
        if not face_id or not name:
            continue
        names[face_id] = name
    return names


def _get_int_env(name: str, default: int) -> int:
    val = os.getenv(name, str(default))
    try:
        return int(val)
    except ValueError:
        print(f"[ERROR] {name} musí být celé číslo, dostali jsme {val!r}")
        sys.exit(2)


def _get_float_env(name: str, default: float) -> float:
    val = os.getenv(name, str(default))
    try:
        return float(val)
    except ValueError:
        print(f"[ERROR] {name} musí být desetinné číslo, dostali jsme {val!r}")
        sys.exit(2)


CAMERA_IP = os.getenv("TAPO_IP", "")
ONVIF_USER = os.getenv("ONVIF_USER", "")
ONVIF_PASS = os.getenv("ONVIF_PASS", "")
ONVIF_PORT = _get_int_env("ONVIF_PORT", 2020)
RTSP_URL = os.getenv("RTSP_URL") or (
    f"rtsp://{urllib.parse.quote(ONVIF_USER, safe='')}:{urllib.parse.quote(ONVIF_PASS, safe='')}@{CAMERA_IP}:554/stream1"
)


def mask_secrets(text: str) -> str:
    """Odstraní ONVIF přihlašovací údaje z textu před logováním."""
    for secret in (ONVIF_PASS, ONVIF_USER):
        if not secret:
            continue
        text = text.replace(secret, "***")
        encoded = urllib.parse.quote(secret, safe="")
        if encoded != secret:
            text = text.replace(encoded, "***")
    return text

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", ""))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

TAPO_EMAIL = os.getenv("TAPO_EMAIL", "")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")

POLL_INTERVAL = _get_float_env("POLL_INTERVAL", 3.0)
COOLDOWN = _get_int_env("COOLDOWN", 120)
RECONCILE_INTERVAL = _get_int_env("RECONCILE_INTERVAL", 60)
RETURN_DELAY = _get_int_env("RETURN_DELAY", 90)
HOME_PRESET = os.getenv("HOME_PRESET", "2")
SUBSCRIPTION_MAXAGE = _get_int_env("SUBSCRIPTION_MAXAGE", 540)
MONITOR_START = os.getenv("MONITOR_START", "22:30")
MONITOR_END = os.getenv("MONITOR_END", "05:30")
STRICT_PEOPLE = os.getenv("STRICT_PEOPLE", "1").lower() not in ("0", "false", "no", "off")
SNAP_DELAY = _get_float_env("SNAP_DELAY", 1.5)
SNAP_FRAMES = _get_int_env("SNAP_FRAMES", 3)
FACE_ID_NAMES = parse_face_id_names(os.getenv("FACE_ID_NAMES", ""))
LOG_FACE_IDS = os.getenv("LOG_FACE_IDS", "0").lower() in ("1", "true", "yes", "on")
CAMERA_DOWN_ALERT_SEC = _get_int_env("CAMERA_DOWN_ALERT_SEC", 900)
AUTOTRACK_REASSERT_INTERVAL = _get_int_env("AUTOTRACK_REASSERT_INTERVAL", 300)


def outage_alert_due(fail_since, now, already_alerted, threshold=None):
    """True kdyz souvisly vypadek trva dele nez threshold a alert jeste nebyl poslan."""
    if fail_since is None or already_alerted:
        return False
    if threshold is None:
        threshold = CAMERA_DOWN_ALERT_SEC
    return now - fail_since >= threshold


def autotrack_reassert_due(last_assert, now, interval=None):
    """True kdyz od posledniho re-assertu auto-trackingu uplynul interval.
    last_assert=0 -> True hned po startu (assert po prvnim pripojeni)."""
    if interval is None:
        interval = AUTOTRACK_REASSERT_INTERVAL
    return now - last_assert >= interval


def should_reassert_autotrack(night, last_assert, now, interval=None):
    """Reassert autotrack jen kdyz je SKUTECNA astral noc.
    person_monitor monitoruje do MONITOR_END (05:30), ale camera_automation prepne
    na den uz v astral sunrise+30 (~05:09). Bez teto branky by reassert v okne
    05:09-05:30 znovu zapnul autotrack, ktery camera_automation prave vypnul, a
    kamera by pres den honila pohyb (auta). Viz night_window.is_night()."""
    return night and autotrack_reassert_due(last_assert, now, interval)

last_alert_time: float = 0.0
daily_count: int = 0
daily_date: str = ""
last_seen_event_time: int = 0
last_reconcile: float = 0.0
last_autotrack_assert: float = 0.0
last_person_event_time: float = 0.0
returned_to_home: bool = True
subscription_start_time: float = 0.0
_tapo_client = None
_getevents_initialized: bool = False


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_hhmm(value, name):
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except ValueError:
        print(f"[{ts()}] ERROR: {name} musi byt ve formatu HH:MM, aktualne {value!r}")
        sys.exit(2)


MONITOR_START_HM = parse_hhmm(MONITOR_START, "MONITOR_START")
MONITOR_END_HM = parse_hhmm(MONITOR_END, "MONITOR_END")


def is_time_in_window(start_min: int, end_min: int, cur_min: int) -> bool:
    """Vrátí True pokud cur_min leží v [start_min, end_min).
    Podporuje okna ve stejný den (start < end) i přes půlnoc (start > end).
    Pokud start == end, vrací vždy False (nulové okno = žádné monitorování)."""
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def require_config():
    missing = [
        name for name, value in {
            "TAPO_IP": CAMERA_IP,
            "ONVIF_USER": ONVIF_USER,
            "ONVIF_PASS": ONVIF_PASS,
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
            "GROQ_API_KEY": GROQ_API_KEY,
        }.items()
        if not value
    ]
    if missing:
        print(f"[{ts()}] ERROR: chybi environment variables: {', '.join(missing)}")
        sys.exit(2)


def is_monitoring_time():
    """Hlídáme lidi přesně během astral noci (jediný zdroj pravdy = night_window).
    Handoff s groq_watch i s autotrackem (camera_automation) je tak ve stejný okamžik."""
    return is_night()


def connect_onvif():
    from onvif import ONVIFCamera
    cam = ONVIFCamera(CAMERA_IP, ONVIF_PORT, ONVIF_USER, ONVIF_PASS)
    events_svc = cam.create_events_service()
    events_svc.CreatePullPointSubscription({"InitialTerminationTime": "PT1H"})
    return cam.create_pullpoint_service()


def pull_events(pull_svc):
    resp = pull_svc.PullMessages({"MessageLimit": 10, "Timeout": "PT3S"})
    msgs = resp.NotificationMessage or []
    return msgs if isinstance(msgs, list) else [msgs]


def _parse_lxml_message(msg_val):
    """Parse lxml element returned by some camera firmware instead of zeep object.
    Returns (property_operation, items_dict)."""
    prop_op = (msg_val.get("PropertyOperation") or "").lower()
    items = {}
    ns = "http://www.onvif.org/ver10/schema"
    for si in (
        msg_val.findall(f"{{{ns}}}Data/{{{ns}}}SimpleItem")
        or msg_val.findall(f".//{{{ns}}}SimpleItem")
        or msg_val.findall(".//SimpleItem")
    ):
        name = si.get("Name", "").lower()
        value = si.get("Value", "").lower()
        if name:
            items[name] = value
    return prop_op, items


def simple_items(msg):
    items = {}
    try:
        msg_val = msg.Message._value_1
        if hasattr(msg_val, "tag"):
            _, items = _parse_lxml_message(msg_val)
        else:
            data = msg_val.Data
            simple = data.SimpleItem or []
            if not isinstance(simple, list):
                simple = [simple]
            for item in simple:
                items[str(item.Name).lower()] = str(item.Value).lower()
    except Exception:
        pass
    return items


TYPE_EMOJI = {
    "person":  "👤",
    "vehicle": "🚗",
    "pet":     "🐾",
    "tamper":  "⚠️",
    "motion":  "👁",
}


def is_person_event(msg) -> tuple:
    """Vrátí (triggered, event_type). event_type je klíč do TYPE_EMOJI."""
    try:
        topic_raw = msg.Topic._value_1
        topic = str(topic_raw).lower() if topic_raw is not None else ""

        prop_op = ""
        items = {}
        try:
            msg_val = msg.Message._value_1
            if hasattr(msg_val, "tag"):
                prop_op, items = _parse_lxml_message(msg_val)
            else:
                items = simple_items(msg)
        except Exception:
            pass

        print(f"[{ts()}] topic={topic!r} op={prop_op!r} items={items}")

        # Cars are never alerted
        if items.get("iscar") == "true":
            return False, ""

        if items.get("ispeople") == "true":
            return True, "person"
        if items.get("ispet") == "true":
            return True, "pet"
        if "tamper" in topic:
            return True, "tamper"
        if any(k in topic for k in ["person", "human"]):
            return True, "person"
        if items.get("ismotion") == "true" and not STRICT_PEOPLE:
            return True, "motion"
        if not STRICT_PEOPLE and any(k in topic for k in ["motion", "analytics", "ruleengine"]):
            return True, "motion"

        # Firmware returns no parseable topic or items: use PropertyOperation as
        # fallback. "Changed" = actual detection event; "Initialized" = state init at boot.
        if prop_op == "changed" and not topic and not items:
            return True, "motion"

        return False, ""
    except Exception as e:
        print(f"[{ts()}] Event parse chyba: {e}")
        return False, ""


def remove_quiet(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _sharpness(path):
    """Laplacian variance — vyšší = ostřejší."""
    try:
        import cv2
        import numpy as np
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0.0
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except Exception:
        return 0.0


def grab_snapshot_full():
    """Stáhne SNAP_FRAMES snímků z RTSP (po SNAP_DELAY sekundách) a vrátí nejostřejší."""
    time.sleep(SNAP_DELAY)
    candidates = []
    for i in range(max(1, SNAP_FRAMES)):
        path = f"/tmp/tapo_full_{int(time.time())}_{i}.jpg"
        try:
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", RTSP_URL,
                "-frames:v", "1",
                "-q:v", "2", "-y", path
            ], capture_output=True, timeout=8)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                candidates.append(path)
            else:
                stderr = result.stderr.decode(errors="replace").strip().splitlines()
                detail = stderr[-1] if stderr else f"exit {result.returncode}"
                print(f"[{ts()}] ffmpeg snimek {i} selhal: {mask_secrets(detail)}")
                remove_quiet(path)
        except Exception as e:
            print(f"[{ts()}] ffmpeg chyba: {e}")
            remove_quiet(path)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    scored = [(p, _sharpness(p)) for p in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    best, score = scored[0]
    print(f"[{ts()}] Nejostrejsi snimek: {score:.0f} bodů z {len(candidates)}")
    for path, _ in scored[1:]:
        remove_quiet(path)
    return best


def _detect_faces(gray, img, scale):
    """Zkusí frontální kaskádu, pak profilovou. Vrátí největší detekci nebo None."""
    import cv2
    cascades = [
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
        cv2.data.haarcascades + "haarcascade_profileface.xml",
    ]
    best = None
    for cascade_path in cascades:
        cascade = cv2.CascadeClassifier(cascade_path)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(12, 12))
        if len(faces) > 0:
            candidate = max(faces, key=lambda f: f[2] * f[3])
            if best is None or candidate[2] * candidate[3] > best[2] * best[3]:
                best = candidate
    if best is None:
        return None
    x, y, fw, fh = best
    return int(x / scale), int(y / scale), int(fw / scale), int(fh / scale)


def process_frame(full_path):
    """Ze 4K frame vrati (wide_crop_path, zoom_crop_path).
    Zoom = oblicej (Haar), nebo horni cast wide jako fallback."""
    import cv2
    img = cv2.imread(full_path)
    if img is None:
        return None, None

    h, w = img.shape[:2]
    crop_w, crop_h = min(1920, w), min(1080, h)
    cx, cy = max(0, (w - crop_w) // 2), max(0, (h - crop_h) // 2)
    wide = img[cy:cy+crop_h, cx:cx+crop_w]
    wide_path = full_path.replace("_full_", "_wide_")
    if not cv2.imwrite(wide_path, wide, [cv2.IMWRITE_JPEG_QUALITY, 85]):
        return None, None

    scale = 0.25
    small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    face = _detect_faces(gray, img, scale)

    face_path = full_path.replace("_full_", "_face_")
    if face is not None:
        x, y, fw, fh = face
        pad = int(max(fw, fh) * 0.8)
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(w, x + fw + pad), min(h, y + fh + pad)
        face_img = img[y1:y2, x1:x2]
        if face_img.size > 0:
            cv2.imwrite(face_path, face_img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(f"[{ts()}] Oblicej Haar: {fw}x{fh}px")
            return wide_path, face_path

    # Fallback: horní 55 % wide záběru — kde bývá obličej/hlava
    wh, ww = wide.shape[:2]
    upper = wide[:int(wh * 0.55), :]
    if upper.size > 0:
        cv2.imwrite(face_path, upper, [cv2.IMWRITE_JPEG_QUALITY, 88])
        print(f"[{ts()}] Oblicej nenalezen, zoom = horni cast ({upper.shape[1]}x{upper.shape[0]}px)")
    return wide_path, face_path


def groq_describe(img_path):
    if not os.path.exists(img_path):
        return ""
    try:
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = json.dumps({
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "Jsi noctni bezpecnostni kamera. "
                    "Pokud vidis osobu (jednu nebo vice): popis obleceni, chovani a smer pohybu (max 15 slov cesky). "
                    "Pokud vidis jen vozidlo bez osoby, nebo prazdny zaber: odpoved pouze 'prazdny zaber'. "
                    "Jen popis, nic jineho."
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}],
            "max_tokens": 60
        }).encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "groq-python/0.13.0"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        print(f"[{ts()}] Groq HTTP {e.code}: {detail}")
        return ""
    except Exception as e:
        print(f"[{ts()}] Groq chyba: {e}")
        return ""


def telegram_text(text):
    """Posle textovou (tech) zpravu do Telegramu."""
    try:
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"
        }).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        ), timeout=10)
    except Exception as e:
        print(f"[{ts()}] Telegram text chyba: {e}")


def telegram_photo(img_path, caption):
    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
        boundary = "TapoBoundary42"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{TELEGRAM_CHAT}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="snap.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            response = resp.read().decode(errors="replace")
            if resp.status >= 300 or '"ok":true' not in response:
                print(f"[{ts()}] Telegram odpoved {resp.status}: {response[:200]}")
                return False
        print(f"[{ts()}] Telegram OK: {caption[:50]}")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        print(f"[{ts()}] Telegram HTTP {e.code}: {detail}")
        return False
    except Exception as e:
        print(f"[{ts()}] Telegram chyba: {e}")
        return False

def _update_daily_count() -> int:
    global daily_count, daily_date
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_date != today:
        daily_count = 0
        daily_date = today
    daily_count += 1
    return daily_count


def handle_detection(event_type: str = "motion", detail: str = "", event_time: float | None = None):
    global last_alert_time, last_person_event_time, returned_to_home

    now = time.time()
    last_person_event_time = now
    returned_to_home = False
    capture_ts = event_time if event_time is not None else now

    if now - last_alert_time < COOLDOWN:
        remaining = int(COOLDOWN - (now - last_alert_time))
        print(f"[{ts()}] Cooldown aktivni ({remaining}s zbývá), skip")
        return

    prev_alert_time = last_alert_time
    count = _update_daily_count()
    emoji = TYPE_EMOJI.get(event_type, "👁")

    since_last = ""
    if count > 1 and prev_alert_time > 0:
        minutes_ago = max(1, int((now - prev_alert_time) / 60))
        since_last = f" · minulá před {minutes_ago} min"

    print(f"[{ts()}] Nova detekce ({event_type}) -> snapshot...")

    full = wide = face = None
    try:
        full = grab_snapshot_full()
        if not full:
            return

        wide, face = process_frame(full)
        if not wide:
            return

        desc = groq_describe(wide)
        print(f"[{ts()}] Groq: '{desc}'")

        if "prazdny" in desc.lower() or "prázdný" in desc.lower():
            print(f"[{ts()}] Prazdny zaber -> skip")
            return

        time_str = datetime.fromtimestamp(capture_ts).strftime("%H:%M")
        headline = f"{emoji} {detail} {time_str}".strip() if detail else f"{emoji} {time_str}"
        if desc:
            caption = f"{headline}\n\"{desc}\"\n📊 {count}. detekce dnes{since_last}"
        else:
            caption = f"{headline}\n📊 {count}. detekce dnes{since_last}"

        sent = telegram_photo(wide, caption)
        if not sent:
            print(f"[{ts()}] Telegram selhal (wide foto), cooldown se neaktivuje")
            return

        last_alert_time = now
        if face:
            zoom_desc = groq_describe(face)
            if zoom_desc and "prazdny" not in zoom_desc.lower() and "prázdný" not in zoom_desc.lower():
                telegram_photo(face, "🔍 zoom")
            else:
                print(f"[{ts()}] Zoom skip: kamera nezabírá osobu ({zoom_desc!r:.60})")

        print(f"[{ts()}] Odeslano ({event_type}), dalsi alert za {COOLDOWN}s")
    finally:
        for path in (full, wide, face):
            remove_quiet(path)


def test_pipeline():
    """--test flag: preskoci ONVIF, rovnou snapshot -> face crop -> Groq -> Telegram"""
    print(f"[{ts()}] TEST MODE: snapshot -> face crop -> Groq -> Telegram")
    full = wide = face = None
    try:
        full = grab_snapshot_full()
        if not full:
            print("FAIL: snapshot selhal")
            sys.exit(1)
        print(f"[{ts()}] Full 4K OK: {full}")
        wide, face = process_frame(full)
        if not wide:
            print("FAIL: process_frame selhal")
            sys.exit(1)
        desc = groq_describe(wide)
        print(f"[{ts()}] Groq: '{desc}'")
        time_str = datetime.now().strftime("%H:%M")
        if desc:
            caption = f"👤 TEST {time_str}\n\"{desc}\"\n📊 test run"
        else:
            caption = f"👁 TEST pohyb {time_str}"
        telegram_photo(wide, caption)
        if face:
            telegram_photo(face, "🔍 TEST zoom")
            print(f"[{ts()}] Face crop odeslán")
        else:
            print(f"[{ts()}] Žádný obličej nenalezen")
    finally:
        for path in (full, wide, face):
            remove_quiet(path)
    print("[TEST] Hotovo")


def return_to_home_preset():
    global returned_to_home
    tapo = get_tapo_client()
    if tapo is None:
        return
    try:
        tapo.setPreset(HOME_PRESET)
        returned_to_home = True
        print(f"[{ts()}] Vrácení na home preset {HOME_PRESET} OK")
    except Exception as e:
        print(f"[{ts()}] setPreset chyba: {e}")


def reassert_autotrack():
    """Bezpecnostni sit: kdyby v noci auto-tracking pres camera_automation prece
    jen spadl (firmware reset, reboot kamery), znovu zapne master prepinac.
    NEvolame setSmartTrackConfig — to samo auto-tracking shazuje a people-only
    konfig persistuje nezavisle (nastaveny pri prechodu den->noc)."""
    tapo = get_tapo_client()
    if tapo is None:
        return
    try:
        enabled = tapo.getAutoTrackTarget().get("enabled", "").lower() == "on"
        if enabled:
            return
        tapo.setAutoTrackTarget(True)
        print(f"[{ts()}] Auto-tracking byl shozen -> znovu zapnut")
    except Exception as e:
        print(f"[{ts()}] reassert_autotrack chyba: {e}")


def get_tapo_client():
    global _tapo_client
    if _tapo_client is not None:
        return _tapo_client
    if not TAPO_EMAIL or not TAPO_PASSWORD:
        return None
    try:
        from pytapo import Tapo
        _tapo_client = Tapo(CAMERA_IP, TAPO_EMAIL, TAPO_PASSWORD, TAPO_EMAIL)
        print(f"[{ts()}] pytapo klient inicializovan")
        return _tapo_client
    except Exception as e:
        print(f"[{ts()}] pytapo init selhal: {e}")
        return None


_GETEVENTS_PERSON_TYPES = frozenset({"person", "human", "people"})
_GETEVENTS_SKIP_TYPES = frozenset({"vehicle", "car"})


def _event_face_ids(ev: dict) -> list:
    info = ev.get("event_info")
    if not isinstance(info, list):
        return []
    face_ids = []
    for item in info:
        if isinstance(item, dict) and item.get("face_id") is not None:
            face_ids.append(str(item["face_id"]))
    return face_ids


def _face_detail(face_ids: list) -> str:
    if not face_ids:
        return ""
    known = [FACE_ID_NAMES[face_id] for face_id in face_ids if face_id in FACE_ID_NAMES]
    unknown_count = len(face_ids) - len(known)
    labels = known[:]
    if unknown_count == 1:
        labels.append("neznama tvar")
    elif unknown_count > 1:
        labels.append(f"{unknown_count} neznamych tvari")
    return ", ".join(labels)


def _log_face_summary(face_ids: list):
    if not face_ids:
        return
    labels = []
    for face_id in face_ids:
        if face_id in FACE_ID_NAMES:
            labels.append(FACE_ID_NAMES[face_id])
        elif LOG_FACE_IDS:
            labels.append(f"unknown:{face_id}")
        else:
            labels.append("unknown:<redacted>")
    print(f"[{ts()}] getEvents() face_info: {', '.join(labels)}")


def _classify_get_event(ev: dict) -> str:
    """Map a getEvents() dict to a TYPE_EMOJI key, or '' to skip."""
    if _event_face_ids(ev):
        return "person"
    raw = str(ev.get("event_type") or ev.get("type") or "").lower()
    if any(t in raw for t in _GETEVENTS_PERSON_TYPES):
        return "person"
    if any(t in raw for t in _GETEVENTS_SKIP_TYPES):
        return ""
    if "pet" in raw:
        return "pet"
    if "tamper" in raw:
        return "tamper"
    return "motion"


def reconcile_get_events():
    global last_seen_event_time, _tapo_client, _getevents_initialized
    tapo = get_tapo_client()
    if tapo is None:
        return
    try:
        events = tapo.getEvents() or []
        if not events:
            return
        new_events = [e for e in events if e.get("start_time", 0) > last_seen_event_time]
        if not new_events:
            return
        newest_ts = max(e.get("start_time", 0) for e in new_events)
        last_seen_event_time = newest_ts
        if not _getevents_initialized:
            _getevents_initialized = True
            print(f"[{ts()}] getEvents() baseline: {len(events)} eventů ignorováno po restartu")
            return
        print(f"[{ts()}] getEvents() záloha: {len(new_events)} nových eventů")
        for ev in sorted(new_events, key=lambda item: item.get("start_time", 0)):
            face_ids = _event_face_ids(ev)
            _log_face_summary(face_ids)
            event_type = _classify_get_event(ev)
            if not event_type:
                print(f"[{ts()}] getEvents() skip (vozidlo): {ev.get('event_type','?')}")
                continue
            if STRICT_PEOPLE and event_type == "motion":
                print(f"[{ts()}] getEvents() skip (strict_people, motion)")
                continue
            handle_detection(event_type, _face_detail(face_ids), event_time=ev.get("start_time"))
            break
    except Exception as e:
        print(f"[{ts()}] getEvents() záloha chyba: {e}")
        _tapo_client = None


def main():
    require_config()
    if "--test" in sys.argv:
        test_pipeline()
        return

    print(f"[{ts()}] Person monitor start (okno=astral noc dle night_window, "
          f"strict_people={STRICT_PEOPLE}, cooldown={COOLDOWN}s)")
    pull_svc = None
    fail_since = None      # zacatek souvisleho vypadku ONVIF spojeni
    down_alerted = False   # uz jsme poslali alert na tento vypadek
    global last_reconcile, subscription_start_time, last_autotrack_assert

    while True:
        try:
            if not is_monitoring_time():
                if pull_svc:
                    print(f"[{ts()}] Mimo hodiny, odpojuji ONVIF")
                    pull_svc = None
                fail_since = None
                down_alerted = False
                time.sleep(30)
                continue

            if pull_svc is None:
                print(f"[{ts()}] Pripojuji ONVIF...")
                pull_svc = connect_onvif()
                subscription_start_time = time.time()
                print(f"[{ts()}] ONVIF subscription aktivni")
                if down_alerted:
                    mins = int((time.time() - fail_since) / 60)
                    telegram_text(
                        f"🟢 <b>{os.uname().nodename}</b>: kamera {CAMERA_IP} zase dostupná "
                        f"(výpadek {mins} min)."
                    )
                fail_since = None
                down_alerted = False
            elif time.time() - subscription_start_time > SUBSCRIPTION_MAXAGE:
                print(f"[{ts()}] Proaktivni reconnect ONVIF ({SUBSCRIPTION_MAXAGE}s)")
                pull_svc = None
                continue

            msgs = pull_events(pull_svc)
            if msgs:
                print(f"[{ts()}] {len(msgs)} event(u)")
                for msg in msgs:
                    triggered, event_type = is_person_event(msg)
                    if triggered:
                        handle_detection(event_type)
                        break

            if time.time() - last_reconcile > RECONCILE_INTERVAL:
                reconcile_get_events()
                last_reconcile = time.time()

            if should_reassert_autotrack(is_night(), last_autotrack_assert, time.time()):
                reassert_autotrack()
                last_autotrack_assert = time.time()

            if (last_person_event_time > 0
                    and not returned_to_home
                    and time.time() - last_person_event_time > RETURN_DELAY):
                return_to_home_preset()

        except Exception as e:
            now = time.time()
            if fail_since is None:
                fail_since = now
            if outage_alert_due(fail_since, now, down_alerted):
                down_alerted = True
                mins = int((now - fail_since) / 60)
                telegram_text(
                    f"🔴 <b>{os.uname().nodename}</b>: kamera {CAMERA_IP} nedostupná už {mins} min "
                    f"(person-monitor).\nZkontroluj kameru / IP — mohla se změnit přes DHCP."
                )
                print(f"[{ts()}] Tech alert: kamera nedostupna {mins} min")
            print(f"[{ts()}] Chyba: {e}, reconnect za 30s")
            pull_svc = None
            time.sleep(30)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
