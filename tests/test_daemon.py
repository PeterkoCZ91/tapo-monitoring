import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg
from tapo_monitor import daemon


def _cam(**overrides):
    base = {"name": "c", "host": "1.1.1.1"}
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


# ── run_once (injected deps) ─────────────────────────────────────────────────

def test_run_once_plans_each_camera():
    app = cfg.load_config_from_dict({"cameras": [
        {"name": "a", "host": "1.1.1.1"},
        {"name": "b", "host": "1.1.1.2", "role": "static"},
    ]})
    plans = daemon.run_once(app, now=1000, is_night=lambda: True, is_raining=lambda *a, **k: False)
    assert plans["a"].autotrack_on is True
    assert plans["b"].autotrack_on is False


def test_run_once_skips_weather_when_strategy_none():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "1.1.1.1"}]})
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
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "1.1.1.1"}]})

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
        "cameras": [{"name": "a", "host": "1.1.1.1"}],
    })
    secrets = daemon.resolve_secrets(app)
    assert secrets == {"telegram_token": "tok123", "telegram_chat": "555",
                       "groq_key": "gk789", "face_names": {12: "alice"}}


def test_resolve_secrets_missing_env_is_empty(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    app = cfg.load_config_from_dict({
        "telegram": {"token_env": "MISSING_TOKEN", "chat_id_env": "MISSING_CHAT"},
        "groq": {},
        "cameras": [{"name": "a", "host": "1.1.1.1"}],
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
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "1.1.1.1"}]})
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
        {"name": "a", "host": "1.1.1.1", "detection": {"sources": ["onvif"]}},
    ]})
    cam = _FakeEventCam([[{"start_time": 100, "event_type": "personDetection"}]])
    state = daemon.MonitorState()
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    daemon.run_monitor_pass(app, {"a": cam}, state, now=1, secrets=secrets,
                            snapshot_for=_no_snapshot, time_str=lambda e: "t")
    assert "a" not in state.last_seen


def test_run_monitor_pass_skips_cameras_without_client():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "1.1.1.1"}]})
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
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "1.1.1.1"}]})
    secrets = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}
    calls = {"control": 0, "watchdog": 0, "monitor": 0, "drain": 0}

    def fake_control(app, now, connect):
        calls["control"] += 1
        connect(app.cameras[0])  # populate cam_clients like the real connect does

    def fake_watchdog(app, cc, state, *, now, secrets):
        calls["watchdog"] += 1

    def fake_monitor(app, cc, state, *, now, secrets):
        calls["monitor"] += 1

    def fake_drain(app, cc, state, *, now, secrets):
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
        "cameras": [{"name": "a", "host": "1.1.1.1", "enrich": {"groq": False}}],
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
        "cameras": [{"name": "a", "host": "1.1.1.1"}],  # strict_people defaults True
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
        "cameras": [{"name": "a", "host": "1.1.1.1"}],
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
        host="192.168.1.50",
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
    assert captured["url"] == "rtsp://rtspadmin:rtsppw@192.168.1.50:8554/stream2"


def test_default_snapshot_empty_creds_when_env_missing(monkeypatch):
    monkeypatch.delenv("MISSING_RTSP_USER", raising=False)
    monkeypatch.delenv("MISSING_RTSP_PW", raising=False)
    camcfg = _cam(
        host="192.168.1.51",
        rtsp_user_env="MISSING_RTSP_USER",
        rtsp_password_env="MISSING_RTSP_PW",
    )
    captured = {}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kwargs: captured.setdefault("url", url))
    snap = daemon._default_snapshot(camcfg)
    snap(object(), {"start_time": 1})
    # defaults: port 554, stream1; empty credentials
    assert captured["url"] == "rtsp://:@192.168.1.51:554/stream1"


# ── alert_gate: module-level cooldown helper ──────────────────────────────────

def test_alert_gate_confirmed_gated_only_by_confirmed():
    state = daemon.MonitorState()
    can, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    assert can("person") is True          # nothing sent yet
    on("person")                          # records confirmed at now=1000
    can2, _ = daemon.alert_gate(state, "a", cooldown=120, now=1050)
    assert can2("person") is False        # within cooldown of a confirmed alert


def test_alert_gate_motion_does_not_block_person():
    state = daemon.MonitorState()
    _, on = daemon.alert_gate(state, "a", cooldown=120, now=1000)
    on("motion")                          # a motion alert at now=1000
    can, _ = daemon.alert_gate(state, "a", cooldown=120, now=1050)
    assert can("person") is True          # motion never silences a person
    assert can("motion") is False         # but motion silences motion


# ── process_pending_sd ────────────────────────────────────────────────────────

def _pending(cam_clients, sent, *, sd_ok=True, rtsp_ok=True, snapshot_calls=None):
    """Build app+state+collaborators for process_pending_sd tests."""
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True}]})
    state = daemon.MonitorState()
    def fetch_frames(cfg_, start_time, span=None):
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
                        lambda tok, chat, img, cap: sent.append((img, cap)))
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
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1",
                                    "sd_snapshot": True, "sd_jobs_per_tick": 1}]})
    state = daemon.MonitorState()
    calls = []

    def fetch_frames(cfg_, start_time, span=None):
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
    def fetch_frames(cfg_, start_time, span=None):
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


def test_pending_removes_sd_candidate_frames(monkeypatch, tmp_path):
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    frames = [tmp_path / "f0.jpg", tmp_path / "f1.jpg", tmp_path / "f2.jpg"]
    for frame in frames:
        frame.write_bytes(b"jpg")
    frame_paths = [str(frame) for frame in frames]

    def fetch_frames(cfg_, start_time, span=None):
        return frame_paths

    descs = {frame_paths[0]: "empty scene", frame_paths[1]: "Person in a red jacket",
             frame_paths[2]: "empty scene"}

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": True}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda _key, img: descs[img])
    assert len(sent) == 1
    assert all(not frame.exists() for frame in frames)


def test_pending_removes_rtsp_fallback_snapshot(monkeypatch, tmp_path):
    sent = []
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True}]})
    state = daemon.MonitorState()
    image = tmp_path / "rtsp.jpg"

    def snapshot_for(cfg_):
        def snap(cam, ev):
            image.write_bytes(b"jpg")
            return str(image)
        return snap

    state.pending_sd = [{"camera": "a", "etype": "person",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, lambda _cfg, _start, span=None: [],
                 snapshot_for, sent, monkeypatch)
    assert len(sent) == 1
    assert not image.exists()


def test_pending_all_empty_with_live_sent_sends_nothing(monkeypatch):
    # Live (empty) already went out; SD finds no subject -> no duplicate empty ping.
    sent = []
    app, state, _fetch, snapshot_for = _pending({}, sent)
    frames = ["/tmp/f0.jpg", "/tmp/f1.jpg", "/tmp/f2.jpg"]
    def fetch_frames(cfg_, start_time, span=None):
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
    def fetch_frames(cfg_, start_time, span=None):
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
                        lambda tok, chat, img, cap: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True}]})
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


def test_defer_due_at_follows_camera_event_end_time(monkeypatch):
    # A long event (camera says 73 s) needs a wider window AND a later due time, or the
    # download window end is still inside pytapo's freshness guard when the fetch fires.
    sent = []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True}]})
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

    def fetch_frames(cfg_, start_time, span=None):
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
                        lambda tok, chat, img, cap: sent.append(img))
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "empty scene")
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True,
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
    def fetch_frames(cfg_, start_time, span=None):
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
        {"groq": {}, "cameras": [{"name": "a", "host": "1.1.1.1", "sd_snapshot": True,
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
    def fetch_frames(cfg_, start_time, span=None):
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
    def fetch_frames(cfg_, start_time, span=None):
        return ["/tmp/f0.jpg"]
    state.pending_sd = [{"camera": "a", "etype": "motion",
                         "event": {"start_time": 1000}, "due_at": 1075, "live_sent": False}]
    _run_pending(app, state, {"a": object()}, 1080, fetch_frames, snapshot_for, sent,
                 monkeypatch, groq=lambda *a, **k: "Woman with two children by bicycles")
    assert len(sent) == 1                     # the rescued in-frame photo
