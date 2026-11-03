"""ONVIF client rebuilds back off while a camera is down, and their garbage is collected.

Each ``panlimit.build_ptz`` parses the WSDL/XSD into ~2.8 MB of cyclic garbage. Rebuilt
on every 6 s pan-guard poll while a camera was offline, it took a Pi to OOM in an hour.
"""

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, stopcontrol
from tests.test_stopcontrol import FakeGuard, FakePTZ, NativeClient, app_config


def _app():
    return cfg.load_config_from_dict({"cameras": [{
        "name": "camera-a", "host": "203.0.113.10", "pan_limit": {
            "enabled": True, "margin": 0.01, "poll_interval": 6,
            "onvif_user_env": "U", "onvif_password_env": "P"}}]})


@pytest.fixture
def collected(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.gc, "collect", lambda *a: calls.append(1) or 0)
    monkeypatch.setattr(stopcontrol.gc, "collect", lambda *a: calls.append(1) or 0)
    return calls


def _patch(monkeypatch, builds, *, fail=True):
    def build(*args, **kwargs):
        builds.append(kwargs)
        if fail:
            raise ConnectionError("offline")
        return "PTZ", "tok"

    monkeypatch.setattr(daemon.panlimit, "build_ptz", build)
    monkeypatch.setattr(daemon.panlimit, "read_preset_bounds", lambda p, t: (0.39, "1", 0.61, "3"))
    monkeypatch.setattr(daemon.panlimit, "read_pan_x", lambda p, t: 0.5)
    monkeypatch.setattr(daemon.panlimit, "goto_preset", lambda *a: None)


def _poll_for(app, state, seconds, step=6):
    for now in range(0, seconds, step):
        daemon._pan_guard_pass(app, {}, state, now=1000 + now, secrets={}, night=True)


def test_failed_builds_back_off_instead_of_rebuilding_every_poll(monkeypatch, collected):
    app, state, builds = _app(), daemon.MonitorState(), []
    _patch(monkeypatch, builds)
    _poll_for(app, state, 3600)                  # one hour offline: 600 polls
    assert 5 <= len(builds) <= 25                # 600 without the backoff
    assert len(collected) == len(builds)         # every failed build is collected
    assert all(0 < k.get("timeout", 0) <= 5 for k in builds)
    g = state.pan_guard["camera-a"]
    assert g["retry_at"] - g["last_poll"] <= daemon.PAN_GUARD_REBUILD_CAP


def test_rebuild_backoff_is_capped_and_overflow_safe(monkeypatch, collected):
    app, state, builds = _app(), daemon.MonitorState(), []
    _patch(monkeypatch, builds)
    state.pan_guard["camera-a"] = {"fails": 10**6}
    daemon._pan_guard_pass(app, {}, state, now=1000, secrets={}, night=True)
    g = state.pan_guard["camera-a"]
    assert g["retry_at"] == 1000 + daemon.PAN_GUARD_REBUILD_CAP


def test_successful_build_resets_the_backoff(monkeypatch, collected):
    app, state, builds = _app(), daemon.MonitorState(), []
    _patch(monkeypatch, builds)
    _poll_for(app, state, 600)
    _patch(monkeypatch, builds, fail=False)
    g = state.pan_guard["camera-a"]
    daemon._pan_guard_pass(app, {}, state, now=g["retry_at"], secrets={}, night=True)
    assert "ptz" in g and "fails" not in g and "retry_at" not in g
    before = len(builds)
    daemon._pan_guard_pass(app, {}, state, now=g["last_poll"] + 6, secrets={}, night=True)
    assert len(builds) == before                 # the cached client is reused


def test_failure_of_a_cached_client_does_not_force_collection(monkeypatch, collected):
    app, state, builds = _app(), daemon.MonitorState(), []
    _patch(monkeypatch, builds, fail=False)
    daemon._pan_guard_pass(app, {}, state, now=1000, secrets={}, night=True)

    def broken(*a):
        raise TimeoutError("read")

    monkeypatch.setattr(daemon.panlimit, "read_pan_x", broken)
    daemon._pan_guard_pass(app, {}, state, now=1006, secrets={}, night=True)
    assert collected == []
    assert "ptz" not in state.pan_guard["camera-a"]


def test_no_rebuild_while_the_network_check_reports_the_camera_down(monkeypatch, collected):
    app, state, builds = _app(), daemon.MonitorState(), []
    _patch(monkeypatch, builds)
    state.network_reachable["camera-a"] = False
    _poll_for(app, state, 600)
    assert builds == []


# ── StopControl.start ────────────────────────────────────────────────────────────────

class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_stop_control_rebuild_backs_off_and_collects(collected):
    clock, builds = Clock(), []
    client, ptz = NativeClient(), FakePTZ()
    state = {"fail": True}

    def build(*args):
        builds.append(args)
        if state["fail"]:
            raise ConnectionError("offline")
        return ptz, "profile"

    controller = stopcontrol.StopControl(app_config().cameras[0], lambda: client,
                                         build_ptz=build, guard_factory=FakeGuard, clock=clock)
    attempts = 0
    for t in range(0, 3600, 60):                 # one control pass a minute for an hour
        clock.now = t
        with pytest.raises(Exception):  # noqa: B017 - caller treats any failure as "inhibit"
            controller.start()
        attempts += 1
    assert attempts == 60
    assert 4 <= len(builds) <= 15
    assert len(collected) == len(builds)
    state["fail"] = False
    clock.now = controller._retry_at
    controller.start()
    assert controller.guard is not None and controller._fails == 0


def test_stop_control_backoff_survives_huge_failure_counts(collected):
    clock = Clock()
    controller = stopcontrol.StopControl(
        app_config().cameras[0], lambda: None, guard_factory=FakeGuard, clock=clock,
        build_ptz=lambda *a: (_ for _ in ()).throw(ConnectionError("x")))
    controller._fails = 10**6
    with pytest.raises(ConnectionError):
        controller.start()
    assert controller._retry_at == stopcontrol.REBUILD_CAP
