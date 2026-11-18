"""A camera that sleeps between events is never logged in to by the daemon."""
from tapo_monitor import camera as camera_mod
from tapo_monitor import config as cfg
from tapo_monitor import daemon

SECRETS = {"telegram_token": "t", "telegram_chat": "c", "groq_key": ""}


def _wake_app(**extra):
    cam = {"name": "door", "host": "192.0.2.70", "detection": {"sources": ["batterywake"]}}
    cam.update(extra)
    return cfg.load_config_from_dict({"cameras": [cam]})


def _hub_app():
    return cfg.load_config_from_dict({"cameras": [
        {"name": "gate", "host": "192.0.2.50", "detection": {"sources": ["hubpoll"]},
         "hub_host": "192.0.2.60", "go2rtc_src": "gate", "enrich": {"groq": False}}]})


def test_connect_skips_batterywake_camera(monkeypatch):
    pinged = []
    monkeypatch.setattr(camera_mod, "ping_reachable",
                        lambda host, **k: pinged.append(host) or True)
    monkeypatch.setattr(camera_mod, "connect",
                        lambda factory: (_ for _ in ()).throw(AssertionError("logged in")))
    monkeypatch.setattr(camera_mod, "tapo_factory",
                        lambda *a: (_ for _ in ()).throw(AssertionError("built client")))
    app = _wake_app()
    state = daemon.MonitorState()
    clients = {}

    client, err = daemon._connect_camera(clients, state, 0)(app.cameras[0])

    assert client is None and err == "sleeping"
    assert clients == {} and pinged == []
    assert state.connect_fails == {} and state.network_reachable == {}


def test_connect_hubpoll_camera_keeps_its_own_reason():
    app = _hub_app()
    result = daemon._connect_camera({}, daemon.MonitorState(), 0)(app.cameras[0])
    assert result == (None, "hubpoll")


def test_outage_loop_skips_batterywake(monkeypatch):
    sent = []
    monkeypatch.setattr(daemon.notify, "send_text", lambda t, c, text: sent.append(text) or True)
    monkeypatch.setattr(daemon.notify, "send_operational",
                        lambda t, c, text: sent.append(text) or True)
    app = _wake_app()
    state = daemon.MonitorState()
    state.network_reachable["door"] = False

    daemon._watchdog_pass(app, {}, state, now=0, secrets=SECRETS)
    daemon._watchdog_pass(app, {}, state, now=100_000, secrets=SECRETS)

    assert sent == [] and state.fail_since == {}


def test_status_marks_batterywake_without_control_health():
    app = _wake_app()
    state = daemon.MonitorState()
    state.twin_fleet["door"] = {"captured_at": 90, "health": {"status": "ok"}}

    snap = daemon.fleet_health_snapshot(app, state, now=100)

    entry = snap["cameras"]["door"]
    assert entry["control_health"] is None
    assert entry["sleeps"] is True and "hub" not in entry


def test_twin_and_pan_guard_never_touch_a_batterywake_camera(monkeypatch):
    app = _wake_app(pan_limit={"enabled": True})
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.panlimit, "build_ptz",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ONVIF login")))
    probed = []

    daemon._pan_guard_pass(app, {}, state, now=1000, secrets={}, night=True)
    daemon.process_digital_twin(app, {"door": object()}, state, now=1000, secrets={},
                                probe=lambda *a, **k: probed.append(1) or {})

    assert probed == [] and "door" not in state.pan_guard
