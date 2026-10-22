"""The opt-in ``lastalarm`` detection source: config, daemon pass, SD guard, pacing, auth."""

import time

import pytest

from tapo_monitor import alarminfo, daemon
from tapo_monitor import config as cfg

SECRETS = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}


def _load(**camera):
    base = {"name": "a", "host": "203.0.113.10"}
    base.update(camera)
    return cfg.load_config_from_dict({"cameras": [base]})


class _AlarmCam:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def executeFunction(self, method, params):  # noqa: N802
        self.calls += 1
        item = self.responses.pop(0) if self.responses else self.responses_last
        if isinstance(item, Exception):
            raise item
        return item

    responses_last = {}


def _resp(ts, kind="motion"):
    return {"system": {"last_alarm_info": {"last_alarm_time": ts, "last_alarm_type": kind}}}


def _pass(app, cam, state, snaps=None):
    snaps = snaps if snaps is not None else []

    def snapshot_for(c):
        def snap(camera, event):
            snaps.append(event)
            return None
        return snap

    daemon.run_monitor_pass(app, {"a": cam}, state, now=time.time(), secrets=SECRETS,
                            snapshot_for=snapshot_for, time_str=lambda e: "t")
    return snaps


# ── config ──────────────────────────────────────────────────────────────────

def test_config_accepts_lastalarm_alone_and_with_other_sources():
    assert _load(detection={"sources": ["lastalarm"]}).cameras[0].detection.sources == ["lastalarm"]
    assert "lastalarm" in _load(detection={"sources": ["lastalarm", "motion"]}).cameras[0].detection.sources


def test_config_rejects_lastalarm_with_getevents():
    with pytest.raises(cfg.ConfigError, match="pick one"):
        _load(detection={"sources": ["getevents", "lastalarm"]})


def test_config_rejects_lastalarm_with_sd_options():
    with pytest.raises(cfg.ConfigError, match="SD card"):
        _load(detection={"sources": ["lastalarm"]}, sd_snapshot=True)


def test_default_stays_getevents():
    assert _load().cameras[0].detection.sources == ["getevents"]


# ── daemon pass ─────────────────────────────────────────────────────────────

def test_poll_returns_one_event_then_dedupes_and_never_queues_sd():
    app = _load(detection={"sources": ["lastalarm"]})
    ts = int(time.time())
    cam = _AlarmCam([_resp(ts), _resp(ts)])
    state = daemon.MonitorState()
    snaps = _pass(app, cam, state)
    assert {e["start_time"] for e in snaps} == {ts}
    first = len(snaps)
    assert state.last_seen["a"] == ts
    assert state.pending_sd == []
    # same alarm again (and past the pacing window): nothing new
    state.lastalarm_clients["a"]._last_call = None
    _pass(app, cam, state, snaps)
    assert len(snaps) == first and cam.calls == 2
    assert state.pending_sd == []


def test_no_defer_even_when_the_camera_somehow_has_sd_snapshot():
    app = _load(detection={"sources": ["lastalarm"]})
    object.__setattr__(app.cameras[0], "sd_snapshot", True)
    cam = _AlarmCam([_resp(int(time.time()), "person")])
    state = daemon.MonitorState()
    _pass(app, cam, state)
    assert state.pending_sd == []


# ── wrapper ─────────────────────────────────────────────────────────────────

def test_pacing_skips_calls_closer_than_1_5_seconds():
    t = [100.0]
    cam = _AlarmCam([_resp(int(time.time()))])
    wrap = alarminfo.LastAlarmClient(cam, alarminfo.SeenAlarms(), clock=lambda: t[0])
    assert len(wrap.getEvents()) == 1
    t[0] = 101.0
    assert wrap.getEvents() == [] and cam.calls == 1
    t[0] = 101.6
    wrap.getEvents()
    assert cam.calls == 2


def test_ordinary_errors_return_empty():
    cam = _AlarmCam([RuntimeError("boom")])
    wrap = alarminfo.LastAlarmClient(cam, alarminfo.SeenAlarms())
    assert wrap.getEvents() == []


def test_auth_error_propagates():
    cam = _AlarmCam([RuntimeError("Invalid authentication data (-40413)")])
    wrap = alarminfo.LastAlarmClient(cam, alarminfo.SeenAlarms())
    with pytest.raises(Exception, match="authentication"):
        wrap.getEvents()


def test_auth_error_reaches_poll_observer_through_the_pass():
    app = _load(detection={"sources": ["lastalarm"]})
    cam = _AlarmCam([RuntimeError("Invalid authentication data")])
    state = daemon.MonitorState()
    _pass(app, cam, state)
    assert state.events_reachable["a"] is False
    assert "Invalid authentication" in state.event_error["a"]


def test_motion_event_type_is_kept_for_the_classifier():
    cam = _AlarmCam([_resp(int(time.time()), "motion")])
    wrap = alarminfo.LastAlarmClient(cam, alarminfo.SeenAlarms())
    (event,) = wrap.getEvents()
    assert event["event_type"] == "motion"


def test_ordinary_error_is_logged_once_per_streak_with_recovery(caplog):
    cam = _AlarmCam([RuntimeError("-40109"), RuntimeError("-40109"), _resp(int(time.time()))])
    wrap = alarminfo.LastAlarmClient(cam, alarminfo.SeenAlarms(), min_interval=0, name="c545d")
    with caplog.at_level("INFO", logger="tapo_monitor.alarminfo"):
        assert wrap.getEvents() == []
        assert wrap.getEvents() == []
        assert len(wrap.getEvents()) == 1
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["getLastAlarmInfo c545d failed: RuntimeError: -40109",
                     "getLastAlarmInfo c545d recovered after 2 failure(s)"]
