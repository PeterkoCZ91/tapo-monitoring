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
import math
import os
import stat
import sys
import tempfile
import urllib.parse
import urllib.request

_warned_env: set[str] = set()


def _warn_env(name, raw, default):
    # Once per variable: import-time config problems must not spam or crash.
    if name not in _warned_env:
        _warned_env.add(name)
        print(f"[weather] invalid {name}={raw!r}, using {default!r}", file=sys.stderr, flush=True)


def _env_number(name, default, cast):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = cast(raw.strip())
        if isinstance(val, float) and not math.isfinite(val):
            raise ValueError(raw)
        return val
    except (ValueError, OverflowError):
        _warn_env(name, raw, default)
        return default


def _env_coord(primary, fallback):
    name = primary if os.getenv(primary) else fallback
    return _env_number(name, None, float)


LAT = _env_coord("RAIN_LAT", "NIGHT_LAT")
LON = _env_coord("RAIN_LON", "NIGHT_LON")
TZ = os.getenv("RAIN_TZ") or os.getenv("NIGHT_TZ") or "UTC"
PRECIP_THRESHOLD = _env_number("RAIN_PRECIP_THRESHOLD", 0.1, float)
CLEAR_DELAY = _env_number("RAIN_CLEAR_DELAY", 1800, int)
POLL_INTERVAL = _env_number("RAIN_POLL_INTERVAL", 900, int)
# After a failed fetch, do not retry for this long (each attempt blocks up to 5 s).
FAIL_BACKOFF = 120
# A cached "raining" older than this is not trusted when the API is down.
STALE_MAX = 6 * 3600
_fail_until: dict[str, float] = {}
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
        ts_str, raining_str = _read_text(path).strip().split(",", 1)
        ts = float(ts_str)
        if not math.isfinite(ts) or raining_str not in ("0", "1"):
            return None, None
        return ts, raining_str == "1"
    except (OSError, ValueError):
        return None, None


def _read_text(path):
    """Read a small regular file without following a symlink or blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.read(fd, 256).decode("ascii")
    finally:
        os.close(fd)


def _atomic_write(path, text):
    """Write via a private temp file + rename; a planted symlink at ``path`` is replaced,
    never followed."""
    directory = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tapo_rain_", dir=directory)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def write_cache(now, raining, path=CACHE_PATH):
    return _atomic_write(path, f"{now},{'1' if raining else '0'}")


# ── last-rain (hysteresis) ──────────────────────────────────────────────────

def read_last_rain(path=LAST_RAIN_PATH):
    try:
        val = float(_read_text(path).strip())
        return val if math.isfinite(val) else None
    except (OSError, ValueError):
        return None


def touch_rain(now, path=LAST_RAIN_PATH):
    return _atomic_write(path, str(now))


def is_raining_now(now, threshold=PRECIP_THRESHOLD, poll_interval=POLL_INTERVAL,
                   cache_path=CACHE_PATH, lat=LAT, lon=LON, tz=TZ):
    """Is it raining now? Hits open-meteo at most once per poll_interval (else cache).
    On API error falls back to the last cached value (or False)."""
    cache_ts, cached_raining = read_cache(cache_path)
    if is_cache_fresh(cache_ts, now, poll_interval):
        return cached_raining
    # Stale cache: only trusted for STALE_MAX seconds, then "dry" (never rain forever).
    if cached_raining and (cache_ts is None or now - cache_ts >= STALE_MAX):
        cached_raining = False
    fallback = cached_raining if cached_raining is not None else False
    if lat is None or lon is None:
        # No coordinates configured -> skip weather gating (safe default).
        return fallback
    if now < _fail_until.get(cache_path, float("-inf")):
        return fallback
    try:
        raining = parse_precip(_fetch_weather(lat, lon, tz), threshold)
    except Exception:
        _fail_until[cache_path] = now + FAIL_BACKOFF
        return fallback
    _fail_until.pop(cache_path, None)
    write_cache(now, raining, cache_path)
    return raining
