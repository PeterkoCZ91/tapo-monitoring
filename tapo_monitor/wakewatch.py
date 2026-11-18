"""Detect a battery camera waking up using ping only.

A battery camera sleeps most of the time and any login wakes it, so the only
free way to notice that it woke (PIR, ring, app use) is to ping it.  This module
never logs in; it only reports the False/None -> True edge of reachability.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def woke(prev: bool | None, up: bool) -> bool:
    """True when the camera just became reachable (unknown counts as down)."""
    return up and not prev


def ping_once(host: str, timeout: float = 1.0) -> bool:  # pragma: no cover
    """One ICMP echo; True when the host answered."""
    try:
        result = subprocess.run(
            ["ping", "-c1", f"-W{max(1, int(timeout))}", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class WakeWatcher(threading.Thread):
    """Ping a host forever and call ``on_wake(name)`` on every wake-up edge."""

    def __init__(
        self,
        name: str,
        host: str,
        interval: float,
        on_wake: Callable[[str], None],
        ping: Callable[[str], bool] = ping_once,
        sleep: Callable[[float], None] = time.sleep,
        stop_event: threading.Event | None = None,
        busy: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(name=f"wakewatch-{name}", daemon=True)
        self.camera = name
        self.host = host
        self.interval = interval
        self.on_wake = on_wake
        self._ping = ping
        self._sleep = sleep
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._busy = busy

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        prev: bool | None = None
        while not self._stop_event.is_set():
            try:
                up = bool(self._ping(self.host))
            except Exception:  # noqa: BLE001 - a probe failure must not kill the watcher
                log.exception("wakewatch %s: ping failed", self.camera)
                up = False
            if woke(prev, up) and not (self._busy and self._busy(self.camera)):
                try:
                    self.on_wake(self.camera)
                except Exception:  # noqa: BLE001
                    log.exception("wakewatch %s: on_wake failed", self.camera)
            prev = up
            if self._stop_event.is_set():
                break
            self._sleep(self.interval)
