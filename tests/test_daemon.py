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
