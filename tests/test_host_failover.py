"""Opt-in ``host`` candidate list: one active address, failover when the camera is gone."""

import json
import logging

import pytest

from tapo_monitor import camera, daemon, health
from tapo_monitor import config as cfg

A, B, C = "203.0.113.10", "198.51.100.20", "198.51.100.30"


def _cam(host=None, **extra):
    data = {"name": "c", "host": A if host is None else host}
    data.update(extra)
    return cfg.load_config_from_dict({"cameras": [data]}).cameras[0]


# -- config parsing ----------------------------------------------------------

def test_scalar_host_is_unchanged():
    cam = _cam()
    assert cam.host == A
    assert camera.candidate_hosts(cam) == (A,)
    assert camera.active_host(cam) == A


def test_list_host_keeps_first_as_host_and_all_as_candidates():
    cam = _cam([A, B])
    assert cam.host == A
    assert camera.candidate_hosts(cam) == (A, B)
    assert camera.active_host(cam) == A


@pytest.mark.parametrize("bad", [[], [A, ""], [A, 3], [A, A], 7])
def test_bad_host_values_are_refused(bad):
    with pytest.raises(cfg.ConfigError):
        _cam(bad)


def test_set_active_host_only_accepts_candidates():
    cam = _cam([A, B])
    camera.set_active_host(cam, B)
    assert camera.active_host(cam) == B
    with pytest.raises(ValueError):
        camera.set_active_host(cam, C)


# -- TCP probe ---------------------------------------------------------------

class _Sock:
    def close(self):
        pass


def test_find_alternate_host_returns_first_answering_other_candidate():
    cam = _cam([A, B, C])
    seen = []

    def connect(addr, timeout=None):
        seen.append(addr)
        if addr[0] == C:
            return _Sock()
        raise OSError("down")

    assert camera.find_alternate_host(cam, connect=connect) == C
    assert seen == [(B, 443), (C, 443)]      # never probes the active address


def test_find_alternate_host_none_when_nothing_answers_or_single_host():
    def down(addr, timeout=None):
        raise OSError("down")
    assert camera.find_alternate_host(_cam([A, B]), connect=down) is None
    assert camera.find_alternate_host(_cam(), connect=down) is None


def test_find_alternate_host_skips_rejected_candidates():
    cam = _cam([A, B, C])
    seen = []

    def connect(addr, timeout=None):
        seen.append(addr[0])
        return _Sock()

    assert camera.find_alternate_host(cam, connect=connect, skip=(B,)) == C
    assert seen == [C]


# -- daemon: failover through the existing outage detection --------------------

class _Client:
    def __init__(self, mac):
        self.mac = mac

    def getBasicInfo(self):
        return {"device_info": {"basic_info": {"mac": self.mac}}}


def _patch(monkeypatch, *, up, alt=None, mac="AA-BB"):
    """Ping answers only for hosts in ``up``; clients report ``mac`` (str or per host)."""
    built = []
    monkeypatch.setattr(camera, "ping_reachable", lambda host: host in up)
    monkeypatch.setattr(camera, "find_alternate_host",
                        lambda cam, connect=None, skip=(): alt if alt not in skip else None)

    def factory(host, *a):
        built.append(host)
        return lambda: _Client(mac if isinstance(mac, str) else mac[host])
    monkeypatch.setattr(camera, "tapo_factory", factory)
    monkeypatch.setattr(camera, "connect", lambda f: (f(), None))
    return built


def test_unreachable_active_host_fails_over_after_repeated_misses(monkeypatch, caplog):
    cam = _cam([A, B])
    built = _patch(monkeypatch, up={B}, alt=B)
    state = daemon.MonitorState()
    state.host_identity["c"] = "AA-BB"
    connect = daemon._connect_camera({}, state, now=100)

    with caplog.at_level(logging.INFO):
        first = connect(cam)
        second = connect(cam)

    assert isinstance(first[1], ConnectionError)          # one miss is not a switch
    assert camera.active_host(cam) == B
    assert second[1] is None and built == [B]
    assert state.active_hosts == {"c": B}
    assert state.network_reachable["c"] is True
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "host switch" in m]) == 1
    audit = [m for m in messages if "action=host_switch" in m]
    assert len(audit) == 1 and B not in audit[0] and A not in audit[0]


def test_switch_closes_the_stale_client(monkeypatch):
    cam = _cam([A, B])
    _patch(monkeypatch, up={B}, alt=B)
    closed = []
    monkeypatch.setattr(camera, "close_client", closed.append)
    state = daemon.MonitorState()
    state.host_identity["c"] = "AA-BB"
    stale = object()
    clients = {"c": stale}
    connect = daemon._connect_camera(clients, state, now=100)
    connect(cam)
    connect(cam)
    assert stale in closed
    assert clients["c"] is not stale


def test_different_camera_on_candidate_is_rejected_and_reverted(monkeypatch):
    cam = _cam([A, B])
    _patch(monkeypatch, up={B}, alt=B, mac={A: "AA-BB", B: "CC-DD"})
    state = daemon.MonitorState()
    state.host_identity["c"] = "AA-BB"
    connect = daemon._connect_camera({}, state, now=100)
    connect(cam)
    client, err = connect(cam)
    assert client is None and isinstance(err, ConnectionError)
    assert camera.active_host(cam) == A
    assert state.active_hosts == {}
    assert state.network_reachable["c"] is False
    assert B in state.failover_rejected["c"]      # not retried on the very next pass


def test_single_host_never_probes_alternates(monkeypatch):
    cam = _cam()
    monkeypatch.setattr(camera, "ping_reachable", lambda host: False)
    monkeypatch.setattr(camera, "find_alternate_host",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
    state = daemon.MonitorState()
    connect = daemon._connect_camera({}, state, now=100)
    for _ in range(5):
        connect(cam)
    assert camera.active_host(cam) == A


def test_identity_is_learned_once_per_camera(monkeypatch):
    cam = _cam([A, B])
    _patch(monkeypatch, up={A})
    state = daemon.MonitorState()
    daemon._connect_camera({}, state, now=100)(cam)
    assert state.host_identity == {"c": "AA-BB"}


def test_unverifiable_identity_switches_but_says_so(monkeypatch, caplog):
    cam = _cam([A, B])
    _patch(monkeypatch, up={B}, alt=B)
    state = daemon.MonitorState()                    # nothing learned yet
    connect = daemon._connect_camera({}, state, now=100)
    with caplog.at_level(logging.WARNING):
        connect(cam)
        connect(cam)
    assert camera.active_host(cam) == B
    assert any("unverified" in r.getMessage() for r in caplog.records)


# -- persistence -------------------------------------------------------------

def test_active_host_survives_a_restart(tmp_path):
    path = tmp_path / "health.json"
    state = daemon.MonitorState()
    state.active_hosts["c"] = B
    assert health.save_state(str(path), state)
    assert json.loads(path.read_text())["active_hosts"] == {"c": B}

    fresh = daemon.MonitorState()
    assert health.load_state(str(path), fresh)
    cam = _cam([A, B])
    daemon.apply_active_hosts([cam], fresh)
    assert camera.active_host(cam) == B


def test_persisted_host_no_longer_in_config_is_ignored():
    state = daemon.MonitorState()
    state.active_hosts["c"] = C
    cam = _cam([A, B])
    daemon.apply_active_hosts([cam], state)
    assert camera.active_host(cam) == A
