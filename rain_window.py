"""Backward-compatible shim. The implementation now lives in tapo_monitor.weather.

Kept so the standalone scripts keep working with `import rain_window` during the
migration to the package.
"""

from tapo_monitor.weather import (  # noqa: F401
    CLEAR_DELAY,
    POLL_INTERVAL,
    PRECIP_THRESHOLD,
    is_cache_fresh,
    is_rain_active,
    is_raining_now,
    parse_precip,
    read_cache,
    read_last_rain,
    touch_rain,
    write_cache,
)
