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
import sys
from datetime import datetime, timedelta

DEFAULT_SUNSET_OFFSET = 30
DEFAULT_SUNRISE_OFFSET = 30

# The daemon asks is_night() every poll (~4 s); an unset NIGHT_TZ would otherwise
# repeat the same fallback warning ~20 000 times a day and drown the journal.
_warned_fallback = None
_warned_env = set()


def _parse_hhmm(val, default_hour):
    val = str(val).strip()
    if val == "":
        return default_hour * 60
    if ":" in val:
        h, m = val.split(":", 1)
        h, m = int(h), int(m)
    else:
        h, m = int(val), 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"clock time out of range: {val!r}")
    return h * 60 + m


def _env_minutes(name, default_hour):
    """NIGHT_START/NIGHT_END as minutes; an invalid value falls back to the default (log once)."""
    raw = os.getenv(name, str(default_hour))
    try:
        return _parse_hhmm(raw, default_hour)
    except ValueError:
        if name not in _warned_env:
            _warned_env.add(name)
            print(f"[scheduling] invalid {name}={raw!r}, using {default_hour}:00",
                  file=sys.stderr, flush=True)
        return default_hour * 60


def _is_night_hhmm(now=None):
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start = _env_minutes("NIGHT_START", 22)
    end = _env_minutes("NIGHT_END", 6)
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
        global _warned_fallback
        reason = str(e)
        if reason != _warned_fallback:
            _warned_fallback = reason
            # stderr, not stdout: this is a diagnostic, and stdout carries the CLI's
            # machine-readable output — a warning mixed into it breaks any --json consumer.
            print(f"[scheduling] astral failed ({e}), falling back to HH:MM",
                  file=sys.stderr, flush=True)
        return _is_night_hhmm(now)


def parse_clock_window(spec):
    """Parse a "HH:MM-HH:MM" spec into a (start_minute, end_minute) pair.

    Raises ValueError on anything else. The pair may wrap past midnight
    (``end < start``, e.g. "22:00-05:00") — :func:`in_clock_window` handles that.
    """
    try:
        start_s, end_s = str(spec).split("-", 1)
        if not start_s.strip() or not end_s.strip():
            raise ValueError("empty clock time")
        start = _parse_hhmm(start_s, 0)
        end = _parse_hhmm(end_s, 0)
    except (ValueError, IndexError):
        raise ValueError(f"invalid clock window {spec!r}, expected 'HH:MM-HH:MM'") from None
    return start, end


def in_clock_window(window, now=None):
    """Whether ``now`` (a datetime, default real local time) falls in ``window``.

    ``window`` is a ``(start_minute, end_minute)`` pair from :func:`parse_clock_window`.
    A window that wraps past midnight (end <= start) is treated as spanning the gap,
    matching NIGHT_START/NIGHT_END's own wraparound in :func:`_is_night_hhmm`.
    """
    start, end = window
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


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
