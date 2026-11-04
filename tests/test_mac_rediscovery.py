"""Opt-in ``mac:``: find a camera that DHCP moved to another address, never recover falsely."""

import json
import logging
from types import SimpleNamespace

import pytest

from tapo_monitor import camera, daemon, health, rediscover
from tapo_monitor import config as cfg

OLD, NEW, OTHER = "192.0.2.10", "192.0.2.77", "192.0.2.99"
MAC = "02:00:00:aa:bb:cc"
MAC_ID = "02-00-00-AA-BB-CC"          # how getBasicInfo reports it
STRANGER = "02:00:00:11:22:33"
REAL_REDISCOVERY = True       # conftest leaves the address lookups to this module's fakes


def _cam(**extra):
    data = {"name": "c", "host": OLD}
    data.update(extra)
    return cfg.load_config_from_dict({"cameras": [data]}).cameras[0]


def _neigh(*entries):
    return json.dumps([{"dst": ip, "dev": "eth0", "lladdr": mac, "state": [st]}
                       for ip, mac, st in entries])


class FakeNet:
    """subprocess.run stand-in for ``ip -j neigh|route|addr`` and ``ping``."""

    def __init__(self, neigh="[]", after_sweep=None, answering=(), route=None, addr=None):
        self.neigh = neigh
        self.after_sweep = after_sweep
        self.answering = set(answering)
        self.route = route if route is not None else json.dumps([{"dst": OLD, "dev": "eth0",
                                                                   "prefsrc": "192.0.2.2"}])
        self.addr = addr if addr is not None else json.dumps([{
            "ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.0.2.2",
                                             "prefixlen": 24}]}])
        self.calls = []
        self.pinged = []

    def __call__(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append((argv, kwargs))
        assert not kwargs.get("shell")
        assert 0 < kwargs["timeout"] <= 5
        if argv[:3] == ("ip", "-j", "neigh"):
            out = self.after_sweep if (self.pinged and self.after_sweep is not None) else self.neigh
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if argv[:3] == ("ip", "-j", "route"):
            return SimpleNamespace(returncode=0, stdout=self.route, stderr="")
        if argv[:3] == ("ip", "-j", "addr"):
            return SimpleNamespace(returncode=0, stdout=self.addr, stderr="")
        if argv[0] == "ping":
            self.pinged.append(argv[-1])
            return SimpleNamespace(returncode=0 if argv[-1] in self.answering else 1,
                                   stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")


def _finder(net, **kw):
    return rediscover.Rediscovery(spawn=lambda job: job(), run=net, platform="linux",
                                  which=lambda name: "/usr/sbin/" + name, **kw)


# -- config ------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["02:00:00:AA:BB:CC", "02-00-00-aa-bb-cc", " 02:00:00:aa:BB:cc "])
def test_mac_is_normalised(raw):
    assert _cam(mac=raw).mac == MAC


@pytest.mark.parametrize("bad", ["02:00:00:aa:bb", "zz:00:00:aa:bb:cc", 7, ""])
def test_bad_mac_is_refused(bad):
    with pytest.raises(cfg.ConfigError, match="mac"):
        _cam(mac=bad)


def test_mac_defaults_off():
    assert _cam().mac is None


# -- the search ----------------------------------------------------------------

def _found_lines(caplog):
    return [r.getMessage() for r in caplog.records if "seen at" in r.getMessage()]


def test_found_in_neighbour_table_without_sweep(caplog):
    net = FakeNet(neigh=_neigh((OLD, STRANGER, "STALE"), (NEW, MAC, "REACHABLE")))
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        assert finder.request("c", MAC, OLD, now=1000)
    assert _found_lines(caplog) == [f"mac rediscovery c: mac {MAC} seen at {NEW} "
                                    "(neighbour table, no sweep)"]
    assert finder.take("c") == NEW
    assert finder.take("c") is None              # handed over once
    assert net.pinged == []                      # the cheap path never sweeps


def test_found_after_a_gentle_sweep(caplog):
    net = FakeNet(after_sweep=_neigh((NEW, MAC, "REACHABLE")), answering={NEW})
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        finder.request("c", MAC, OLD, now=1000)
    assert _found_lines(caplog) == [f"mac rediscovery c: mac {MAC} seen at {NEW} "
                                    "after a sweep (253 addresses, 1 answered)"]
    assert finder.take("c") == NEW
    pings = [(a, k) for a, k in net.calls if a[0] == "ping"]
    assert len(pings) == 253                     # the /24 minus this host's own address
    assert "192.0.2.2" not in net.pinged
    for argv, kwargs in pings:
        wait = int(argv[argv.index("-W") + 1])
        assert 2 <= wait <= 3                    # weak Wi-Fi answers slowly
        assert kwargs["timeout"] > wait
        assert argv[argv.index("-c") + 1] == "1"


def test_sweep_concurrency_is_bounded():
    assert 1 < rediscover.SWEEP_WORKERS <= 32


def test_not_found_leaves_the_camera_where_it_is(caplog):
    net = FakeNet()
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        finder.request("c", MAC, OLD, now=1000)
    assert finder.take("c") is None
    assert any("not found" in r.getMessage() for r in caplog.records)


def test_mac_at_the_current_address_is_not_a_move():
    net = FakeNet(neigh=_neigh((OLD, MAC, "REACHABLE")))
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=1000)
    assert finder.take("c") is None


def test_sweeps_are_rate_limited_with_backoff():
    net = FakeNet()
    finder = _finder(net)

    def sweeps():
        return len([a for a, _ in net.calls if a[:3] == ("ip", "-j", "route")])

    finder.request("c", MAC, OLD, now=0)
    assert sweeps() == 1
    finder.request("c", MAC, OLD, now=30)                    # neighbour reads too are spaced
    assert len([a for a, _ in net.calls if a[:3] == ("ip", "-j", "neigh")]) == 2
    finder.request("c", MAC, OLD, now=rediscover.NEIGH_EVERY + 1)
    assert sweeps() == 1                                     # neigh only, sweep not due
    finder.request("c", MAC, OLD, now=rediscover.SWEEP_EVERY)
    assert sweeps() == 2
    # Second miss in a row: the next sweep waits twice as long.
    finder.request("c", MAC, OLD, now=2 * rediscover.SWEEP_EVERY)
    assert sweeps() == 2
    finder.request("c", MAC, OLD, now=3 * rediscover.SWEEP_EVERY)
    assert sweeps() == 3
    finder.reset("c")                                        # reachable again: start over
    finder.request("c", MAC, OLD, now=3 * rediscover.SWEEP_EVERY + 100)
    assert sweeps() == 4


def test_one_search_in_flight_per_camera():
    jobs = []
    net = FakeNet()
    finder = rediscover.Rediscovery(spawn=jobs.append, run=net, platform="linux",
                                    which=lambda name: "/usr/sbin/ip")
    assert finder.request("c", MAC, OLD, now=0)
    assert not finder.request("c", MAC, OLD, now=10 ** 6)    # still running
    jobs.pop()()
    assert finder.request("c", MAC, OLD, now=10 ** 6)


def test_routed_camera_is_not_swept():
    route = json.dumps([{"dst": OLD, "gateway": "198.51.100.1", "dev": "eth0"}])
    net = FakeNet(route=route)
    _finder(net).request("c", MAC, OLD, now=0)
    assert net.pinged == []


def test_off_linux_or_without_ip_the_feature_logs_once_and_stays_off(caplog):
    net = FakeNet()
    for finder in (rediscover.Rediscovery(spawn=lambda j: j(), run=net, platform="darwin",
                                          which=lambda n: "/usr/sbin/ip"),
                   rediscover.Rediscovery(spawn=lambda j: j(), run=net, platform="linux",
                                          which=lambda n: None)):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
            assert not finder.request("c", MAC, OLD, now=0)
            assert not finder.request("c", MAC, OLD, now=10 ** 6)
        assert len([r for r in caplog.records if "rediscovery" in r.getMessage()]) == 1
        assert finder.own_interface(OLD, now=0) is None
    assert net.calls == []


def test_own_interface_names_the_interface_and_is_cached():
    addr = json.dumps([{"ifname": "wlan0", "addr_info": [{"family": "inet", "local": OLD,
                                                          "prefixlen": 24}]}])
    net = FakeNet(addr=addr)
    finder = _finder(net)
    assert finder.own_interface(OLD, now=0) == "wlan0"
    assert finder.own_interface(NEW, now=1) is None
    assert len(net.calls) == 1


# -- daemon --------------------------------------------------------------------

class _Client:
    def __init__(self, host, mac=MAC_ID):
        self.host = host
        self.mac = mac

    def getBasicInfo(self):
        return {"device_info": {"basic_info": {"mac": self.mac}}}


def _wire(monkeypatch, *, up, macs=None, own=None, neigh_mac=None):
    """Ping answers for ``up``; clients report ``macs[host]``; ``own`` maps local IPs."""
    built, closed = [], []
    monkeypatch.setattr(camera, "ping_reachable", lambda host: host in up)

    def factory(host, *a):
        built.append(host)
        return lambda: _Client(host, (macs or {}).get(host, MAC_ID))
    monkeypatch.setattr(camera, "tapo_factory", factory)
    monkeypatch.setattr(camera, "connect", lambda f: (f(), None))
    monkeypatch.setattr(camera, "close_client", closed.append)
    monkeypatch.setattr(rediscover, "local_addresses", lambda run=None: dict(own or {}))
    monkeypatch.setattr(rediscover, "neighbour_mac",
                        lambda ip, run=None: (neigh_mac or {}).get(ip))
    return built, closed


class _Finder:
    """Records search requests; ``found`` is handed over by ``take``."""

    def __init__(self, found=None):
        self.found = found
        self.requests = []
        self.resets = []
        self.rejected = []

    def available(self):
        return True

    def request(self, name, mac, ip, now):
        self.requests.append((name, mac, ip))
        return True

    def take(self, name):
        found, self.found = self.found, None
        return found

    def reset(self, name):
        self.resets.append(name)

    def reject(self, name, ip, now):
        self.rejected.append(ip)

    def own_interface(self, ip, now):
        return (rediscover.local_addresses() or {}).get(ip)


def _state(finder):
    state = daemon.MonitorState()
    state.rediscovery = finder
    return state


def test_feature_off_without_mac(monkeypatch):
    _wire(monkeypatch, up=set())
    finder = _Finder()
    state = _state(finder)
    connect = daemon._connect_camera({}, state, now=100)
    for _ in range(10):
        connect(_cam())
    assert finder.requests == []


def test_search_starts_after_repeated_misses(monkeypatch):
    _wire(monkeypatch, up=set())
    finder = _Finder()
    state = _state(finder)
    cam = _cam(mac=MAC)
    connect = daemon._connect_camera({}, state, now=100)
    for _ in range(rediscover.SEARCH_AFTER_FAILS - 1):
        connect(cam)
    assert finder.requests == []
    connect(cam)
    assert finder.requests == [("c", MAC, OLD)]


def test_found_mac_switches_every_host_consumer(monkeypatch, caplog, tmp_path):
    built, closed = _wire(monkeypatch, up={NEW})
    finder = _Finder()
    state = _state(finder)
    state.health_path = str(tmp_path / "health.json")
    cam = _cam(mac=MAC)
    stale = object()
    clients = {"c": stale}
    connect = daemon._connect_camera(clients, state, now=100)
    for _ in range(rediscover.SEARCH_AFTER_FAILS):
        connect(cam)
    state.connect_backoff_until["c"] = 10 ** 9           # failed logins before the move
    finder.found = NEW
    with caplog.at_level(logging.INFO):
        client, err = connect(cam)

    assert err is None and client.host == NEW
    assert built == [NEW]                                 # pytapo login on the new address
    assert stale in closed
    assert camera.active_host(cam) == NEW                 # RTSP, ONVIF, stop guard, SD read it
    seen = []
    monkeypatch.setattr(daemon.snapshot, "rtsp_url", lambda host, *a, **k: seen.append(host) or "u")
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp", lambda *a, **k: None)
    daemon._default_snapshot(cam)(None, None)
    assert seen == [NEW]
    assert state.network_reachable["c"] is True
    messages = [r.getMessage() for r in caplog.records]
    moved = [r for r in caplog.records if "moved" in r.getMessage()]
    assert moved and moved[0].levelno == logging.WARNING
    assert f"camera c moved {OLD} -> {NEW} (mac {MAC})" in messages
    assert any("reserve" in m for m in messages)          # hint: fix config / router
    assert state.host_moved_notices["c"]
    saved = json.loads((tmp_path / "health.json").read_text())
    assert saved["discovered_hosts"] == {"c": {"host": NEW, "mac": MAC}}
    assert saved["active_hosts"] == {"c": NEW}


def test_move_is_refused_when_the_device_has_another_mac(monkeypatch):
    built, _ = _wire(monkeypatch, up={NEW}, macs={NEW: "02-00-00-11-22-33"})
    finder = _Finder(found=NEW)
    state = _state(finder)
    cam = _cam(mac=MAC)
    client, err = daemon._connect_camera({}, state, now=100)(cam)
    assert client is None
    assert camera.active_host(cam) == OLD
    assert camera.candidate_hosts(cam) == (OLD,)
    assert finder.rejected == [NEW]
    assert state.discovered_hosts == {}


def test_move_is_refused_when_login_fails(monkeypatch):
    _wire(monkeypatch, up={NEW})
    monkeypatch.setattr(camera, "connect", lambda f: (None, RuntimeError("login")))
    finder = _Finder(found=NEW)
    state = _state(finder)
    cam = _cam(mac=MAC)
    client, _ = daemon._connect_camera({}, state, now=100)(cam)
    assert client is None
    assert camera.active_host(cam) == OLD
    assert finder.rejected == [NEW]


def test_move_notice_is_sent_once(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [{"name": "c", "host": OLD, "mac": MAC}]})
    sent = []
    monkeypatch.setattr(daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    state.host_moved_notices["c"] = f"camera 'c' moved {OLD} -> {NEW}"
    secrets = {"telegram_token": "t", "telegram_chat": "x"}
    daemon._watchdog_pass(app, {}, state, now=0, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=60, secrets=secrets)
    assert len([m for m in sent if "moved" in m]) == 1


def test_discovered_host_survives_a_restart(tmp_path):
    path = tmp_path / "health.json"
    state = daemon.MonitorState()
    state.discovered_hosts["c"] = {"host": NEW, "mac": MAC}
    state.active_hosts["c"] = NEW
    assert health.save_state(str(path), state)

    fresh = daemon.MonitorState()
    assert health.load_state(str(path), fresh)
    cam = _cam(mac=MAC.upper())
    daemon.apply_active_hosts([cam], fresh)
    assert camera.active_host(cam) == NEW
    assert camera.candidate_hosts(cam) == (OLD, NEW)


@pytest.mark.parametrize("mac", [None, STRANGER])
def test_persisted_discovery_is_dropped_without_the_same_mac(tmp_path, mac):
    state = daemon.MonitorState()
    state.discovered_hosts["c"] = {"host": NEW, "mac": MAC}
    state.active_hosts["c"] = NEW
    cam = _cam(**({"mac": mac} if mac else {}))
    daemon.apply_active_hosts([cam], state)
    assert camera.active_host(cam) == OLD
    assert "c" not in state.discovered_hosts


def test_ip_of_this_host_is_not_the_camera(monkeypatch, caplog):
    _wire(monkeypatch, up={OLD}, own={OLD: "wlan0"})
    finder = _Finder()
    state = _state(finder)
    connect = daemon._connect_camera({}, state, now=100)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            client, err = connect(_cam())                # also without mac: never "reachable"
    assert client is None and isinstance(err, ConnectionError)
    assert state.network_reachable["c"] is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [f"camera c IP {OLD} belongs to this host (wlan0)"]
    assert not any("reachable again" in r.getMessage() for r in caplog.records)


def test_ip_of_this_host_starts_the_search(monkeypatch):
    _wire(monkeypatch, up={OLD}, own={OLD: "wlan0"})
    finder = _Finder()
    state = _state(finder)
    connect = daemon._connect_camera({}, state, now=100)
    for _ in range(rediscover.SEARCH_AFTER_FAILS):
        connect(_cam(mac=MAC))
    assert finder.requests == [("c", MAC, OLD)]


def test_other_mac_at_the_camera_address_is_unreachable(monkeypatch, caplog):
    built, _ = _wire(monkeypatch, up={OLD}, neigh_mac={OLD: STRANGER})
    finder = _Finder()
    state = _state(finder)
    connect = daemon._connect_camera({}, state, now=100)
    with caplog.at_level(logging.WARNING):
        for _ in range(rediscover.SEARCH_AFTER_FAILS):
            client, _ = connect(_cam(mac=MAC))
    assert client is None and built == []
    assert state.network_reachable["c"] is False
    assert finder.requests == [("c", MAC, OLD)]
    assert any(STRANGER in r.getMessage() for r in caplog.records)


def test_matching_mac_at_the_camera_address_is_reachable(monkeypatch):
    built, _ = _wire(monkeypatch, up={OLD}, neigh_mac={OLD: MAC})
    finder = _Finder()
    state = _state(finder)
    client, err = daemon._connect_camera({}, state, now=100)(_cam(mac=MAC))
    assert err is None and built == [OLD]
    assert finder.resets == ["c"]


def test_recovery_notice_waits_for_a_working_login(monkeypatch):
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": OLD}]})
    sent = []
    monkeypatch.setattr(daemon.notify, "send_text", lambda tok, chat, msg: sent.append(msg) or True)
    state = daemon.MonitorState()
    secrets = {"telegram_token": "t", "telegram_chat": "x"}
    state.network_reachable["a"] = False
    daemon._watchdog_pass(app, {}, state, now=0, secrets=secrets)
    daemon._watchdog_pass(app, {}, state, now=1900, secrets=secrets)
    assert len(sent) == 1 and "unreachable" in sent[0]

    # Something answers ping at the address, but the camera login keeps failing.
    state.network_reachable["a"] = True
    state.connect_fails["a"] = 2
    daemon._watchdog_pass(app, {}, state, now=2000, secrets=secrets)
    assert len(sent) == 1
    assert "a" in state.fail_since                        # the outage is not over

    state.connect_fails.pop("a")                           # a login worked
    daemon._watchdog_pass(app, {"a": object()}, state, now=2100, secrets=secrets)
    assert len(sent) == 2 and "back online" in sent[1]


def test_return_to_primary_never_probes_an_own_address(monkeypatch):
    monkeypatch.setattr(rediscover, "local_addresses", lambda run=None: {OLD: "wlan0"})
    probed = []
    monkeypatch.setattr(camera, "tcp_reachable", lambda host, **k: probed.append(host) or True)
    cam = _cam(mac=MAC)
    cam.discovered_host = NEW
    camera.set_active_host(cam, NEW)
    state = _state(_Finder())
    assert daemon._maybe_return_to_primary(cam, state, {}, 10 ** 6) is None
    assert probed == []


def test_cli_uses_the_rediscovered_host(tmp_path, monkeypatch):
    from tapo_monitor import cli
    path = tmp_path / "health.json"
    monkeypatch.setattr(health, "default_state_path", lambda: str(path))
    saved = daemon.MonitorState()
    saved.discovered_hosts["c"] = {"host": NEW, "mac": MAC}
    saved.active_hosts["c"] = NEW
    assert health.save_state(str(path), saved)
    cam = _cam(mac=MAC)
    cli._use_saved_host(cam)
    assert camera.active_host(cam) == NEW
    plain = _cam()
    cli._use_saved_host(plain)
    assert camera.active_host(plain) == OLD


def test_failover_never_probes_an_own_address(monkeypatch):
    monkeypatch.setattr(rediscover, "local_addresses", lambda run=None: {OTHER: "wlan0"})
    skipped = []
    monkeypatch.setattr(camera, "find_alternate_host",
                        lambda cam, connect=None, skip=(): skipped.append(set(skip)))
    cam = cfg.load_config_from_dict({"cameras": [{"name": "c", "host": [OLD, OTHER]}]}).cameras[0]
    daemon._fail_over(cam, _state(_Finder()), {}, 100)
    assert skipped == [{OTHER}]
