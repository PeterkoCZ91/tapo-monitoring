"""Pinning a hostname to an IP when the local resolver refuses to answer for it."""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import dnsfix


def test_parse_reads_host_ip_pairs():
    assert dnsfix.parse_overrides("api.example.org=192.0.2.10") == {
        "api.example.org": ["192.0.2.10"]}


def test_parse_accepts_several_hosts_and_several_ips():
    parsed = dnsfix.parse_overrides(
        " api.example.org = 192.0.2.10 192.0.2.11 , other.example.net=192.0.2.20 ")
    assert parsed == {"api.example.org": ["192.0.2.10", "192.0.2.11"],
                      "other.example.net": ["192.0.2.20"]}


def test_parse_ignores_junk_instead_of_raising():
    # A typo in an env var must not stop a daemon from starting.
    assert dnsfix.parse_overrides("") == {}
    assert dnsfix.parse_overrides(None) == {}
    assert dnsfix.parse_overrides("nonsense,=,=1.2.3.4,host=") == {}


def test_resolver_answers_for_an_overridden_host():
    resolve = dnsfix.resolver({"api.example.org": ["192.0.2.10"]},
                              getaddrinfo=lambda *a, **k: 1 / 0)
    entries = resolve("api.example.org", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    assert [entry[4] for entry in entries] == [("192.0.2.10", 443)]
    assert entries[0][0] == socket.AF_INET
    assert entries[0][1] == socket.SOCK_STREAM


def test_resolver_keeps_the_port_from_a_service_name():
    resolve = dnsfix.resolver({"api.example.org": ["192.0.2.10"]},
                              getaddrinfo=lambda *a, **k: 1 / 0)
    entries = resolve("api.example.org", "https", socket.AF_UNSPEC, socket.SOCK_STREAM)
    assert entries[0][4] == ("192.0.2.10", 443)


def test_resolver_delegates_every_other_host():
    called = []

    def real(host, port, *rest, **kw):
        called.append(host)
        return ["real answer"]

    resolve = dnsfix.resolver({"api.example.org": ["192.0.2.10"]}, getaddrinfo=real)
    assert resolve("elsewhere.example.net", 80) == ["real answer"]
    assert called == ["elsewhere.example.net"]


def test_resolver_offers_every_pinned_address():
    resolve = dnsfix.resolver({"h": ["192.0.2.10", "192.0.2.11"]},
                              getaddrinfo=lambda *a, **k: 1 / 0)
    assert [e[4][0] for e in resolve("h", 443)] == ["192.0.2.10", "192.0.2.11"]


def test_install_from_env_is_a_no_op_without_the_variable():
    before = socket.getaddrinfo
    assert dnsfix.install_from_env({}) == {}
    assert socket.getaddrinfo is before


def test_install_from_env_patches_and_reports_what_it_pinned():
    before = socket.getaddrinfo
    try:
        pinned = dnsfix.install_from_env({"DNS_OVERRIDES": "api.example.org=192.0.2.10"})
        assert pinned == {"api.example.org": ["192.0.2.10"]}
        assert socket.getaddrinfo is not before
        assert socket.getaddrinfo("api.example.org", 443)[0][4] == ("192.0.2.10", 443)
        # An unrelated host still goes through the real resolver.
        assert socket.getaddrinfo("localhost", 80)
    finally:
        socket.getaddrinfo = before
