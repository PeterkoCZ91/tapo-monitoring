"""Network diagnosis logged on a camera outage: neighbour state, host Wi-Fi, camera signal."""

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from tapo_monitor import netdiag

IP = "203.0.113.10"


def fake_run(outputs, calls=None):
    """subprocess.run stand-in: argv prefix (tuple) -> stdout, or an exception to raise."""

    def run(argv, **kwargs):
        if calls is not None:
            calls.append((tuple(argv), kwargs))
        for prefix, out in outputs.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(out, BaseException):
                    raise out
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")

    return run


IW_LINK = """Connected to aa:bb:cc:dd:ee:ff (on wlan0)
\tSSID: home-net
\tfreq: 2437
\tRX: 123 bytes (4 packets)
\tsignal: -67 dBm
\trx bitrate: 65.0 MBit/s
\ttx bitrate: 72.2 MBit/s MCS 7 short GI
"""


def test_neighbor_states():
    reachable = json.dumps([{"dst": IP, "dev": "wlan0", "lladdr": "11:22:33:44:55:66",
                             "state": ["REACHABLE"]}])
    run = fake_run({("ip", "-j", "neigh"): reachable})
    assert netdiag.neighbor(IP, run=run) == "neigh=REACHABLE lladdr=11:22:33:44:55:66"
    failed = json.dumps([{"dst": IP, "dev": "wlan0", "state": ["FAILED"]}])
    assert netdiag.neighbor(IP, run=fake_run({("ip", "-j", "neigh"): failed})) == "neigh=FAILED"
    assert netdiag.neighbor(IP, run=fake_run({("ip", "-j", "neigh"): "[]"})) == "neigh=absent"
    assert netdiag.neighbor(IP, run=fake_run({})) == "neigh=unknown"


def test_subprocess_calls_are_bounded_and_shell_free():
    calls = []
    run = fake_run({("ip", "-j", "neigh"): "[]", ("ip", "-j", "route"): "[]"}, calls)
    netdiag.describe(IP, run=run, is_wireless=lambda dev: False, platform="linux")
    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, tuple) and argv[0] in ("ip", "iw")
        assert 0 < kwargs["timeout"] <= 3
        assert not kwargs.get("shell")


def test_wireless_route_reports_host_link():
    route = json.dumps([{"dst": IP, "dev": "wlan0", "prefsrc": "203.0.113.2"}])
    run = fake_run({("ip", "-j", "neigh"): "[]", ("ip", "-j", "route"): route,
                    ("iw", "dev", "wlan0", "link"): IW_LINK})
    text = netdiag.describe(IP, run=run, is_wireless=lambda dev: dev == "wlan0", platform="linux")
    assert "neigh=absent" in text
    assert "host_wifi=wlan0 signal=-67dBm ssid=home-net tx=72.2MBit/s" in text


def test_wired_route_skips_iw():
    calls = []
    route = json.dumps([{"dst": IP, "dev": "eth0"}])
    run = fake_run({("ip", "-j", "neigh"): "[]", ("ip", "-j", "route"): route}, calls)
    text = netdiag.describe(IP, run=run, is_wireless=lambda dev: False, platform="linux")
    assert "via=eth0" in text
    assert not any(argv[0] == "iw" for argv, _ in calls)


def test_missing_iw_and_disconnected_link_degrade_gracefully():
    route = json.dumps([{"dst": IP, "dev": "wlan0"}])
    run = fake_run({("ip", "-j", "neigh"): "[]", ("ip", "-j", "route"): route,
                    ("iw",): FileNotFoundError("iw")})
    text = netdiag.describe(IP, run=run, is_wireless=lambda dev: True, platform="linux")
    assert "host_wifi=wlan0 link=unknown" in text
    run = fake_run({("ip", "-j", "neigh"): "[]", ("ip", "-j", "route"): route,
                    ("iw",): "Not connected.\n"})
    text = netdiag.describe(IP, run=run, is_wireless=lambda dev: True, platform="linux")
    assert "host_wifi=wlan0 link=not-connected" in text


def test_timeouts_and_garbage_are_swallowed():
    run = fake_run({("ip",): subprocess.TimeoutExpired("ip", 2)})
    assert netdiag.describe(IP, run=run, is_wireless=lambda dev: True,
                            platform="linux") == "neigh=unknown route=unknown"
    run = fake_run({("ip",): "not json"})
    assert netdiag.describe(IP, run=run, is_wireless=lambda dev: True,
                            platform="linux") == "neigh=unknown route=unknown"


def test_non_linux_is_skipped():
    def run(*a, **k):
        raise AssertionError("no subprocess off Linux")

    assert netdiag.describe(IP, run=run, platform="darwin") == "netdiag=unsupported"


# ── camera-side signal ────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def executeFunction(self, method, params):
        assert method == "getConnectionType"
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def conn(dbm, level):
    return {"network": {"get_connection_type": {"link_type": "wifi", "rssi": str(level),
                                                "rssiValue": str(dbm), "ssid": "x"}}}


def test_read_signal_parses_and_rejects_junk():
    assert netdiag.read_signal(Client([conn(-58, 3)])) == (-58, 3)
    assert netdiag.read_signal(Client([{"network": {}}])) is None
    assert netdiag.read_signal(Client([Exception("-40210")])) is None
    assert netdiag.read_signal(object()) is None


def diag():
    lines = []
    d = netdiag.Diagnostics(spawn=lambda fn: fn(),
                            describe=lambda host: f"neigh=FAILED host={host}")
    return d, lines


def test_signal_is_polled_rarely_and_logged_hourly_or_on_level_change(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.netdiag")
    d, _ = diag()
    client = Client([conn(-58, 3), conn(-60, 3), conn(-75, 1), conn(-74, 1)])
    d.poll_signal("cam", client, now=0)
    d.poll_signal("cam", client, now=10)          # not due yet: no camera call
    assert client.calls == 1
    d.poll_signal("cam", client, now=netdiag.SIGNAL_POLL_EVERY)       # same level: quiet
    d.poll_signal("cam", client, now=2 * netdiag.SIGNAL_POLL_EVERY)   # level 3 -> 1: logged
    d.poll_signal("cam", client, now=2 * netdiag.SIGNAL_POLL_EVERY + netdiag.SIGNAL_LOG_EVERY)  # hourly
    assert client.calls == 4
    lines = [r.getMessage() for r in caplog.records if "wifi signal" in r.getMessage()]
    assert len(lines) == 3
    assert "cam" in lines[0] and "-58 dBm" in lines[0] and "level=3/4" in lines[0]
    assert "-75 dBm" in lines[1] and "level=1/4" in lines[1]


def test_unsupported_signal_query_backs_off(caplog):
    d, _ = diag()
    client = Client([Exception("-40210")] * 3)
    d.poll_signal("cam", client, now=0)
    d.poll_signal("cam", client, now=netdiag.SIGNAL_POLL_EVERY)
    assert client.calls == 1
    d.poll_signal("cam", client, now=netdiag.SIGNAL_LOG_EVERY)
    assert client.calls == 2


def test_outage_diagnosis_is_rate_limited_and_names_last_signal(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.netdiag")
    d, _ = diag()
    d.poll_signal("cam", Client([conn(-71, 2)]), now=1000)
    d.network_event("cam", IP, reachable=False, was_reachable=True, now=1600)    # start
    d.network_event("cam", IP, reachable=False, was_reachable=False, now=1660)   # quiet
    d.network_event("cam", IP, reachable=False, was_reachable=False,
                    now=1600 + netdiag.REPEAT_EVERY - 1)                          # quiet
    d.network_event("cam", IP, reachable=False, was_reachable=False,
                    now=1600 + netdiag.REPEAT_EVERY)                              # repeat
    d.network_event("cam", IP, reachable=True, was_reachable=False, now=4000)    # recovered
    d.network_event("cam", IP, reachable=True, was_reachable=True, now=4060)     # quiet
    lines = [r for r in caplog.records if "network diagnosis" in r.getMessage()]
    assert [r.levelname for r in lines] == ["WARNING", "WARNING", "INFO"]
    first = lines[0].getMessage()
    assert "cam" in first and "outage" in first and f"neigh=FAILED host={IP}" in first
    assert "camera_wifi=-71dBm/2 age=600s" in first
    assert "still down" in lines[1].getMessage() and "down=1800s" in lines[1].getMessage()
    assert "recovered" in lines[2].getMessage() and "down=2400s" in lines[2].getMessage()


def test_outage_without_any_signal_reading_says_unknown(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.netdiag")
    d, _ = diag()
    d.network_event("cam", IP, reachable=False, was_reachable=None, now=5)
    [line] = [r.getMessage() for r in caplog.records if "network diagnosis" in r.getMessage()]
    assert "camera_wifi=unknown" in line


def test_diagnosis_never_raises_and_runs_off_the_caller(caplog):
    spawned = []

    def boom(host):
        raise RuntimeError("x")

    d = netdiag.Diagnostics(spawn=spawned.append, describe=boom)
    d.network_event("cam", IP, reachable=False, was_reachable=True, now=0)
    assert len(spawned) == 1          # the slow part is handed to a worker, not run here
    spawned[0]()                      # and the worker swallows its own failure


def test_daemon_connect_triggers_diagnosis_on_outage(monkeypatch):
    from tapo_monitor import camera, daemon
    from tapo_monitor import config as cfgmod

    cam = cfgmod.load_config_from_dict(
        {"cameras": [{"name": "c", "host": IP}]}).cameras[0]
    events = []
    state = daemon.MonitorState()
    monkeypatch.setattr(state.net_diag, "network_event",
                        lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(camera, "ping_reachable", lambda host: False)
    client, err = daemon._connect_camera({}, state, now=100)(cam)
    assert client is None
    [(args, kwargs)] = events
    assert args[:2] == ("c", IP)
    assert kwargs == {"reachable": False, "was_reachable": None, "now": 100}


@pytest.mark.parametrize("answer", [conn(-58, 3)])
def test_daemon_connect_polls_camera_signal_after_login(monkeypatch, answer):
    from tapo_monitor import camera, daemon
    from tapo_monitor import config as cfgmod

    cam = cfgmod.load_config_from_dict(
        {"cameras": [{"name": "c", "host": IP}]}).cameras[0]
    state = daemon.MonitorState()
    polled = []
    monkeypatch.setattr(state.net_diag, "poll_signal",
                        lambda name, client, now: polled.append((name, now)))
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: (lambda: None))
    monkeypatch.setattr(camera, "connect", lambda f: (Client([answer]), None))
    monkeypatch.setattr(camera, "observe_client_requests", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "resolve_camera_credentials", lambda c: ("u", "p", None))
    client, err = daemon._connect_camera({}, state, now=100)(cam)
    assert client is not None
    assert polled == [("c", 100)]
