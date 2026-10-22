"""Reolink cameras as an event source: Baichuan push events turned into getEvents dicts.

A Reolink camera has none of the Tapo control API, so the daemon uses it only for what
travels over RTSP and its native event channel (TCP 9000): a motion/AI push starts the
usual live pass — RTSP frame, scorer, Telegram. Control, presets, tracking, the digital
twin and API restarts are skipped for it (see :func:`is_reolink`).

The camera's own AI label only decides ``person`` vs ``motion`` for the classifier; the
scorer still decides whether a person is in the frame. ``reolink_aio`` (MIT, the library
behind Home Assistant's integration) is imported lazily: install the ``reolink`` extra
where such a camera is configured.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

SOURCE = "reolink"
DEFAULT_STREAM = "h264Preview_01_main"
# Lenses whose pushes count as the camera's event: a dual-lens RLC-81MA reports the wide
# lens as channel 0 and the tele lens as 1. A single-lens camera never sends channel 1.
CHANNELS = (0, 1)
# Push states reolink_aio tracks per channel; the parser only updates keys that already
# exist, and without its HTTP API (often closed on these cameras) nothing creates them.
AI_TYPES = ("people", "vehicle", "dog_cat")
TRIGGERS = ("motion",) + AI_TYPES    # what may start an event (config: reolink_triggers)
LOGIN_TIMEOUT_S = 20.0
RESUBSCRIBE_EVERY_S = 60.0
MAX_QUEUED = 64


def is_reolink(cfg: Any) -> bool:
    """True for a camera whose events come from a Reolink push channel."""
    detection = getattr(cfg, "detection", None)
    return SOURCE in (getattr(detection, "sources", None) or ())


class EdgeTracker:
    """Turn push state snapshots into getEvents-shaped events. Pure.

    One event when the camera goes from idle to active (``person`` when its AI says
    people, else ``motion``), and one more ``person`` when people appear later in the
    same active spell. Going idle ends the spell and emits nothing. Only the states in
    ``triggers`` count as active (e.g. people + dog_cat: bare motion and cars stay idle).
    """

    def __init__(self, triggers: tuple[str, ...] = TRIGGERS) -> None:
        self.triggers = tuple(triggers)
        self.active = False
        self.person_sent = False

    def update(self, state: dict, now: float) -> list[dict]:
        active = any(state.get(k) for k in self.triggers)
        people = bool(state.get("people"))
        events: list[dict] = []
        if not active:
            self.active = self.person_sent = False
            return events
        if not self.active:
            events.append(_event("person" if people else "motion", state, now))
            self.person_sent = people
        elif people and not self.person_sent:
            events.append(_event("person", state, now))
            self.person_sent = True
        self.active = True
        return events


def _event(kind: str, state: dict, now: float) -> dict:
    return {
        "start_time": int(now),
        "end_time": None,
        "event_type": kind,
        "type": kind,
        "source": SOURCE,
        # What the camera's own AI said, for the log only (it called a face "dog_cat").
        "camera_ai": sorted(k for k, v in state.items() if v and k != "motion"),
    }


class ReolinkClient:
    """Holds one Baichuan push subscription on a private asyncio thread.

    ``getEvents()`` drains the events queued since the last call; it raises
    ``ConnectionError`` once the subscription has died so the daemon's event-health path
    sees it, and the next control pass reconnects. ``close()`` logs out and ends the thread.
    """

    def __init__(self, host: str, user: str, password: str, *,
                 channels: tuple[int, ...] = CHANNELS,
                 triggers: tuple[str, ...] = TRIGGERS,
                 host_factory: Any = None, clock: Any = time.time) -> None:
        self.host = host
        self.channels = tuple(channels)
        self._user = user
        self._password = password
        self._host_factory = host_factory
        self._clock = clock
        self._tracker = EdgeTracker(triggers)
        self._queue: deque[dict] = deque(maxlen=MAX_QUEUED)
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None
        self._api: Any = None
        self.closed = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, timeout: float = LOGIN_TIMEOUT_S) -> ReolinkClient:
        """Log in and subscribe; raise the login error (or a timeout) in the caller."""
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,),
                                        name=f"reolink-{self.host}", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            self.close()
            raise ConnectionError(f"reolink {self.host}: login timed out")
        if self._error is not None:
            err = self._error
            self.close()
            raise err
        return self

    @property
    def alive(self) -> bool:
        return (not self.closed and self._error is None
                and self._thread is not None and self._thread.is_alive())

    def close(self) -> None:
        self.closed = True
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    # ── daemon interface ─────────────────────────────────────────────────────

    def getEvents(self) -> list[dict]:  # noqa: N802 - mirrors pytapo's name
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
        if not events and not self.alive:
            raise ConnectionError(f"reolink {self.host}: event subscription lost"
                                  + (f" ({self._error})" if self._error else ""))
        return events

    def push_state(self, state: dict) -> None:
        """Feed one state snapshot (called from the push callback; public for tests)."""
        events = self._tracker.update(state, self._clock())
        if events:
            with self._lock:
                self._queue.extend(events)
            for e in events:
                log.info("reolink %s: %s event (camera AI: %s)", self.host, e["type"],
                         ", ".join(e["camera_ai"]) or "motion only")

    # ── worker thread ────────────────────────────────────────────────────────

    def _make_api(self) -> Any:
        if self._host_factory is not None:
            return self._host_factory(self.host, self._user, self._password)
        from reolink_aio.api import Host
        return Host(self.host, self._user, self._password)

    def _read_state(self) -> dict:
        """One state for the camera: a type is on when it is on for any lens."""
        api = self._api
        state = {"motion": any(bool(api.motion_detected(ch)) for ch in self.channels)}
        for kind in AI_TYPES:
            on = False
            for ch in self.channels:
                try:
                    on = on or bool(api.ai_detected(ch, kind))
                except Exception:  # noqa: BLE001 - an unsupported type is just absent
                    pass
            state[kind] = on
        return state

    def _run(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main(ready))
        except BaseException as exc:  # noqa: BLE001 - surfaced through getEvents/start
            self._error = exc
        finally:
            ready.set()
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _main(self, ready: threading.Event) -> None:
        self._stop = asyncio.Event()
        api = self._api = self._make_api()
        bc = api.baichuan
        try:
            await bc.login()
            for ch in self.channels:
                states = api._ai_detection_states.setdefault(ch, {})
                for kind in AI_TYPES:
                    states.setdefault(kind, False)
            bc.register_callback("tapo_monitor", lambda: self.push_state(self._read_state()),
                                 cmd_id=33)
            await bc.subscribe_events()
            log.info("reolink %s: subscribed to push events", self.host)
            ready.set()
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), RESUBSCRIBE_EVERY_S)
                except asyncio.TimeoutError:
                    await bc.check_subscribe_events()
        finally:
            try:
                await bc.unsubscribe_events()
            except Exception:  # noqa: BLE001 - best-effort logout
                log.debug("reolink %s: logout failed", self.host, exc_info=True)
            close = getattr(api, "logout", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass


def connect(host: str, user: str, password: str, **kwargs: Any) -> ReolinkClient:
    """Return a started :class:`ReolinkClient` (raises on login failure)."""
    return ReolinkClient(host, user, password, **kwargs).start()
