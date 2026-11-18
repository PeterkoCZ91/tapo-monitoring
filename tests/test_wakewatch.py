"""Tests for the ping-only wake detector."""

from __future__ import annotations

import threading

from tapo_monitor.wakewatch import WakeWatcher, woke


def test_woke_only_on_rising_edge():
    assert woke(None, True) is True
    assert woke(False, True) is True
    assert woke(True, True) is False
    assert woke(True, False) is False
    assert woke(False, False) is False
    assert woke(None, False) is False


def _run(sequence, *, busy=None):
    stop = threading.Event()
    calls: list[str] = []
    seq = list(sequence)
    pings: list[str] = []

    def ping(host: str) -> bool:
        pings.append(host)
        value = seq.pop(0)
        if not seq:
            stop.set()
        return value

    watcher = WakeWatcher(
        "cam",
        "host.invalid",
        0.5,
        calls.append,
        ping=ping,
        sleep=lambda _s: None,
        stop_event=stop,
        busy=busy,
    )
    watcher.run()
    return calls, pings


def test_watcher_signals_once_per_wake():
    calls, pings = _run([False, True, True, False, True])
    assert calls == ["cam", "cam"]
    assert len(pings) == 5


def test_watcher_stops():
    watcher = WakeWatcher(
        "cam", "h", 0.1, lambda _n: None, ping=lambda _h: False, sleep=lambda _s: None
    )
    watcher.stop()
    watcher.run()  # returns immediately because the stop event is already set
    assert watcher.is_alive() is False


def test_busy_suppresses_signal():
    calls, pings = _run([False, True, False, True], busy=lambda _n: True)
    assert calls == []
    assert len(pings) == 4
