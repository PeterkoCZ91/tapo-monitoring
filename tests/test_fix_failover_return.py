"""Failed-over cameras return to the primary address once it is stable again."""

import types

import pytest

from tapo_monitor import camera, cli, daemon, health
from tapo_monitor import config as cfg

A, B = "203.0.113.10", "198.51.100.20"


def _cam():
    return cfg.load_config_from_dict(
        {"cameras": [{"name": "c", "host": [A, B]}]}).cameras[0]


def test_should_return_rules():
    cam = _cam()
    assert not camera.should_return_to_primary(cam, 1000, 0, 5)   # already primary
    camera.set_active_host(cam, B)
    assert camera.should_return_to_primary(cam, 1000, 0, 3)
    assert not camera.should_return_to_primary(cam, 1000, 0, 2)          # streak too short
    assert not camera.should_return_to_primary(cam, 1000, 800, 3)        # switched <5 min ago
    assert not camera.should_return_to_primary(cam, 1000, 0, 3, rejected_until=2000)
    assert not camera.should_return_to_primary(cam, None, 0, 3)


def _st():
    return daemon.MonitorState()


def test_return_needs_three_consecutive_probes(monkeypatch):
    cam, st = _cam(), _st()
    camera.set_active_host(cam, B)
    monkeypatch.setattr(daemon, "PRIMARY_PROBE_INTERVAL", 0)
    answers = iter([True, True, False, True, True, True])
    monkeypatch.setattr(camera, "tcp_reachable", lambda h, **k: next(answers))
    results = [daemon._maybe_return_to_primary(
            cam, st, {}, 10_000 + i * daemon.PRIMARY_PROBE_INTERVAL) for i in range(6)]
    assert results[:5] == [None] * 5            # the failed probe reset the streak
    assert results[5] == B
    assert camera.active_host(cam) == A


def test_no_return_right_after_switch(monkeypatch):
    cam, st = _cam(), _st()
    camera.set_active_host(cam, B)
    st.host_switched_at["c"] = 9_900
    monkeypatch.setattr(daemon, "PRIMARY_PROBE_INTERVAL", 0)
    monkeypatch.setattr(camera, "tcp_reachable", lambda h, **k: True)
    for _ in range(5):
        assert daemon._maybe_return_to_primary(cam, st, {}, 10_000) is None
    assert camera.active_host(cam) == B


def test_confirm_return_clears_saved_choice_and_audits(caplog):
    cam, st = _cam(), _st()
    camera.set_active_host(cam, A)
    st.active_hosts["c"] = B
    client = types.SimpleNamespace(
        getBasicInfo=lambda: {"device_info": {"basic_info": {"mac": "AA-BB"}}})
    st.host_identity["c"] = "AA-BB"
    with caplog.at_level("INFO"):
        assert daemon._confirm_switch(cam, st, client, B, 5000)
    assert "c" not in st.active_hosts
    assert "action=host_return" in caplog.text


def test_returned_other_device_is_rejected(monkeypatch):
    cam, st = _cam(), _st()
    camera.set_active_host(cam, A)
    st.host_identity["c"] = "AA-BB"
    client = types.SimpleNamespace(
        getBasicInfo=lambda: {"device_info": {"basic_info": {"mac": "CC-DD"}}},
        close=lambda: None)
    assert not daemon._confirm_switch(cam, st, client, B, 5000)
    assert camera.active_host(cam) == B
    assert A in st.failover_rejected["c"]


def test_cli_uses_saved_failover_host(tmp_path, monkeypatch):
    cam = _cam()
    path = tmp_path / "health.json"
    monkeypatch.setattr(health, "default_state_path", lambda: str(path))
    saved = daemon.MonitorState()
    saved.active_hosts["c"] = B
    assert health.save_state(str(path), saved)
    cli._use_saved_host(cam)
    assert camera.active_host(cam) == B


def test_primary_is_probed_at_most_once_per_interval(monkeypatch):
    cam, st = _cam(), _st()
    camera.set_active_host(cam, B)
    probes = []
    monkeypatch.setattr(camera, "tcp_reachable", lambda h, **k: probes.append(h) or False)
    for second in range(0, daemon.PRIMARY_PROBE_INTERVAL):
        daemon._maybe_return_to_primary(cam, st, {}, 10_000 + second)
    assert len(probes) == 1
    daemon._maybe_return_to_primary(cam, st, {}, 10_000 + daemon.PRIMARY_PROBE_INTERVAL)
    assert len(probes) == 2


# -- return driven through the real connect() ----------------------------------

import json

from tests.test_host_failover import _Client


def _ready(monkeypatch, *, mac="AA-BB", login_ok=None):
    """Camera failed over to B; both ping, primary A answers TCP. ``login_ok``: hosts that log in."""
    built = []
    monkeypatch.setattr(daemon, "PRIMARY_PROBE_INTERVAL", 0)
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tcp_reachable", lambda host, **k: True)

    def factory(host, *a):
        built.append(host)
        return lambda: _Client(mac if isinstance(mac, str) else mac[host])
    monkeypatch.setattr(camera, "tapo_factory", factory)

    def connect(f):
        client = f()
        host = built[-1]
        if login_ok is not None and host not in login_ok:
            return None, ConnectionError("login")
        return client, None
    monkeypatch.setattr(camera, "connect", connect)
    monkeypatch.setattr(camera, "close_client", lambda c: None)
    cam, st = _cam(), _st()
    camera.set_active_host(cam, B)
    st.active_hosts["c"] = B
    st.host_identity["c"] = "AA-BB"
    return cam, st, built


def _pass(connect, cam, n):
    out = None
    for _ in range(n):
        out = connect(cam)
    return out


def test_return_happens_through_connect_and_clears_saved_choice(monkeypatch):
    cam, st, built = _ready(monkeypatch)
    connect = daemon._connect_camera({}, st, now=10_000)
    client, err = _pass(connect, cam, 3)
    assert err is None and camera.active_host(cam) == A
    assert "c" not in st.active_hosts


def test_return_is_not_tried_during_connect_backoff(monkeypatch):
    cam, st, built = _ready(monkeypatch)
    st.connect_backoff_until["c"] = 20_000
    connect = daemon._connect_camera({}, st, now=10_000)
    for _ in range(5):
        assert connect(cam)[1] == "backoff"
    assert camera.active_host(cam) == B and st.active_hosts == {"c": B}


def test_failed_login_on_primary_keeps_working_camera_out_of_backoff(monkeypatch):
    cam, st, built = _ready(monkeypatch, login_ok={B})
    connect = daemon._connect_camera({}, st, now=10_000)
    client, err = _pass(connect, cam, 3)
    assert client is not None and err is None          # relogged on the secondary
    assert camera.active_host(cam) == B and st.active_hosts == {"c": B}
    assert "c" not in st.connect_fails and "c" not in st.connect_backoff_until
    assert A in st.failover_rejected["c"]


def test_auth_failure_on_primary_is_rejected_for_an_hour_and_keeps_choice(monkeypatch):
    cam, st, built = _ready(monkeypatch)

    class AuthFailing(_Client):
        def getBasicInfo(self):
            return super().getBasicInfo()

    real = camera.connect

    def connect(f):
        client, err = real(f)
        if built[-1] == A:
            client._tapo_auth_failed = True
        return client, err
    monkeypatch.setattr(camera, "connect", connect)
    c = daemon._connect_camera({}, st, now=10_000)
    client, err = _pass(c, cam, 3)
    assert client is not None
    assert camera.active_host(cam) == B and st.active_hosts == {"c": B}
    assert st.failover_rejected["c"][A] - 10_000 == daemon.FAILOVER_AUTH_REJECT_SECONDS


def test_mac_mismatch_rejection_grows_after_three_in_a_row():
    st = _st()
    seconds = [daemon._reject_seconds(st, "c", A) for _ in range(6)]
    assert seconds[:2] == [600, 600]
    assert seconds[2] == 600 or seconds[2] > 600
    assert seconds[3] > seconds[2] and seconds[-1] <= 6 * 3600
    assert seconds[4] == 2 * seconds[3]


def test_restart_restores_hold_down(monkeypatch):
    cam, st, _ = _ready(monkeypatch)
    st.host_switched_at.clear()
    st.active_hosts["c"] = B
    camera.set_active_host(cam, A)
    monkeypatch.setattr(daemon._time, "time", lambda: 5000.0)
    daemon.apply_active_hosts([cam], st)
    assert st.host_switched_at["c"] == 5000.0


def test_damaged_field_does_not_drop_active_hosts(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"version": 1, "state": {"online_since": "bad"},
                                "active_hosts": {"c": B}}))
    st = _st()
    st.online_since = {"c": 7.0}
    # online_since is a persisted field: its damaged shape makes restore() raise
    with pytest.raises(ValueError):
        health.restore(st, json.loads(path.read_text()))
    assert health.load_state(str(path), st) is False   # reported as a failed load...
    assert st.active_hosts == {"c": B}                 # ...yet the failed-over host survived
    assert st.online_since == {"c": 7.0}               # and the damaged field was not applied
