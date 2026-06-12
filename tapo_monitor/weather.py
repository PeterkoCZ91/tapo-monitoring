"""Weather-based gating for auto-tracking cameras.

In the rain, an auto-tracking camera latches onto raindrops and IR reflections. This
module tells the daemon whether it's currently raining so it can lower motion
sensitivity or pause tracking.

Source: open-meteo (free, no key). To avoid hammering the API, the result is cached and
only refetched once per ``RAIN_POLL_INTERVAL`` (default 15 min) even if the caller runs
every minute. Hysteresis (``RAIN_CLEAR_DELAY``) keeps "rain mode" on for a while after
the rain stops — drops linger on the lens.

On API error the last cached value is used (or False). Coordinates come from env only
(RAIN_* falling back to NIGHT_*); with none set, gating is skipped (returns False).
"""

import json
import os
import urllib.parse
import urllib.request


def _env_coord(primary, fallback):
    val = os.getenv(primary) or os.getenv(fallback)
    return float(val) if val else None


LAT = _env_coord("RAIN_LAT", "NIGHT_LAT")
LON = _env_coord("RAIN_LON", "NIGHT_LON")
TZ = os.getenv("RAIN_TZ") or os.getenv("NIGHT_TZ") or "UTC"
PRECIP_THRESHOLD = float(os.getenv("RAIN_PRECIP_THRESHOLD", "0.1"))
CLEAR_DELAY = int(os.getenv("RAIN_CLEAR_DELAY", "1800"))
POLL_INTERVAL = int(os.getenv("RAIN_POLL_INTERVAL", "900"))
STATE_DIR = os.getenv("STATE_DIR", "/tmp")
CACHE_PATH = os.path.join(STATE_DIR, "tapo_rain_cache")
LAST_RAIN_PATH = os.path.join(STATE_DIR, "tapo_rain_last_seen")

_API = "https://api.open-meteo.com/v1/forecast"


def _fetch_weather(lat=LAT, lon=LON, tz=TZ, timeout=5):
    """Fetch current precipitation from open-meteo. Raises on error."""
    qs = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "precipitation,rain,showers",
        "timezone": tz,
    })
    req = urllib.request.Request(f"{_API}?{qs}", headers={"User-Agent": "tapo-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_precip(api_json, threshold=PRECIP_THRESHOLD):
    """Return True if the open-meteo response reports precipitation >= threshold (mm)."""
    current = api_json.get("current") if isinstance(api_json, dict) else None
    if not isinstance(current, dict):
        return False
    value = max(
        current.get("precipitation", 0) or 0,
        current.get("rain", 0) or 0,
        current.get("showers", 0) or 0,
    )
    return value >= threshold


def is_rain_active(now, last_seen, clear_delay=CLEAR_DELAY):
    """Hysteresis: rain mode stays on for clear_delay seconds after the last rain."""
    if last_seen is None:
        return False
    return (now - last_seen) < clear_delay


def is_cache_fresh(cache_ts, now, poll_interval=POLL_INTERVAL):
    """True if the cached value is younger than poll_interval (keeps API calls sparse)."""
    if cache_ts is None:
        return False
    return (now - cache_ts) < poll_interval


# ── cache (raining now) ─────────────────────────────────────────────────────

def read_cache(path=CACHE_PATH):
    """Return (timestamp, raining_bool) from the cache, or (None, None)."""
    try:
        with open(path) as f:
            ts_str, raining_str = f.read().strip().split(",", 1)
            return float(ts_str), raining_str == "1"
    except (OSError, ValueError):
        return None, None


def write_cache(now, raining, path=CACHE_PATH):
    try:
        with open(path, "w") as f:
            f.write(f"{now},{'1' if raining else '0'}")
        return True
    except OSError:
        return False


# ── last-rain (hysteresis) ──────────────────────────────────────────────────

def read_last_rain(path=LAST_RAIN_PATH):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def touch_rain(now, path=LAST_RAIN_PATH):
    try:
        with open(path, "w") as f:
            f.write(str(now))
        return True
    except OSError:
        return False


def is_raining_now(now, threshold=PRECIP_THRESHOLD, poll_interval=POLL_INTERVAL,
                   cache_path=CACHE_PATH, lat=LAT, lon=LON, tz=TZ):
    """Is it raining now? Hits open-meteo at most once per poll_interval (else cache).
    On API error falls back to the last cached value (or False)."""
    cache_ts, cached_raining = read_cache(cache_path)
    if is_cache_fresh(cache_ts, now, poll_interval):
        return cached_raining
    if lat is None or lon is None:
        # No coordinates configured -> skip weather gating (safe default).
        return cached_raining if cached_raining is not None else False
    try:
        raining = parse_precip(_fetch_weather(lat, lon, tz), threshold)
        write_cache(now, raining, cache_path)
        return raining
    except Exception:
        return cached_raining if cached_raining is not None else False
