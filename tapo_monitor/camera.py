"""Camera connection wrapper around pytapo.

pytapo is a thin API client; this adds the operational glue around it:

* **Cheap reachability probe** — ping the device before creating an authenticated session,
  so a disconnected camera is distinct from an API/authentication failure.
* **Lockout-aware connect** — the C560WS locks out a source IP for ~30 min after failed
  logins, and the first login after a reconnect often fails with "Invalid authentication
  data" before a retry succeeds. ``connect`` retries a few times with a delay so callers
  don't rediscover this; ``sleep`` is injectable for tests.
* **getEvents helpers** — pure filtering of new events since a watermark.

The pytapo import is lazy (inside ``tapo_factory``) so the rest of the package — and the
tests — import without the dependency present.
"""

import logging
import subprocess
import time as _time

log = logging.getLogger(__name__)

PING_ECHOES = 3
PING_INTERVAL = "0.3"


def ping_reachable(host, timeout=1, run=subprocess.run):
    """True when the camera answers any of a few ICMP echo requests.

    More than one echo on purpose: these cameras drop 2-4 % of packets on a radio whose
    gateway drops none, and a caller that treats one failure as "down" both cries wolf and
    parks the camera until its next pass. ping exits 0 when any echo is answered, so the
    retry costs nothing on a healthy camera and a genuinely offline one still fails them
    all. The command uses an argv list (never a shell), suppresses output and has both
    ping's own deadline and a subprocess deadline. It performs no camera login.
    """
    deadline = max(1, int(timeout))
    try:
        result = run(
            ["ping", "-n", "-c", str(PING_ECHOES), "-i", PING_INTERVAL,
             "-W", str(deadline), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=deadline + PING_ECHOES,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def tapo_factory(host, user, password, cloud_password=None):
    """Return a zero-arg callable that builds a pytapo client (import kept lazy)."""
    def make():
        from pytapo import Tapo
        return Tapo(host, user, password, cloud_password or password)
    return make


def connect(factory, retries=3, sleep=_time.sleep, delay=5):
    """Try ``factory()`` up to ``retries`` times. Returns (client, last_error).

    On success returns (client, None); on exhaustion (None, last_error).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return factory(), None
        except Exception as e:  # noqa: BLE001 - surface the last error to the caller
            last_err = e
            if attempt < retries:
                sleep(delay)
    return None, last_err


def reboot(client):
    """Request a camera reboot through its authenticated API client.

    Returns ``True`` only when the API call was accepted; never raises into the
    monitor loop.
    """
    try:
        client.reboot()
        return True
    except Exception:  # noqa: BLE001 - health recovery must be best effort
        return False


def whitelamp_on(client):
    """Whether the camera's white lamp (full-color night-vision light) is on now.

    Returns None when the call fails — no whitelamp hardware, older firmware, or a
    transient error — so a caller treats "unknown" as "say nothing" rather than
    guessing. Never raises: a caption enrichment must not cost an alert.
    """
    try:
        status = client.getWhitelampStatus()
    except Exception:  # noqa: BLE001 - best-effort caption enrichment only
        return None
    if not isinstance(status, dict):
        return None
    return status.get("status") in (1, "1", True)


# Per camera: (lit_from, lit_until) spans in which the white lamp was seen lit. The
# caption's 🔦 used to be the lamp state at send time, but an SD/sampler alert goes out
# minutes after the event, long after a 60 s lamp has gone dark again (2026-09-23 03:04:
# lit on time, alert at 03:06:49 without the icon). The spans let it mean "lit during
# the event" instead.
_lamp_spans: dict = {}
LAMP_EVENT_SPAN = 120        # seconds after an event's start that still count as "during"
LAMP_DEFAULT_FORCE_TIME = 300  # firmware's own on-time when whitelamp_force_time is unset
_LAMP_KEEP = 3600


def note_whitelamp(camera, lit_from, lit_until):
    """Record that ``camera``'s lamp was lit over ``[lit_from, lit_until]``."""
    spans = _lamp_spans.setdefault(camera, [])
    spans[:] = [sp for sp in spans if sp[1] >= lit_until - _LAMP_KEEP]
    spans.append((float(lit_from), float(lit_until)))


def whitelamp_seen(client, camera, event_start, *, now=None, force_time=None,
                   span=LAMP_EVENT_SPAN):
    """Whether the lamp is lit now or was seen lit during the event. Never raises.

    A lamp read as lit is recorded first: ``rest_time`` says when it goes dark, and with
    the configured on-time also roughly when it came on. True when it is lit now or a
    recorded span overlaps ``[event_start, event_start + span]``; otherwise the current
    read (False, or None when it failed), so an unknown state still says nothing.
    """
    now = _time.time() if now is None else now
    on = None
    rest = None
    try:
        status = client.getWhitelampStatus()
        if isinstance(status, dict):
            on = status.get("status") in (1, "1", True)
            rest = int(status.get("rest_time") or 0)
    except Exception:  # noqa: BLE001 - best-effort caption enrichment only
        on = None
    if on:
        total = force_time or LAMP_DEFAULT_FORCE_TIME
        note_whitelamp(camera, now - max(total - (rest or 0), 0), now + (rest or 0))
        return True
    try:
        start = float(event_start)
    except (TypeError, ValueError):
        return on
    for lit_from, lit_until in _lamp_spans.get(camera, ()):
        if lit_from <= start + span and lit_until >= start:
            return True
    return on


def trigger_whitelamp(client, force_time=None):
    """Turn on the camera's white lamp if supported and not already on. Never raises.

    If force_time is given, set the automatic pulse duration (e.g. 30s instead of
    firmware default 300s) before triggering.
    """
    def lamp_on():
        status = client.getWhitelampStatus()
        if not isinstance(status, dict):
            return None
        return status.get("status") in (1, "1", True)

    try:
        if force_time is not None and hasattr(client, "setWhitelampConfig"):
            try:
                client.setWhitelampConfig(forceTime=int(force_time))
            except Exception:
                pass
        try:
            state = lamp_on()
        except Exception:  # noqa: BLE001 - transient (-40214 right after a switch); retry once
            _time.sleep(1)
            state = lamp_on()
        if state:
            return True  # already on
        if state is None:
            # reverseWhitelampStatus is a toggle: never fire it blind on an unreadable state
            log.warning("failed to trigger whitelamp: status unreadable")
            return False
        if hasattr(client, "reverseWhitelampStatus"):
            try:
                client.reverseWhitelampStatus()
            except Exception as exc:  # noqa: BLE001 - the toggle may still have landed
                _time.sleep(1)
                if lamp_on():
                    return True
                log.warning("failed to trigger whitelamp: %s", exc)
                return False
            return True
        if hasattr(client, "setForceWhitelampState"):
            client.setForceWhitelampState(True)
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("failed to trigger whitelamp: %s", exc)
        return False
    return False


def set_lens_distortion_correction(client, enabled: bool) -> bool:
    """Safely configure Lens Distortion Correction (LDC). Never raises."""
    try:
        if hasattr(client, "setLensDistortionCorrection"):
            client.setLensDistortionCorrection(bool(enabled))
            return True
    except Exception as exc:
        log.warning("failed to set LDC (%s): %s", enabled, exc)
    return False


def set_tamper_detection(client, enabled: bool, sensitivity: str = "normal") -> bool:
    """Safely configure tamper detection. Never raises."""
    try:
        if hasattr(client, "setTamperDetection"):
            client.setTamperDetection(bool(enabled), sensitivity)
            return True
    except Exception as exc:
        log.warning("failed to set tamper detection: %s", exc)
    return False


def set_osd_safe(client, label: str = "", date_enabled: bool = True, week_enabled: bool = False) -> bool:
    """Safely set OSD using executeFunction to avoid raw performRequest IP lockout."""
    try:
        payload = {
            "OSD": {
                "date": {"enabled": "on" if date_enabled else "off", "x_coor": 0, "y_coor": 0},
                "week": {"enabled": "on" if week_enabled else "off", "x_coor": 6000, "y_coor": 500},
            }
        }
        if label:
            payload["OSD"]["label_info_1"] = {
                "enabled": "on",
                "text": label[:16],
                "x_coor": 0,
                "y_coor": 500,
            }
        client.executeFunction("setOsd", payload)
        return True
    except Exception as exc:
        log.warning("failed to set OSD safely: %s", exc)
        return False


def new_events(events, last_seen):
    """Events whose start_time is strictly newer than the watermark, oldest first."""
    fresh = [e for e in (events or []) if e.get("start_time", 0) > last_seen]
    return sorted(fresh, key=lambda e: e.get("start_time", 0))


def newest_start(events):
    """Largest start_time across events, or None when empty."""
    starts = [e.get("start_time", 0) for e in (events or [])]
    return max(starts) if starts else None
