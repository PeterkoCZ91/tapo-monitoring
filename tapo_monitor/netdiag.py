"""One-line network diagnosis for a camera outage, logged at its start, rarely during it
and on recovery.

"Camera unreachable" alone does not say where the fault is. Three cheap observations do:

* the host's ARP/neighbour entry for the camera (``ip -j neigh``): INCOMPLETE or FAILED
  means the camera is gone from the LAN, REACHABLE or STALE means it is on the LAN and
  only its services are not answering;
* the host's own Wi-Fi link (``iw dev <iface> link``), when the route to the camera
  leaves through a wireless interface: a weak host link is not the camera's fault;
* the camera's last reported Wi-Fi signal (``getConnectionType``, read at most every
  few minutes while it is reachable) and its age, i.e. the signal just before it dropped.

Every subprocess has a short timeout and no shell; the subprocess part runs on a worker
thread, never on the daemon loop. Failures are swallowed with a debug line: diagnosis
must never become a fault of its own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT = 2.0               # seconds per subprocess call
REPEAT_EVERY = 1800.0       # seconds between diagnosis lines while an outage lasts
SIGNAL_POLL_EVERY = 300.0   # seconds between camera signal reads while it is reachable
SIGNAL_LOG_EVERY = 3600.0   # seconds between signal lines when the level does not change

_SIGNAL_PARAMS: dict[str, Any] = {"network": {"get_connection_type": []}}


def _run(argv, run):
    try:
        proc = run(list(argv), capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("netdiag: %s failed: %s", argv[0], type(exc).__name__)
        return None
    if proc.returncode != 0:
        log.debug("netdiag: %s exited %s", " ".join(argv[:3]), proc.returncode)
        return None
    return proc.stdout


def _json_list(text):
    if text is None:
        return None
    try:
        value = json.loads(text or "[]")
    except ValueError:
        log.debug("netdiag: unparsable ip -j output")
        return None
    return value if isinstance(value, list) else None


def neighbor(ip, run=subprocess.run):
    """``neigh=<STATE> [lladdr=<mac>]``, ``neigh=absent`` or ``neigh=unknown``."""
    entries = _json_list(_run(("ip", "-j", "neigh", "show", ip), run))
    if entries is None:
        return "neigh=unknown"
    entry = next((e for e in entries if isinstance(e, dict)), None)
    if entry is None:
        return "neigh=absent"
    states = entry.get("state") or []
    state = ",".join(str(s) for s in states) if isinstance(states, list) else str(states)
    text = f"neigh={state or '?'}"
    if entry.get("lladdr"):
        text += f" lladdr={entry['lladdr']}"
    return text


def route_dev(ip, run=subprocess.run):
    """The interface the host would use to reach ``ip``, or None."""
    entries = _json_list(_run(("ip", "-j", "route", "get", ip), run))
    for entry in entries or ():
        if isinstance(entry, dict) and entry.get("dev"):
            return str(entry["dev"])
    return None


def _sysfs_wireless(dev):
    return re.fullmatch(r"[\w.:@-]+", dev) is not None and os.path.isdir(
        f"/sys/class/net/{dev}/wireless")


def wifi_link(dev, run=subprocess.run):
    """``host_wifi=<dev> signal=..dBm ssid=.. tx=..`` for a wireless interface."""
    out = _run(("iw", "dev", dev, "link"), run)
    if out is None:
        return f"host_wifi={dev} link=unknown"
    if out.strip().lower().startswith("not connected"):
        return f"host_wifi={dev} link=not-connected"
    parts = [f"host_wifi={dev}"]
    signal = re.search(r"^\s*signal:\s*(-?\d+)\s*dBm", out, re.M)
    ssid = re.search(r"^\s*SSID:\s*(.+?)\s*$", out, re.M)
    tx = re.search(r"^\s*tx bitrate:\s*([\d.]+)\s*(\S+)", out, re.M)
    if signal:
        parts.append(f"signal={signal.group(1)}dBm")
    if ssid:
        parts.append(f"ssid={ssid.group(1)}")
    if tx:
        parts.append(f"tx={tx.group(1)}{tx.group(2)}")
    return " ".join(parts)


def describe(ip, run=subprocess.run, is_wireless: Callable[[str], bool] = _sysfs_wireless,
             platform=None):
    """One line about how the host sees ``ip``. Blocking (bounded); never raises."""
    if not (platform or sys.platform).startswith("linux"):
        return "netdiag=unsupported"
    parts = [neighbor(ip, run)]
    dev = route_dev(ip, run)
    if dev is None:
        parts.append("route=unknown")
    else:
        try:
            wireless = bool(is_wireless(dev))
        except Exception:  # noqa: BLE001 - diagnosis only
            wireless = False
        parts.append(wifi_link(dev, run) if wireless else f"via={dev}")
    return " ".join(parts)


def _find_signal(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "rssiValue" in item:
                return item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def read_signal(client):
    """``(dBm, level 0-4)`` from the camera's ``getConnectionType``, or None."""
    call = getattr(client, "executeFunction", None)
    if not callable(call):
        return None
    try:
        info = _find_signal(call("getConnectionType", _SIGNAL_PARAMS))
        if info is None:
            return None
        return int(float(info["rssiValue"])), int(float(info.get("rssi", -1)))
    except Exception as exc:  # noqa: BLE001 - an unsupported or failed read is not a fault
        log.debug("netdiag: getConnectionType failed: %s", type(exc).__name__)
        return None


def _thread_spawn(job):
    threading.Thread(target=job, daemon=True, name="netdiag").start()


class Diagnostics:
    """Per-camera, rate-limited outage diagnosis and camera signal bookkeeping."""

    def __init__(self, spawn: Callable[[Callable[[], None]], Any] = _thread_spawn,
                 describe: Callable[[str], str] = describe):
        self._spawn = spawn
        self._describe = describe
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._down_since: dict[str, float] = {}
        self._diag_at: dict[str, float] = {}
        self.signals: dict[str, tuple[int, int, float]] = {}
        self._signal_next: dict[str, float] = {}
        self._signal_logged: dict[str, tuple[float, int]] = {}

    def poll_signal(self, name, client, now):
        """Read the camera's Wi-Fi signal when due; log hourly or when the level changes."""
        try:
            if now < self._signal_next.get(name, -float("inf")):
                return
            reading = read_signal(client)
            if reading is None:
                self._signal_next[name] = now + SIGNAL_LOG_EVERY
                return
            self._signal_next[name] = now + SIGNAL_POLL_EVERY
            dbm, level = reading
            self.signals[name] = (dbm, level, now)
            logged = self._signal_logged.get(name)
            if logged is None or logged[1] != level or now - logged[0] >= SIGNAL_LOG_EVERY:
                self._signal_logged[name] = (now, level)
                log.info("camera %s wifi signal %d dBm level=%d/4", name, dbm, level)
        except Exception:  # noqa: BLE001 - diagnosis must never break a control pass
            log.debug("netdiag: signal bookkeeping failed for %s", name, exc_info=True)

    def _signal_text(self, name, now):
        reading = self.signals.get(name)
        if reading is None:
            return "camera_wifi=unknown"
        dbm, level, at = reading
        return f"camera_wifi={dbm}dBm/{level} age={max(0, now - at):.0f}s"

    def network_event(self, name, host, *, reachable, was_reachable, now):
        """Log a diagnosis at outage start, every ``REPEAT_EVERY`` during it, and on recovery.

        ``was_reachable`` is the caller's previous observation; this class keeps its own
        outage start so a missed transition still yields one start and one recovery line.
        """
        try:
            if reachable:
                if name not in self._down_since:
                    return
                phase, level = "recovered", logging.INFO
                started = self._down_since.pop(name)
                self._diag_at.pop(name, None)
            elif name not in self._down_since:
                phase, level = "outage", logging.WARNING
                started = self._down_since[name] = now
                self._diag_at[name] = now
            elif now - self._diag_at.get(name, now) >= REPEAT_EVERY:
                phase, level = "still down", logging.WARNING
                started = self._down_since[name]
                self._diag_at[name] = now
            else:
                return
            context = f"down={max(0, now - started):.0f}s {self._signal_text(name, now)}"
            with self._lock:
                if name in self._busy:
                    return
                self._busy.add(name)

            def job():
                try:
                    seen = self._describe(str(host))
                    log.log(level, "network diagnosis %s (%s): %s %s", name, phase, seen, context)
                except Exception:  # noqa: BLE001 - diagnosis only
                    log.debug("netdiag: diagnosis failed for %s", name, exc_info=True)
                finally:
                    with self._lock:
                        self._busy.discard(name)

            try:
                self._spawn(job)
            except Exception:  # noqa: BLE001 - e.g. no thread can be started
                with self._lock:
                    self._busy.discard(name)
                raise
        except Exception:  # noqa: BLE001 - diagnosis must never break the network check
            log.debug("netdiag: network event failed for %s", name, exc_info=True)

