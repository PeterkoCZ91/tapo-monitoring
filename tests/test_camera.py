import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import camera


def _no_sleep(_):
    pass


# ── connect (retry / lockout-aware) ──────────────────────────────────────────

def test_connect_succeeds_first_try():
    cam, err = camera.connect(lambda: "CAM", sleep=_no_sleep)
    assert cam == "CAM"
    assert err is None


def test_connect_succeeds_after_transient_failures():
    calls = {"n": 0}
    def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Invalid authentication data")
        return "CAM"
    cam, err = camera.connect(factory, retries=3, sleep=_no_sleep)
    assert cam == "CAM"
    assert err is None
    assert calls["n"] == 3


def test_connect_exhausts_and_returns_error():
    def factory():
        raise RuntimeError("down")
    cam, err = camera.connect(factory, retries=3, sleep=_no_sleep)
    assert cam is None
    assert isinstance(err, RuntimeError)


# ── new_events / newest_start ────────────────────────────────────────────────

def test_new_events_filters_and_sorts():
    events = [
        {"start_time": 50}, {"start_time": 200}, {"start_time": 150},
    ]
    out = camera.new_events(events, last_seen=100)
    assert [e["start_time"] for e in out] == [150, 200]


def test_new_events_empty():
    assert camera.new_events([], last_seen=0) == []


def test_newest_start():
    assert camera.newest_start([{"start_time": 5}, {"start_time": 9}, {"start_time": 7}]) == 9


def test_newest_start_empty():
    assert camera.newest_start([]) is None
