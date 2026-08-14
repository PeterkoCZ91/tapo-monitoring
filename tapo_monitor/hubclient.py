"""Hub client for battery cameras that record to a Tapo hub.

A battery camera with no usable SD keeps no event index of its own — its recordings, and
therefore its detections, live on the hub it is bound to, and it sleeps between events.
So the hub is the event source. Newer hub firmware does *not* expose those cameras through
the child-device family (that one covers sub-GHz sensors and doorbells): they come from a
paired-general-device list, and their recordings are addressed by device id + MAC rather
than by an NVR-style channel. See docs/superpowers/specs for the design and the evidence.

Two operational facts shape this module:

* **The handshake is what is rate-limited, not the queries.** A fresh session is accepted
  only sporadically, while several queries inside an established session are reliable. So
  one session is opened and *held*, and every failure arms an exponential backoff instead
  of an immediate retry. The phone app competes for the hub's session slot, so eviction is
  normal — it is logged, never alerted on.
* **Requests must be wrapped one method at a time.** A bare single-method call hits a
  framing bug on this firmware, and an unknown method sent with empty params has been
  observed to take the whole session down, so params are always namespace-shaped.

Everything above :class:`HubClient` is pure and tested; the real transport at the bottom is
a thin I/O wrapper kept deliberately untested, as elsewhere in this package.
"""

from __future__ import annotations

import json
import logging
import time as _time
import uuid

log = logging.getLogger(__name__)

# Ask the hub which cameras are bound to it for recording.
DEVICE_LIST_PARAMS = {"general_camera_manage": {"paired_general_device_list": {}}}

DEVICE_LIST_METHOD = "getGeneralDeviceList"
DAY_SEARCH_METHOD = "searchDateWithVideo"
CLIP_SEARCH_METHOD = "searchVideoWithUTC"

# A hub answers a refused method rather than dropping the connection, so these are not
# session failures: -40106 unknown method, -40101 bad params, -50021 unsupported model.
SESSION_QUERY_TIMEOUT = 30


def wrap(method, params):
    """Build the ``multipleRequest`` envelope carrying exactly one method. Pure."""
    return {"method": "multipleRequest",
            "params": {"requests": [{"method": method, "params": params}]}}


def unwrap(raw):
    """Return ``(error_code, result)`` from a hub response. Pure.

    A malformed or empty response yields ``(None, {})`` — never ``(0, …)``, so a hub that
    answers something unexpected is not mistaken for a successful call.
    """
    try:
        response = raw["result"]["responses"][0]
    except (TypeError, KeyError, IndexError):
        return None, {}
    result = response.get("result")
    return response.get("error_code"), result if isinstance(result, dict) else {}


def day_search_params(device_id, mac, start_date, end_date):
    """Params for the day index of one camera. Dates are local ``YYYYMMDD``. Pure."""
    return {"playback": {"search_year_utility": {
        "channel": [0],
        "child_device_id": device_id,
        "child_device_mac": mac,
        "start_date": start_date,
        "end_date": end_date,
    }}}


def clip_search_params(device_id, mac, start_time, end_time, player_id, end_index=999):
    """Params for the clip index of one camera over an epoch window. Pure.

    The window is sent as whole seconds: measured against a real hub, a window carrying
    ``time.time()`` floats came back empty while the identical window as integers returned
    the clips. Start floors and end ceils, so the rounding can only widen the window.
    """
    return {"playback": {"search_video_with_utc": {
        "channel": 0,
        "child_device_id": device_id,
        "child_device_mac": mac,
        "start_time": int(start_time // 1),
        "end_time": -int(-end_time // 1),
        "start_index": 0,
        "end_index": end_index,
        "player_id": player_id,
    }}}


def new_player_id():
    """A fresh playback client id in the form the hub expects (upper hex, no dashes)."""
    return uuid.uuid4().hex.upper()


def _indexed_values(entries):
    """Yield the inner dicts of the hub's ``{"<name>_<n>": {...}}`` wrappers. Pure."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for value in entry.values():
            if isinstance(value, dict):
                yield value


def parse_cameras(result):
    """Return the cameras bound to the hub as normalized dicts. Pure.

    Non-camera children and malformed payloads are dropped rather than raised on: this
    runs on every startup and must never be the reason a daemon refuses to boot.
    """
    if not isinstance(result, dict):
        return []
    devices = (result.get("general_camera_manage") or {}).get("paired_general_device_list")
    if not isinstance(devices, list):
        return []
    cameras = []
    for dev in devices:
        if not isinstance(dev, dict) or dev.get("category") != "camera":
            continue
        if not dev.get("device_id") or not dev.get("mac"):
            continue
        cameras.append({
            "alias": dev.get("alias") or "",
            "device_id": dev["device_id"],
            "mac": dev["mac"],
            "model": dev.get("device_model") or "",
            "hub_storage": bool(dev.get("hub_storage_enabled")),
            "record_24h": bool(dev.get("plan_24h_record")),
        })
    return cameras


def parse_days(result):
    """Return the ``YYYYMMDD`` days that hold footage for one camera. Pure."""
    if not isinstance(result, dict):
        return []
    entries = (result.get("playback") or {}).get("search_results")
    return [str(v["date"]) for v in _indexed_values(entries) if v.get("date")]


def parse_clips(result):
    """Return the camera's clips as ``{start_time, end_time, video_type}``, oldest first.

    Pure. Entries without a usable start time are skipped — a clip we cannot place in time
    is not something to alert on.
    """
    if not isinstance(result, dict):
        return []
    entries = (result.get("playback") or {}).get("search_video_results")
    clips = []
    for value in _indexed_values(entries):
        try:
            start = float(value["startTime"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            end = float(value.get("endTime"))
        except (TypeError, ValueError):
            end = None
        clips.append({"start_time": start, "end_time": end,
                      "video_type": value.get("video_type")})
    return sorted(clips, key=lambda c: c["start_time"])


def day_string(timestamp, localtime=_time.localtime):
    """Format an epoch as the local ``YYYYMMDD`` the day index expects. Pure-ish."""
    return _time.strftime("%Y%m%d", localtime(timestamp))


def _normalize_mac(mac):
    return "".join(ch for ch in str(mac or "") if ch.isalnum()).upper()


def match_camera(cameras, name=None, mac=None, device_id=None):
    """Pick the hub-side camera for one configured camera, or None. Pure.

    Tried in order: a configured MAC (separators and case ignored), a configured device id,
    an alias equal to the camera's name, and finally — only when the hub has exactly one
    camera bound — that one. With several cameras and no match it returns None rather than
    guessing: alerting from the wrong camera is worse than not alerting.
    """
    if mac:
        wanted = _normalize_mac(mac)
        for cam in cameras:
            if _normalize_mac(cam["mac"]) == wanted:
                return cam
    if device_id:
        for cam in cameras:
            if cam["device_id"] == device_id:
                return cam
    if name:
        wanted = str(name).strip().lower()
        for cam in cameras:
            if cam["alias"].strip().lower() == wanted:
                return cam
    return cameras[0] if len(cameras) == 1 else None


def _backoff(fails, base, cap):
    """Exponential backoff seconds for ``fails`` consecutive failures. Pure."""
    if fails < 1:
        return 0
    return min(base * (2 ** (fails - 1)), cap)


class HubClient:
    """One held session to the hub, with backoff and no alerting of its own.

    ``connect()`` is injected and must return a session object exposing
    ``send(method, params) -> raw response`` and, optionally, ``close()``. Any exception
    from either is treated as "the session is gone": it is dropped and a backoff armed.
    Query methods return empty results while the session is down, so callers need no
    special case for a hub that is briefly unavailable.
    """

    def __init__(self, connect, *, backoff_base=60, backoff_cap=300, player_id=None):
        self._connect = connect
        self._session = None
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self.player_id = player_id or new_player_id()
        self.fails = 0
        self.retry_at = 0

    @property
    def connected(self):
        return self._session is not None

    def ensure_session(self, now):
        """Open the session if needed. Returns whether one is available."""
        if self._session is not None:
            return True
        if now < self.retry_at:
            return False
        try:
            self._session = self._connect()
        except Exception as exc:  # noqa: BLE001 - a refused handshake is routine here
            self._fail(now, "connect", exc)
            return False
        log.info("hub session established")
        return True

    def _fail(self, now, what, exc):
        self._drop()
        self.fails += 1
        wait = _backoff(self.fails, self._backoff_base, self._backoff_cap)
        self.retry_at = now + wait
        log.info("hub %s failed (#%d): %s; retrying in %ds",
                 what, self.fails, type(exc).__name__, wait)

    def _drop(self):
        session = self._session
        self._session = None
        if session is None:
            return
        close = getattr(session, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - closing a dead session may itself fail
            log.debug("closing hub session failed", exc_info=True)

    def query(self, method, params, now):
        """Run one method inside the held session. Returns ``(error_code, result)``."""
        if not self.ensure_session(now):
            return None, {}
        try:
            raw = self._session.send(method, params)
        except Exception as exc:  # noqa: BLE001 - includes eviction by the phone app
            self._fail(now, f"query {method}", exc)
            return None, {}
        self.fails = 0
        self.retry_at = 0
        code, result = unwrap(raw)
        if code not in (0, None):
            # The hub answered and the session is fine; the method or params were refused.
            log.debug("hub %s returned error_code %s", method, code)
        return code, result

    def close(self):
        """Release the session (e.g. on shutdown)."""
        self._drop()

    def list_cameras(self, now):
        """Cameras currently bound to the hub for recording; empty when unavailable."""
        _, result = self.query(DEVICE_LIST_METHOD, DEVICE_LIST_PARAMS, now)
        return parse_cameras(result)

    def search_days(self, device_id, mac, start_date, end_date, now):
        """Local ``YYYYMMDD`` days holding footage for one camera."""
        _, result = self.query(DAY_SEARCH_METHOD,
                               day_search_params(device_id, mac, start_date, end_date), now)
        return parse_days(result)

    def search_clips(self, device_id, mac, since, until, now):
        """Clips of one camera started after ``since`` and no later than ``until``.

        The hub's window is inclusive, so the clip sitting exactly on the cursor is
        filtered out here — otherwise every poll would re-deliver the last alert.
        """
        _, result = self.query(
            CLIP_SEARCH_METHOD,
            clip_search_params(device_id, mac, since, until, self.player_id), now)
        return [c for c in parse_clips(result) if c["start_time"] > since]


def kasa_session(host, email, password, timeout=SESSION_QUERY_TIMEOUT):  # pragma: no cover
    """Open a real SslAes session to the hub. Thin I/O wrapper around python-kasa.

    The kasa transport is async while the daemon is not, so the session owns a private
    event loop on its own thread: coroutines are submitted to it and waited on. That keeps
    one long-lived connection alive across daemon ticks without ever nesting event loops —
    the trap that makes the camera-side media APIs unusable from here.
    """
    import asyncio
    import threading

    class _Session:
        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever,
                                            name="hub-session", daemon=True)
            self._thread.start()
            self._device = self._await(self._open())

        def _await(self, coro):
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

        async def _open(self):
            from kasa import Discover
            from kasa.credentials import Credentials
            return await Discover.discover_single(host, credentials=Credentials(email, password))

        async def _send(self, method, params):
            transport = self._device.protocol._transport
            return await transport.send(json.dumps(wrap(method, params)))

        def send(self, method, params):
            return self._await(self._send(method, params))

        def close(self):
            try:
                self._await(self._device.protocol.close())
            except Exception:  # noqa: BLE001 - best effort; the loop still has to stop
                log.debug("hub protocol close failed", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)

    return _Session()
