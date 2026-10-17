"""Endstop integration with shared sessions and scheduled camera control."""

from types import SimpleNamespace as NS

import pytest
from requests.exceptions import ConnectTimeout

from tapo_monitor import camera, config, daemon, stopcontrol
from tapo_monitor.stopguard import GuardSnapshot


def app_config(**pan_overrides):
    pan = {"enabled": True, "mode": "stop", "right_preset": "right",
           "stop_margin": 0.1}
    pan.update(pan_overrides)
    return config.load_config_from_dict({"cameras": [{
        "name": "test-camera", "host": "192.168.1.20", "pan_limit": pan,
        "tracking": {"day_preset": "home", "night_preset": None},
    }]})


class NativeClient:
    def __init__(self):
        self.enabled = "on"
        self.requests = []
        self.readbacks = 0
        self.refuse_off = False

    def performRequest(self, request):
        self.requests.append(request)
        value = request["params"]["target_track"]["target_track_info"]["enabled"]
        if value != "off" or not self.refuse_off:
            self.enabled = value

    def setAutoTrackTarget(self, enabled):
        self.performRequest({"method": "setTargetTrackConfig", "params": {
            "target_track": {"target_track_info": {"enabled": "on" if enabled else "off"}},
        }})

    def getAutoTrackTarget(self):
        self.readbacks += 1
        return {"enabled": self.enabled}


class FakePTZ:
    def __init__(self):
        self.zeep_client = NS(transport=NS(operation_timeout=None))
        self.positions = {"far-left": -0.95, "home": 0.0, "right": 0.6, "stray": 0.99}
        self.stops = []

    def GetPresets(self, request):
        assert request == {"ProfileToken": "profile"}
        return [NS(token=token, PTZPosition=NS(PanTilt=NS(x=x)))
                for token, x in self.positions.items()]

    def GetStatus(self, request):
        return NS(Position=NS(PanTilt=NS(x=0.0)))

    def Stop(self, request):
        self.stops.append(request)


class FakeGuard:
    def __init__(self, read, stop, disable, **options):
        self.read, self.stop, self.disable = read, stop, disable
        self.options = options
        self.allowed = False
        self.confirm_rearm = False
        self.started = False
        self.recoveries = []
        self.recovery_ok = False

    def start(self):
        self.started = True

    def snapshot(self):
        return GuardSnapshot(0.0, True, not self.allowed, self.allowed, False,
                             "armed" if self.allowed else "boundary")

    def rearm(self):
        if self.confirm_rearm:
            self.allowed = True
        return self.allowed

    def prepare_recovery(self, target):
        self.recoveries.append(target)
        return self.recovery_ok

    def close(self, timeout):
        self.allowed = False
        return True


def harness(cfg=None):
    cfg = cfg or app_config().cameras[0]
    client, ptz = NativeClient(), FakePTZ()
    controller = stopcontrol.StopControl(cfg, lambda: client,
        build_ptz=lambda *args: (ptz, "profile"), guard_factory=FakeGuard)
    controller.attach(client)
    return controller, client, ptz


def test_explicit_right_boundary_ignores_unrelated_extreme_presets():
    controller, _, ptz = harness()
    controller.start()
    assert controller.guard.options["right_limit"] == 0.6
    assert controller.guard.options["left_limit"] is None
    assert controller.safe_preset("far-left")
    assert not controller.safe_preset("right")
    assert not controller.safe_preset("stray")
    assert controller.guard.read() == 0.0
    controller.guard.stop()
    assert ptz.stops == [{"ProfileToken": "profile", "PanTilt": True, "Zoom": True}]


@pytest.mark.parametrize("operation", ["GetStatus", "Stop"])
def test_observer_retries_a_wrapped_connection_timeout_without_losing_the_operation(operation):
    controller, _, ptz = harness()
    original = getattr(ptz, operation)
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            try:
                raise ConnectTimeout("connect deadline")
            except ConnectTimeout as exc:
                raise RuntimeError("ONVIF wrapper") from exc
        return original(request)

    setattr(ptz, operation, flaky)
    controller.start()
    if operation == "Stop":
        controller.guard.stop()
        assert len(ptz.stops) == 1
    else:
        assert controller.guard.read() == 0.0
    assert len(calls) == 2


@pytest.mark.parametrize("scheduled", ["day_preset", "night_preset"])
def test_unsafe_scheduled_return_is_rejected_before_guard_starts(scheduled):
    cfg = app_config().cameras[0]
    setattr(cfg.tracking, scheduled, "stray")
    controller, client, _ = harness(cfg)
    with pytest.raises(ValueError, match="scheduled preset"):
        controller.start()
    assert controller.guard is None
    client.setAutoTrackTarget(True)
    assert client.enabled == "off"


def test_existing_session_disables_tracking_only_with_positive_readback():
    controller, client, _ = harness()
    controller.start()
    assert controller.guard.disable() is True
    assert client.enabled == "off"
    assert client.readbacks == 1
    client.enabled, client.refuse_off = "on", True
    assert controller.guard.disable() is False
    assert client.readbacks == 2


def test_stale_enable_plan_is_inhibited_until_guard_confirms_rearm():
    controller, client, _ = harness()
    controller.start()
    stale = daemon.CameraPlan(True, False, 60, ("people",), None, back_time=30)
    blocked = controller.control_plan(stale)
    assert blocked.autotrack_on is False
    assert blocked.preset == "home"
    assert blocked.back_time is None
    client.setAutoTrackTarget(stale.autotrack_on)
    assert client.enabled == "off"
    controller.guard.confirm_rearm = True
    assert controller.control_plan(stale) is stale
    client.setAutoTrackTarget(True)
    assert client.enabled == "on"


def test_night_only_home_remains_available_for_latched_recovery():
    cfg = app_config().cameras[0]
    cfg.tracking.day_preset = None
    cfg.tracking.night_preset = "home"
    controller, _, _ = harness(cfg)
    controller.start()
    stale = daemon.CameraPlan(True, False, 60, ("people",), None, back_time=30)
    blocked = controller.control_plan(stale)
    assert blocked.autotrack_on is False
    assert blocked.preset == "home"
    assert blocked.back_time is None


def test_guard_preserves_safe_scheduled_destination_over_fallback_home():
    cfg = app_config().cameras[0]
    cfg.tracking.night_preset = "far-left"
    controller, _, _ = harness(cfg)
    controller.start()
    plan = daemon.CameraPlan(True, False, 60, ("people",), "far-left")
    assert controller.control_plan(plan).preset == "far-left"


def test_run_once_connected_session_obeys_latched_guard(monkeypatch):
    app = app_config()
    controller, client, _ = harness(app.cameras[0])
    state, clients = daemon.MonitorState(), {}
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *args: object())
    monkeypatch.setattr(camera, "connect", lambda factory: (client, None))
    monkeypatch.setattr(stopcontrol, "StopControl", lambda *args, **kwargs: controller)
    applied = []
    def apply(cam, plan, *args, **kwargs):
        applied.append(plan)
        cam.setAutoTrackTarget(plan.autotrack_on)
        return True
    monkeypatch.setattr(daemon, "apply_plan", apply)
    plans = daemon.run_once(app, now=1000, is_night=lambda: True,
                            is_raining=lambda *args, **kwargs: False,
                            connect=daemon._connect_camera(clients, state, now=1000))
    assert state.stop_controls["test-camera"] is controller
    assert clients["test-camera"] is client
    assert controller.guard.started
    assert plans["test-camera"].autotrack_on is False
    assert applied[0].preset == "home"
    assert client.enabled == "off"


def test_legacy_pan_recall_pass_skips_stop_mode(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy recall guard must not control a stop-mode camera")
    monkeypatch.setattr(daemon.panlimit, "build_ptz", forbidden)
    state = daemon.MonitorState()
    daemon._pan_guard_pass(app_config(), {}, state, now=1000, secrets={})
    assert not state.pan_guard


def test_failed_guard_initialization_disables_existing_tracking_immediately(monkeypatch):
    app = app_config()
    controller, client, ptz = harness(app.cameras[0])
    del ptz.positions["right"]
    state, clients = daemon.MonitorState(), {}
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *args: object())
    monkeypatch.setattr(camera, "connect", lambda factory: (client, None))
    monkeypatch.setattr(stopcontrol, "StopControl", lambda *args, **kwargs: controller)
    connected, err = daemon._connect_camera(clients, state, now=1000)(app.cameras[0])
    assert connected is client
    assert err is None
    assert controller.guard is None
    assert client.enabled == "off"
    assert client.readbacks >= 1


def test_recall_needs_guard_authorization_and_a_safe_known_destination():
    controller, client, _ = harness()
    controller.start()
    client._rotation_stop_control = controller
    recalled = []
    client.setPreset = recalled.append
    assert not daemon._recall_preset(client, "stray", camera="test-camera")
    assert not daemon._recall_preset(client, "missing", camera="test-camera")
    assert not controller.guard.recoveries
    assert not daemon._recall_preset(client, "home", camera="test-camera")
    assert not recalled
    assert controller.guard.recoveries == [0.0]
    controller.guard.recovery_ok = True
    assert daemon._recall_preset(client, "home", camera="test-camera")
    assert recalled == ["home"]
    assert controller.guard.recoveries == [0.0, 0.0]


def test_recall_retry_rechecks_recovery_permission_after_failed_attempt():
    controller, client, _ = harness()
    controller.start()
    controller.guard.recovery_ok = True
    client._rotation_stop_control = controller
    recalled = []
    def attempt(preset):
        recalled.append(preset)
        controller.guard.recovery_ok = False
        raise ConnectionError("transport expired")
    client.setPreset = attempt
    assert not daemon._recall_preset(client, "home", camera="test-camera")
    assert recalled == ["home"]
    assert controller.guard.recoveries == [0.0, 0.0]


@pytest.mark.parametrize("settings", [
    {"right_preset": None}, {"right_preset": "right", "left_preset": "right"},
    {"tilt": True}, {"stop_margin": float("nan")}, {"stop_margin": 0},
    {"stop_poll_interval": 0.01}, {"stop_cooldown": 0},
])
def test_invalid_stop_configuration_fails_before_any_camera_connection(settings):
    with pytest.raises(config.ConfigError):
        app_config(**settings)


def test_off_diagnostics_distinguish_missing_retired_and_request_error(caplog):
    from tapo_monitor import sessionguard

    controller, client, _ = harness()
    caplog.set_level("INFO", logger="tapo_monitor.stopcontrol")
    assert controller.disable_tracking()
    assert "result=verified client_unavailable=False" in caplog.messages[-1]
    assert "lock_wait=" in caplog.messages[-1] and "request=" in caplog.messages[-1]
    controller.get_client = lambda: None
    assert controller.disable_tracking() is False
    assert "result=missing client_unavailable=True" in caplog.messages[-1]
    controller.get_client = lambda: client

    def fail(_):
        raise TimeoutError("sensitive credential text")

    client.setAutoTrackTarget = fail
    assert controller.disable_tracking() is False
    assert "result=error client_unavailable=False" in caplog.messages[-1]
    assert "error=TimeoutError" in caplog.messages[-1]
    assert "sensitive" not in caplog.text
    sessionguard.retire_client(client, lambda: None)
    assert controller.disable_tracking() is False
    assert "result=retired client_unavailable=True" in caplog.messages[-1]


def _native_session(monkeypatch):
    app = app_config()
    cfg = app.cameras[0]
    controller, client, _ = harness(cfg)
    clients, state, logins = {}, daemon.MonitorState(), []
    controller.get_client = lambda: clients.get(cfg.name)
    monkeypatch.setattr(camera, 'ping_reachable', lambda host: True)
    monkeypatch.setattr(camera, 'tapo_factory', lambda *args: object())
    def login(factory):
        logins.append(True)
        return client, None
    monkeypatch.setattr(camera, 'connect', login)
    monkeypatch.setattr(stopcontrol, 'StopControl', lambda *a, **k: controller)
    connect = daemon._connect_camera(clients, state, now=1000)
    assert connect(cfg)[0] is client
    state.events_reachable[cfg.name] = True
    return app, controller, client, clients, state, logins


def test_healthy_stop_session_is_reused_without_a_second_login(monkeypatch):
    app, controller, client, clients, state, logins = _native_session(monkeypatch)
    assert daemon._connect_camera(clients, state, now=1060)(app.cameras[0])[0] is client
    assert len(logins) == 1
    assert not client._tapo_session_guard.retired
    assert controller.disable_tracking()


def test_control_pass_keeps_the_emergency_off_session_available(monkeypatch):
    app, controller, client, clients, state, logins = _native_session(monkeypatch)
    calls = []
    def control(app, *, connect, **kwargs):
        # This is the old missing-client window, before the new control pass connects.
        calls.append(controller.disable_tracking())
        assert connect(app.cameras[0])[0] is client
        return {}
    def noop(*a, **k):
        return None
    daemon.loop_step(app, clients, state, now=1060, secrets={}, last_control=1000,
                     control_interval=60, run_control=control, watchdog=noop, inspect=noop,
                     monitor=noop, drain=noop, sample=noop, guard=noop, digest=noop,
                     hubpoll=noop, late=noop, privacy_notice=noop, detection_notice=noop,
                     is_night=lambda: True)
    assert calls == [True] and len(logins) == 1
    assert clients[app.cameras[0].name] is client


@pytest.mark.parametrize('failure', ['events', 'host', 'retired'])
def test_unusable_stop_session_is_retired_before_replacement(monkeypatch, failure):
    app, controller, old, clients, state, _ = _native_session(monkeypatch)
    cfg = app.cameras[0]
    if failure == 'events':
        state.events_reachable[cfg.name] = False
    elif failure == 'host':
        old._rotation_stop_host = 'old-address'
    else:
        camera.close_client(old)
    new = NativeClient()
    def login(factory):
        assert old._tapo_session_guard.retired
        assert cfg.name not in clients
        return new, None
    monkeypatch.setattr(camera, 'connect', login)
    assert daemon._connect_camera(clients, state, now=1060)(cfg)[0] is new
    assert clients[cfg.name] is new
    assert controller.disable_tracking()


def test_unreachable_camera_does_not_leave_a_cached_session_in_event_polling(monkeypatch):
    app, _, old, clients, state, _ = _native_session(monkeypatch)
    monkeypatch.setattr(camera, 'ping_reachable', lambda host: False)
    client, err = daemon._connect_camera(clients, state, now=1060)(app.cameras[0])
    assert client is None and isinstance(err, ConnectionError)
    assert not clients and old._tapo_session_guard.retired


def _observe_guard_drift(monkeypatch, *, allowed=False, expected=True, actual='off'):
    from tests.test_twin import _snapshot
    app = app_config()
    app.observability.digital_twin = True
    app.observability.drift_alerts = True
    controller, client, _ = harness(app.cameras[0])
    controller.start()
    controller.guard.allowed = allowed
    client._rotation_stop_control = controller
    state = daemon.MonitorState()
    state.desired_plans[app.cameras[0].name] = daemon.CameraPlan(expected, False, 60, (), None)
    sent = []
    monkeypatch.setattr(daemon.notify, 'send_operational',
                        lambda token, chat, text: sent.append(text) or True)
    daemon.process_digital_twin(app, {app.cameras[0].name: client}, state, now=1000,
                               secrets={'telegram_token': 't', 'telegram_chat': 'c'},
                               probe=lambda *a, **k: _snapshot(auto=actual))
    return app, controller, state, sent


def test_expected_safety_off_has_one_context_for_immediate_notice_and_digest(monkeypatch):
    from tapo_monitor import twin
    app, controller, state, sent = _observe_guard_drift(monkeypatch)
    observed = state.twin_fleet[app.cameras[0].name]
    assert len(twin.alertable_results(observed)) == 1     # raw diagnostics survive
    assert twin.actionable_results(observed) == []
    assert observed['tracking_inhibition']['reason'] == 'boundary'
    assert sent == []
    # A later rearm must not reinterpret this older observation as spontaneous drift.
    controller.guard.allowed = True
    snap = daemon.fleet_health_snapshot(app, state, now=1001)
    health = snap['cameras'][app.cameras[0].name]['control_health']
    assert health['drift'] == 0 and health['tracking_inhibition']['active']


def test_tracking_on_against_a_safety_off_plan_remains_a_critical_notice(monkeypatch):
    app, _, state, sent = _observe_guard_drift(monkeypatch, expected=False, actual='on')
    assert len(sent) == 1 and 'tracking.auto.enabled' in sent[0]
    health = daemon.fleet_health_snapshot(app, state, now=1001)
    control = health['cameras'][app.cameras[0].name]['control_health']
    assert control['drift'] == 1 and control['tracking_inhibition']['active']


def test_unexpected_off_with_an_armed_guard_still_alerts(monkeypatch):
    app, _, state, sent = _observe_guard_drift(monkeypatch, allowed=True)
    assert len(sent) == 1
    control = daemon.fleet_health_snapshot(app, state, now=1001)
    assert control['cameras'][app.cameras[0].name]['control_health']['drift'] == 1


def test_position_loss_is_visible_even_when_the_control_plan_already_requested_off(monkeypatch):
    app, controller, state, sent = _observe_guard_drift(monkeypatch, expected=False)
    name = app.cameras[0].name
    state.twin_fleet[name]['tracking_inhibition']['reason'] = 'pose_unavailable'
    assert sent == []
    health = daemon.fleet_health_snapshot(app, state, now=1001)
    from tapo_monitor import reviewdigest
    headline = reviewdigest.fleet_lines(health)[0]
    assert 'endstop hold after position loss' in headline
    assert 'Fleet degraded' in headline
