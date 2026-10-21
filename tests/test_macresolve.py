import subprocess
from types import SimpleNamespace

import pytest

from tapo_monitor import macresolve as mr

NEIGH = """\
192.0.2.10 dev eth0 lladdr AA:BB:CC:00:11:22 REACHABLE
192.0.2.11 dev eth0 lladdr aa:bb:cc:00:11:33 STALE
192.0.2.12 dev eth0  FAILED
192.0.2.13 dev eth0 lladdr aa:bb:cc:00:11:44 DELAY
fe80::1 dev eth0 lladdr aa:bb:cc:00:11:55 router REACHABLE
"""
ARP = "? (192.0.2.20) at AA:BB:CC:00:22:01 [ether] on eth0\n? (192.0.2.21) at <incomplete> on eth0\n"


def test_parse_ip_neigh():
    d = mr.parse_neighbors(NEIGH)
    assert d["aa:bb:cc:00:11:22"] == "192.0.2.10"
    assert d["aa:bb:cc:00:11:44"] == "192.0.2.13"
    assert d["aa:bb:cc:00:11:55"] == "fe80::1"
    assert "aa:bb:cc:00:11:33" not in d
    assert len(d) == 3


def test_parse_arp():
    assert mr.parse_neighbors(ARP) == {"aa:bb:cc:00:22:01": "192.0.2.20"}


def run_ok(out, rc=0):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=rc, stdout=out)

    run.calls = calls
    return run


def test_resolve_uppercase_and_fixed_argv():
    run = run_ok(NEIGH)
    assert mr.resolve_via_ssh("AA-BB-CC-00-11-22", "gw@host.lan", run=run) == "192.0.2.10"
    argv, kw = run.calls[0]
    assert argv == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "gw@host.lan", "ip", "neigh", "show"]
    assert not kw.get("shell")


def test_unknown_mac_and_nonzero():
    assert mr.resolve_via_ssh("aa:bb:cc:00:99:99", "h", run=run_ok(NEIGH)) is None
    assert mr.resolve_via_ssh("aa:bb:cc:00:11:22", "h", run=run_ok(NEIGH, rc=255)) is None


@pytest.mark.parametrize("bad", ["a b", "h;rm", "-oProxyCommand=x", "", "h$(x)"])
def test_bad_ssh_host(bad):
    run = run_ok(NEIGH)
    with pytest.raises(ValueError):
        mr.resolve_via_ssh("aa:bb:cc:00:11:22", bad, run=run)
    assert not run.calls


def test_timeout_returns_none():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 8)

    assert mr.resolve_via_ssh("aa:bb:cc:00:11:22", "h", run=run) is None


def test_candidates_from_mac_dedup_skips_bad():
    out = mr.candidates_from_mac("aa:bb:cc:00:11:22", ["h1", "bad host", "h2"], run=run_ok(NEIGH))
    assert out == ["192.0.2.10"]
