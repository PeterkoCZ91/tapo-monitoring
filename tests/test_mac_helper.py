"""Opt-in ``mac_helper``: ask a host on the camera's own LAN when ARP cannot see it."""

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, rediscover

from .test_mac_rediscovery import MAC, NEW, OLD, STRANGER, FakeNet, _cam, _finder, _neigh, _wire

REAL_REDISCOVERY = True       # conftest leaves the address lookups to this module's fakes

ROUTED = json.dumps([{"dst": OLD, "gateway": "198.51.100.1", "dev": "eth0"}])


def _ssh_neigh(*entries):
    return "".join(f"{ip} dev eth0 lladdr {mac} {st}\n" for ip, mac, st in entries)


class HelperNet(FakeNet):
    """FakeNet plus ``ssh <host> ip neigh show`` answered per host."""

    def __init__(self, helpers=None, **kw):
        super().__init__(**kw)
        self.helpers = helpers or {}
        self.ssh = []

    def __call__(self, argv, **kwargs):
        if argv[0] != "ssh":
            return super().__call__(argv, **kwargs)
        assert not kwargs.get("shell")
        assert kwargs["timeout"] > 0
        host = argv[-4]
        self.ssh.append(host)
        reply = self.helpers.get(host, (255, ""))
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(returncode=reply[0], stdout=reply[1], stderr="remote noise")


# -- config ------------------------------------------------------------------

def test_mac_helper_defaults_off():
    assert _cam(mac=MAC).mac_helper == ()


@pytest.mark.parametrize("raw,want", [
    ("gw", ("gw",)),
    (["gw", "monitor@gw2"], ("gw", "monitor@gw2")),
])
def test_mac_helper_accepts_a_host_or_a_list(raw, want):
    assert _cam(mac=MAC, mac_helper=raw).mac_helper == want


@pytest.mark.parametrize("bad", ["", [], "a b", "-oProxyCommand=x", "h;rm", 7, ["gw", "gw"],
                                 ["gw", ""], ["a", "b", "c", "d", "e"]])
def test_bad_mac_helper_is_refused(bad):
    with pytest.raises(cfg.ConfigError, match="mac_helper"):
        _cam(mac=MAC, mac_helper=bad)


def test_mac_helper_needs_mac():
    with pytest.raises(cfg.ConfigError, match="mac_helper.*mac"):
        _cam(mac_helper="gw")


# -- the search ----------------------------------------------------------------

def test_without_helpers_no_ssh_is_run():
    net = HelperNet(route=ROUTED)
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=0)
    assert net.ssh == []
    assert finder.take("c") is None


def test_local_hit_skips_the_helper():
    net = HelperNet(neigh=_neigh((NEW, MAC, "REACHABLE")),
                    helpers={"gw": (0, _ssh_neigh(("192.0.2.88", MAC, "REACHABLE")))})
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert net.ssh == []
    assert finder.take("c") == NEW


def test_local_miss_helper_hit_returns_the_new_address(caplog):
    net = HelperNet(route=ROUTED, helpers={
        "gw": (0, _ssh_neigh((OLD, STRANGER, "REACHABLE"), (NEW, MAC.upper(), "REACHABLE")))})
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        assert finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert finder.take("c") == NEW
    assert net.ssh == ["gw"]
    assert net.pinged == []
    lines = [r.getMessage() for r in caplog.records]
    assert f"mac rediscovery c: mac {MAC} seen at {NEW} (helper host gw)" in lines


def test_helper_hit_skips_the_sweep_on_link():
    net = HelperNet(helpers={"gw": (0, _ssh_neigh((NEW, MAC, "REACHABLE")))})
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert finder.take("c") == NEW
    assert net.pinged == []


def test_helper_answer_at_an_excluded_address_is_ignored():
    net = HelperNet(route=ROUTED, helpers={"gw": (0, _ssh_neigh((OLD, MAC, "REACHABLE")))})
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert finder.take("c") is None


def test_second_helper_is_asked_when_the_first_fails():
    net = HelperNet(route=ROUTED, helpers={"gw1": (255, ""),
                                           "gw2": (0, _ssh_neigh((NEW, MAC, "REACHABLE")))})
    finder = _finder(net)
    finder.request("c", MAC, OLD, now=0, helpers=("gw1", "gw2"))
    assert net.ssh == ["gw1", "gw2"]
    assert finder.take("c") == NEW


@pytest.mark.parametrize("reply", [(255, ""), subprocess.TimeoutExpired(["ssh"], 8),
                                   OSError("no ssh")])
def test_helper_failure_degrades_to_the_old_path(caplog, reply):
    net = HelperNet(helpers={"gw": reply}, after_sweep=_neigh((NEW, MAC, "REACHABLE")),
                    answering={NEW})
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert finder.take("c") == NEW           # the local sweep still ran
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["mac rediscovery c: helper host gw did not answer"]
    assert all("noise" not in r.getMessage() for r in caplog.records)


def test_helper_miss_is_logged_and_backs_off(caplog):
    net = HelperNet(route=ROUTED, helpers={"gw": (0, _ssh_neigh((NEW, STRANGER, "REACHABLE")))})
    finder = _finder(net)
    with caplog.at_level(logging.INFO, logger="tapo_monitor.rediscover"):
        finder.request("c", MAC, OLD, now=0, helpers=("gw",))
    assert finder.take("c") is None
    assert f"mac rediscovery c: mac {MAC} not seen by helper host gw" in [
        r.getMessage() for r in caplog.records]
    finder.request("c", MAC, OLD, now=rediscover.NEIGH_EVERY + 1, helpers=("gw",))
    assert net.ssh == ["gw"]                 # helper asked on the sweep schedule only
    finder.request("c", MAC, OLD, now=rediscover.SWEEP_EVERY, helpers=("gw",))
    assert net.ssh == ["gw", "gw"]
    finder.request("c", MAC, OLD, now=2 * rediscover.SWEEP_EVERY, helpers=("gw",))
    assert net.ssh == ["gw", "gw"]           # second miss: the interval doubled
    finder.request("c", MAC, OLD, now=3 * rediscover.SWEEP_EVERY, helpers=("gw",))
    assert net.ssh == ["gw", "gw", "gw"]


# -- the daemon ----------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True

    def take(self, name):
        return None

    def reset(self, name):
        pass

    def reject(self, name, ip, now):
        pass

    def own_interface(self, ip, now):
        return None


@pytest.mark.parametrize("extra,kwargs", [({}, {}), ({"mac_helper": ["gw"]},
                                                     {"helpers": ("gw",)})])
def test_daemon_passes_helpers_only_when_set(monkeypatch, extra, kwargs):
    _wire(monkeypatch, up=set())
    finder = _Recorder()
    state = daemon.MonitorState()
    state.rediscovery = finder
    cam = _cam(mac=MAC, **extra)
    connect = daemon._connect_camera({}, state, now=100)
    for _ in range(rediscover.SEARCH_AFTER_FAILS):
        connect(cam)
    assert finder.calls == [(("c", MAC, OLD, 100), kwargs)]
