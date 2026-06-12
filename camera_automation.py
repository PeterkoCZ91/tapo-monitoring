#!/usr/bin/env python3
"""
Tapo C560WS camera automation
- Night (astral sunset−30 → sunrise+30, location via env): auto-tracking ON
- Day: auto-tracking OFF
- Idempotent: runs every minute via systemd timer, only acts on mode transitions
"""

import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import rain_window
from night_window import describe_window, is_night

CAMERA_IP = os.getenv("TAPO_IP", "")
CAMERA_USER = os.getenv("TAPO_EMAIL", "")
CAMERA_PASSWORD = os.getenv("TAPO_PASSWORD", "")
STATE_FILE = os.getenv("STATE_FILE", "/tmp/tapo_autotrack_state")
RAIN_STATE_FILE = STATE_FILE + ".rain"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT", ""))

DAY_PRESET = os.getenv("DAY_PRESET", "2")
NIGHT_PRESET = os.getenv("NIGHT_PRESET", "")

# Déšť snižuje citlivost na pohyb (kapky/odlesky), aby kamera míň plašila.
MOTION_SENS_NORMAL = os.getenv("MOTION_SENS_NORMAL", "60")
MOTION_SENS_RAIN = os.getenv("MOTION_SENS_RAIN", "20")


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def require_config():
    missing = [
        name for name, value in {
            "TAPO_IP": CAMERA_IP,
            "TAPO_EMAIL": CAMERA_USER,
            "TAPO_PASSWORD": CAMERA_PASSWORD,
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
        }.items()
        if not value
    ]
    if missing:
        print(f"[{ts()}] ERROR: chybi environment variables: {', '.join(missing)}")
        sys.exit(2)


def read_last_state(path=None):
    path = path or STATE_FILE
    try:
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def write_state(state: str, path=None) -> bool:
    path = path or STATE_FILE
    try:
        with open(path, "w") as f:
            f.write(state)
        return True
    except Exception as e:
        print(f"[{ts()}] ERROR: Could not write state file: {e}")
        return False


def decide_motion_sensitivity(rain_active, normal=None, rain=None):
    """Čistá funkce: při dešti vrátí sníženou citlivost na pohyb, jinak normální."""
    normal = normal if normal is not None else MOTION_SENS_NORMAL
    rain = rain if rain is not None else MOTION_SENS_RAIN
    return rain if rain_active else normal


def telegram_send(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        print(f"[{ts()}] Telegram send failed: {e}")


def telegram_alert(message: str):
    telegram_send(f"⚠️ Tapo kamera:\n{message}")


def set_autotrack(tapo, enabled: bool):
    if hasattr(tapo, "setAutoTrackTarget"):
        try:
            tapo.setAutoTrackTarget(enabled)
            return True
        except Exception as e:
            print(f"[{ts()}] setAutoTrackTarget raised: {e} -- trying fallback")

    value = "on" if enabled else "off"
    try:
        tapo.executeFunction("setAutoTrackTarget", {"auto_track_target": {"enabled": value}})
        return True
    except Exception as e:
        print(f"[{ts()}] executeFunction setAutoTrackTarget raised: {e}")

    try:
        tapo.executeFunction("setDetectionConfig", {"detection_config": {"auto_track_target": value}})
        return True
    except Exception as e:
        print(f"[{ts()}] executeFunction setDetectionConfig raised: {e}")

    return False


def verify_autotrack(tapo, expected: bool) -> bool:
    """Přečte zpět stav auto-trackingu z kamery a ověří že odpovídá expected."""
    try:
        result = tapo.getAutoTrackTarget()
        actual = result.get("enabled", "").lower() == "on"
        return actual == expected
    except Exception as e:
        print(f"[{ts()}] verify_autotrack chyba: {e}")
        return False


def connect_camera(Tapo):
    """Připojí se ke kameře (3 pokusy). Vrací (tapo|None, last_err)."""
    last_err = None
    for attempt in range(1, 4):
        try:
            return Tapo(CAMERA_IP, CAMERA_USER, CAMERA_PASSWORD, CAMERA_PASSWORD), None
        except Exception as e:
            last_err = e
            print(f"[{ts()}] ERROR (attempt {attempt}/3): {e}")
            if attempt < 3:
                time.sleep(5)
    return None, last_err


def alert_unreachable(last_err):
    """Tech alert na nedostupnou kameru, max 1× za UNREACHABLE_ALERT_COOLDOWN."""
    cooldown = int(os.getenv("UNREACHABLE_ALERT_COOLDOWN", "3600"))
    stamp = STATE_FILE + ".unreachable"
    now = time.time()
    last_alert = 0.0
    try:
        with open(stamp) as f:
            last_alert = float(f.read().strip())
    except (OSError, ValueError):
        pass
    if now - last_alert >= cooldown:
        telegram_alert(f"Nedostupná kamera {CAMERA_IP}\n<code>{last_err}</code>")
        try:
            with open(stamp, "w") as f:
                f.write(str(now))
        except OSError:
            pass
    else:
        print(f"[{ts()}] Kamera nedostupna, alert v cooldownu ({int(now - last_alert)}s)")


def apply_day_night_config(tapo, enable_tracking):
    """SmartTrack people-only + person detection + preset. BEZ autotracku —
    ten se nastavuje až úplně nakonec (viz ensure_autotrack)."""
    if enable_tracking:
        try:
            tapo.executeFunction("setSmartTrackConfig", {
                "smart_track": {"smart_track_info": {
                    "people_enabled": "on",
                    "vehicle_enabled": "off",
                    "pet_enabled": "off",
                    "baby_enabled": "off"
                }}
            })
            print(f"[{ts()}] SmartTrack: people only OK.")
        except Exception as e:
            print(f"[{ts()}] SmartTrack warning: {e}")

    try:
        tapo.setPersonDetection(enable_tracking)
        print(f"[{ts()}] PersonDetection: {'ON' if enable_tracking else 'OFF'} OK.")
    except Exception as e:
        print(f"[{ts()}] PersonDetection warning: {e}")

    if not enable_tracking:
        try:
            tapo.setPreset(DAY_PRESET)
            print(f"[{ts()}] Preset: {DAY_PRESET} (day home) OK.")
        except Exception as e:
            print(f"[{ts()}] Preset warning: {e}")
    elif NIGHT_PRESET:
        try:
            tapo.setPreset(NIGHT_PRESET)
            print(f"[{ts()}] Preset: {NIGHT_PRESET} (night home) OK.")
        except Exception as e:
            print(f"[{ts()}] Night preset warning: {e}")


def apply_rain_sensitivity(tapo, rain_active):
    """Při dešti sníží citlivost detekce pohybu, aby kamera míň plašila kapkami."""
    sensitivity = decide_motion_sensitivity(rain_active)
    stav = "déšť" if rain_active else "sucho"
    try:
        # POZOR: musí jít int. Číselný STRING ("60") pytapo přemapuje na label
        # "high" (digital 80) — opak záměru. int nastaví přesně digital_sensitivity.
        tapo.setMotionDetection(sensitivity=int(sensitivity))
        print(f"[{ts()}] Motion sensitivity -> {sensitivity} ({stav}) OK.")
    except Exception as e:
        print(f"[{ts()}] setMotionDetection warning: {e}")


def ensure_autotrack(tapo, enable_tracking):
    """Auto-track VŽDY jako poslední (setSmartTrackConfig/motion mohou shodit
    master přepínač) + verify + retry. Vrací True při úspěchu."""
    action_label = "ON (night mode)" if enable_tracking else "OFF (day mode)"
    print(f"[{ts()}] Setting auto-tracking {action_label} ...")
    if not set_autotrack(tapo, enable_tracking):
        print(f"[{ts()}] ERROR: All tracking methods failed.")
        telegram_alert(f"Selhalo nastavení auto-trackingu ({action_label})\nKamera: {CAMERA_IP}")
        return False

    time.sleep(1)
    if verify_autotrack(tapo, enable_tracking):
        print(f"[{ts()}] Verifikace OK.")
        return True
    print(f"[{ts()}] Verifikace selhala — retry ...")
    time.sleep(3)
    set_autotrack(tapo, enable_tracking)
    time.sleep(2)
    if not verify_autotrack(tapo, enable_tracking):
        print(f"[{ts()}] ERROR: Kamera nepřijala nastavení ani po retry.")
        telegram_alert(
            f"Auto-tracking {action_label}: příkaz přijat ale kamera se nepřepnula\nKamera: {CAMERA_IP}"
        )
        return False
    print(f"[{ts()}] Verifikace OK (po retry).")
    return True


def main():
    require_config()
    try:
        from pytapo import Tapo
    except ImportError:
        print(f"[{ts()}] ERROR: pytapo not installed.")
        sys.exit(1)

    now = time.time()

    # Dva nezávislé idempotentní stavy: den/noc (autotrack) a déšť (citlivost).
    desired = "night" if is_night() else "day"
    last_dn = read_last_state()

    raining = rain_window.is_raining_now(now)
    if raining:
        rain_window.touch_rain(now)
    rain_active = rain_window.is_rain_active(now, rain_window.read_last_rain())
    rain_state = "rain" if rain_active else "dry"
    last_rain = read_last_state(RAIN_STATE_FILE)

    need_dn = (last_dn != desired)
    need_rain = (last_rain != rain_state)
    if not need_dn and not need_rain:
        sys.exit(0)

    mode, info = describe_window()
    print(f"[{ts()}] window={mode} {info} | dn:{last_dn!r}->{desired!r} rain:{last_rain!r}->{rain_state!r}")
    print(f"[{ts()}] Connecting to {CAMERA_IP} ...")

    tapo, last_err = connect_camera(Tapo)
    if tapo is None:
        alert_unreachable(last_err)
        sys.exit(1)

    # Kamera zase dostupna — vycisti unreachable stamp pro pristi vypadek
    try:
        os.remove(STATE_FILE + ".unreachable")
    except OSError:
        pass

    enable_tracking = (desired == "night")

    if need_dn:
        apply_day_night_config(tapo, enable_tracking)
    if need_rain:
        apply_rain_sensitivity(tapo, rain_active)

    # Autotrack vzdy jako uplne posledni — i kdyz se menila jen citlivost, aby
    # setMotionDetection/SmartTrack nenechaly master prepinac ve spatnem stavu.
    if not ensure_autotrack(tapo, enable_tracking):
        sys.exit(1)

    if need_dn:
        write_state(desired)
    if need_rain:
        write_state(rain_state, RAIN_STATE_FILE)

    hostname = os.uname().nodename
    if need_dn:
        emoji = "🌙" if enable_tracking else "☀️"
        label = "noční režim — sledování zapnuto" if enable_tracking else "denní režim — sledování vypnuto"
        telegram_send(f"{emoji} <b>{hostname}</b>: Kamera přepnuta: {label}")
    if need_rain and last_rain is not None:
        # last_rain None = první běh po deployi (baseline) -> netelegramovat "sucho"
        emoji = "🌧" if rain_active else "🌤"
        label = (f"déšť — citlivost pohybu snížena ({MOTION_SENS_RAIN})" if rain_active
                 else f"sucho — citlivost pohybu zpět na {MOTION_SENS_NORMAL}")
        telegram_send(f"{emoji} <b>{hostname}</b>: {label}")


if __name__ == "__main__":
    main()
