"""Card-less event source: ``getLastAlarmInfo`` as a poor man's ``getEvents``.

Without an SD card ``getEvents`` fails with -71114. The camera still reports its last
alarm (type + time) via ``getLastAlarmInfo`` with ``{"system": {"name":
["last_alarm_info"]}}`` (see docs/tapo-local-api.md section 6). This module turns such a
response into getEvents-shaped dicts. The parsing is pure; :class:`LastAlarmClient` wires it
into the daemon pass as the opt-in ``lastalarm`` detection source.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

log = logging.getLogger(__name__)

ALARM_PARAMS = {"system": {"name": ["last_alarm_info"]}}
# An alarm older than this is "the last one that ever happened", not news.
DEFAULT_MAX_AGE_S = 600.0
# Alarm times further ahead than this are clock garbage.
_MAX_FUTURE_S = 120.0
# Plausible epoch range (2001..2100); rejects 0, "", ms timestamps and junk.
_MIN_TS, _MAX_TS = 1_000_000_000, 4_102_444_800


def _find_info(response: Any) -> dict | None:
    """Locate the info dict in the known response shapes (or None)."""
    if not isinstance(response, dict):
        return None
    system = response.get("system")
    if isinstance(system, dict) and isinstance(system.get("last_alarm_info"), dict):
        return system["last_alarm_info"]
    if isinstance(response.get("last_alarm_info"), dict):
        return response["last_alarm_info"]
    if "last_alarm_time" in response or "last_alarm_type" in response:
        return response
    # multipleRequest style: {"result": {...}} / {"responses": [{"result": ...}]}
    result = response.get("result")
    if isinstance(result, dict):
        return _find_info(result)
    return None


def _to_ts(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    return ts if _MIN_TS <= ts <= _MAX_TS else None


def parse_last_alarm(response: Any, now: float | None = None,
                     max_age_s: float = DEFAULT_MAX_AGE_S) -> list[dict]:
    """Return ``[event]`` for a fresh, recognised last alarm, else ``[]``.

    The event has ``start_time`` (int epoch), ``end_time`` None (unknown),
    ``event_type`` / ``type`` (the camera's coarse type, lowercased; classifies via
    ``detection.classify_getevent``) and ``source`` = ``"getLastAlarmInfo"``. No
    ``events_1`` bitmask exists on this path.
    """
    info = _find_info(response)
    if info is None:
        return []
    ts = _to_ts(info.get("last_alarm_time"))
    if ts is None:
        return []
    now = time.time() if now is None else now
    if ts > now + _MAX_FUTURE_S or now - ts > max_age_s:
        return []
    atype = info.get("last_alarm_type")
    if not isinstance(atype, str) or not atype.strip():
        return []
    atype = atype.strip().lower()
    return [{
        "start_time": ts,
        "end_time": None,
        "event_type": atype,
        "type": atype,
        "source": "getLastAlarmInfo",
    }]


class SeenAlarms:
    """Small bounded set of (start_time, type) keys already reported."""

    def __init__(self, maxlen: int = 32) -> None:
        self._keys: OrderedDict[tuple, None] = OrderedDict()
        self._maxlen = max(1, maxlen)

    def add(self, event: dict) -> bool:
        """Record the event; True when it is new, False when seen before."""
        key = (event.get("start_time"), event.get("event_type"))
        if key in self._keys:
            self._keys.move_to_end(key)
            return False
        self._keys[key] = None
        while len(self._keys) > self._maxlen:
            self._keys.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._keys)


def dedupe(events: list[dict], seen: SeenAlarms) -> list[dict]:
    """Drop events already in ``seen`` (and record the new ones)."""
    return [e for e in events if seen.add(e)]


def events_without_sd(client: Any, last_seen: float, now: float | None = None,
                      max_age_s: float = DEFAULT_MAX_AGE_S,
                      reraise: Any = None, on_error: Any = None) -> list[dict]:
    """Poll ``client.getLastAlarmInfo`` and return events newer than ``last_seen``.

    Never raises: any camera/client error yields ``[]``, except one for which the optional
    ``reraise(exc)`` predicate is true (the daemon passes the auth-failure test). A
    swallowed error is handed to the optional ``on_error(exc)`` so it can be logged.
    """
    try:
        call = getattr(client, "executeFunction", None)
        if callable(call):
            response = call("getLastAlarmInfo", ALARM_PARAMS)
        else:
            response = client.getLastAlarmInfo()
    except Exception as exc:  # noqa: BLE001 - a flaky camera must not break the poll loop
        if reraise is not None and reraise(exc):
            raise
        if on_error is not None:
            on_error(exc)
        return []
    events = parse_last_alarm(response, now=now, max_age_s=max_age_s)
    return [e for e in events if e["start_time"] > last_seen]


MIN_CALL_INTERVAL_S = 1.5   # the camera answers -40109 to calls closer together


def _is_auth_error(exc: BaseException) -> bool:
    from .camera import is_authentication_error
    return is_authentication_error(exc)


class LastAlarmClient:
    """Wraps a camera client so ``getEvents()`` is answered from ``getLastAlarmInfo``.

    One instance lives per camera (with one :class:`SeenAlarms`); the daemon rebinds
    ``cam`` / ``last_seen`` each pass. Calls closer than ``min_interval`` return ``[]``
    without touching the camera (nothing is lost: the alarm stays "last" on the camera).
    Authentication errors propagate so the daemon's observer path sees them; any other
    error is logged once per streak (with the count when the camera answers again).
    """

    def __init__(self, cam: Any, seen: SeenAlarms, last_seen: float = 0,
                 min_interval: float = MIN_CALL_INTERVAL_S,
                 clock: Any = time.monotonic, name: str = "") -> None:
        self.cam = cam
        self.name = name
        self.failures = 0
        self.seen = seen
        self.last_seen = last_seen
        self.min_interval = min_interval
        self._clock = clock
        self._last_call: float | None = None

    def getEvents(self) -> list[dict]:  # noqa: N802 - mirrors pytapo's name
        t = self._clock()
        if self._last_call is not None and t - self._last_call < self.min_interval:
            return []
        self._last_call = t
        failed: list[BaseException] = []
        events = events_without_sd(self.cam, self.last_seen, reraise=_is_auth_error,
                                   on_error=failed.append)
        if failed:
            self.failures += 1
            if self.failures == 1:
                log.warning("getLastAlarmInfo %s failed: %s: %s", self.name or "?",
                            type(failed[0]).__name__, failed[0])
            return []
        if self.failures:
            log.info("getLastAlarmInfo %s recovered after %d failure(s)",
                     self.name or "?", self.failures)
            self.failures = 0
        return dedupe(events, self.seen)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.cam, name)
