#!/usr/bin/env python3
"""
Tapo C560WS — denni monitor (06:00-22:00)
getEvents() polling -> events_1 bit 19 (kamera AI osoba)
-> SD karta continuous clip (vedio_type=1, prefer vedio_type=2) -> Groq popis -> Telegram
"""

import os, time, json, base64, subprocess, asyncio, urllib.request
from datetime import datetime

from night_window import is_night

CAMERA_IP      = os.getenv("TAPO_IP", "")
TAPO_EMAIL     = os.getenv("TAPO_EMAIL", "")
TAPO_PASSWORD  = os.getenv("TAPO_PASSWORD", "")
_user          = os.getenv("ONVIF_USER", "")
_pass          = os.getenv("ONVIF_PASS", "")
RTSP_URL       = os.getenv("RTSP_URL") or f"rtsp://{_user}:{_pass}@{CAMERA_IP}:554/stream1"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", ""))
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

POLL_INTERVAL  = 30
EVENT_WINDOW   = 300     # zpetne okno — pokryva MIN_EVENT_AGE + presah
MIN_EVENT_AGE  = 90      # zpracuj event az po 90s — klip musi byt finalizovan na SD
PERSON_BIT     = 524288  # events_1 bit 19 = kamera AI potvrdila osobu
PIR_ALARM      = 6       # alarm_type=6 = PIR sensor potvrzen
REQUIRE_PIR    = os.getenv("REQUIRE_PIR", "false").lower() == "true"  # C560#1 nema PIR u osob
GROQ_PERSON_ONLY = os.getenv("GROQ_PERSON_ONLY", "false").lower() == "true"  # skip kdyz Groq nevidi osobu
_nfs           = os.getenv("NIGHT_FILTER_START", "")  # HH:MM — od teto doby akceptuj jakykoli pohyb
NIGHT_FILTER_START = tuple(int(x) for x in _nfs.split(":")) if _nfs else None

# Face ID mapping — format: "face_id:name,face_id:name"
_raw_face_ids = os.getenv("FACE_ID_NAMES", "")
FACE_ID_NAMES: dict = {}
if _raw_face_ids:
    for _pair in _raw_face_ids.split(","):
        if ":" in _pair:
            _fid, _name = _pair.strip().split(":", 1)
            try:
                FACE_ID_NAMES[int(_fid)] = _name.strip()
            except ValueError:
                pass
LOG_FACE_IDS = os.getenv("LOG_FACE_IDS", "0") == "1"

def _parse_hhmm(val, default):
    """'HH:MM' -> (h, m); pri chybe vrati default."""
    try:
        h, m = str(val).strip().split(":", 1)
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except (ValueError, AttributeError):
        pass
    return default


# Default start 05:30 — navazuje na konec person_monitor okna, zadna ranni dira
MONITOR_START  = _parse_hhmm(os.getenv("GROQ_MONITOR_START", ""), (5, 30))
MONITOR_END    = _parse_hhmm(os.getenv("GROQ_MONITOR_END", ""), (22, 30))

processed_events = set()
sd_fail_streak   = 0   # pocet po sobe jdoucich SD selhani
reconnect_times  = []  # timestampy reconnectu za posledni hodinu

SD_FAIL_ALERT_AT = 3   # pocet selhani pred tech alertem
CAMERA_DOWN_ALERT_SEC = int(os.getenv("CAMERA_DOWN_ALERT_SEC", "900"))  # 15 min


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_monitoring_time():
    """Denní Telegram pipeline běží přesně když NENÍ astral noc (jediný zdroj pravdy).
    V noci přebírá person_monitor, ve dne groq_watch — handoff ve stejný okamžik."""
    return not is_night()


def outage_alert_due(fail_since, now, already_alerted, threshold=None):
    """True kdyz souvisly vypadek trva dele nez threshold a alert jeste nebyl poslan."""
    if fail_since is None or already_alerted:
        return False
    if threshold is None:
        threshold = CAMERA_DOWN_ALERT_SEC
    return now - fail_since >= threshold


def is_night_filter():
    """Po NIGHT_FILTER_START akceptujeme jakykoli pohyb (events_1 > 0), ne jen AI osobu."""
    if not NIGHT_FILTER_START:
        return False
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    return cur >= NIGHT_FILTER_START[0] * 60 + NIGHT_FILTER_START[1]


# ── Tech notifikace ───────────────────────────────────────────────────────────

def telegram_text(text):
    """Posle textovou zpravu (bez fotky) do Telegramu."""
    payload = json.dumps({"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        ), timeout=10)
    except Exception as e:
        print(f"[{ts()}] Telegram text chyba: {e}", flush=True)


# ── SD karta pipeline ──────────────────────────────────────────────────────────

def parse_recordings(raw):
    """Vraci list {startTime, endTime, vedio_type} ze surove odpovedi getRecordingsUTC."""
    clips = []
    for item in raw:
        for val in item.values():
            if isinstance(val, dict) and "startTime" in val:
                clips.append(val)
    return clips


def find_best_clip(clips, raw_start, raw_end):
    """Najde nejlepsi klip pro event. Preferuje vedio_type=2, fallback na vedio_type=1."""
    event_clip = None
    continuous_clip = None
    for clip in clips:
        if clip["startTime"] > raw_end or clip["endTime"] < raw_start:
            continue
        vt = str(clip.get("vedio_type", ""))
        if vt == "2" and event_clip is None:
            event_clip = clip
        elif vt == "1" and continuous_clip is None:
            continuous_clip = clip
    return event_clip or continuous_clip


async def _dl_clip_async(cam, clip_start, clip_end, time_correction, out_dir, out_file):
    from pytapo.media_stream.downloader import Downloader
    dl = Downloader(
        cam,
        clip_start,
        min(clip_end, clip_start + 5),
        time_correction,
        out_dir,
        overwriteFiles=True,
        fileName=out_file,
    )
    async for _ in dl.download():
        pass


def grab_sd_frame(cam, event):
    """Stahne frame ze SD karty pro dany event. Vraci cestu k JPG nebo None."""
    try:
        time_correction = cam.getTimeCorrection()
        if time_correction is False:
            return None

        raw_start = event["start_time"] - time_correction
        raw_end   = event["end_time"]   - time_correction

        et = datetime.fromtimestamp(event["start_time"]).strftime("%H:%M:%S")
        print(f"[{ts()}] SD query: tc={time_correction}s, raw_window={datetime.fromtimestamp(raw_start - 60).strftime('%H:%M:%S')}–{datetime.fromtimestamp(raw_end + 60).strftime('%H:%M:%S')}", flush=True)

        recordings = cam.getRecordingsUTC(int(raw_start - 60), int(raw_end + 60))
        if not recordings:
            date_str = datetime.fromtimestamp(event["start_time"]).strftime("%Y%m%d")
            recordings = cam.getRecordings(date_str)
            if not recordings:
                print(f"[{ts()}] SD prazdne pro event @ {et}", flush=True)
                return None
            print(f"[{ts()}] SD fallback getRecordings({date_str})", flush=True)

        clips = parse_recordings(recordings)
        clip  = find_best_clip(clips, raw_start, raw_end)

        if not clip:
            print(f"[{ts()}] SD klip nenalezen pro event @ {et}", flush=True)
            return None

        vt = str(clip.get("vedio_type", "?"))
        dl_start = max(clip["startTime"], raw_start)

        base       = f"sd_{int(time.time() * 1000)}"
        out_dir    = "/tmp/"
        clip_file  = os.path.join(out_dir, base + ".mp4")
        frame_path = os.path.join(out_dir, base + ".jpg")

        print(f"[{ts()}] SD stahuji vedio_type={vt} @ {datetime.fromtimestamp(dl_start).strftime('%H:%M:%S')}", flush=True)

        asyncio.run(_dl_clip_async(
            cam, dl_start, dl_start + 5,
            time_correction, out_dir, base + ".mp4"
        ))

        if not os.path.exists(clip_file) or os.path.getsize(clip_file) == 0:
            print(f"[{ts()}] SD stazeni selhalo (prazdny soubor)", flush=True)
            return None

        subprocess.run([
            "ffmpeg", "-i", clip_file,
            "-frames:v", "1", "-vf", "scale=1280:-1",
            "-q:v", "4", "-y", frame_path
        ], capture_output=True, timeout=15)

        try:
            os.remove(clip_file)
        except OSError:
            pass

        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
            print(f"[{ts()}] SD frame OK (vedio_type={vt})", flush=True)
            return frame_path

    except Exception as e:
        print(f"[{ts()}] grab_sd_frame chyba: {e}", flush=True)

    return None


# ── Live fallback ──────────────────────────────────────────────────────────────

def grab_frame():
    for attempt in range(3):
        path = f"/tmp/gw_{int(time.time() * 1000)}.jpg"
        try:
            subprocess.run([
                "ffmpeg", "-rtsp_transport", "tcp", "-i", RTSP_URL,
                "-frames:v", "1", "-vf", "scale=1280:-1", "-q:v", "4", "-y", path
            ], capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[{ts()}] RTSP timeout pokus {attempt+1}/3", flush=True)
            continue
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


# ── Groq + Telegram ───────────────────────────────────────────────────────────

_PERSON_WORDS = {
    "osoba", "člověk", "clovek", "muž", "muz", "žena", "zena",
    "dívka", "divka", "chlapec", "dítě", "dite", "chodec",
    "postava", "figura", "pohyb",
}

def has_person(desc: str) -> bool:
    low = desc.lower()
    return any(w in low for w in _PERSON_WORDS)


def groq_describe(img_path):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                "Popis jednou vetou (3-6 slov cesky) co nejdulezitejsiho vidis v kamerovem zaberu. "
                "Vidis-li osobu: popis obleceni, co dela, smer pohybu. "
                "Vidis-li jen auto (bez osoby): barva a pohyb. "
                "Pokud neni zadna osoba — pouze strom, stin, vitr, prazdno: napisi presne: prazdny zaber. "
                "Pouze popis, zadny dalsi text, zadne uvozovky."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]}],
        "max_tokens": 50
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
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def telegram_photo(img_path, caption):
    with open(img_path, "rb") as f:
        data = f.read()
    boundary = "GWBoundary42"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.jpg\"\r\n"
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    ), timeout=15)


# ── Handler ───────────────────────────────────────────────────────────────────

def handle_person_event(event, cam):
    global sd_fail_streak
    et = datetime.fromtimestamp(event["start_time"]).strftime("%H:%M:%S")
    print(f"[{ts()}] Osoba @ {et} -> SD klip...", flush=True)

    img = None
    source_label = "?"
    groq_ok = True
    try:
        img = grab_sd_frame(cam, event)
        if img:
            if sd_fail_streak >= SD_FAIL_ALERT_AT:
                hostname = os.uname().nodename
                telegram_text(
                    f"✅ <b>{hostname}</b>: SD pipeline OK po {sd_fail_streak}× selhani."
                )
            sd_fail_streak = 0
            source_label = "💾 SD"
        else:
            sd_fail_streak += 1
            source_label = "📡 RTSP"
            if sd_fail_streak == SD_FAIL_ALERT_AT:
                hostname = os.uname().nodename
                telegram_text(
                    f"⚠️ <b>{hostname}</b>: SD karta pipeline selhává "
                    f"{sd_fail_streak}× po sobě — přecházím na RTSP fallback.\n"
                    f"Zkontroluj groq-watch logy."
                )
                print(f"[{ts()}] Tech alert: SD selhani {sd_fail_streak}x", flush=True)
            img = grab_frame()

        if not img:
            print(f"[{ts()}] Frame ziskani selhalo", flush=True)
            return

        try:
            desc = groq_describe(img)
            print(f"[{ts()}] Groq [{source_label}]: '{desc}'", flush=True)
        except Exception as e:
            desc = "?"
            groq_ok = False
            print(f"[{ts()}] Groq chyba: {e}", flush=True)

        groq_tag = "" if groq_ok else " ⚠️Groq"
        face_ids = [f["face_id"] for f in event.get("event_info", []) if "face_id" in f]
        if LOG_FACE_IDS and face_ids:
            print(f"[{ts()}] face_ids: {face_ids}", flush=True)
        face_labels = [FACE_ID_NAMES.get(fid) for fid in face_ids]
        known = [l for l in face_labels if l]
        unknown_count = face_labels.count(None)
        if known and not unknown_count:
            face_line = f"\n👤 {', '.join(known)}"
        elif known and unknown_count:
            face_line = f"\n👤 {', '.join(known)} + neznámá tvář"
        elif face_ids:
            face_line = "\n👤 neznámá tvář"
        else:
            face_line = ""
        if "prazdny" in desc.lower() or "prázdný" in desc.lower():
            print(f"[{ts()}] Groq: prazdny zaber, kamera potvrdila -> pohyb", flush=True)
            desc = "pohyb"
        if GROQ_PERSON_ONLY and groq_ok and not has_person(desc):
            print(f"[{ts()}] GROQ_PERSON_ONLY: zadna osoba ('{desc}') -> skip", flush=True)
            return
        if not groq_ok:
            caption = f"👤 pohyb{face_line}\n{et} [{source_label}{groq_tag}]"
        else:
            caption = f"👤 {desc}{face_line}\n{et} [{source_label}{groq_tag}]"

        telegram_photo(img, caption)
        print(f"[{ts()}] Telegram OK [{source_label}]: {desc!r}", flush=True)

    except Exception as e:
        print(f"[{ts()}] Chyba: {e}", flush=True)
    finally:
        if img and os.path.exists(img):
            try:
                os.remove(img)
            except OSError:
                pass


# ── Hlavni smycka ─────────────────────────────────────────────────────────────

def main():
    from pytapo import Tapo
    print(
        f"[{ts()}] Denni monitor start - SD clip pipeline "
        f"(poll={POLL_INTERVAL}s, window={EVENT_WINDOW}s, min_age={MIN_EVENT_AGE}s, person_bit={PERSON_BIT}, require_pir={REQUIRE_PIR})",
        flush=True
    )

    cam = None
    fail_since = None      # zacatek souvisleho vypadku spojeni
    down_alerted = False   # uz jsme poslali alert na tento vypadek

    while True:
        try:
            if not is_monitoring_time():
                if cam is not None:
                    cam = None
                    print(f"[{ts()}] Mimo monitoring cas - pytapo uvolneno", flush=True)
                # mimo okno nezkousime spojeni -> vypadek nelze merit
                fail_since = None
                down_alerted = False
                time.sleep(60)
                continue

            if cam is None:
                try:
                    cam = Tapo(CAMERA_IP, TAPO_EMAIL, TAPO_PASSWORD, TAPO_PASSWORD)
                    now_t = time.time()
                    if down_alerted:
                        hostname = os.uname().nodename
                        mins = int((now_t - fail_since) / 60)
                        telegram_text(
                            f"🟢 <b>{hostname}</b>: kamera {CAMERA_IP} zase dostupná "
                            f"(výpadek {mins} min)."
                        )
                    fail_since = None
                    down_alerted = False
                    reconnect_times.append(now_t)
                    reconnect_times[:] = [t for t in reconnect_times if t > now_t - 3600]
                    print(f"[{ts()}] Pytapo pripojeno (reconnect #{len(reconnect_times)} za posledni hodinu)", flush=True)
                    if len(reconnect_times) == 4:
                        hostname = os.uname().nodename
                        telegram_text(
                            f"⚠️ <b>{hostname}</b>: kamera se odpojila "
                            f"{len(reconnect_times)}× za poslední hodinu — nestabilní spojení."
                        )
                except Exception as e:
                    now_t = time.time()
                    if fail_since is None:
                        fail_since = now_t
                    if outage_alert_due(fail_since, now_t, down_alerted):
                        down_alerted = True
                        hostname = os.uname().nodename
                        mins = int((now_t - fail_since) / 60)
                        telegram_text(
                            f"🔴 <b>{hostname}</b>: kamera {CAMERA_IP} nedostupná už {mins} min "
                            f"(groq-watch).\nZkontroluj kameru / IP — mohla se změnit přes DHCP."
                        )
                        print(f"[{ts()}] Tech alert: kamera nedostupna {mins} min", flush=True)
                    print(f"[{ts()}] Pytapo chyba: {e} - retry za 30s", flush=True)
                    time.sleep(30)
                    continue

            now = int(time.time())
            events = cam.getEvents(startTime=now - EVENT_WINDOW, endTime=now)
            if is_night_filter():
                person_events = [e for e in events if e.get("events_1", 0) > 0]
            else:
                person_events = [e for e in events
                                 if e.get("events_1", 0) & PERSON_BIT
                                 and (not REQUIRE_PIR or e.get("alarm_type") == PIR_ALARM)]

            if person_events:
                mode = "NOC" if is_night_filter() else "DEN"
                print(f"[{ts()}] {len(person_events)} event(s) v okne [{mode}]", flush=True)

            for event in sorted(person_events, key=lambda e: e["start_time"]):
                key = event["start_time"]
                if key in processed_events:
                    continue
                # Pockat az bude klip na SD dost stary — SD indexuje klip se zpozenim
                if event["start_time"] > now - MIN_EVENT_AGE:
                    continue
                processed_events.add(key)
                handle_person_event(event, cam)

            cutoff = time.time() - 7200
            processed_events.difference_update(
                {k for k in list(processed_events) if k < cutoff}
            )

        except Exception as e:
            print(f"[{ts()}] Chyba smycky: {e} - reconnect", flush=True)
            cam = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
