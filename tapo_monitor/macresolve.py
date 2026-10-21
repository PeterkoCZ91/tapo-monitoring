"""Resolve a camera MAC to an IP through a host on the camera's own network.

ARP is not visible across a subnet route, so a helper host on the camera's LAN
is asked for its neighbour table over ssh. Pure parser plus a small runner;
nothing here is wired into the daemon.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Callable, Iterable

_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._@-]+$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_BAD_STATES = {"FAILED", "INCOMPLETE", "STALE"}


def normalize_mac(mac: str) -> str | None:
    """Return the lowercase colon form of a MAC, or None if it is not one."""
    m = mac.strip().lower().replace("-", ":")
    return m if _MAC_RE.match(m) else None


def _valid_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def parse_neighbors(text: str) -> dict[str, str]:
    """Parse `ip neigh show` and `arp -an` output into {mac: ip}.

    FAILED/INCOMPLETE/STALE entries and lines without a MAC are ignored.
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "?" or (len(tokens) > 1 and tokens[1].startswith("(")):
            # arp -an: "? (192.0.2.5) at aa:bb:.. [ether] on eth0"
            m = re.search(r"\(([^)]+)\)\s+at\s+(\S+)", line)
            if not m or "<incomplete>" in line:
                continue
            ip, mac = _valid_ip(m.group(1)), normalize_mac(m.group(2))
        else:
            # ip neigh: "192.0.2.5 dev eth0 lladdr aa:bb:.. REACHABLE"
            if tokens[-1] in _BAD_STATES or "lladdr" not in tokens:
                continue
            i = tokens.index("lladdr")
            if i + 1 >= len(tokens):
                continue
            ip, mac = _valid_ip(tokens[0]), normalize_mac(tokens[i + 1])
        if ip and mac:
            out[mac] = ip
    return out


def resolve_via_ssh(
    mac: str,
    ssh_host: str,
    run: Callable[..., object] = subprocess.run,
    timeout: float = 8,
) -> str | None:
    """Ask `ssh_host` for its neighbour table and return the IP for `mac`."""
    if not isinstance(ssh_host, str) or not _SSH_HOST_RE.match(ssh_host):
        raise ValueError("invalid ssh_host")
    if ssh_host.startswith("-"):
        raise ValueError("invalid ssh_host")
    want = normalize_mac(mac)
    if want is None:
        return None
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh_host, "ip", "neigh", "show"]
    try:
        res = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return parse_neighbors(getattr(res, "stdout", "") or "").get(want)


def candidates_from_mac(
    mac: str,
    ssh_hosts: Iterable[str],
    run: Callable[..., object] = subprocess.run,
    timeout: float = 8,
) -> list[str]:
    """IPs for `mac` from each helper host, in order, de-duplicated."""
    found: list[str] = []
    for h in ssh_hosts:
        try:
            ip = resolve_via_ssh(mac, h, run=run, timeout=timeout)
        except ValueError:
            continue
        if ip and ip not in found:
            found.append(ip)
    return found
