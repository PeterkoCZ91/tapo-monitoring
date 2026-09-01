"""Tests for the opt-in JSON status endpoint (tapo_monitor.statusd)."""

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg
from tapo_monitor import daemon, statusd


def _app(**observability):
    data = {"cameras": [{"name": "front", "host": "192.0.2.50"},
                        {"name": "yard", "host": "192.0.2.51"}]}
    if observability:
        data["observability"] = observability
    return cfg.load_config_from_dict(data)


def _probed_state():
    """A MonitorState carrying what the daemon would have observed by now."""
    state = daemon.MonitorState()
    state.last_tick_ok = True
    state.last_tick_at = 2000.0
    state.network_reachable = {"front": True, "yard": False}
    state.twin_fleet = {
        "front": {
            "captured_at": 1990.0,
            "health": {"status": "degraded",
                       "layers": {"network": "ok", "api": "ok", "events": "ok",
                                  "rtsp": "down", "storage": "ok"},
                       "causes": ["rtsp"],
                       "reason": "one or more layers are impaired"},
            "drift": {"counts": {"drift": 2, "match": 3,
                                 "unknown": 0, "unsupported": 0}},
        },
    }
    return state


def _serve(snapshot_fn):
    srv = statusd.make_server(snapshot_fn, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── handler ──────────────────────────────────────────────────────────────────

def test_status_returns_the_snapshot_as_json():
    srv, url = _serve(lambda: {"tick": {"ok": True}})
    try:
        with urllib.request.urlopen(f"{url}/status", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/json"
            assert json.loads(resp.read()) == {"tick": {"ok": True}}
    finally:
        srv.shutdown()


def test_unknown_path_is_404():
    srv, url = _serve(lambda: {})
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{url}/metrics", timeout=5)
        assert err.value.code == 404
    finally:
        srv.shutdown()


def test_post_is_not_answered():
    srv, url = _serve(lambda: {})
    try:
        req = urllib.request.Request(f"{url}/status", data=b"x")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 501
    finally:
        srv.shutdown()


def test_snapshot_error_becomes_500_and_the_server_survives():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    srv, url = _serve(flaky)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{url}/status", timeout=5)
        assert err.value.code == 500
        with urllib.request.urlopen(f"{url}/status", timeout=5) as resp:
            assert json.loads(resp.read()) == {"ok": True}
    finally:
        srv.shutdown()


# ── status_snapshot ──────────────────────────────────────────────────────────

def test_snapshot_shape_reflects_twin_and_tick_state():
    snap = statusd.status_snapshot(_app(), _probed_state(),
                                   started_at=1000.0, now=2050.0)
    assert set(snap) == {"package", "started_at", "now", "tick", "cameras"}
    assert snap["started_at"] == 1000.0
    assert snap["now"] == 2050.0
    assert snap["tick"] == {"ok": True, "at": 2000.0}
    assert set(snap["package"]) == {"version", "fingerprint"}
    assert snap["package"]["version"]
    front = snap["cameras"]["front"]
    assert front["reachable"] is True
    assert front["drift_count"] == 2
    assert front["probed_at"] == 1990.0
    assert front["health"]["status"] == "degraded"
    assert front["health"]["layers"]["rtsp"] == "down"


def test_snapshot_covers_cameras_the_twin_has_not_probed():
    snap = statusd.status_snapshot(_app(), _probed_state(),
                                   started_at=1000.0, now=2050.0)
    assert snap["cameras"]["yard"] == {
        "reachable": False, "health": None, "drift_count": None, "probed_at": None,
    }


def test_snapshot_is_json_safe_and_free_of_camera_addresses():
    text = json.dumps(statusd.status_snapshot(_app(), _probed_state(),
                                              started_at=1000.0))
    assert "192.0.2.50" not in text
    assert "192.0.2.51" not in text


# ── start (the daemon's wiring point) ────────────────────────────────────────

def test_start_is_off_by_default():
    assert statusd.start(_app(), daemon.MonitorState()) is None


def test_start_serves_the_endpoint_when_opted_in():
    app = _app(status_port=_free_port())
    srv = statusd.start(app, _probed_state(), started_at=1000.0)
    assert srv is not None
    try:
        assert srv.server_address[0] == "127.0.0.1"
        url = f"http://127.0.0.1:{app.observability.status_port}/status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read())
        assert set(payload["cameras"]) == {"front", "yard"}
        assert payload["started_at"] == 1000.0
    finally:
        srv.shutdown()


def test_start_contains_a_failed_bind():
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert statusd.start(_app(status_port=port), daemon.MonitorState()) is None
    finally:
        holder.close()
