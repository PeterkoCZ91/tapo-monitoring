import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg
from tapo_monitor import daemon, notify, snapshot
from tests.conftest import FakeResponse as _Resp


def _cam(**overrides):
    base = {"name": "c", "host": "203.0.113.10"}
    base.update(overrides)
    app = cfg.load_config_from_dict({"cameras": [base]})
    return app.cameras[0]


# ── plan_camera (pure) ───────────────────────────────────────────────────────

def test_plan_tracking_night_dry():
    plan = daemon.plan_camera(_cam(), night=True, rain_active=False)
    assert plan.autotrack_on is True
    assert plan.rain_parked is False
    assert plan.motion_sensitivity == 60
    assert plan.preset is None  # no night_preset by default

def test_plan_tracking_day_parks_at_day_preset():
    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    assert plan.autotrack_on is False
    assert plan.preset == "2"

def test_plan_rain_lower_sensitivity_keeps_tracking():
    cam = _cam(weather={"strategy": "lower_sensitivity", "motion_normal": 60, "motion_rain": 20})
    plan = daemon.plan_camera(cam, night=True, rain_active=True)
    assert plan.autotrack_on is True
    assert plan.motion_sensitivity == 20

def test_plan_rain_disable_tracking_parks():
    cam = _cam(weather={"strategy": "disable_tracking"})
    plan = daemon.plan_camera(cam, night=True, rain_active=True)
    assert plan.autotrack_on is False
    assert plan.rain_parked is True
    assert plan.preset == "2"

def test_plan_static_never_tracks():
    plan = daemon.plan_camera(_cam(role="static"), night=True, rain_active=False)
    assert plan.autotrack_on is False

def test_plan_night_vision_untouched_by_default():
    assert daemon.plan_camera(_cam(), night=True, rain_active=False).night_vision is None
    assert daemon.plan_camera(_cam(), night=False, rain_active=False).night_vision is None

def test_plan_night_vision_ir_follows_schedule():
    cam = _cam(night_vision="ir")
    assert daemon.plan_camera(cam, night=True, rain_active=False).night_vision == "on"
    assert daemon.plan_camera(cam, night=False, rain_active=False).night_vision == "off"

def test_plan_night_vision_auto_reasserts():
    cam = _cam(night_vision="auto")
    assert daemon.plan_camera(cam, night=True, rain_active=False).night_vision == "auto"
    assert daemon.plan_camera(cam, night=False, rain_active=False).night_vision == "auto"


def test_effective_night_honours_per_camera_schedule():
    assert daemon.effective_night(_cam(schedule="always_day"), True) is False
    assert daemon.effective_night(_cam(schedule="always_night"), False) is True
    assert daemon.effective_night(_cam(schedule="astral"), True) is True


# ── run_once (injected deps) ─────────────────────────────────────────────────

def test_run_once_plans_each_camera():
    app = cfg.load_config_from_dict({"cameras": [
        {"name": "a", "host": "203.0.113.10"},
        {"name": "b", "host": "203.0.113.11", "role": "static"},
    ]})
    plans = daemon.run_once(app, now=1000, is_night=lambda: True, is_raining=lambda *a, **k: False)
    assert plans["a"].autotrack_on is True
    assert plans["b"].autotrack_on is False


def test_run_once_skips_weather_when_strategy_none():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    called = {"weather": 0}
    def is_raining(*a, **k):
        called["weather"] += 1
        return True
    # default weather strategy is "none" -> weather must not be queried
    daemon.run_once(app, now=1, is_night=lambda: True, is_raining=is_raining)
    assert called["weather"] == 0


def test_run_once_applies_via_connect(monkeypatch):
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})

    class FakeCam:
        def __init__(self):
            self.state = False
            self.sensitivity = None
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            self.sensitivity = sensitivity
        def setAutoTrackTarget(self, enabled):
            self.state = enabled
        def getAutoTrackTarget(self):
            return {"enabled": "on" if self.state else "off"}

    cam = FakeCam()
    daemon.run_once(app, now=1, connect=lambda c: (cam, None),
                    is_night=lambda: True, is_raining=lambda *a, **k: False)
    assert cam.state is True             # auto-track asserted on (night)
    assert cam.sensitivity == 60         # sensitivity applied as int


def test_apply_plan_self_heals_person_detection(monkeypatch):
    # Regression 2026-06-15: the camera's AI person detection (events_1 bit 19)
    # silently went 'off' after a daemon restart, so people arrived only as bare
    # motion and got dropped. apply_plan must re-assert person detection ON each
    # tick — the same self-heal the daemon already does for motion/auto-track.
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def __init__(self):
            self.person = None
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setPersonDetection(self, enabled, sensitivity=False):
            self.person = enabled
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    cam = FakeCam()
    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.person is True


def test_apply_plan_sets_person_sensitivity_when_configured(monkeypatch):
    # The camera's AI person detector (events_1 bit 19) can false-fire "person" on an
    # empty yard. Lowering its sensitivity at the source quiets those empty alerts.
    # When a per-camera person_sensitivity is configured, apply_plan must pass it to
    # setPersonDetection so it is re-asserted (self-healing) every control tick.
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def __init__(self):
            self.person = None
            self.person_sensitivity = "unset"
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setPersonDetection(self, enabled, sensitivity=False):
            self.person = enabled
            self.person_sensitivity = sensitivity
        def setVehicleDetection(self, enabled, sensitivity=False):
            pass
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    cam = FakeCam()
    plan = daemon.plan_camera(_cam(person_sensitivity=40), night=False, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.person is True
    assert cam.person_sensitivity == 40


def test_apply_plan_person_sensitivity_default_unchanged(monkeypatch):
    # With no person_sensitivity configured, apply_plan must NOT pass a sensitivity,
    # leaving the camera's value untouched (pytapo default sentinel False).
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def __init__(self):
            self.person = None
            self.person_sensitivity = "unset"
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setPersonDetection(self, enabled, sensitivity=False):
            self.person = enabled
            self.person_sensitivity = sensitivity
        def setVehicleDetection(self, enabled, sensitivity=False):
            pass
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    cam = FakeCam()
    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.person is True
    assert cam.person_sensitivity is False  # default sentinel -> camera value unchanged


def _nightvision_fakecam():
    from tapo_monitor import tracking

    class FakeCam:
        def __init__(self):
            self.daynight = "unset"
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setPersonDetection(self, enabled, sensitivity=False):
            pass
        def setVehicleDetection(self, enabled, sensitivity=False):
            pass
        def setDayNightMode(self, mode):
            self.daynight = mode
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    return FakeCam, tracking


def test_apply_plan_forces_ir_at_night(monkeypatch):
    # night_vision: ir -> daemon forces the camera to IR ("on") during the night window.
    FakeCam, tracking = _nightvision_fakecam()
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)
    cam = FakeCam()
    plan = daemon.plan_camera(_cam(night_vision="ir"), night=True, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.daynight == "on"


def test_apply_plan_ir_daytime_restores_colour(monkeypatch):
    # By day the same camera must be put back to day/colour ("off"), not left on IR.
    FakeCam, tracking = _nightvision_fakecam()
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)
    cam = FakeCam()
    plan = daemon.plan_camera(_cam(night_vision="ir"), night=False, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.daynight == "off"


def test_select_recording_frame_picks_sharpest_above_threshold():
    cam = _cam(sd_snapshot=True, snapshot_source="recording",
               scorer={"url": "http://x/score", "threshold": 0.4})
    frames = ["f0.jpg", "f1.jpg", "f2.jpg"]
    scores = {"f0.jpg": 0.2, "f1.jpg": 0.9, "f2.jpg": 0.7}   # f0 below threshold
    blur = {"f1.jpg": 8.0, "f2.jpg": 3.0}                    # f2 sharper
    image, s = daemon._select_recording_frame(
        cam, event={"start_time": 1}, etype="person", frames=frames,
        score=lambda f: scores[f], blur_score=lambda f: blur[f])
    assert image == "f2.jpg"
    assert s == 0.7


def test_select_recording_frame_none_when_all_below():
    cam = _cam(sd_snapshot=True, snapshot_source="recording",
               scorer={"url": "http://x/score", "threshold": 0.4})
    image, s = daemon._select_recording_frame(
        cam, event={"start_time": 1}, etype="person", frames=["a.jpg"],
        score=lambda f: 0.05, blur_score=lambda f: 1.0)
    assert image is None and s is None


def test_select_recording_frame_passes_through_when_scorer_down():
    cam = _cam(sd_snapshot=True, snapshot_source="recording",
               scorer={"url": "http://x/score", "threshold": 0.4})
    image, s = daemon._select_recording_frame(
        cam, event={"start_time": 1}, etype="person", frames=["a.jpg", "b.jpg"],
        score=lambda f: None, blur_score=lambda f: 1.0)
    assert image == "a.jpg" and s is None


def test_apply_plan_leaves_daynight_untouched_by_default(monkeypatch):
    # With no night_vision configured, apply_plan must never touch the camera's day/night
    # mode (a camera happily on its own "auto" stays there).
    FakeCam, tracking = _nightvision_fakecam()
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)
    cam = FakeCam()
    plan = daemon.plan_camera(_cam(), night=True, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.daynight == "unset"


def test_apply_plan_disables_vehicle_detection(monkeypatch):
    # The cameras should follow people, not cars. The C560WS auto-track follows any
    # AI-detected target, so leaving vehicle detection on makes the camera swing after
    # passing traffic. apply_plan re-asserts vehicle detection OFF each tick (mirror of
    # the person-detection self-heal).
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def __init__(self):
            self.vehicle = None
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setPersonDetection(self, enabled, sensitivity=False):
            pass
        def setVehicleDetection(self, enabled, sensitivity=False):
            self.vehicle = enabled
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    cam = FakeCam()
    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    daemon.apply_plan(cam, plan)
    assert cam.vehicle is False


def test_apply_plan_smarttrack_is_last_config_before_autotrack(monkeypatch):
    # Live evidence (2026-06-23): one of setMotionDetection/setPersonDetection/
    # setVehicleDetection/setPreset resets the camera's smart_track_info to ALL-OFF.
    # When apply_smarttrack ran FIRST (old order), the people-only filter set at night
    # was wiped by those later calls, leaving auto-track with no SmartTrack category, so
    # the camera followed any motion — including passing cars. apply_smarttrack must be
    # the LAST configuration call, immediately before ensure_autotrack (which must stay
    # truly last because setSmartTrackConfig clears the auto-track master switch).
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def __init__(self):
            self.order = []
        def executeFunction(self, name, *a, **k):
            if name == "setSmartTrackConfig":
                self.order.append("smarttrack")
        def setMotionDetection(self, sensitivity=False):
            self.order.append("motion")
        def setPersonDetection(self, enabled, sensitivity=False):
            self.order.append("person")
        def setVehicleDetection(self, enabled, sensitivity=False):
            self.order.append("vehicle")
        def setPreset(self, preset):
            self.order.append("preset")
        def setAutoTrackTarget(self, enabled):
            self.order.append("autotrack")
        def getAutoTrackTarget(self):
            return {"enabled": "on"}

    cam = FakeCam()
    # NIGHT plan so apply_smarttrack runs (only fires when plan.autotrack_on).
    plan = daemon.plan_camera(_cam(), night=True, rain_active=False)
    daemon.apply_plan(cam, plan)

    o = cam.order
    assert "smarttrack" in o and "autotrack" in o
    assert o.index("smarttrack") > o.index("vehicle")
    assert o.index("smarttrack") > o.index("person")
    assert o.index("smarttrack") > o.index("motion")
    assert o.index("autotrack") == len(o) - 1   # auto-track is truly last
    assert o.index("autotrack") > o.index("smarttrack")


def test_apply_plan_survives_camera_without_person_detection(monkeypatch):
    # Older firmware / pytapo without setPersonDetection must not break the tick.
    from tapo_monitor import tracking
    monkeypatch.setattr(tracking._time, "sleep", lambda _: None)

    class FakeCam:
        def executeFunction(self, *a, **k):
            pass
        def setMotionDetection(self, sensitivity=False):
            pass
        def setAutoTrackTarget(self, enabled):
            pass
        def getAutoTrackTarget(self):
            return {"enabled": "off"}

    plan = daemon.plan_camera(_cam(), night=False, rain_active=False)
    daemon.apply_plan(FakeCam(), plan)  # must not raise


# ── backoff_seconds ──────────────────────────────────────────────────────────

def test_backoff_doubles_from_base():
    assert daemon.backoff_seconds(1) == 60
    assert daemon.backoff_seconds(2) == 120
    assert daemon.backoff_seconds(3) == 240

def test_backoff_caps_at_30_min():
    assert daemon.backoff_seconds(10) == 1800
    assert daemon.backoff_seconds(100) == 1800

def test_backoff_zero_before_first_failure():
    assert daemon.backoff_seconds(0) == 0


# ── ping preflight / API backoff separation ──────────────────────────────────

def test_connect_camera_ping_failure_skips_login(monkeypatch):
    from tapo_monitor import camera
    monkeypatch.setattr(camera, "ping_reachable", lambda host: False)
    monkeypatch.setattr(
        camera, "tapo_factory",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not build API client")))
    state = daemon.MonitorState()

    client, err = daemon._connect_camera({}, state, now=100)(_cam())

    assert client is None
    assert isinstance(err, ConnectionError)
    assert state.network_reachable["c"] is False
    assert state.network_fails["c"] == 1
    assert state.connect_fails == {}


def test_connect_camera_ping_success_then_logs_in(monkeypatch):
    from tapo_monitor import camera
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: lambda: "CAM")
    monkeypatch.setattr(camera, "connect", lambda factory: (factory(), None))
    state = daemon.MonitorState()
    clients = {}

    client, err = daemon._connect_camera(clients, state, now=100)(_cam())

    assert (client, err) == ("CAM", None)
    assert clients == {"c": "CAM"}
    assert state.network_reachable["c"] is True


def test_connect_camera_still_pings_during_api_backoff(monkeypatch):
    from tapo_monitor import camera
    calls = []
    monkeypatch.setattr(camera, "ping_reachable", lambda host: calls.append(host) or True)
    state = daemon.MonitorState()
    state.connect_backoff_until["c"] = 1000

    assert daemon._connect_camera({}, state, now=100)(_cam()) == (None, "backoff")
    assert calls == ["203.0.113.10"]
    assert state.network_reachable["c"] is True


def test_connect_camera_success_after_failures_logs_recovery(monkeypatch, caplog):
    import logging

    from tapo_monitor import camera
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: lambda: "CAM")
    monkeypatch.setattr(camera, "connect", lambda factory: (factory(), None))
    state = daemon.MonitorState()
    state.connect_fails["c"] = 3

    with caplog.at_level(logging.INFO, logger="tapo_monitor.daemon"):
        client, err = daemon._connect_camera({}, state, now=100)(_cam())

    assert client == "CAM"
    assert state.connect_fails == {}
    assert "connect c succeeded after 3 failure(s)" in caplog.text


def test_connect_camera_api_failure_keeps_exponential_backoff(monkeypatch):
    from tapo_monitor import camera
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: lambda: None)
    monkeypatch.setattr(camera, "connect", lambda factory: (None, RuntimeError("bad auth")))
    state = daemon.MonitorState()

    client, err = daemon._connect_camera({}, state, now=100)(_cam())

    assert client is None
    assert isinstance(err, RuntimeError)
    assert state.network_reachable["c"] is True
    assert state.connect_fails["c"] == 1
    assert state.connect_backoff_until["c"] == 160


# ── resolve_secrets (monkeypatched env) ──────────────────────────────────────

def test_resolve_secrets_reads_env(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "tok123")
    monkeypatch.setenv("TG_CHAT", "555")
    monkeypatch.setenv("GROQ_KEY", "gk789")
    monkeypatch.setenv("FACES", "12:alice")
    app = cfg.load_config_from_dict({
        "telegram": {"token_env": "TG_TOKEN", "chat_id_env": "TG_CHAT"},
        "groq": {"api_key_env": "GROQ_KEY"},
        "faces": {"names_env": "FACES"},
        "cameras": [{"name": "a", "host": "203.0.113.10"}],
    })
    secrets = daemon.resolve_secrets(app)
    assert secrets == {"telegram_token": "tok123", "telegram_chat": "555",
                       "groq_key": "gk789", "face_names": {12: "alice"}}


def test_resolve_secrets_missing_env_is_empty(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    app = cfg.load_config_from_dict({
        "telegram": {"token_env": "MISSING_TOKEN", "chat_id_env": "MISSING_CHAT"},
        "groq": {},
        "cameras": [{"name": "a", "host": "203.0.113.10"}],
    })
    secrets = daemon.resolve_secrets(app)
    assert secrets == {"telegram_token": "", "telegram_chat": "", "groq_key": "",
                       "face_names": {}}


# ── run_monitor_pass (injected deps, fake camera) ────────────────────────────

class _FakeEventCam:
    """A fake camera returning a scripted list of getEvents() batches across ticks."""
    def __init__(self, batches):
        self._batches = list(batches)
    def getEvents(self):
        return self._batches.pop(0) if self._batches else []


def _no_snapshot(cfg):
    # snapshot returning None short-circuits enrich/notify (no network/ffmpeg)
    return lambda cam, event: None


def test_run_monitor_pass_advances_watermark_across_ticks():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    cam = _FakeEventCam([
        [{"start_time": 100, "event_type": "personDetection"}],
        [{"start_time": 250, "event_type": "personDetection"}],
    ])
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}

    daemon.run_monitor_pass(app, {"a": cam}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t")
    assert state.last_seen["a"] == 100

    daemon.run_monitor_pass(app, {"a": cam}, state, now=2, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t")
    assert state.last_seen["a"] == 250  # advanced; old events not re-alerted


def test_run_monitor_pass_skips_non_getevents_cameras():
    app = cfg.load_config_from_dict({"cameras": [
        {"name": "a", "host": "203.0.113.10", "detection": {"sources": ["onvif"]}},
    ]})
    cam = _FakeEventCam([[{"start_time": 100, "event_type": "personDetection"}]])
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t")
    assert "a" not in state.last_seen


def test_run_monitor_pass_skips_cameras_without_client():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t")
    assert state.last_seen == {}


# ── loop cadence: decoupled control vs fast event poll ───────────────────────

def test_control_due_first_tick_and_at_interval():
    assert daemon.control_due(None, 0, 60) is True       # never run yet
    assert daemon.control_due(0, 30, 60) is False         # 30s < 60s
    assert daemon.control_due(0, 60, 60) is True          # exactly due
    assert daemon.control_due(0, 100, 60) is True         # overdue


def test_loop_step_decouples_control_from_event_poll():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    calls = {"control": 0, "watchdog": 0, "monitor": 0, "drain": 0}

    def fake_control(app, now, connect):
        calls["control"] += 1
        connect(app.cameras[0])  # populate cam_clients like the real connect does

    def fake_watchdog(app, cc, state, *, now, secrets, night=True):
        calls["watchdog"] += 1

    def fake_monitor(app, cc, state, *, now, secrets, night=True):
        calls["monitor"] += 1

    def fake_drain(app, cc, state, *, now, secrets, night=True):
        calls["drain"] += 1

    def fake_connect_factory(cam_clients, state, now):
        def connect(c):
            cam_clients[c.name] = "client"
            return "client", None
        return connect

    state = daemon.MonitorState()
    cam_clients = {}
    kw = dict(secrets=secrets, control_interval=60, run_control=fake_control,
              watchdog=fake_watchdog, monitor=fake_monitor, drain=fake_drain,
              connect_factory=fake_connect_factory)

    last = daemon.loop_step(app, cam_clients, state, now=0, last_control=None, **kw)
    assert last == 0
    assert calls == {"control": 1, "watchdog": 1, "monitor": 1, "drain": 1}
    assert cam_clients == {"a": "client"}

    last = daemon.loop_step(app, cam_clients, state, now=4, last_control=last, **kw)
    assert last == 0
    assert calls == {"control": 1, "watchdog": 1, "monitor": 2, "drain": 2}
    assert cam_clients == {"a": "client"}     # reused, not cleared

    last = daemon.loop_step(app, cam_clients, state, now=60, last_control=last, **kw)
    assert last == 60
    assert calls == {"control": 2, "watchdog": 2, "monitor": 3, "drain": 3}


# ── update_stall (the daemon's own dead-man's switch) ────────────────────────

def test_stall_no_alert_before_threshold():
    state = daemon.MonitorState()
    ev, _ = daemon.update_stall(state, ok=False, now=1000, threshold=900)
    assert ev is None
    ev, _ = daemon.update_stall(state, ok=False, now=1500, threshold=900)
    assert ev is None  # 500s < 900s


def test_stall_one_alert_at_threshold():
    state = daemon.MonitorState()
    daemon.update_stall(state, ok=False, now=1000, threshold=900)
    ev, _ = daemon.update_stall(state, ok=False, now=1900, threshold=900)
    assert ev == "alert"


def test_stall_no_duplicate_alerts():
    state = daemon.MonitorState()
    daemon.update_stall(state, ok=False, now=1000, threshold=900)
    daemon.update_stall(state, ok=False, now=1900, threshold=900)   # alert
    ev, _ = daemon.update_stall(state, ok=False, now=3000, threshold=900)
    assert ev is None


def test_stall_recovery_after_alert():
    state = daemon.MonitorState()
    daemon.update_stall(state, ok=False, now=1000, threshold=900)
    daemon.update_stall(state, ok=False, now=1900, threshold=900)   # alert
    ev, _ = daemon.update_stall(state, ok=True, now=2000, threshold=900)
    assert ev == "recovered"


def test_stall_recovery_without_prior_alert_is_silent():
    state = daemon.MonitorState()
    daemon.update_stall(state, ok=False, now=1000, threshold=900)   # below threshold
    ev, _ = daemon.update_stall(state, ok=True, now=1100, threshold=900)
    assert ev is None


def test_stall_healthy_tick_is_noop():
    state = daemon.MonitorState()
    ev, _ = daemon.update_stall(state, ok=True, now=1000, threshold=900)
    assert ev is None


# ── stall_watchdog (outer-loop notification) ─────────────────────────────────

def _app_with_stall_threshold(seconds):
    return cfg.load_config_from_dict({
        "cameras": [{"name": "c", "host": "203.0.113.10"}],
        "alerts": {"stall_threshold": seconds},
    })


def test_stall_watchdog_alerts_once_then_recovers(monkeypatch):
    # The failure this exists for: loop_step raises every tick, so the per-camera
    # watchdog inside it never runs. This one is in the outer loop and must still speak.
    app = _app_with_stall_threshold(900)
    state = daemon.MonitorState()
    sent = []
    monkeypatch.setattr(daemon.notify, "send_text",
                        lambda tok, chat, text: (sent.append(text), True)[1])
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon.stall_watchdog(app, state, secrets, ok=False, now=1000)
    daemon.stall_watchdog(app, state, secrets, ok=False, now=1500)
    assert sent == []
    daemon.stall_watchdog(app, state, secrets, ok=False, now=1900)
    daemon.stall_watchdog(app, state, secrets, ok=False, now=2500)
    assert len(sent) == 1 and sent[0].startswith("🔴")

    daemon.stall_watchdog(app, state, secrets, ok=True, now=2600)
    assert len(sent) == 2 and sent[1].startswith("🟢")


def test_stall_watchdog_retries_undelivered_alert(monkeypatch):
    # A swallowed Telegram message must not permanently mute an outage whose whole
    # symptom is silence — the alert is due again on the next tick.
    app = _app_with_stall_threshold(900)
    state = daemon.MonitorState()
    attempts = []
    monkeypatch.setattr(daemon.notify, "send_text",
                        lambda tok, chat, text: (attempts.append(text), False)[1])
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon.stall_watchdog(app, state, secrets, ok=False, now=1000)
    daemon.stall_watchdog(app, state, secrets, ok=False, now=1900)   # undelivered
    daemon.stall_watchdog(app, state, secrets, ok=False, now=2000)   # must try again
    assert len(attempts) == 2


def test_stall_watchdog_never_raises_into_the_loop(monkeypatch):
    # It runs in the one place that survives a broken tick; it must not become the
    # reason that loop dies.
    app = _app_with_stall_threshold(900)
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.notify, "send_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")))

    daemon.stall_watchdog(app, state, {"telegram_token": "t", "telegram_chat": "c"},
                          ok=False, now=1000)
    daemon.stall_watchdog(app, state, {"telegram_token": "t", "telegram_chat": "c"},
                          ok=False, now=1900)   # would raise if unguarded


# ── update_outage (pure transitions) ─────────────────────────────────────────

def test_outage_no_alert_before_threshold():
    state = daemon.MonitorState()
    ev, _ = daemon.update_outage(state, "a", ok=False, now=1000, threshold=900)
    assert ev is None
    ev, _ = daemon.update_outage(state, "a", ok=False, now=1500, threshold=900)
    assert ev is None  # 500s < 900s


def test_outage_one_alert_at_threshold():
    state = daemon.MonitorState()
    daemon.update_outage(state, "a", ok=False, now=1000, threshold=900)
    ev, _ = daemon.update_outage(state, "a", ok=False, now=1900, threshold=900)
    assert ev == "alert"


def test_outage_no_duplicate_alerts():
    state = daemon.MonitorState()
    daemon.update_outage(state, "a", ok=False, now=1000, threshold=900)
    daemon.update_outage(state, "a", ok=False, now=1900, threshold=900)  # alert
    ev, _ = daemon.update_outage(state, "a", ok=False, now=3000, threshold=900)
    assert ev is None  # already alerted


def test_outage_recovery_after_alert():
    state = daemon.MonitorState()
    daemon.update_outage(state, "a", ok=False, now=1000, threshold=900)
    daemon.update_outage(state, "a", ok=False, now=1900, threshold=900)  # alert
    ev, _ = daemon.update_outage(state, "a", ok=True, now=2000, threshold=900)
    assert ev == "recovered"
    assert "a" not in state.fail_since
    assert "a" not in state.outage_alerted


def test_outage_recovery_without_prior_alert_is_silent():
    state = daemon.MonitorState()
    daemon.update_outage(state, "a", ok=False, now=1000, threshold=900)  # below threshold
    ev, _ = daemon.update_outage(state, "a", ok=True, now=1100, threshold=900)
    assert ev is None  # never alerted, so no 🟢 spam


def test_outage_ok_camera_is_noop():
    state = daemon.MonitorState()
    ev, _ = daemon.update_outage(state, "a", ok=True, now=1000, threshold=900)
    assert ev is None
    assert state.fail_since == {}


def test_outage_tracks_observed_uptime_and_recovery_metrics():
    state = daemon.MonitorState()
    daemon.update_outage(state, "a", ok=True, now=100, threshold=900)
    daemon.update_outage(state, "a", ok=True, now=200, threshold=900)
    daemon.update_outage(state, "a", ok=False, now=300, threshold=900)

    assert state.online_since["a"] == 100
    assert state.last_success["a"] == 200
    assert state.last_observed_uptime["a"] == 200
    assert state.total_observed_online["a"] == 200

    daemon.update_outage(state, "a", ok=False, now=1200, threshold=900)
    ev, _ = daemon.update_outage(state, "a", ok=True, now=1300, threshold=900)

    assert ev == "recovered"
    assert state.online_since["a"] == 1300
    assert state.last_outage_duration["a"] == 1000
    assert state.total_observed_offline["a"] == 1000
    assert state.reconnect_count["a"] == 1


# ── detection cooldown wired into run_monitor_pass ────────────────────────────

class _CountingNotify:
    """Capture send_photo calls so we can assert the cooldown rate-limits bursts."""
    def __init__(self):
        self.photos = 0
    def send_photo(self, *a, **k):
        self.photos += 1
        return True


def test_run_monitor_pass_cooldown_rate_limits(monkeypatch):
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.notify, "is_empty_scene", lambda d: False)

    app = cfg.load_config_from_dict({
        "alerts": {"cooldown": 120},
        "cameras": [{"name": "a", "host": "203.0.113.10", "enrich": {"groq": False}}],
    })
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": ""}

    def snap(cfg):  # always returns an image path
        return lambda cam, event: "/tmp/x.jpg"

    # first tick: a detection -> one alert
    cam = _FakeEventCam([
        [{"start_time": 100, "event_type": "personDetection"}],
        [{"start_time": 200, "event_type": "personDetection"}],
        [{"start_time": 5000, "event_type": "personDetection"}],
    ])
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    assert counter.photos == 1

    # second tick within cooldown: detection present but suppressed
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1050, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    assert counter.photos == 1

    # third tick after cooldown: alert allowed again
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1200, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    assert counter.photos == 2


# ── motion funnel: strict motion alerts only on Groq-confirmed person/animal ──

def _motion_app():
    return cfg.load_config_from_dict({
        "cameras": [{"name": "a", "host": "203.0.113.10"}],  # strict_people defaults True
    })


def _run_motion_once(monkeypatch, groq_reply):
    """Feed one bare-motion event, script Groq's reply, return photos sent."""
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: groq_reply)

    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 2}]])  # bare motion, no person bit

    def snap(cfg):
        return lambda cam, event: "/tmp/x.jpg"

    daemon.run_monitor_pass(_motion_app(), {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    return counter.photos


def test_motion_alerts_when_groq_sees_person(monkeypatch):
    assert _run_motion_once(monkeypatch, "a man in a dark coat walking left") == 1


def test_motion_alerts_when_groq_sees_animal(monkeypatch):
    assert _run_motion_once(monkeypatch, "a cat crossing the driveway") == 1


def test_motion_dropped_when_groq_reports_empty(monkeypatch):
    # A lone vehicle / empty frame -> Groq says "empty scene" -> no person/animal -> drop.
    assert _run_motion_once(monkeypatch, "empty scene") == 0


def test_motion_alerts_on_clothing_without_person_noun(monkeypatch):
    # Regression (the 11:18 miss): Groq often describes a person by clothing/behaviour
    # without ever using a person noun. We must trust the prompt (non-empty = a person
    # or animal) and alert, not require a magic word.
    assert _run_motion_once(
        monkeypatch, "grey hoodie, black pants, white shoes, walking away towards a white car"
    ) == 1


# ── camera-confirmed person: trust the camera, always send (Groq = caption only) ─

def _run_person_once(monkeypatch, groq_reply):
    """Feed one camera-confirmed person event (AI person bit), return photos sent."""
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: groq_reply)

    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 524288}]])  # PERSON_BIT

    def snap(cfg):
        return lambda cam, event: "/tmp/x.jpg"

    daemon.run_monitor_pass(_motion_app(), {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    return counter.photos


# ── corroboration wired into the live monitor pass ───────────────────────────

def _corroboration_app(motion_send=0.6, sd_snapshot=False):
    scorer = {"url": "http://127.0.0.1:1/score", "threshold": 0.3}
    if motion_send is not None:
        scorer["motion_send_threshold"] = motion_send
    camera = {
        "name": "a", "host": "203.0.113.10",
        "sampler": {"enabled": True, "interval": 30, "max_frames": 6, "group_gap": 90},
        "scorer": scorer,
    }
    if sd_snapshot:
        camera["sd_snapshot"] = True
    return cfg.load_config_from_dict({"groq": {}, "cameras": [camera]})


def _run_marginal_motion_pass(monkeypatch, motion_send):
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: "x")
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.4, "animal": 0.0})
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 2}]])   # bare non-PIR motion
    daemon.run_monitor_pass(_corroboration_app(motion_send), {"a": cam}, state,
                            now=1000, secrets=secrets,
                            snapshot_for=lambda c: (lambda cam, ev: "/tmp/x.jpg"),
                            time_str=lambda e: "t")
    return counter.photos, state


def test_run_monitor_pass_holds_first_marginal_motion(monkeypatch):
    photos, state = _run_marginal_motion_pass(monkeypatch, motion_send=0.6)
    assert photos == 0                                   # held, not sent
    assert state.groups["a"]["motion_candidates"] == 1   # candidate recorded


def test_run_monitor_pass_sends_marginal_when_feature_off(monkeypatch):
    photos, _ = _run_marginal_motion_pass(monkeypatch, motion_send=None)
    assert photos == 1                                   # legacy: 0.4 >= threshold -> send


def test_corroborated_motion_sends_on_the_next_sampler_frame(monkeypatch):
    # End to end across two ticks: the live pass holds a marginal non-PIR motion frame and
    # the candidate survives on the group, so the sampler's next marginal frame corroborates
    # it and the alert goes out. Unit tests cover the two halves separately; this ties the
    # shared group state together.
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: "x")
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.4, "animal": 0.0})
    monkeypatch.setattr(daemon, "_safe_unlink", lambda p: None)
    app = _corroboration_app(motion_send=0.6)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k", "face_names": {}}
    cam = _FakeEventCam([[{"start_time": 1000, "events_1": 2}]])   # bare non-PIR motion

    def snap(_cfg):
        return lambda cam_, ev: "/tmp/x.jpg"

    daemon.run_monitor_pass(app, {"a": cam}, state, now=1005, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    assert counter.photos == 0                          # tick 1: single frame is held
    assert state.groups["a"]["motion_candidates"] == 1

    daemon.process_sampler(app, {"a": cam}, state, now=1040, secrets=secrets,
                           snapshot_for=snap, time_str=lambda e: "t")
    assert counter.photos == 1                          # tick 2: corroborated -> sent
    g = state.groups["a"]
    assert g["motion_candidates"] == 2
    assert g["sent"] is True and g["delivered"] is True


def _run_confirmed_person_with_open_group(monkeypatch, sent, delivered=False,
                                          last_event_at=995):
    """One confirmed-person event, empty-scoring live frame, on an sd_snapshot camera
    with an open group for "a" already marked ``sent``. Returns (photos, state)."""
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.1, "animal": 0.0})
    app = _corroboration_app(sd_snapshot=True)
    state = daemon.MonitorState()
    state.groups["a"] = {
        "camera": "a", "etype": "motion", "event": {"start_time": 90},
        "started": 90, "last_event_at": last_event_at, "frames": 0,
        "next_due": 10**9, "sent": sent, "delivered": delivered,
    }
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 524288}]])
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=lambda c: (lambda cam, ev: "/tmp/x.jpg"),
                            time_str=lambda e: "t")
    return counter.photos, state


def test_run_monitor_pass_skips_sd_followup_when_burst_delivered(monkeypatch):
    # A bare-motion frame of the same passage already alerted seconds earlier: the
    # confirmed-but-empty-live person must not queue a duplicate SD follow-up.
    photos, state = _run_confirmed_person_with_open_group(monkeypatch, sent=True,
                                                          delivered=True)
    assert photos == 0
    assert state.pending_sd == []


def test_run_monitor_pass_defers_when_burst_unsent(monkeypatch):
    # No prior alert for this burst: the SD follow-up is still queued as before.
    photos, state = _run_confirmed_person_with_open_group(monkeypatch, sent=False)
    assert photos == 0
    assert len(state.pending_sd) == 1


def test_run_monitor_pass_defers_when_burst_sent_but_undelivered(monkeypatch):
    # ``sent`` also covers "queued for the SD follow-up" and "Telegram refused the
    # photo" — neither delivered anything, so a camera-confirmed person must still get
    # its own follow-up instead of being silently dropped.
    photos, state = _run_confirmed_person_with_open_group(monkeypatch, sent=True,
                                                          delivered=False)
    assert photos == 0
    assert len(state.pending_sd) == 1


def test_run_monitor_pass_defers_when_delivered_group_is_stale(monkeypatch):
    # The delivered alert belongs to an older burst (last event past group_gap=90):
    # this person is new information, not a duplicate.
    photos, state = _run_confirmed_person_with_open_group(monkeypatch, sent=True,
                                                          delivered=True,
                                                          last_event_at=880)
    assert photos == 0
    assert len(state.pending_sd) == 1


def test_run_monitor_pass_marks_group_delivered_on_live_send(monkeypatch):
    # Wiring: a delivered live alert records ``delivered`` on the group, which is what
    # the SD-follow-up dedupe guard reads.
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: "Person")
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.9, "animal": 0.0})
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 524288}]])
    daemon.run_monitor_pass(_corroboration_app(), {"a": cam}, state, now=1000,
                            secrets=secrets,
                            snapshot_for=lambda c: (lambda cam, ev: "/tmp/x.jpg"),
                            time_str=lambda e: "t")
    assert counter.photos == 1
    assert state.groups["a"]["delivered"] is True


# ── scorer retry before passthrough ──────────────────────────────────────────

def test_score_for_retries_once_before_passthrough(monkeypatch):
    cam_cfg = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "scorer": {"url": "http://x/score", "threshold": 0.3}}]}).cameras[0]
    calls = []

    def fake_score_image(url, path, timeout=10, tiles=1):
        calls.append(1)
        return None if len(calls) == 1 else {"person": 0.9, "animal": 0.0}

    monkeypatch.setattr(daemon.scorer, "score_image", fake_score_image)
    monkeypatch.setattr(daemon._time, "sleep", lambda *_: None)
    s = daemon.score_for(cam_cfg)("/tmp/x.jpg")
    assert len(calls) == 2           # retried once
    assert float(s) == 0.9           # second attempt's value used


def _fake_clock(monkeypatch, values):
    """Feed ``daemon._time.monotonic`` fixed readings (no real sleeping/waiting)."""
    clock = iter(values)
    real = daemon._time.monotonic
    monkeypatch.setattr(daemon._time, "monotonic", lambda: next(clock, real()))


def test_score_for_skips_retry_when_first_attempt_hung(monkeypatch):
    # A hung scorer already cost a full timeout; retrying doubles that for every scored
    # frame of every tick, so only a fast (connection-refused-style) failure is retried.
    cam_cfg = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "scorer": {"url": "http://x/score", "timeout": 10}}]}).cameras[0]
    calls, slept = [], []
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda *a, **k: calls.append(1) or None)
    monkeypatch.setattr(daemon._time, "sleep", lambda d: slept.append(d))
    _fake_clock(monkeypatch, [0.0, 10.5])       # first attempt ran past the timeout

    assert daemon.score_for(cam_cfg)("/tmp/x.jpg") is None
    assert len(calls) == 1                      # no second full-timeout attempt
    assert slept == []


def test_score_for_logs_retry_after_fast_failure(monkeypatch, caplog):
    cam_cfg = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "scorer": {"url": "http://x/score", "timeout": 10}}]}).cameras[0]
    calls = []
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda *a, **k: calls.append(1) or None)
    monkeypatch.setattr(daemon._time, "sleep", lambda *_: None)
    _fake_clock(monkeypatch, [0.0, 0.2])        # refused instantly -> a blip, retry it

    with caplog.at_level("INFO", logger="tapo_monitor.daemon"):
        assert daemon.score_for(cam_cfg)("/tmp/x.jpg") is None
    assert len(calls) == 2
    assert "scorer retry" in caplog.text        # a flaky scorer stays visible


def test_score_for_returns_none_after_two_failures(monkeypatch):
    cam_cfg = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "scorer": {"url": "http://x/score"}}]}).cameras[0]
    calls = []
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda *a, **k: calls.append(1) or None)
    monkeypatch.setattr(daemon._time, "sleep", lambda *_: None)
    assert daemon.score_for(cam_cfg)("/tmp/x.jpg") is None
    assert len(calls) == 2           # tried twice, then gave up


def test_confirmed_person_sent_even_when_groq_empty(monkeypatch):
    # The 08:49 miss: camera confirmed a person but the (stale) RTSP frame looked empty
    # to Groq, so we dropped a real person. Trust the camera: send anyway.
    assert _run_person_once(monkeypatch, "empty scene") == 1


def test_confirmed_person_sent_with_description(monkeypatch):
    assert _run_person_once(monkeypatch, "a man in a dark coat walking left") == 1


# ── per-type cooldown: motion must not suppress a person; person quiets motion ─

def _two_tick_pass(monkeypatch, first_event, second_event):
    """Run two ticks 5s apart with the same camera; return number of photos sent."""
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: "a man walking")
    app = cfg.load_config_from_dict({
        "alerts": {"cooldown": 120},
        "cameras": [{"name": "a", "host": "203.0.113.10"}],
    })
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[first_event], [second_event]])

    def snap(cfg):
        return lambda c, e: "/tmp/x.jpg"

    daemon.run_monitor_pass(app, {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1005, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    return counter.photos


def test_motion_alert_does_not_suppress_following_person(monkeypatch):
    # The live leak: a motion alert armed the shared cooldown and ate a real person
    # seconds later. Person must alert regardless of a recent motion alert.
    photos = _two_tick_pass(
        monkeypatch,
        {"start_time": 100, "events_1": 2},        # bare motion -> motion alert
        {"start_time": 200, "events_1": 524288},   # confirmed person 5s later
    )
    assert photos == 2


def test_person_alert_suppresses_following_motion(monkeypatch):
    # No duplicate of the same walk: a person alert quiets motion within the cooldown.
    photos = _two_tick_pass(
        monkeypatch,
        {"start_time": 100, "events_1": 524288},   # confirmed person -> alert
        {"start_time": 200, "events_1": 2},         # motion 5s later (same person)
    )
    assert photos == 1


# ── snapshot retry: a slow Pi's transient RTSP failure must not lose a person ──

def _run_with_snapshots(monkeypatch, snapshot_results):
    """Run one person event; snapshot() yields snapshot_results in order. Returns
    (photos_sent, snapshot_call_count)."""
    from tapo_monitor import monitor as mon
    counter = _CountingNotify()
    monkeypatch.setattr(mon.notify, "send_photo", counter.send_photo)
    monkeypatch.setattr(mon.enrich, "groq_describe", lambda *a, **k: "a man walking")
    calls = {"n": 0}

    def snap(cfg):
        def _s(cam, event):
            i = calls["n"]
            calls["n"] += 1
            return snapshot_results[i] if i < len(snapshot_results) else snapshot_results[-1]
        return _s

    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 524288}]])  # confirmed person
    daemon.run_monitor_pass(_motion_app(), {"a": cam}, state, now=1000, secrets=secrets,
                            snapshot_for=snap, time_str=lambda e: "t")
    return counter.photos, calls["n"]


def test_snapshot_retried_once_then_succeeds(monkeypatch):
    # First RTSP grab fails (None), retry succeeds -> person still reaches Telegram.
    photos, attempts = _run_with_snapshots(monkeypatch, [None, "/tmp/x.jpg"])
    assert photos == 1
    assert attempts == 2  # failed once, retried once


def test_snapshot_failure_after_retry_drops(monkeypatch):
    # Both attempts fail -> drop (no infinite retry), exactly two attempts.
    photos, attempts = _run_with_snapshots(monkeypatch, [None, None])
    assert photos == 0
    assert attempts == 2


# ── _default_snapshot builds the URL from config, not the cam object ──────────

def test_default_snapshot_uses_config_rtsp_credentials(monkeypatch):
    monkeypatch.setenv("CAM_RTSP_USER", "rtspadmin")
    monkeypatch.setenv("CAM_RTSP_PASSWORD", "rtsppw")
    camcfg = _cam(
        host="192.0.2.50",
        rtsp_user_env="CAM_RTSP_USER",
        rtsp_password_env="CAM_RTSP_PASSWORD",
        rtsp_port=8554,
        rtsp_stream="stream2",
    )
    captured = {}

    def fake_capture(url, **kwargs):
        captured["url"] = url
        return "/tmp/snap.jpg"

    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", fake_capture)
    snap = daemon._default_snapshot(camcfg)
    # cam object has NO user/password/host attributes — must not be relied upon.
    result = snap(object(), {"start_time": 1})
    assert result == "/tmp/snap.jpg"
    assert captured["url"] == "rtsp://rtspadmin:rtsppw@192.0.2.50:8554/stream2"


def test_default_snapshot_empty_creds_when_env_missing(monkeypatch):
    monkeypatch.delenv("MISSING_RTSP_USER", raising=False)
    monkeypatch.delenv("MISSING_RTSP_PW", raising=False)
    camcfg = _cam(
        host="192.0.2.51",
        rtsp_user_env="MISSING_RTSP_USER",
        rtsp_password_env="MISSING_RTSP_PW",
    )
    captured = {}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kwargs: captured.setdefault("url", url))
    snap = daemon._default_snapshot(camcfg)
    snap(object(), {"start_time": 1})
    # defaults: port 554, stream1; empty credentials
    assert captured["url"] == "rtsp://:@192.0.2.51:554/stream1"


def test_default_snapshot_does_not_use_recorder_fallback_by_default(monkeypatch):
    camcfg = _cam(host="192.0.2.52")
    called = {"recorder": False}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda url, **kwargs: None)

    def fake_latest(*args, **kwargs):
        called["recorder"] = True
        return "/tmp/rec.jpg"

    monkeypatch.setattr(daemon.snapshot, "latest_recording_frame", fake_latest)

    assert daemon._default_snapshot(camcfg)(object(), {"start_time": 1}) is None
    assert called["recorder"] is False


def test_default_snapshot_uses_recorder_fallback_when_enabled(monkeypatch):
    camcfg = _cam(host="192.0.2.52")
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda url, **kwargs: None)
    monkeypatch.setattr(
        daemon.snapshot, "latest_recording_frame", lambda host, **kwargs: f"/tmp/{host}.jpg"
    )

    snap = daemon._default_snapshot(camcfg, recorder_fallback=True)

    assert snap(object(), {"start_time": 1}) == "/tmp/192.0.2.52.jpg"


# ── alert_gate: module-level cooldown helper ──────────────────────────────────

def test_alert_gate_confirmed_gated_only_by_confirmed():
    state = daemon.MonitorState()
    can, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    assert can("person") is True          # nothing sent yet
    on("person")                          # records confirmed at now=1000
    can2, _ = daemon.alert_gate(state, "a", cooldown=120, now=1050)
    assert can2("person") is False        # within cooldown of a confirmed alert


def test_alert_gate_gates_delayed_same_event_burst_by_camera_time():
    state = daemon.MonitorState()
    can, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    first = {"start_time": 1783598881}
    assert can("person", first) is True
    on("person", first)

    # Tapo may emit another person event one second later but the daemon only sees it
    # minutes later after SD work. It is still the same camera-side passage.
    can2, _ = daemon.alert_gate(state, "a", cooldown=120, now=1300)
    assert can2("person", {"start_time": 1783598889}) is False


def test_alert_gate_allows_later_camera_event_after_event_cooldown():
    state = daemon.MonitorState()
    can, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    first = {"start_time": 1783598881}
    on("person", first)

    can2, _ = daemon.alert_gate(state, "a", cooldown=120, now=1300)
    assert can2("person", {"start_time": 1783599244}) is True


def test_alert_gate_motion_does_not_block_person():
    state = daemon.MonitorState()
    _, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    on("motion")                          # a motion alert at now=1000
    can, _ = daemon.alert_gate(state, "a", cooldown=120, now=1050)
    assert can("person") is True          # motion never silences a person
    assert can("motion") is False         # but motion silences motion


# ── pan_limit (soft PTZ bound) ────────────────────────────────────────────────

def _raw_cam_dict():
    return {"name": "camera-a", "host": "203.0.113.10", "pan_limit": {
        "enabled": True, "margin": 0.01, "poll_interval": 6,
        "onvif_user_env": "U", "onvif_password_env": "P"}}


def _patch_panlimit(monkeypatch, pan_x, gotos):
    monkeypatch.setattr(daemon.panlimit, "build_ptz", lambda *a, **k: ("PTZ", "tok"))
    monkeypatch.setattr(daemon.panlimit, "read_preset_bounds",
                        lambda ptz, tok: (0.39, "1", 0.61, "3"))
    monkeypatch.setattr(daemon.panlimit, "read_pan_x", lambda ptz, tok: pan_x)
    monkeypatch.setattr(daemon.panlimit, "goto_preset",
                        lambda ptz, tok, target: gotos.append(target))


def test_pan_guard_recalls_when_past_right_bound(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [_raw_cam_dict()]})
    gotos = []
    _patch_panlimit(monkeypatch, pan_x=0.63, gotos=gotos)   # past the wall
    state = daemon.MonitorState()
    daemon._pan_guard_pass(app, {}, state, now=100, secrets={}, night=True)
    assert gotos == ["3"]                                    # recalled to Maximalne


def test_pan_guard_noop_within_bounds(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [_raw_cam_dict()]})
    gotos = []
    _patch_panlimit(monkeypatch, pan_x=0.58, gotos=gotos)   # mid-range
    state = daemon.MonitorState()
    daemon._pan_guard_pass(app, {}, state, now=100, secrets={}, night=True)
    assert gotos == []


def test_pan_guard_throttles_by_poll_interval(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [_raw_cam_dict()]})
    calls = []
    monkeypatch.setattr(daemon.panlimit, "build_ptz", lambda *a, **k: ("PTZ", "tok"))
    monkeypatch.setattr(daemon.panlimit, "read_preset_bounds",
                        lambda ptz, tok: (0.39, "1", 0.61, "3"))
    monkeypatch.setattr(daemon.panlimit, "read_pan_x",
                        lambda ptz, tok: calls.append(1) or 0.58)
    monkeypatch.setattr(daemon.panlimit, "goto_preset", lambda *a: None)
    state = daemon.MonitorState()
    daemon._pan_guard_pass(app, {}, state, now=100, secrets={}, night=True)
    daemon._pan_guard_pass(app, {}, state, now=103, secrets={}, night=True)  # within 6s
    daemon._pan_guard_pass(app, {}, state, now=107, secrets={}, night=True)  # past 6s
    assert len(calls) == 2


def test_pan_guard_disabled_does_nothing(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [{"name": "c", "host": "203.0.113.10"}]})
    monkeypatch.setattr(daemon.panlimit, "build_ptz",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not build")))
    daemon._pan_guard_pass(app, {}, daemon.MonitorState(), now=1, secrets={}, night=True)


# ── crop_to_subject (zoom) ────────────────────────────────────────────────────

def test_compute_crop_pads_and_centres_on_box():
    # small box (100..140 x, 100..200 y) in a 1000x1000 frame -> padded, min-size crop
    rect = daemon.compute_crop([100, 100, 140, 200], 1000, 1000, pad=0.4, min_frac=0.22)
    x, y, cw, ch = rect
    assert cw >= 220 and ch >= 220               # enforced min 22% of frame
    assert x <= 120 <= x + cw and y <= 150 <= y + ch   # box centre inside the crop
    assert 0 <= x and x + cw <= 1000 and 0 <= y and y + ch <= 1000


def test_compute_crop_none_when_subject_fills_frame():
    assert daemon.compute_crop([0, 0, 900, 900], 1000, 1000, skip_frac=0.55) is None


def test_compute_crop_clamps_to_frame_edges():
    rect = daemon.compute_crop([0, 0, 30, 40], 1000, 1000)   # box in the corner
    x, y, cw, ch = rect
    assert x == 0 and y == 0 and x + cw <= 1000 and y + ch <= 1000


def test_crop_for_subject_crops_to_box(tmp_path):
    cam = _cam(crop_to_subject=True, scorer={"url": "http://x/score", "tiles": 2})
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    result = {"person": 0.9, "box": [100, 100, 200, 300], "w": 1000, "h": 800}
    captured = {}
    def fake_ffmpeg(image, out_path, rect):
        captured["rect"] = rect
        open(out_path, "w").write("crop")
    out = daemon.crop_for_subject(cam, str(src), str(tmp_path),
                                  score_result=result, run_ffmpeg=fake_ffmpeg)
    assert out != str(src) and out.endswith(".jpg")
    assert captured["rect"][2] > 0 and captured["rect"][3] > 0


def test_crop_for_subject_returns_original_without_box(tmp_path):
    cam = _cam(crop_to_subject=True, scorer={"url": "http://x/score"})
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    out = daemon.crop_for_subject(cam, str(src), str(tmp_path),
                                  score_result={"person": 0.1, "box": None},
                                  run_ffmpeg=lambda *a: None)
    assert out == str(src)


def test_crop_for_subject_noop_when_disabled(tmp_path):
    cam = _cam(scorer={"url": "http://x/score"})     # crop_to_subject defaults False
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    out = daemon.crop_for_subject(cam, str(src), str(tmp_path),
                                  score_result={"box": [1, 2, 3, 4], "w": 100, "h": 100})
    assert out == str(src)


# ── send_alert_photo (crop goes out, whole scene is archived) ────────────────

def test_send_alert_photo_archives_uncropped_scene(monkeypatch, tmp_path):
    # The zoom is what the user wants in Telegram; the sent log needs the scene, or a
    # false positive on an empty yard is unreviewable — it archives a blurry close-up.
    cam = _cam(crop_to_subject=True, scorer={"url": "http://x/score", "tiles": 2})
    full = tmp_path / "frame.jpg"
    full.write_bytes(b"\xff\xd8WHOLE-SCENE")
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"\xff\xd8ZOOM")
    monkeypatch.setattr(daemon, "crop_for_subject", lambda *a, **k: str(crop))
    posted = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (posted.append(req.data), _Resp(b'{"ok":true}'))[1])
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))

    ok = daemon.send_alert_photo(cam, {"telegram_token": "t", "telegram_chat": "c"},
                                 str(full), "caption")

    assert ok is True
    assert b"ZOOM" in posted[0]                     # Telegram got the zoom
    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8WHOLE-SCENE"


def test_default_snapshot_returns_delivery_frame_carrying_its_native_twin(monkeypatch, tmp_path):
    # The snapshot factory feeds *every* consumer (live scorer, sampler, Groq, Telegram).
    # Handing them a native frame made all of them pay 4K; only the crop wants it. So the
    # frame handed out is delivery width, with the native original riding along for the crop.
    native = tmp_path / "native.jpg"
    native.write_bytes(b"4k")
    scene = tmp_path / "scene.jpg"
    scene.write_bytes(b"1280")
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda url, **kw: str(native))
    monkeypatch.setattr(daemon.snapshot, "image_size", lambda p: (3840, 2160))
    monkeypatch.setattr(daemon, "_reduced", lambda src, out_dir, run=None, width=None: str(scene))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    out = daemon._default_snapshot(_cam(crop_to_subject=True, crop_from_native=True))(None, None)

    assert str(out) == str(scene)              # consumers see the delivery-width frame
    assert out.native == str(native)           # the crop can still reach the full detail
    assert out.native_width == 3840


def test_tick_reports_a_failing_loop_step_to_the_watchdog(monkeypatch):
    # The wiring this whole feature rests on: a raising loop_step must be what feeds the
    # stall watchdog. Isolated tests of update_stall cannot catch this being mis-wired.
    app = _app_with_stall_threshold(900)
    state = daemon.MonitorState()
    sent = []
    monkeypatch.setattr(daemon.notify, "send_text",
                        lambda tok, chat, text: (sent.append(text), True)[1])
    monkeypatch.setattr(daemon, "loop_step",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken tick")))
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    for now in (1000, 1500, 1900, 2500):
        daemon.tick(app, {}, state, now=now, secrets=secrets,
                    last_control=None, control_interval=60)

    assert len(sent) == 1 and sent[0].startswith("🔴")


def test_tick_returns_last_control_from_a_healthy_loop_step(monkeypatch):
    app = _app_with_stall_threshold(900)
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon, "loop_step", lambda *a, **k: 4242)

    out = daemon.tick(app, {}, state, now=1000, secrets={"telegram_token": "t",
                                                         "telegram_chat": "c"},
                      last_control=None, control_interval=60)

    assert out == 4242
    assert state.tick_fail_since is None


def test_safe_unlink_removes_the_native_twin_too(tmp_path):
    # Every consumer already funnels its frame through _safe_unlink; carrying the twin
    # there is what keeps a sampler that drops 5 of 6 frames from leaking 5 native files.
    native = tmp_path / "native.jpg"
    native.write_bytes(b"4k")
    scene = tmp_path / "scene.jpg"
    scene.write_bytes(b"1280")

    daemon._safe_unlink(snapshot.Frame(str(scene), native=str(native)))

    assert not scene.exists() and not native.exists()


def test_send_alert_photo_crops_from_the_native_twin(monkeypatch, tmp_path):
    # No config flag is consulted here: the frame either carries a native original or not.
    cam = _cam(crop_to_subject=True, scorer={"url": "http://x/score", "tiles": 2})
    native = tmp_path / "native.jpg"
    native.write_bytes(b"\xff\xd8NATIVE")
    scene = tmp_path / "scene.jpg"
    scene.write_bytes(b"\xff\xd8SCENE")
    seen = {}

    def fake_crop(cfg, image, out_dir, secrets=None, **kw):
        seen.update(image=image, source=kw.get("source"), width=kw.get("source_width"))
        crop = tmp_path / "crop.jpg"
        crop.write_bytes(b"\xff\xd8CROP")
        return str(crop)

    monkeypatch.setattr(daemon, "crop_for_subject", fake_crop)
    monkeypatch.setattr(daemon, "_reduced", lambda *a, **k: None)   # crop already narrow
    posted = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (posted.append(req.data), _Resp(b'{"ok":true}'))[1])
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))

    frame = snapshot.Frame(str(scene), native=str(native), native_width=3840)
    ok = daemon.send_alert_photo(cam, {"telegram_token": "t", "telegram_chat": "c"},
                                 frame, "caption")

    assert ok is True
    assert seen == {"image": str(scene), "source": str(native), "width": 3840}
    assert b"CROP" in posted[0]
    assert list(archive.glob("*.jpg"))[0].read_bytes() == b"\xff\xd8SCENE"


def test_default_snapshot_keeps_native_resolution_for_crop_cameras(monkeypatch, tmp_path):
    # The crop is only worth taking from a frame that still has the detail in it.
    seen = {}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kw: seen.update(kw) or str(tmp_path / "f.jpg"))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    daemon._default_snapshot(_cam(crop_to_subject=True, crop_from_native=True))(None, None)
    assert seen["scale"] is False


def test_default_snapshot_scales_for_crop_camera_without_native_opt_in(monkeypatch, tmp_path):
    # A native grab is measurably slower on a weak machine (~+0.5s on a Pi Zero, plus a
    # probe and a reduction), so cropping alone must not opt a camera into it.
    seen = {}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kw: seen.update(kw) or str(tmp_path / "f.jpg"))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    daemon._default_snapshot(_cam(crop_to_subject=True))(None, None)
    assert seen["scale"] is True


def test_default_snapshot_scales_when_not_cropping(monkeypatch, tmp_path):
    # Every other camera keeps paying the cheap downscale at capture, as before.
    seen = {}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kw: seen.update(kw) or str(tmp_path / "f.jpg"))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    daemon._default_snapshot(_cam())(None, None)
    assert seen["scale"] is True


def test_reduced_skips_images_already_within_delivery_width(monkeypatch, tmp_path):
    # A crop is usually narrower than the delivery width; running it through the scaler
    # would upscale it, adding no detail and inflating the file (measured 74kB -> 122kB).
    src = tmp_path / "crop.jpg"
    src.write_bytes(b"jpeg")
    monkeypatch.setattr(daemon.snapshot, "image_width", lambda p: 842)
    ran = []

    assert daemon._reduced(str(src), str(tmp_path), lambda s, o: ran.append(s)) is None
    assert ran == []


def test_reduced_still_scales_a_wide_image(monkeypatch, tmp_path):
    src = tmp_path / "native.jpg"
    src.write_bytes(b"jpeg")
    monkeypatch.setattr(daemon.snapshot, "image_width", lambda p: 3840)

    out = daemon._reduced(str(src), str(tmp_path), lambda s, o: open(o, "w").write("small"))
    assert out and out != str(src)


def test_crop_for_subject_scales_box_into_source_frame(tmp_path):
    # Scoring a 4K frame costs the shared scorer 2-3x (measured 4.7-7.0s vs 2.4s) for a
    # decision it makes at 640px anyway. So the box is found on the downscaled copy and
    # scaled back up to crop the native frame — same grab, so no time skew between them.
    cam = _cam(crop_to_subject=True, crop_from_native=True,
               scorer={"url": "http://x/score", "tiles": 2})
    small = tmp_path / "small.jpg"
    small.write_bytes(b"jpeg-1280")
    native = tmp_path / "native.jpg"
    native.write_bytes(b"jpeg-3840")
    scored = {"person": 0.9, "box": [100, 100, 200, 300], "w": 1280, "h": 720}
    captured = {}

    def fake_ffmpeg(image, out_path, rect):
        captured["image"] = image
        captured["rect"] = rect
        open(out_path, "w").write("crop")

    out = daemon.crop_for_subject(cam, str(small), str(tmp_path), score_result=scored,
                                  run_ffmpeg=fake_ffmpeg, source=str(native),
                                  source_width=3840)

    assert out != str(small)
    assert captured["image"] == str(native)          # the native frame is what gets cropped
    plain = daemon.compute_crop(scored["box"], 1280, 720)
    assert captured["rect"] == tuple(round(v * 3.0) for v in plain)   # 3840/1280


def test_send_alert_photo_removes_the_crop_it_created(monkeypatch, tmp_path):
    # The crop is this function's own temp file. The caller unlinks the frame it passed in,
    # so an unremoved crop leaks one file per alert — that is how the Pi tmpfs filled up.
    cam = _cam(crop_to_subject=True, scorer={"url": "http://x/score", "tiles": 2})
    full = tmp_path / "frame.jpg"
    full.write_bytes(b"\xff\xd8WHOLE")
    crop = tmp_path / "crop_123.jpg"
    crop.write_bytes(b"\xff\xd8ZOOM")
    monkeypatch.setattr(daemon, "crop_for_subject", lambda *a, **k: str(crop))
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"ok":true}'))
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)

    daemon.send_alert_photo(cam, {"telegram_token": "t", "telegram_chat": "c"},
                            str(full), "caption")

    assert not crop.exists()        # the zoom we made is gone
    assert full.exists()            # the frame we were handed is still the caller's


def test_send_alert_photo_archives_sent_frame_without_crop(monkeypatch, tmp_path):
    # No crop configured: sent and archived frame are the same, as before.
    cam = _cam(scorer={"url": "http://x/score"})
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"\xff\xd8PLAIN")
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"ok":true}'))
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))

    assert daemon.send_alert_photo(cam, {"telegram_token": "t", "telegram_chat": "c"},
                                   str(src), "caption") is True

    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8PLAIN"


# ── process_pending_sd ────────────────────────────────────────────────────────

def test_pending_recording_source_sends_sharpest(monkeypatch):
    # A recording-source camera: process_pending_sd must route through
    # _select_recording_frame (score all, pick sharpest above threshold) and send it,
    # not stop at the first above-threshold frame like the SD path.
    sent = []
    app = cfg.load_config_from_dict({"groq": {}, "cameras": [
        {"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
         "snapshot_source": "recording",
         "scorer": {"url": "http://x/score", "threshold": 0.4}}]})
    state = daemon.MonitorState()
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    scores = {"/f0.jpg": 0.2, "/f1.jpg": 0.9, "/f2.jpg": 0.7}
    blur = {"/f1.jpg": 8.0, "/f2.jpg": 3.0}          # f2 sharper than the higher-scoring f1
    monkeypatch.setattr(daemon, "score_for", lambda cfg_: (lambda f: scores[f]))
    monkeypatch.setattr(daemon.recclip, "blur_score", lambda f: blur[f])
    monkeypatch.setattr(daemon, "_caption_describe", lambda *a, **k: "")
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img) or True)
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.process_pending_sd(
        app, {"a": object()}, state, now=1075, secrets=secrets,
        snapshot_for=lambda cfg_: (lambda cam, ev: None), time_str=lambda ev: "T",
        fetch_frames=lambda c, s, span=None, out_dir=None: ["/f0.jpg", "/f1.jpg", "/f2.jpg"])
    assert sent == ["/f2.jpg"]




def _pending(cam_clients, sent, *, sd_ok=True, rtsp_ok=True, snapshot_calls=None):
    """Build app+state+collaborators for process_pending_sd tests."""
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/sd.jpg"] if sd_ok else []
    def snapshot_for(cfg_):
        def snap(cam, ev):
            if snapshot_calls is not None:
                snapshot_calls.append(ev.get("start_time"))
            return "/tmp/rtsp.jpg" if rtsp_ok else None
        return snap
    return app, state, fetch_frames, snapshot_for


def _run_pending(app, state, cam_clients, now, fetch_frames, snapshot_for, sent, monkeypatch,
                 groq=None):
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append((img, cap)) or True)
    monkeypatch.setattr(daemon.enrich, "groq_describe",
                        groq or (lambda *a, **k: "Person at door"))
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    return daemon.process_pending_sd(
        app, cam_clients, state, now=now, secrets=secrets,
        snapshot_for=snapshot_for, time_str=lambda ev: "T",
        fetch_frames=fetch_frames)


def test_pending_respects_camera_sd_jobs_per_tick(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10",
                                    "sd_snapshot": True, "sd_jobs_per_tick": 1}]})
    state = daemon.MonitorState()
    calls = []

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        calls.append(start_time)
        return [f"/tmp/sd-{start_time}.jpg"]

    def snapshot_for(cfg_):
        return lambda cam, ev: None

    state.pending_sd = [
        {"camera": "a", "etype": "person",
         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True},
        {"camera": "a", "etype": "motion",
         "event": {"start_time": 1001}, "due_at": 1075, "live_sent": False},
    ]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert calls == [1000]
    assert len(sent) == 1
    assert state.pending_sd == [{"camera": "a", "etype": "motion",
                                 "event": {"start_time": 1001},
                                 "due_at": 1075, "live_sent": False}]

def test_pending_not_due_is_kept(monkeypatch):
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1050, fetch_frames, snapshot_for, sent, monkeypatch)
    assert len(state.pending_sd) == 1     # still waiting (now < due_at)
    assert sent == []


def test_pending_due_sends_sd_frame(monkeypatch):
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/sd.jpg"    # the SD frame, not RTSP
    assert state.pending_sd == []         # entry consumed


def test_pending_failed_delivery_is_requeued_without_cooldown(monkeypatch):
    app, state, fetch_frames, snapshot_for = _pending({}, [])
    entry = {"camera": "a", "etype": "person",
             "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}
    state.pending_sd = [entry]
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: False)
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "Person")
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c",
               "face_names": {}}

    daemon.process_pending_sd(
        app, {"a": object()}, state, now=1080, secrets=secrets,
        snapshot_for=snapshot_for, time_str=lambda ev: "T", fetch_frames=fetch_frames)

    assert state.pending_sd == [entry]
    assert entry["due_at"] == 1140
    assert ("a", "person") not in state.last_alert


def test_pending_falls_back_to_rtsp_when_sd_fails(monkeypatch):
    # live never went out (live_sent=False): SD fails -> live-RTSP fallback rescues.
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent, sd_ok=False, rtsp_ok=True)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/rtsp.jpg"  # live RTSP fallback


def test_pending_dropped_when_sd_and_rtsp_fail(monkeypatch):
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent, sd_ok=False, rtsp_ok=False)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert sent == []
    assert state.pending_sd == []         # given up, not retried forever


def test_pending_follow_up_ignores_cooldown(monkeypatch):
    # The live alert just set a confirmed cooldown timestamp; the SD follow-up for that
    # same event must still send (it's a better photo, not a new detection).
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    state.last_alert[("a", "confirmed")] = 1075     # live alert moments ago
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert len(sent) == 1                  # follow-up sent despite recent confirmed alert
    assert sent[0][0] == "/tmp/sd.jpg"


def test_pending_sd_send_updates_cooldown_for_later_motion(monkeypatch):
    # Regression: a person was rescued by SD, then the same passage appeared
    # as motion minutes later and sent again. SD sends must update the shared alert gate.
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]

    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)

    assert len(sent) == 1
    can, _ = daemon.alert_gate(state, "a", cooldown=120, now=1090)
    assert can("motion", {"start_time": 1010}) is False


def test_pending_stale_entry_dropped(monkeypatch, caplog):
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    # event 700 s old (> PENDING_MAX_AGE) -> drop without sending
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    with caplog.at_level("INFO", logger="tapo_monitor.daemon"):
        _run_pending(app, state, {"a": object()}, 1700, fetch_frames, snapshot_for, sent, monkeypatch)
    assert sent == []
    assert state.pending_sd == []
    assert "too old" in caplog.text and "age=700s" in caplog.text   # not a silent drop


def test_pending_waits_when_camera_offline(monkeypatch):
    sent = []
    app, state, fetch_frames, snapshot_for = _pending({}, sent)
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)  # no client
    assert sent == []
    assert len(state.pending_sd) == 1     # retained for a later tick


def test_pending_groq_picks_frame_with_subject(monkeypatch):
    # Camera fires on motion start, so the first SD frame is often empty and the
    # subject only walks into view a few seconds in. Groq must pick that frame.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    frames = ["/tmp/f0.jpg", "/tmp/f1.jpg", "/tmp/f2.jpg"]
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return frames
    descs = {"/tmp/f0.jpg": "empty scene", "/tmp/f1.jpg": "empty scene",
             "/tmp/f2.jpg": "Person in a red jacket"}
    def groq(key, img):
        return descs[img]
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=groq)
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/f2.jpg"            # the frame with the person
    assert "Person in a red jacket" in sent[0][1]  # caption from that frame


def test_pending_removes_sd_job_dir(monkeypatch):
    # The daemon hands the fetch a private job dir and must delete the whole tree
    # afterwards (candidate frames and anything else the download left in it).
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    seen = {}

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        seen["dir"] = out_dir
        frame_paths = []
        for i, _desc in enumerate(("empty scene", "Person in a red jacket", "empty scene")):
            path = os.path.join(out_dir, f"f{i}.jpg")
            with open(path, "wb") as fh:
                fh.write(b"jpg")
            frame_paths.append(path)
        seen["descs"] = dict(zip(frame_paths, ("empty scene", "Person in a red jacket",
                                               "empty scene"), strict=True))
        return frame_paths

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda _key, img: seen["descs"][img])
    assert len(sent) == 1
    assert not os.path.exists(seen["dir"])       # whole job dir gone, no frames left behind


def test_pending_cleans_job_dir_when_subprocess_leaves_orphans(monkeypatch):
    # Regression: a Pi Zero blows the SD subprocess timeout, the killed child never runs
    # its own cleanup, and it leaves the segment mp4 + partial frames behind while
    # returning no usable frames. The daemon must still drop the whole job dir.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    seen = {}

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        seen["dir"] = out_dir
        # simulate the orphans a killed download subprocess leaves in its out_dir
        with open(os.path.join(out_dir, "sd_1000.mp4"), "wb") as fh:
            fh.write(b"x" * 1024)
        with open(os.path.join(out_dir, "sdf_1000_partial.jpg"), "wb") as fh:
            fh.write(b"jpg")
        return []                                 # timeout -> no usable frames returned

    state.pending_sd = [{"camera": "a", "etype": "motion",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert sent == []                             # nothing to send (motion, no subject)
    assert not os.path.exists(seen["dir"])        # orphaned mp4 + partial frame cleaned up


def test_pending_removes_rtsp_fallback_snapshot(monkeypatch, tmp_path):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()
    image = tmp_path / "rtsp.jpg"

    def snapshot_for(cfg_):
        def snap(cam, ev):
            image.write_bytes(b"jpg")
            return str(image)
        return snap

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, lambda _cfg, _start, span=None, out_dir=None: [],
                 snapshot_for, sent, monkeypatch)
    assert len(sent) == 1
    assert not image.exists()


def test_pending_all_empty_with_live_sent_sends_nothing(monkeypatch):
    # Live (empty) already went out; SD finds no subject -> no duplicate empty ping.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    frames = ["/tmp/f0.jpg", "/tmp/f1.jpg", "/tmp/f2.jpg"]
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return frames
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: "empty scene")
    assert sent == []                    # nothing sent
    assert state.pending_sd == []        # entry consumed


def test_pending_all_empty_without_live_sends_nothing(monkeypatch):
    # Groq saw nobody in ANY SD frame and no live went out either: sending the middle
    # frame anyway meant a blank photo ping. Live 2026-07-02..06: every such case was a
    # night false positive on a passing car (visually verified against the SD clip), so
    # drop it — the audit log keeps the trace.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    frames = ["/tmp/f0.jpg", "/tmp/f1.jpg", "/tmp/f2.jpg"]
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return frames
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: "empty scene")
    assert sent == []                    # no blank photo
    assert state.pending_sd == []        # entry consumed, not retried


def test_run_monitor_pass_enqueues_sd_without_live_send_when_empty(monkeypatch):
    # Confirmed person on an sd_snapshot camera with an empty live frame: nothing is
    # pinged now; the SD follow-up is enqueued with live_sent=False so it delivers the
    # real event-time frame (or an event-time fallback) instead of a duplicate empty.
    sent = []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()

    class Cam:
        def getEvents(self):
            return [{"start_time": 500, "events_1": 524290, "alarm_type": 2}]

    def snapshot_for(_cfg):
        return lambda cam, ev: "/tmp/live.jpg"

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.run_monitor_pass(app, {"a": Cam()}, state, now=2000, secrets=secrets,
                            snapshot_for=snapshot_for, time_str=lambda ev: "T")
    assert sent == []                                     # no empty live ping
    assert len(state.pending_sd) == 1
    assert state.pending_sd[0]["camera"] == "a"
    assert state.pending_sd[0]["event"]["start_time"] == 500
    assert state.pending_sd[0]["live_sent"] is False
    assert state.pending_sd[0]["due_at"] == 500 + daemon.sdclip.SD_FRESH_DELAY


def test_snapshot_failed_defer_blocks_near_duplicate_person(monkeypatch):
    # When live RTSP fails, the first confirmed person is handed to SD. That defer must
    # arm the same gate as a send, otherwise a near-identical Tapo event creates another
    # SD job and later sends a duplicate photo.
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()

    class Cam:
        def getEvents(self):
            return [
                {"start_time": 500, "events_1": 524290, "alarm_type": 2},
                {"start_time": 501, "events_1": 524290, "alarm_type": 2},
            ]

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.run_monitor_pass(app, {"a": Cam()}, state, now=2000, secrets=secrets,
                            snapshot_for=lambda _cfg: (lambda cam, ev: None),
                            time_str=lambda ev: "T")

    assert len(state.pending_sd) == 1
    assert state.pending_sd[0]["event"]["start_time"] == 500


def test_defer_due_at_follows_camera_event_end_time(monkeypatch):
    # A long event (camera says 73 s) needs a wider window AND a later due time, or the
    # download window end is still inside pytapo's freshness guard when the fetch fires.
    sent = []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()

    class Cam:
        def getEvents(self):
            return [{"start_time": 500, "end_time": 573,
                     "events_1": 524290, "alarm_type": 2}]

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.run_monitor_pass(app, {"a": Cam()}, state, now=2000, secrets=secrets,
                            snapshot_for=lambda _cfg: (lambda cam, ev: "/tmp/live.jpg"),
                            time_str=lambda ev: "T")
    assert len(state.pending_sd) == 1
    expected_span = daemon.sdclip.event_span(state.pending_sd[0]["event"])
    assert expected_span == daemon.sdclip.SD_SPAN_CAP
    assert state.pending_sd[0]["due_at"] == 500 + daemon.sdclip.fresh_delay(expected_span)


def test_pending_passes_event_span_to_fetch(monkeypatch):
    # The SD fetch must pull the window the camera's event seconds dictate, not a fixed one.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    got = {}

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        got["span"] = span
        return ["/tmp/sd.jpg"]

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000, "end_time": 1073},
                         "due_at": 1117, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1200, fetch_frames, snapshot_for, sent, monkeypatch)
    assert got["span"] == daemon.sdclip.SD_SPAN_CAP
    assert len(sent) == 1


def test_defer_and_fetch_honor_camera_sd_span_cap(monkeypatch):
    # A camera allowed a wider budget (Pi 4) must size BOTH the due time and the fetch
    # window from its own cap, not the package default.
    sent = []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                  "sd_span_cap": 120}]})
    state = daemon.MonitorState()

    class Cam:
        def getEvents(self):
            return [{"start_time": 500, "end_time": 640,
                     "events_1": 524290, "alarm_type": 2}]

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.run_monitor_pass(app, {"a": Cam()}, state, now=2000, secrets=secrets,
                            snapshot_for=lambda _cfg: (lambda cam, ev: "/tmp/live.jpg"),
                            time_str=lambda ev: "T")
    assert state.pending_sd[0]["due_at"] == 500 + daemon.sdclip.fresh_delay(120)

    got = {}
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        got["span"] = span
        return ["/tmp/sd.jpg"]
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "Person")
    daemon.process_pending_sd(app, {"a": Cam()}, state, now=500 + 200, secrets=secrets,
                              snapshot_for=lambda _cfg: (lambda cam, ev: None),
                              time_str=lambda ev: "T", fetch_frames=fetch_frames)
    assert got["span"] == 120


def test_defer_dedups_pending_motion_per_camera():
    # A yard burst fires several empty-motion events in minutes; each deferred one costs
    # a ~2 min SD download, so at most ONE motion follow-up may wait per camera. Person
    # entries are unaffected (they are confirmed and must all deliver).
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                  "sd_motion": True,
                                  "detection": {"strict_people": False}}]})
    state = daemon.MonitorState()
    events = [{"start_time": 500, "events_1": 34, "alarm_type": 6},
              {"start_time": 540, "events_1": 34, "alarm_type": 6}]

    class Cam:
        def getEvents(self):
            return events

    import unittest.mock as mock
    with mock.patch.object(daemon.notify, "send_photo"), \
         mock.patch.object(daemon.enrich, "groq_describe", return_value="empty scene"):
        secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c",
                   "face_names": {}}
        daemon.run_monitor_pass(app, {"a": Cam()}, state, now=2000, secrets=secrets,
                                snapshot_for=lambda _cfg: (lambda cam, ev: "/tmp/live.jpg"),
                                time_str=lambda ev: "T")
    motions = [e for e in state.pending_sd if e["etype"] == "motion"]
    assert len(motions) == 1                  # burst deduped to one follow-up


def test_pending_motion_never_sends_blind_fallback(monkeypatch):
    # Motion is UNCONFIRMED: when SD yields no frames there is zero evidence of a
    # subject, so no live-RTSP fallback may fire (that safety net is for camera-
    # confirmed people only).
    sent = []
    calls = []
    app, state, _fetch, _snap = _pending({}, sent)
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return []
    def snapshot_for(cfg_):
        def snap(cam, ev):
            calls.append(1)
            return "/tmp/rtsp.jpg"
        return snap
    state.pending_sd = [{"camera": "a", "etype": "motion",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert sent == [] and calls == []         # no blind ping, no wasted grab


def test_pending_motion_with_subject_sends(monkeypatch):
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/f0.jpg"]
    state.pending_sd = [{"camera": "a", "etype": "motion",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: "Woman with two children by bicycles")
    assert len(sent) == 1                     # the rescued in-frame photo


def test_pending_scorer_pick_captions_with_frame_sequence(monkeypatch):
    # With a scorer as arbiter, Groq only captions the send. Passing the whole frame
    # sequence (not just the winning frame) lets the caption describe movement and
    # direction across the event instead of one frozen pose.
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                  "scorer": {"url": "http://127.0.0.1:1/score",
                                             "threshold": 0.4}}]})
    state = daemon.MonitorState()
    frames = [f"/tmp/f{i}.jpg" for i in range(3)]

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return frames

    scores = {"/tmp/f0.jpg": 0.1, "/tmp/f1.jpg": 0.9, "/tmp/f2.jpg": 0.8}
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": scores[img], "animal": 0.0})
    groq_calls = []
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames,
                 lambda cfg_: (lambda cam, ev: None), sent, monkeypatch,
                 groq=lambda _key, images, **k: groq_calls.append(images) or "Person walks right")
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/f1.jpg"          # scorer's pick is still the photo sent
    assert groq_calls == [frames]               # caption sees the whole sequence, in order


def test_pending_send_marks_open_group_delivered(monkeypatch):
    # The SD follow-up is the alert the burst was waiting for: mark the group delivered
    # so the dedupe guard can suppress a duplicate follow-up for the same passage.
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                  "sampler": {"enabled": True, "interval": 30,
                                              "max_frames": 6, "group_gap": 90},
                                  "scorer": {"url": "http://127.0.0.1:1/score",
                                             "threshold": 0.4}}]})
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.9, "animal": 0.0})
    state.groups["a"] = {"camera": "a", "etype": "person", "event": {"start_time": 1000},
                         "started": 1000, "last_event_at": 1010, "frames": 0,
                         "next_due": 10**9, "sent": True, "delivered": False}
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080,
                 lambda cfg_, start, span=None, out_dir=None: ["/tmp/sd.jpg"],
                 lambda cfg_: (lambda cam, ev: None), sent, monkeypatch)
    assert len(sent) == 1
    assert state.groups["a"]["delivered"] is True


def test_pending_raw_mode_sends_sd_frame_without_groq(monkeypatch):
    # enrich.groq=false = raw mode: the SD follow-up must not require Groq to see a
    # subject (blank desc would read as "no subject" and drop everything) — send the
    # first frame directly.
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                    "enrich": {"groq": False}}]})
    state = daemon.MonitorState()
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/sd.jpg"]
    def snapshot_for(cfg_):
        return lambda cam, ev: None
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    groq_calls = []
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: groq_calls.append(a) or "empty scene")
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/sd.jpg"
    assert groq_calls == []
    assert state.pending_sd == []

# -- process_sampler ----------------------------------------------------------

def _sampler_app(threshold=0.4, url="http://127.0.0.1:1/score", motion_send=None):
    scorer = {"url": url, "threshold": threshold}
    if motion_send is not None:
        scorer["motion_send_threshold"] = motion_send
    return cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10",
                                    "sampler": {"enabled": True, "interval": 30,
                                                "max_frames": 6, "group_gap": 90},
                                    "scorer": scorer}]})


def _group(started=1000, etype="motion", sent=False, frames=0):
    return {"camera": "a", "etype": etype, "event": {"start_time": started},
            "started": started, "last_event_at": started + 10,
            "frames": frames, "next_due": started + 30, "sent": sent}


def _run_sampler(app, state, now, sent, monkeypatch, *, score=0.9, snap="/tmp/f.jpg",
                 groq="Person", delivered=True):
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append((img, cap)) or delivered)
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: groq)
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: None if score is None
                        else {"person": score, "animal": 0.0})
    monkeypatch.setattr(daemon, "_safe_unlink", lambda p: None)
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.process_sampler(app, {"a": object()}, state, now=now, secrets=secrets,
                           snapshot_for=lambda c: (lambda cam, ev: snap),
                           time_str=lambda ev: "T")


def test_sampler_due_group_scores_and_sends(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.9)
    assert len(sent) == 1
    g = state.groups["a"]
    assert g["sent"] is True
    assert g["frames"] == 1
    assert state.last_alert[("a", "motion")] == 1035   # cooldown recorded


def test_sampler_send_marks_group_delivered(monkeypatch):
    # The sampler's own alert is a real delivery: a later confirmed-but-empty person of
    # the same burst may then skip its duplicate SD follow-up.
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.9)
    assert state.groups["a"]["delivered"] is True


def test_sampler_failed_delivery_leaves_group_undelivered(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.9, delivered=False)
    assert state.groups["a"].get("delivered") is not True


def test_sampler_failed_delivery_keeps_group_retryable(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()

    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.9, delivered=False)

    assert len(sent) == 1
    assert state.groups["a"]["sent"] is False
    assert ("a", "motion") not in state.last_alert


def test_sampler_below_threshold_keeps_sampling(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.1)
    assert sent == []
    g = state.groups["a"]
    assert g["sent"] is False
    assert g["frames"] == 1
    assert g["next_due"] == 1035 + 30


def test_sampler_low_scores_close_motion_group_early(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10",
                                  "sampler": {"enabled": True, "interval": 30,
                                              "max_frames": 6, "group_gap": 90,
                                              "low_score_exit": 1, "low_score": 0.15},
                                  "scorer": {"url": "http://127.0.0.1:1/score",
                                             "threshold": 0.4}}]})
    state = daemon.MonitorState()
    state.groups["a"] = _group()

    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.05)

    assert sent == []
    g = state.groups["a"]
    scfg = app.cameras[0].sampler
    assert daemon.sampler.due(g, 1035 + 31, scfg) is False   # closed early
    assert daemon.sampler.expired(g, g["last_event_at"] + 91, scfg) is True


def test_sampler_scorer_down_sends_once(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=None)
    assert len(sent) == 1                    # passthrough, but bounded:
    assert state.groups["a"]["sent"] is True  # one send per group, not six


def test_sampler_not_due_or_sent_group_untouched(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group(sent=True)
    _run_sampler(app, state, 1035, sent, monkeypatch)
    assert sent == []
    state.groups["a"] = _group()
    _run_sampler(app, state, 1010, sent, monkeypatch)   # before next_due
    assert sent == []
    assert state.groups["a"]["frames"] == 0


def test_process_sampler_holds_first_marginal_motion(monkeypatch):
    # A single marginal follow-up frame (threshold <= score < motion_send_threshold)
    # must be held for corroboration, not sent.
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.4)
    assert sent == []
    g = state.groups["a"]
    assert g["sent"] is False
    assert g["motion_candidates"] == 1
    assert g["frames"] == 1                    # still sampling


def test_process_sampler_hold_archives_review_frame(monkeypatch):
    reviews = []
    monkeypatch.setattr(daemon.sentlog, "archive_review_if_configured",
                        lambda path, meta, **k: reviews.append(meta))
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.4)
    assert sent == []
    assert len(reviews) == 1 and reviews[0]["verdict"] == "hold"


def test_process_sampler_hold_clears_low_score_streak(monkeypatch):
    # A marginal (held) frame is evidence the group is not empty, so it must clear the
    # low-score streak like any other above-low frame — otherwise low/low/hold/low closes
    # a group that is still holding a candidate.
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10",
                                  "sampler": {"enabled": True, "interval": 30,
                                              "max_frames": 6, "group_gap": 90,
                                              "low_score_exit": 2, "low_score": 0.15},
                                  "scorer": {"url": "http://127.0.0.1:1/score",
                                             "threshold": 0.3,
                                             "motion_send_threshold": 0.6}}]})
    state = daemon.MonitorState()
    g = _group()
    g["low_streak"] = 1                       # one low frame short of the early exit
    state.groups["a"] = g
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.4)   # marginal -> hold
    assert sent == []
    assert g["motion_candidates"] == 1
    assert "low_streak" not in g              # the hold cleared the streak
    assert g.get("early_exit") is None
    assert daemon.sampler.due(g, 1066, app.cameras[0].sampler) is True   # still sampling


def test_process_sampler_sends_second_marginal_motion(monkeypatch):
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    g = _group()
    g["motion_candidates"] = 1                 # one candidate already seen
    state.groups["a"] = g
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.4)
    assert len(sent) == 1                       # corroborated -> send
    assert state.groups["a"]["sent"] is True


def test_process_sampler_pir_group_unaffected_by_corroboration(monkeypatch):
    # PIR-backed motion keeps the legacy immediate path: 0.4 >= threshold -> send.
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    g = _group()
    g["pir_backed"] = True
    state.groups["a"] = g
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.4)
    assert len(sent) == 1
    assert state.groups["a"]["sent"] is True


def test_sampler_expired_group_removed(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group(sent=True)
    _run_sampler(app, state, 1000 + 10 + 91, sent, monkeypatch)   # past gap
    assert "a" not in state.groups
    assert sent == []


def test_sampler_expired_unconfirmed_hold_audited(monkeypatch, caplog):
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    g = _group()
    g["motion_candidates"] = 1
    g["last_hold_score"] = 0.41
    state.groups["a"] = g
    with caplog.at_level("INFO", logger="tapo_monitor.monitor"):
        _run_sampler(app, state, 1000 + 30 * 6 + 91, sent, monkeypatch)  # past window+gap
    assert "a" not in state.groups
    assert sent == []
    assert "action=drop" in caplog.text
    assert "reason=hold_expired" in caplog.text
    assert "score=0.4100" in caplog.text


def test_sampler_expired_group_without_candidates_not_audited(monkeypatch, caplog):
    sent = []
    app = _sampler_app(threshold=0.3, motion_send=0.6)
    state = daemon.MonitorState()
    g = _group()
    state.groups["a"] = g
    with caplog.at_level("INFO", logger="tapo_monitor.monitor"):
        _run_sampler(app, state, 1000 + 30 * 6 + 91, sent, monkeypatch)  # past window+gap
    assert "a" not in state.groups
    assert sent == []
    assert "hold_expired" not in caplog.text


def test_sampler_grab_failure_counts_attempt(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    _run_sampler(app, state, 1035, sent, monkeypatch, snap=None)
    assert sent == []
    g = state.groups["a"]
    assert g["frames"] == 1                  # attempt consumed, schedule advanced
    assert g["next_due"] == 1035 + 30


def test_sampler_cooldown_blocks_and_closes(monkeypatch):
    sent = []
    app = _sampler_app()
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    state.last_alert[("a", "confirmed")] = 1030   # fresh confirmed alert
    _run_sampler(app, state, 1035, sent, monkeypatch, score=0.9)
    assert sent == []
    assert state.groups["a"]["sent"] is True      # this walk already alerted


def test_score_for_none_without_url():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    assert daemon.score_for(app.cameras[0]) is None


def test_score_for_maps_result_and_failure(monkeypatch):
    app = _sampler_app(threshold=0.4)
    camera_cfg = app.cameras[0]
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.2, "animal": 0.6})
    assert daemon.score_for(camera_cfg)("/tmp/x.jpg") == 0.2
    monkeypatch.setattr(daemon.scorer, "score_image", lambda url, img, timeout=10, tiles=1: None)
    assert daemon.score_for(camera_cfg)("/tmp/x.jpg") is None


def test_loop_step_runs_sampler_every_tick(monkeypatch):
    app = _sampler_app()
    calls = []
    daemon.loop_step(
        app, {}, daemon.MonitorState(), now=1000, secrets={},
        last_control=1000, control_interval=60,
        run_control=lambda *a, **k: {}, watchdog=lambda *a, **k: None,
        monitor=lambda *a, **k: None, drain=lambda *a, **k: None,
        sample=lambda *a, **k: calls.append(k["now"]))
    assert calls == [1000]

def test_loop_step_runs_review_digest_every_tick():
    app = _sampler_app()
    calls = []
    daemon.loop_step(
        app, {}, daemon.MonitorState(), now=1000, secrets={"telegram_token": "t"},
        last_control=1000, control_interval=60,
        run_control=lambda *a, **k: {}, watchdog=lambda *a, **k: None,
        monitor=lambda *a, **k: None, drain=lambda *a, **k: None,
        sample=lambda *a, **k: None,
        digest=lambda *, now, secrets: calls.append((now, secrets["telegram_token"])))
    assert calls == [(1000, "t")]


def test_pending_scorer_picks_frame_above_threshold(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                    "scorer": {"url": "http://127.0.0.1:1/score",
                                               "threshold": 0.4}}]})
    state = daemon.MonitorState()
    scores = {"/tmp/sd-1.jpg": 0.1, "/tmp/sd-2.jpg": 0.9}
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": scores[img], "animal": 0.0})

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/sd-1.jpg", "/tmp/sd-2.jpg"]

    def snapshot_for(cfg_):
        return lambda cam, ev: None

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: "Person at door")
    assert len(sent) == 1
    assert sent[0][0] == "/tmp/sd-2.jpg"           # first frame ABOVE threshold wins
    assert "Person at door" in sent[0][1]          # Groq captioned the approved frame


def test_pending_scorer_all_below_threshold_drops(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                    "scorer": {"url": "http://127.0.0.1:1/score",
                                               "threshold": 0.4}}]})
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, img, timeout=10, tiles=1: {"person": 0.05, "animal": 0.0})

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/sd-1.jpg"]

    def snapshot_for(cfg_):
        return lambda cam, ev: None

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert sent == []


def test_pending_scorer_failure_passes_frame_through(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                    "scorer": {"url": "http://127.0.0.1:1/score",
                                               "threshold": 0.4}}]})
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.scorer, "score_image", lambda url, img, timeout=10, tiles=1: None)

    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        return ["/tmp/sd-1.jpg"]

    def snapshot_for(cfg_):
        return lambda cam, ev: None

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent, monkeypatch)
    assert len(sent) == 1



# ── night_only gating ─────────────────────────────────────────────────────────

def _capture_mute(monkeypatch):
    captured = {}
    def fake_run_monitor(cam, c, last_seen, **kw):
        captured["mute"] = kw.get("mute")
        return 42
    monkeypatch.setattr(daemon.monitor, "run_monitor", fake_run_monitor)
    return captured


def test_run_monitor_pass_mutes_night_only_camera_by_day(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True}]})
    captured = _capture_mute(monkeypatch)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {"a": object()}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t", night=False)
    assert captured["mute"] is True
    assert state.last_seen["a"] == 42          # watermark still advanced (silent drain)


def test_run_monitor_pass_active_night_only_camera_at_night(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True}]})
    captured = _capture_mute(monkeypatch)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {"a": object()}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t", night=True)
    assert captured["mute"] is False


def test_run_monitor_pass_never_mutes_normal_camera_by_day(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    captured = _capture_mute(monkeypatch)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {"a": object()}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t", night=False)
    assert captured["mute"] is False


def test_sampler_skips_night_only_camera_by_day(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True,
                                    "sampler": {"enabled": True},
                                    "scorer": {"url": "http://x/score"}}]})
    state = daemon.MonitorState()
    state.groups["a"] = _group()
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(daemon, "_safe_unlink", lambda p: None)
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.process_sampler(app, {"a": object()}, state, now=1035, secrets=secrets,
                           snapshot_for=lambda c: (lambda cam, ev: "/tmp/f.jpg"),
                           time_str=lambda ev: "T", night=False)
    assert sent == []
    assert state.groups["a"]["frames"] == 0          # untouched by day


def test_pending_skips_night_only_camera_by_day(monkeypatch):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True,
                                    "night_only": True}]})
    state = daemon.MonitorState()
    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    called = []
    def fetch_frames(cfg_, start_time, span=None, out_dir=None):
        called.append(start_time)
        return ["/tmp/sd.jpg"]
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: sent.append(a))
    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    daemon.process_pending_sd(app, {"a": object()}, state, now=1080, secrets=secrets,
                              snapshot_for=lambda c: (lambda cam, ev: None),
                              time_str=lambda ev: "T", fetch_frames=fetch_frames, night=False)
    assert sent == []
    assert called == []                              # SD not even fetched by day
    assert state.pending_sd == []                    # entry dropped (won't replay at night)


def test_watchdog_skips_night_only_camera_by_day(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True}]})
    sent = []
    monkeypatch.setattr(
        daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    state.fail_since["a"] = 0
    secrets = {"telegram_token": "t", "telegram_chat": "c"}
    daemon._watchdog_pass(app, {}, state, now=100000, secrets=secrets, night=False)
    assert sent == []                                # no 🔴 by day for a night_only camera


def test_watchdog_alerts_night_only_camera_at_night(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True}]})
    sent = []
    monkeypatch.setattr(
        daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    state.fail_since["a"] = 0
    secrets = {"telegram_token": "t", "telegram_chat": "c"}
    daemon._watchdog_pass(app, {}, state, now=100000, secrets=secrets, night=True)
    assert len(sent) == 1 and "unreachable" in sent[0]


def test_watchdog_messages_include_uptime_and_outage_duration(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    sent = []
    monkeypatch.setattr(
        daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon._watchdog_pass(app, {"a": object()}, state, now=0, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=3600, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=4500, secrets=secrets)
    daemon._watchdog_pass(app, {"a": object()}, state, now=5400, secrets=secrets)

    assert sent == [
        "🔴 camera 'a' unreachable after 1h observed uptime",
        "🟢 camera 'a' back online after 30m outage",
    ]


def test_watchdog_uses_ping_health_independently_from_api_client(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    sent = []
    monkeypatch.setattr(
        daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    state.network_reachable["a"] = True
    daemon._watchdog_pass(app, {}, state, now=0, secrets=secrets)
    assert state.online_since["a"] == 0
    assert state.fail_since == {}

    state.network_reachable["a"] = False
    daemon._watchdog_pass(app, {}, state, now=1000, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=1900, secrets=secrets)
    assert len(sent) == 1 and "unreachable" in sent[0]

    state.network_reachable["a"] = True
    daemon._watchdog_pass(app, {}, state, now=2000, secrets=secrets)
    assert len(sent) == 2 and "back online after 16m 40s outage" in sent[1]


def test_watchdog_retries_failed_outage_delivery(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    state = daemon.MonitorState()
    state.network_reachable["a"] = False
    state.fail_since["a"] = 0
    results = iter((False, True))
    sent = []

    def send_text(token, chat, message):
        sent.append(message)
        return next(results)

    monkeypatch.setattr(daemon.notify, "send_text", send_text)
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon._watchdog_pass(app, {}, state, now=1000, secrets=secrets)
    assert "a" not in state.outage_alerted
    daemon._watchdog_pass(app, {}, state, now=1001, secrets=secrets)

    assert len(sent) == 2
    assert state.outage_alerted["a"] is True


def test_watchdog_retries_failed_recovery_delivery(monkeypatch):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    state = daemon.MonitorState()
    state.network_reachable["a"] = True
    state.fail_since["a"] = 0
    state.outage_alerted["a"] = True
    results = iter((False, True))
    sent = []

    def send_text(token, chat, message):
        sent.append(message)
        return next(results)

    monkeypatch.setattr(daemon.notify, "send_text", send_text)
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon._watchdog_pass(app, {}, state, now=1000, secrets=secrets)
    assert state.recovery_pending["a"] == 1000
    daemon._watchdog_pass(app, {}, state, now=1001, secrets=secrets)

    assert len(sent) == 2
    assert "a" not in state.recovery_pending


def test_watchdog_persists_only_when_durable_health_changes(monkeypatch, tmp_path):
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    state = daemon.MonitorState()
    state.health_path = str(tmp_path / "health.json")
    state.network_reachable["a"] = True
    saved = []
    monkeypatch.setattr(
        daemon.health, "save_state",
        lambda path, current, logger=None: saved.append((path, daemon.health.snapshot(current))))
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    daemon._watchdog_pass(app, {}, state, now=100, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=200, secrets=secrets)

    assert len(saved) == 1
    assert saved[0][0] == state.health_path
    assert saved[0][1]["online_since"] == {"a": 100}


def test_loop_step_passes_night_to_detection_passes():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    seen = {}
    daemon.loop_step(
        app, {}, daemon.MonitorState(), now=1000, secrets={},
        last_control=1000, control_interval=60,
        run_control=lambda *a, **k: {},
        watchdog=lambda *a, **k: seen.__setitem__("wd", k.get("night")),
        monitor=lambda *a, **k: seen.__setitem__("mon", k.get("night")),
        drain=lambda *a, **k: seen.__setitem__("drain", k.get("night")),
        sample=lambda *a, **k: seen.__setitem__("sample", k.get("night")),
        is_night=lambda: True)
    assert seen == {"mon": True, "sample": True, "drain": True}


def _twin_snapshot(*, vehicle=False):
    def available(value):
        return {"state": "available", "value": value}

    return {
        "schema_version": 1,
        "groups": {
            "basic": {"info": available({"model": "test"})},
            "storage": {"sd_card": available({"status": "normal"})},
            "detection": {
                "person": available({"enabled": "on"}),
                "vehicle": available({"enabled": "on" if vehicle else "off"}),
                "motion": available({"sensitivity": 60}),
            },
            "track": {"auto_target": available({"enabled": "on"})},
        },
    }


def _twin_app(*, drift_alerts=False):
    return cfg.load_config_from_dict({
        "observability": {
            "digital_twin": True,
            "probe_interval": 60,
            "drift_alerts": drift_alerts,
        },
        "cameras": [{"name": "a", "host": "203.0.113.10"}],
    })


def _twin_state():
    state = daemon.MonitorState()
    state.network_reachable["a"] = True
    state.events_reachable["a"] = True
    state.rtsp_reachable["a"] = True
    state.desired_plans["a"] = daemon.CameraPlan(
        autotrack_on=True,
        rain_parked=False,
        motion_sensitivity=60,
        smarttrack=("person",),
        preset=None,
    )
    return state


def test_digital_twin_reuses_client_and_respects_probe_interval():
    app = _twin_app()
    state = _twin_state()
    camera_client = object()
    seen = []

    def probe(client):
        seen.append(client)
        return _twin_snapshot()

    daemon.process_digital_twin(
        app, {"a": camera_client}, state, now=100, secrets={}, probe=probe)
    daemon.process_digital_twin(
        app, {"a": camera_client}, state, now=130, secrets={}, probe=probe)

    assert seen == [camera_client]
    assert state.twin_fleet["a"]["health"]["status"] == "ok"
    assert state.twin_fleet["a"]["drift"]["counts"]["drift"] == 0


def test_digital_twin_deduplicates_drift_and_reports_recovery(monkeypatch):
    app = _twin_app(drift_alerts=True)
    state = _twin_state()
    messages = []
    monkeypatch.setattr(
        daemon.notify, "send_text",
        lambda token, chat, message: messages.append(message) or True,
    )
    snapshots = iter((_twin_snapshot(vehicle=True), _twin_snapshot(vehicle=True),
                      _twin_snapshot(vehicle=False)))
    secrets = {"telegram_token": "t", "telegram_chat": "c"}

    for now in (100, 160, 220):
        daemon.process_digital_twin(
            app, {"a": object()}, state, now=now, secrets=secrets,
            probe=lambda client: next(snapshots),
        )

    assert len(messages) == 2
    assert "configuration drift" in messages[0]
    assert "recovered" in messages[1]
    assert state.twin_alerted["a"] == set()


def test_digital_twin_probe_failure_isolated_and_throttled():
    app = _twin_app()
    state = _twin_state()
    calls = []

    def broken_probe(client):
        calls.append(client)
        raise RuntimeError("broken probe")

    daemon.process_digital_twin(
        app, {"a": object()}, state, now=100, secrets={}, probe=broken_probe)
    daemon.process_digital_twin(
        app, {"a": object()}, state, now=130, secrets={}, probe=broken_probe)

    assert len(calls) == 1
    assert state.twin_fleet == {}


def test_loop_step_preserves_control_plan_for_digital_twin():
    app = _twin_app()
    state = daemon.MonitorState()
    plan = daemon.CameraPlan(True, False, 60, ("person",), None)
    seen = []

    daemon.loop_step(
        app, {}, state, now=100, secrets={}, last_control=None, control_interval=60,
        run_control=lambda *args, **kwargs: {"a": plan},
        watchdog=lambda *args, **kwargs: None,
        monitor=lambda *args, **kwargs: None,
        drain=lambda *args, **kwargs: None,
        sample=lambda *args, **kwargs: None,
        guard=lambda *args, **kwargs: None,
        inspect=lambda app, clients, current, **kwargs:
            seen.append(current.desired_plans["a"]),
        connect_factory=lambda *args: None,
        is_night=lambda: True,
    )

    assert seen == [plan]


def test_motion_sd_short_window_retries_full_span(monkeypatch):
    app = cfg.load_config_from_dict({
        "groq": {},
        "cameras": [{"name": "a", "host": "203.0.113.10",
                     "sd_snapshot": True, "sd_motion": True,
                     "sd_span_cap": 120, "sd_motion_span_cap": 48,
                     "scorer": {"url": "http://scorer"}}],
    })
    assert daemon.sd_followup_spans(
        app.cameras[0], {"start_time": 1000, "end_time": 1120}, "motion") == (48, 120)
    state = daemon.MonitorState()
    state.pending_sd = [{"camera": "a", "etype": "motion",
                         "event": {"start_time": 1000, "end_time": 1120},
                         "due_at": 0, "live_sent": False,
                         "span": 48, "full_span": 120}]
    calls = []
    monkeypatch.setattr(daemon, "score_for", lambda _cfg: lambda _path: 0.01)

    def fetch_frames(_cfg, _start, span=None, out_dir=None):
        calls.append(span)
        return ["/tmp/sd.jpg"]

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c",
               "face_names": {}}
    daemon.process_pending_sd(app, {"a": object()}, state, now=1200,
                              secrets=secrets, fetch_frames=fetch_frames)
    assert calls == [48]


def test_default_snapshot_skips_the_twin_when_grab_is_already_delivery_width(
        monkeypatch, tmp_path):
    # A substream that is natively 1280 wide has no detail to gain: the "native" grab
    # returned the same frame, so there is nothing to reduce and no twin to carry.
    grabbed = tmp_path / "snap.jpg"
    grabbed.write_bytes(b"jpeg")
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda url, **kw: str(grabbed))
    monkeypatch.setattr(daemon.snapshot, "image_size", lambda p: (1280, 720))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    def _no_downscale(*a, **k):
        raise AssertionError("must not downscale a frame already at delivery width")

    monkeypatch.setattr(daemon, "_reduced", _no_downscale)

    out = daemon._default_snapshot(_cam(crop_to_subject=True, crop_from_native=True))(
        None, None)

    assert str(out) == str(grabbed)
    assert getattr(out, "native", None) is None


def test_default_snapshot_carries_native_height_on_the_twin(monkeypatch, tmp_path):
    # The crop clamps its scaled rect against the original's bounds, so the height has
    # to travel with the width.
    native = tmp_path / "native.jpg"
    native.write_bytes(b"4k")
    scene = tmp_path / "scene.jpg"
    scene.write_bytes(b"1280")
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda url, **kw: str(native))
    monkeypatch.setattr(daemon.snapshot, "image_size", lambda p: (3840, 2160))
    monkeypatch.setattr(daemon, "_reduced",
                        lambda src, out_dir, run=None, width=None: str(scene))
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda cfg: ("u", "p"))

    out = daemon._default_snapshot(_cam(crop_to_subject=True, crop_from_native=True))(
        None, None)

    assert out.native == str(native)
    assert out.native_width == 3840
    assert out.native_height == 2160


def test_compute_crop_keeps_a_usefully_wide_portrait_zoom():
    # A standing figure is naturally taller than the scene. It needs to remain a zoom,
    # not expand all the way to 16:9 just to match neighbouring photos in the chat.
    _, _, cw, ch = daemon.compute_crop([953, 192, 1000, 321], 1280, 720)
    assert cw / ch >= 1.2, f"portrait zoom widened too much: {cw}x{ch}"


def test_compute_crop_caps_widening_and_never_returns_a_noodle():
    # Reaching 16:9 here would need 1095px of a 1280 frame — the zoom would vanish. The
    # widen stops at the minimum ratio instead, which is enough to kill the sliver.
    _, _, cw, ch = daemon.compute_crop([0, 185, 60, 527], 1280, 720)
    assert cw / ch >= 2 / 3 - 0.01, f"noodle: {cw}x{ch}"
    assert cw <= 0.60 * 1280, f"widen cap exceeded: {cw}"


def test_compute_crop_never_narrows_an_already_wide_crop():
    _, _, cw, _ = daemon.compute_crop([972, 25, 1026, 117], 1280, 720)
    assert cw >= 281, "widening must never shrink the existing width"


def test_compute_crop_rounds_instead_of_truncating():
    # 0.22 * 1280 = 281.6 -> 282. Truncation quietly shaved a pixel off every crop.
    _, _, cw, _ = daemon.compute_crop([600, 300, 610, 320], 1280, 720)
    assert cw == 282


def test_compute_crop_keeps_the_rect_inside_the_frame_after_widening():
    # A tall box hard against the right edge: widening must not push the rect out.
    x, y, cw, ch = daemon.compute_crop([1240, 100, 1275, 600], 1280, 720)
    assert 0 <= x and x + cw <= 1280
    assert 0 <= y and y + ch <= 720


def test_compute_crop_clips_an_overflowing_detector_box():
    # Detector coordinates may run past an edge; only the visible subject should guide
    # the crop, otherwise the centre can be pulled outside the actual image.
    x, y, cw, ch = daemon.compute_crop([-50, 100, 40, 300], 1280, 720)
    assert x == 0 and y >= 0 and x + cw <= 1280 and y + ch <= 720


def test_compute_crop_rejects_a_box_outside_the_frame():
    assert daemon.compute_crop([-80, 100, -10, 300], 1280, 720) is None


def test_crop_for_subject_clamps_the_scaled_rect_inside_the_native_frame(tmp_path):
    # Scaling assumes the native frame has the scored frame's aspect ratio. A rotation or
    # a letterboxed stream breaks that, and an overflowing rect only shows up as a failed
    # ffmpeg and an alert quietly missing its zoom — so clamp instead.
    cam = _cam(crop_to_subject=True, crop_from_native=True,
               scorer={"url": "http://x/score", "tiles": 2})
    small = tmp_path / "small.jpg"
    small.write_bytes(b"jpeg-1280")
    native = tmp_path / "native.jpg"
    native.write_bytes(b"jpeg-native")
    scored = {"person": 0.9, "box": [1200, 600, 1270, 715], "w": 1280, "h": 720}
    captured = {}

    def fake_ffmpeg(image, out_path, rect):
        captured["rect"] = rect
        open(out_path, "w").write("crop")

    out = daemon.crop_for_subject(cam, str(small), str(tmp_path), score_result=scored,
                                  run_ffmpeg=fake_ffmpeg, source=str(native),
                                  source_width=3840, source_height=2000)

    x, y, cw, ch = captured["rect"]
    assert x >= 0 and y >= 0
    assert x + cw <= 3840, f"rect overflows native width: {captured['rect']}"
    assert y + ch <= 2000, f"rect overflows native height: {captured['rect']}"
    assert out != str(small)


def test_crop_for_subject_infers_native_height_when_not_given(tmp_path):
    # Older callers pass only the width; the scale factor then implies the height, which
    # is right whenever the aspect ratio is preserved.
    cam = _cam(crop_to_subject=True, crop_from_native=True,
               scorer={"url": "http://x/score", "tiles": 2})
    small = tmp_path / "small.jpg"
    small.write_bytes(b"jpeg-1280")
    native = tmp_path / "native.jpg"
    native.write_bytes(b"jpeg-3840")
    scored = {"person": 0.9, "box": [100, 100, 200, 300], "w": 1280, "h": 720}
    captured = {}

    def fake_ffmpeg(image, out_path, rect):
        captured["rect"] = rect
        open(out_path, "w").write("crop")

    daemon.crop_for_subject(cam, str(small), str(tmp_path), score_result=scored,
                            run_ffmpeg=fake_ffmpeg, source=str(native), source_width=3840)

    plain = daemon.compute_crop(scored["box"], 1280, 720)
    assert captured["rect"] == tuple(round(v * 3.0) for v in plain)


def test_crop_for_subject_scales_native_coordinates_per_axis(tmp_path):
    cam = _cam(crop_to_subject=True, crop_from_native=True,
               scorer={"url": "http://x/score", "tiles": 2})
    small, native = tmp_path / "small.jpg", tmp_path / "native.jpg"
    small.write_bytes(b"jpeg-small")
    native.write_bytes(b"jpeg-native")
    scored = {"person": 0.9, "box": [100, 100, 200, 300], "w": 1280, "h": 720}
    captured = {}

    def fake_ffmpeg(image, out_path, rect):
        captured["rect"] = rect
        open(out_path, "w").write("crop")

    daemon.crop_for_subject(cam, str(small), str(tmp_path), score_result=scored,
                            run_ffmpeg=fake_ffmpeg, source=str(native),
                            source_width=2560, source_height=2160)

    plain = daemon.compute_crop(scored["box"], 1280, 720)
    assert captured["rect"] == (plain[0] * 2, plain[1] * 3,
                                plain[2] * 2, plain[3] * 3)


def test_run_monitor_pass_live_alert_goes_through_the_cropping_sender(monkeypatch, tmp_path):
    # The wiring, not the pieces: the live pass must reach send_alert_photo, so a
    # crop_to_subject camera zooms on the live path too and the sent log keeps the scene.
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "a", "host": "203.0.113.10", "crop_to_subject": True,
        "scorer": {"url": "http://x/score"},
    }]})
    cam = _FakeEventCam([[{"start_time": 100, "events_1": 524290, "alarm_type": 2}]])
    frame = tmp_path / "live.jpg"
    frame.write_bytes(b"\xff\xd8LIVE")
    cropped = []

    monkeypatch.setattr(daemon, "crop_for_subject",
                        lambda *a, **k: cropped.append(a[1]) or a[1])
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: True)
    monkeypatch.setattr(daemon.monitor.enrich, "groq_describe", lambda *a, **k: "a person")

    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "k"}
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1, secrets=secrets,
                            snapshot_for=lambda c: (lambda _cam, _ev: str(frame)),
                            time_str=lambda e: "t")

    assert cropped == [str(frame)], "live alert never reached crop_for_subject"


def test_send_alert_photo_records_camera_and_score_in_the_index(monkeypatch, tmp_path):
    # End to end through the real send path: the index line must name the camera, so a
    # two-camera host can tell from the archive which one fired.
    cam = _cam(name="yard")
    frame = tmp_path / "scene.jpg"
    frame.write_bytes(b"\xff\xd8SCENE")
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    monkeypatch.setattr(daemon.notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"ok":true}'))

    daemon.send_alert_photo(cam, {"telegram_token": "t", "telegram_chat": "c"},
                            str(frame), "caption",
                            score=daemon.scorer.SubjectScore(0.77, 0.31))

    rec = json.loads((archive / "index.jsonl").read_text().strip())
    assert rec["camera"] == "yard"
    assert rec["person"] == 0.77
    assert rec["animal"] == 0.31


def test_crop_for_subject_honours_the_cameras_min_frac(tmp_path):
    # A lower floor is the only lever left once the subject is centred: it decides how
    # much scene surrounds a distant person, and it is per-camera.
    cam = _cam(crop_to_subject=True, crop_min_frac=0.12,
               scorer={"url": "http://x/score", "tiles": 2})
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg")
    result = {"person": 0.9, "box": [600, 300, 640, 400], "w": 1280, "h": 720}
    captured = {}
    def fake_ffmpeg(image, out_path, rect):
        captured["rect"] = rect
        open(out_path, "w").write("crop")
    daemon.crop_for_subject(cam, str(src), str(tmp_path),
                            score_result=result, run_ffmpeg=fake_ffmpeg)
    # 0.12 * 1280 = 153.6 -> 154, against the 282 the 0.22 default would force.
    assert captured["rect"][2] == 154, captured["rect"]
