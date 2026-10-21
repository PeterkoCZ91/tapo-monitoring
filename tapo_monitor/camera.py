"""Camera connection wrapper around pytapo.

pytapo is a thin API client; this adds the operational glue around it:

* **Cheap reachability probe** — ping the device before creating an authenticated session,
  so a disconnected camera is distinct from an API/authentication failure.
* **Lockout-aware API retry** — ``connect`` retries client construction, but not after an
  authentication error. pytapo's constructor already logs in (and tries both passcodes),
  so a wrong password fails there; the daemon backs off for the camera-reported
  suspension time. Later authentication errors are observed per request, the client is
  retired and the daemon backs off before trying again.
* **getEvents helpers** — pure filtering of new events since a watermark.

The pytapo import is lazy (inside ``tapo_factory``) so the rest of the package — and the
tests — import without the dependency present.
"""

import logging
import re
import socket
import subprocess
import threading
import time as _time

from . import apiguard

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


API_PORT = 443


def candidate_hosts(cfg):
    """Every address the camera may live at: the configured list, or just ``cfg.host``."""
    return tuple(getattr(cfg, "hosts", ()) or (cfg.host,))


def active_host(cfg):
    """The one address to use for this camera right now.

    The single read point for the camera's address (pytapo, RTSP, ONVIF, ping, the SD
    download, the scene probe): ``cfg.host`` is the configured primary, and a camera whose
    ``host`` is a list may have failed over to another candidate at runtime.
    """
    return getattr(cfg, "active_host", None) or cfg.host


def host_index(cfg):
    """Position of the active address among the candidates (0 = the first/configured)."""
    try:
        return candidate_hosts(cfg).index(active_host(cfg))
    except (ValueError, AttributeError):
        return 0


def set_active_host(cfg, host):
    """Make ``host`` (one of the candidates) the address in use. Raises ValueError else."""
    if host not in candidate_hosts(cfg):
        raise ValueError(f"{host!r} is not a candidate host of camera {cfg.name!r}")
    cfg.active_host = host


# Return to the primary address only after it answered this many control passes in a row,
# and not sooner than this many seconds after the last switch (no flapping).
PRIMARY_RETURN_PASSES = 3
PRIMARY_RETURN_MIN_SECONDS = 300


def should_return_to_primary(cfg, now, switched_at, ok_streak, rejected_until=0):
    """True when a failed-over camera should go back to its first configured address.

    Pure: ``ok_streak`` is the number of consecutive successful TCP probes of the primary,
    ``switched_at`` when the active address last changed, ``rejected_until`` the end of a
    "that was another device" rejection of the primary.
    """
    if host_index(cfg) == 0 or now is None:
        return False
    if now < rejected_until:
        return False
    if now - (switched_at or 0) < PRIMARY_RETURN_MIN_SECONDS:
        return False
    return ok_streak >= PRIMARY_RETURN_PASSES


def tcp_reachable(host, port=API_PORT, timeout=2, connect=socket.create_connection):
    """True when a TCP connection to ``host:port`` opens. No login, no payload."""
    try:
        sock = connect((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def find_alternate_host(cfg, connect=socket.create_connection, skip=()):
    """The first candidate other than the active one that accepts a TCP connection on the
    camera API port, or None. ``skip`` lists candidates already rejected. Whether it is the
    same camera is the caller's question (see :func:`device_identity`)."""
    current = active_host(cfg)
    for host in candidate_hosts(cfg):
        if host == current or host in skip:
            continue
        if tcp_reachable(host, connect=connect):
            return host
    return None


def device_identity(client):
    """The camera's MAC from one ``getBasicInfo`` call, or None when it cannot be read.

    Not a secret, kept in memory only. It is the only identity a TCP answer cannot fake.
    """
    try:
        info = client.getBasicInfo()
        mac = info["device_info"]["basic_info"]["mac"]
    except Exception:  # noqa: BLE001 - any failure just means "unverified"
        return None
    mac = str(mac).strip().upper().replace(":", "-")
    return mac or None


def tapo_factory(host, user, password, cloud_password=None):
    """Return a zero-arg callable that builds a pytapo client (import kept lazy).

    The client refuses the methods on :data:`apiguard.DENIED_METHODS`.
    """
    def make():
        from pytapo import Tapo
        return apiguard.guard_client(Tapo(host, user, password, cloud_password or password))
    return make


class CameraAuthenticationBackoffError(RuntimeError):
    """A camera request was suppressed after an authentication failure."""


# pytapo const.py: Invalid login credentials, DEVICE_BLOCKED, SYSTEM_BLOCKED,
# TPAP_AUTHENTICATION_FAILED.
_AUTH_ERROR_CODES = (-40209, -40404, -40408, -40418)
_AUTH_CODE_RE = re.compile(r"-(?:40209|40404|40408|40418)(?!\d)")


def is_authentication_error(error):
    """Recognize authentication failures across pytapo's wrapped exceptions."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "code", None)
        if code in _AUTH_ERROR_CODES:
            return True
        message = str(current).lower()
        # -40211 is deliberately absent: pytapo names it MISSING_NECESSARY_PARAMS (also the
        # answer of TPAP firmware to the legacy login probe), not a credential failure.
        if ("invalid authentication data" in message
                or "temporary suspension" in message
                or _AUTH_CODE_RE.search(message)):
            return True
        pending.extend((getattr(current, "__cause__", None),
                        getattr(current, "__context__", None)))
    return False


def authentication_retry_after(error):
    """Return a camera-reported suspension duration, when the error includes one."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        match = re.search(r"try again in\s+(\d+)\s+seconds", str(current), re.IGNORECASE)
        if match:
            return int(match.group(1))
        pending.extend((getattr(current, "__cause__", None),
                        getattr(current, "__context__", None)))
    return None


def observe_client_requests(client, *, on_auth_failure, on_success):
    """Observe requests once, allowing callbacks to change as the daemon state advances."""
    original = getattr(client, "performRequest", None)
    if not callable(original):
        return False
    observer = getattr(client, "_tapo_monitor_request_observer", None)
    if observer is not None:
        observer["on_auth_failure"] = on_auth_failure
        observer["on_success"] = on_success
        return True

    observer = {"on_auth_failure": on_auth_failure, "on_success": on_success,
                "local": threading.local()}

    def perform_request(request_data, *args, **kwargs):
        if (getattr(client, "_tapo_monitor_client_closed", False)
                or getattr(client, "_tapo_auth_failed", False)):
            raise CameraAuthenticationBackoffError("camera client has been retired")
        # pytapo retries -40401 by calling self.performRequest again, i.e. through this
        # wrapper: only the outermost call reports, so one failure counts once.
        local = observer["local"]
        local.depth = getattr(local, "depth", 0) + 1
        try:
            result = original(request_data, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - preserve pytapo's public error
            if local.depth == 1 and is_authentication_error(exc):
                observer["on_auth_failure"](client, exc)
            raise
        finally:
            local.depth -= 1
        if local.depth == 0:
            observer["on_success"](client)
        return result

    client._tapo_monitor_request_observer = observer
    client.performRequest = perform_request
    return True


def close_client(client):
    """Release a pytapo client's transport, HTTP session and event loop; never raises.

    Every ``Tapo`` owns an asyncio loop (an epoll fd and a socket pair). The daemon
    replaces ordinary clients on control passes, so transport resources must close before
    the loop is closed; a client retained elsewhere must no longer be usable.
    """
    if getattr(client, "_tapo_monitor_client_closed", False):
        return
    try:
        client._tapo_monitor_client_closed = True
    except Exception:  # noqa: BLE001 - third-party clients may use slots
        pass
    if getattr(client, "_tapo_session_guard", None) is not None:
        from .sessionguard import retire_client

        retire_client(client, lambda: _close_client_resources(client))
        return
    _close_client_resources(client)


def _close_client_resources(client):
    """Close pytapo transport resources and its loop without allowing cleanup to raise."""
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must not break a control pass
            log.debug("closing a camera transport failed", exc_info=True)

    owners = [client, getattr(client, "transport", None)]
    owners.append(getattr(owners[-1], "transport", None))
    seen = set()
    for owner in owners:
        session = getattr(owner, "session", None) if owner is not None else None
        if session is None or id(session) in seen:
            continue
        seen.add(id(session))
        session_close = getattr(session, "close", None)
        if callable(session_close):
            try:
                session_close()
            except Exception:  # noqa: BLE001 - cleanup must not break a control pass
                log.debug("closing a camera HTTP session failed", exc_info=True)
    _close_client_loop(client)


def _close_client_loop(client):
    handler = getattr(client, "asyncHandler", None)
    loop = getattr(handler, "_loop", None)
    if loop is None:
        return
    try:
        if not loop.is_closed() and not loop.is_running():
            loop.close()
    except Exception:  # noqa: BLE001 - cleanup must not break a control pass
        log.debug("closing a camera client's loop failed", exc_info=True)


def connect(factory, retries=3, sleep=_time.sleep, delay=5):
    """Try ``factory()`` up to ``retries`` times. Returns (client, last_error).

    An authentication error ends the attempts at once: pytapo's constructor already logs in.

    On success returns (client, None); on exhaustion (None, last_error).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return factory(), None
        except Exception as e:  # noqa: BLE001 - surface the last error to the caller
            last_err = e
            if is_authentication_error(e):
                break  # the constructor already logged in (twice); retrying risks a lockout
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


_lamp_locks: dict = {}
_lamp_locks_guard = threading.Lock()
LAMP_LOCK_TIMEOUT = 20.0


def _lamp_lock(client):
    """One lock per physical camera, so read-then-toggle cannot interleave.

    Keyed by the most stable identity the client carries: a camera name or MAC the daemon
    attached, else the host. Without any, the lock lives on the client itself (no id() reuse).
    """
    for attr in ("_tapo_camera_name", "_tapo_device_mac", "host"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            key = (attr == "host" and "host" or "id", value)
            with _lamp_locks_guard:
                return _lamp_locks.setdefault(key, threading.Lock())
    with _lamp_locks_guard:
        lock = getattr(client, "_tapo_lamp_lock", None)
        if lock is None:
            lock = threading.Lock()
            try:
                client._tapo_lamp_lock = lock
            except Exception:  # noqa: BLE001 - slots/frozen client: share one lock
                lock = _lamp_locks.setdefault(("shared", ""), threading.Lock())
        return lock


def trigger_whitelamp(client, force_time=None):
    """Turn on the camera's white lamp if supported and not already on. Never raises.

    If force_time is given, set the automatic pulse duration (e.g. 30s instead of
    firmware default 300s) before triggering. The read and the toggle (which is a
    toggle, not a set) run under a per-camera lock, so two callers cannot switch it off again.
    """
    lock = _lamp_lock(client)
    if not lock.acquire(timeout=LAMP_LOCK_TIMEOUT):
        log.warning("failed to trigger whitelamp: another lamp request is still running")
        return False
    try:
        return _trigger_whitelamp_locked(client, force_time)
    finally:
        lock.release()


def _trigger_whitelamp_locked(client, force_time):
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


def _per_lens(raw, channels):
    """Normalise a ``chn_id`` getter's answer to ``{chn: mapping-or-value}``.

    pytapo answers a one-lens request with the bare value and a many-lens one with a dict
    keyed by the channel as a string. A lens absent from the answer (or an answer that is
    not keyed by channel at all, e.g. one shared switch) is "not known": left out.
    """
    chn = [int(c) for c in channels]
    if raw is None:
        return {}
    if len(chn) == 1 and not (isinstance(raw, dict) and str(chn[0]) in raw):
        if isinstance(raw, dict) and any(
                str(k).isdigit() for k in raw if str(k) != str(chn[0])):
            return {}  # keyed by another channel: this lens is unknown, not that value
        return {chn[0]: raw}
    if not isinstance(raw, dict):
        return {}
    return {c: raw.get(str(c), raw.get(c)) for c in chn
            if raw.get(str(c), raw.get(c)) is not None}


def read_lens_setting(client, kind: str, channels) -> dict:
    """Read one setting on each lens of a dual-lens camera: ``{chn: value}``. Never raises.

    ``kind`` is ``tamper`` (``{"enabled", "sensitivity"}``), ``day_night`` (``on``/``off``/
    ``auto``), ``night_vision_mode`` (the firmware mode string) or ``whitelamp`` (the lamp
    config mapping). A lens that could not be read is missing from the answer, so the
    caller repairs it rather than trusting a guess. The day/night read asks the camera with
    ``name: ["common"]`` as a LIST: the string form ignores ``chn_id`` (measured on a C545D).
    """
    chn = [int(c) for c in channels]
    try:
        if kind == "tamper":
            return _per_lens(client.getTamperDetection(chn_id=chn), chn)
        if kind == "whitelamp":
            return _per_lens(client.getWhitelampConfig(chn_id=chn), chn)
        if kind == "night_vision_mode":
            raw = _per_lens(client.getNightVisionModeConfig(chn_id=chn), chn)
            return {c: v.get("night_vision_mode") for c, v in raw.items()
                    if isinstance(v, dict) and v.get("night_vision_mode") is not None}
        if kind == "day_night":
            if getattr(client, "childID", None):
                return _per_lens(client.getDayNightMode(chn_id=chn), chn)
            data = client.executeFunction(
                "getLightFrequencyInfo", {"image": {"name": ["common"], "chn_id": chn}})
            image = (data or {}).get("image", {})
            common_chn = image.get("common_chn") or {}
            return {c: common_chn[str(c)]["inf_type"] for c in chn
                    if isinstance(common_chn.get(str(c)), dict)
                    and common_chn[str(c)].get("inf_type") is not None}
    except Exception as exc:  # noqa: BLE001 - an optional read must not stop the pass
        log.debug("per-lens %s read failed: %s", kind, exc)
    return {}


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


def recording_hosts(cfg):
    """Directory names the external recorder may use for this camera under RECORDING_ROOT.

    The recorder tree is keyed by an address, not by the camera name, and the repo cannot
    tell which candidate the recorder was configured with (the primary after a failover
    is the common case, but a recorder pointed at the failover address is possible), so
    every candidate is a possible key. The configured primary comes first.
    """
    return candidate_hosts(cfg)


def recording_key(cfg, root=None):
    """The single recorder directory name to use for ``cfg`` under ``root``.

    Among the candidates whose directory exists, the one with the newest recording wins;
    with no ``root`` or no existing directory it is the primary (first candidate).
    """
    import os

    hosts = recording_hosts(cfg)
    if not root or len(hosts) == 1:
        return hosts[0]
    from .snapshot import _newest_recording

    best, best_mtime = hosts[0], None
    for host in hosts:
        cam_dir = os.path.join(root, host)
        if not os.path.isdir(cam_dir):
            continue
        latest = _newest_recording(cam_dir)
        try:
            mtime = os.path.getmtime(latest) if latest else float("-inf")
        except OSError:
            mtime = float("-inf")
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = host, mtime
    return best
