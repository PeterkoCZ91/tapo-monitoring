"""Astral-based day/night scheduling.

``is_night()`` is the single source of truth for "is it night right now", shared by
tracking, monitoring and the daytime pipeline so their hand-offs line up exactly.

Night runs from ``sunset - NIGHT_SUNSET_OFFSET_MIN`` to ``sunrise + NIGHT_SUNRISE_OFFSET_MIN``
(default 30 min each, to cover dusk and dawn).

Configuration via environment (required for astral mode):
  NIGHT_LAT, NIGHT_LON, NIGHT_TZ    — coordinates and IANA timezone (e.g. "Europe/Prague")
  NIGHT_SUNSET_OFFSET_MIN           — minutes before sunset to start night (default 30)
  NIGHT_SUNRISE_OFFSET_MIN          — minutes after sunrise to end night (default 30)
  NIGHT_FORCE_HHMM=1                — ignore astral, use the NIGHT_START/NIGHT_END HH:MM window

Fallback: if NIGHT_LAT/LON/TZ are unset, astral is not installed, or anything fails,
the fixed HH:MM window from NIGHT_START/NIGHT_END is used. No coordinates are hardcoded.
"""

import os
from datetime import datetime, timedelta

DEFAULT_SUNSET_OFFSET = 30
DEFAULT_SUNRISE_OFFSET = 30


def _parse_hhmm(val, default_hour):
    val = str(val).strip()
    if ":" in val:
        h, m = val.split(":", 1)
        return int(h) * 60 + int(m)
    if val == "":
        return default_hour * 60
    return int(val) * 60


def _is_night_hhmm(now=None):
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start = _parse_hhmm(os.getenv("NIGHT_START", "22"), 22)
    end = _parse_hhmm(os.getenv("NIGHT_END", "6"), 6)
    return cur >= start or cur < end


def _is_night_astral(now=None, location=None):
    from zoneinfo import ZoneInfo

    from astral import LocationInfo
    from astral.sun import sun

    lat_env = getattr(location, "lat", None) if location is not None else None
    lon_env = getattr(location, "lon", None) if location is not None else None
    tz_name = getattr(location, "tz", None) if location is not None else None
    lat_env = lat_env if lat_env is not None else os.getenv("NIGHT_LAT")
    lon_env = lon_env if lon_env is not None else os.getenv("NIGHT_LON")
    tz_name = tz_name or os.getenv("NIGHT_TZ")
    if not (lat_env and lon_env and tz_name):
        raise RuntimeError("NIGHT_LAT/NIGHT_LON/NIGHT_TZ not set")
    lat = float(lat_env)
    lon = float(lon_env)
    sunset_off = int(os.getenv("NIGHT_SUNSET_OFFSET_MIN", DEFAULT_SUNSET_OFFSET))
    sunrise_off = int(os.getenv("NIGHT_SUNRISE_OFFSET_MIN", DEFAULT_SUNRISE_OFFSET))

    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    loc = LocationInfo("custom", "custom", tz_name, lat, lon)
    s = sun(loc.observer, date=now.date(), tzinfo=tz)
    night_end = s["sunrise"] + timedelta(minutes=sunrise_off)
    night_start = s["sunset"] - timedelta(minutes=sunset_off)

    if now < night_end:
        return True
    if now < night_start:
        return False
    return True


def is_night(now=None, location=None):
    if os.getenv("NIGHT_FORCE_HHMM", "").strip() in ("1", "true", "yes"):
        return _is_night_hhmm(now)
    try:
        return _is_night_astral(now, location)
    except Exception as e:
        print(f"[scheduling] astral failed ({e}), falling back to HH:MM", flush=True)
        return _is_night_hhmm(now)


def describe_window(now=None):
    """Return (mode, info_string) for logging."""
    try:
        from zoneinfo import ZoneInfo

        from astral import LocationInfo
        from astral.sun import sun

        lat_env = os.getenv("NIGHT_LAT")
        lon_env = os.getenv("NIGHT_LON")
        tz_name = os.getenv("NIGHT_TZ")
        if not (lat_env and lon_env and tz_name):
            raise RuntimeError("NIGHT_LAT/NIGHT_LON/NIGHT_TZ not set")
        lat = float(lat_env)
        lon = float(lon_env)
        sunset_off = int(os.getenv("NIGHT_SUNSET_OFFSET_MIN", DEFAULT_SUNSET_OFFSET))
        sunrise_off = int(os.getenv("NIGHT_SUNRISE_OFFSET_MIN", DEFAULT_SUNRISE_OFFSET))
        tz = ZoneInfo(tz_name)
        now = now or datetime.now(tz)
        loc = LocationInfo("custom", "custom", tz_name, lat, lon)
        s = sun(loc.observer, date=now.date(), tzinfo=tz)
        ns = (s["sunset"] - timedelta(minutes=sunset_off)).strftime("%H:%M")
        ne = (s["sunrise"] + timedelta(minutes=sunrise_off)).strftime("%H:%M")
        return ("astral", f"night={ns}-{ne} (sunset={s['sunset'].strftime('%H:%M')}, sunrise={s['sunrise'].strftime('%H:%M')})")
    except Exception:
        return ("hhmm", f"NIGHT_START={os.getenv('NIGHT_START', '22')} NIGHT_END={os.getenv('NIGHT_END', '6')}")
