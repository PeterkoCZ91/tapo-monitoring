"""Find a camera that moved to another address, by its MAC (opt-in ``mac:``).

After a router outage DHCP may hand a camera a new address while the configured one stays
dead, or worse, goes to another device. A camera with ``mac:`` that stays unreachable is
looked up in the host's neighbour table (``ip -j neigh``, cheap); if it is not there, the
camera's on-link subnet (at most a /24) is swept with single, slow pings at bounded
concurrency to fill the table, which is then read again. Sweeps run at most every
``SWEEP_EVERY`` seconds per camera, doubling after each miss up to ``SWEEP_MAX``.

Everything here runs on a worker thread (one search in flight per camera); the daemon only
picks up the result on its next control pass and switches the camera through the existing
host machinery after the login on the new address proved it is the same device.

The module also answers "is this address one of the host's own?" (cached ``ip -j addr``):
a camera address taken over by the host itself answers ping but is not the camera.

Linux with the ``ip`` command only; elsewhere the feature logs once and stays off.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .backoff import capped_exp
from .macresolve import normalize_mac
from .netdiag import _json_list, _run

log = logging.getLogger(__name__)

SEARCH_AFTER_FAILS = 3      # consecutive unreachable control passes before a search
NEIGH_EVERY = 60.0          # seconds between neighbour-table reads per camera
SWEEP_EVERY = 600.0         # seconds between subnet sweeps per camera (first interval)
SWEEP_MAX = 3600.0          # longest interval after repeated misses
SWEEP_WORKERS = 32          # pings in flight at once
SWEEP_PING_WAIT = 2         # seconds ping waits for each reply (weak Wi-Fi answers slowly)
LOCAL_CACHE_SECONDS = 60.0  # how long the host's own address list is reused
MIN_PREFIX = 24             # never sweep more than a /24
REJECT_SECONDS = 3600.0     # an address whose login/identity failed is skipped this long

# Neighbour states that say the entry is current; STALE/DELAY are kept as weaker hits.
_LIVE = {"REACHABLE", "PERMANENT", "NOARP", "PROBE"}
_DEAD = {"FAILED", "INCOMPLETE"}


def identity_form(mac):
    """``AA-BB-CC-DD-EE-FF``, the form ``camera.device_identity`` reports, or None."""
    norm = normalize_mac(mac) if isinstance(mac, str) else None
    return norm.upper().replace(":", "-") if norm else None


def _entries(text):
    out = []
    for entry in _json_list(text) or ():
        if not isinstance(entry, dict):
            continue
        states = entry.get("state") or []
        states = {str(s) for s in states} if isinstance(states, list) else {str(states)}
        mac = normalize_mac(str(entry.get("lladdr") or ""))
        if mac is None or states & _DEAD:
            continue
        out.append((str(entry.get("dst") or ""), mac, states))
    return out


def neighbours(run=subprocess.run):
    """``[(ip, mac, states)]`` from ``ip -j neigh show``, or None when it cannot be read."""
    text = _run(("ip", "-j", "neigh", "show"), run)
    return None if text is None else _entries(text)


def neighbour_mac(ip, run=subprocess.run):
    """The MAC the host's neighbour table holds for ``ip`` (lowercase), or None."""
    entries = _entries(_run(("ip", "-j", "neigh", "show", ip), run))
    return entries[0][1] if entries else None


def find_mac(mac, entries, exclude=()):
    """The address ``mac`` is seen at, preferring a live entry; None if absent."""
    hits = [(ip, states) for ip, m, states in entries or () if m == mac and ip not in exclude]
    hits.sort(key=lambda hit: not (hit[1] & _LIVE))
    return hits[0][0] if hits else None


def local_addresses(run=subprocess.run):
    """``{address: interface}`` of this host from ``ip -j addr show``, or None."""
    entries = _json_list(_run(("ip", "-j", "addr", "show"), run))
    if entries is None:
        return None
    out = {}
    for link in entries:
        if not isinstance(link, dict):
            continue
        for info in link.get("addr_info") or ():
            if isinstance(info, dict) and info.get("local"):
                out[str(info["local"])] = str(link.get("ifname") or "?")
    return out


def sweep_targets(ip, run=subprocess.run, own=()):
    """Addresses to ping for ``ip``'s on-link subnet (at most a /24), or None.

    None when ``ip`` is not on a directly connected network: ARP does not cross a router,
    so a sweep could never find the MAC there.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    route = next((e for e in _json_list(_run(("ip", "-j", "route", "get", ip), run)) or ()
                  if isinstance(e, dict)), None)
    if route is None or route.get("gateway") or not route.get("dev"):
        return None
    prefix = MIN_PREFIX
    links = _json_list(_run(("ip", "-j", "addr", "show", "dev", str(route["dev"])), run))
    for link in links or ():
        for info in (link.get("addr_info") or ()) if isinstance(link, dict) else ():
            try:
                net = ipaddress.ip_network(f"{info['local']}/{int(info['prefixlen'])}",
                                           strict=False)
            except (KeyError, TypeError, ValueError):
                continue
            if addr in net:
                prefix = max(prefix, net.prefixlen)
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return [str(h) for h in network.hosts() if str(h) not in own]


def ping_once(ip, run=subprocess.run):
    """One echo with a patient reply wait; True when answered. Never raises."""
    try:
        proc = run(["ping", "-n", "-q", "-c", "1", "-W", str(SWEEP_PING_WAIT), ip],
                   capture_output=True, text=True, timeout=SWEEP_PING_WAIT + 2, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 0


def sweep(targets, run=subprocess.run, workers=SWEEP_WORKERS):
    """Ping every target once, ``workers`` at a time. Returns how many answered."""
    if not targets:
        return 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets))),
                            thread_name_prefix="rediscover-ping") as pool:
        return sum(pool.map(lambda ip: ping_once(ip, run), targets))


def restore(cfg, entry):
    """Re-attach a persisted ``{"host", "mac"}`` discovery to ``cfg``. True when applied.

    Only for a camera that still has the same ``mac:``; a host that became a configured
    candidate needs no extra entry (the caller drops the record either way on False).
    """
    mac = getattr(cfg, "mac", None)
    if not mac or not isinstance(entry, dict) or not isinstance(entry.get("host"), str):
        return False
    if normalize_mac(str(entry.get("mac") or "")) != mac:
        return False
    hosts = tuple(getattr(cfg, "hosts", ()) or (cfg.host,))
    if entry["host"] in hosts:
        return False
    cfg.discovered_host = entry["host"]
    return True


def _thread_spawn(job):
    threading.Thread(target=job, daemon=True, name="rediscover").start()


class Rediscovery:
    """Per-camera, rate-limited MAC searches on a worker thread, plus own-address checks."""

    def __init__(self, spawn: Callable[[Callable[[], None]], Any] = _thread_spawn,
                 run=subprocess.run, platform=None,
                 which: Callable[[str], str | None] = shutil.which):
        self._spawn = spawn
        self._run = run
        self._platform = platform
        self._which = which
        self._available: bool | None = None
        self._off_logged = False
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._found: dict[str, str] = {}
        self._neigh_at: dict[str, float] = {}
        self._sweep_at: dict[str, float] = {}
        self._misses: dict[str, int] = {}
        self._rejected: dict[str, dict[str, float]] = {}
        self._routed_logged: set[str] = set()
        self._local: dict[str, str] | None = None
        self._local_at = -float("inf")

    def _supported(self):
        if self._available is None:
            linux = (self._platform or sys.platform).startswith("linux")
            self._available = bool(linux and self._which("ip"))
        return self._available

    def available(self):
        """True on Linux with ``ip``; logs once (for a camera with ``mac:``) otherwise."""
        if not self._supported() and not self._off_logged:
            self._off_logged = True
            log.info("mac rediscovery off: needs Linux with the ip command")
        return self._supported()

    def own_interface(self, ip, now):
        """The host interface that owns ``ip``, or None (also when it cannot be told)."""
        if not self._supported():
            return None
        if self._local is None or now - self._local_at >= LOCAL_CACHE_SECONDS or \
                now < self._local_at:
            self._local = local_addresses(self._run) or {}
            self._local_at = now
        return self._local.get(str(ip))

    def reset(self, name):
        """The camera answers at its address again: forget misses and pending results."""
        with self._lock:
            self._found.pop(name, None)
            self._misses.pop(name, None)
            self._neigh_at.pop(name, None)
            self._sweep_at.pop(name, None)

    def reject(self, name, ip, now):
        """``ip`` was found but its login or identity failed: skip it for a while."""
        with self._lock:
            self._rejected.setdefault(name, {})[ip] = now + REJECT_SECONDS

    def take(self, name):
        """The newly found address of ``name`` (handed over once), or None."""
        with self._lock:
            return self._found.pop(name, None)

    def request(self, name, mac, ip, now):
        """Start a search for ``mac`` (camera ``name`` last at ``ip``) if one is due.

        Returns True when a search was started. Never blocks on the network.
        """
        if not self.available():
            return False
        mac = normalize_mac(mac)
        if mac is None:
            return False
        with self._lock:
            if name in self._busy or now - self._neigh_at.get(name, -float("inf")) < NEIGH_EVERY:
                return False
            self._neigh_at[name] = now
            do_sweep = now >= self._sweep_at.get(name, -float("inf"))
            if do_sweep:
                self._sweep_at[name] = now + capped_exp(
                    SWEEP_EVERY, self._misses.get(name, 0), SWEEP_MAX)
            exclude = {ip} | {h for h, until in self._rejected.get(name, {}).items()
                              if until > now}
            self._busy.add(name)

        def job():
            try:
                self._search(name, mac, ip, exclude, do_sweep)
            except Exception:  # noqa: BLE001 - a search must never become a fault
                log.debug("rediscover: search failed for %s", name, exc_info=True)
            finally:
                with self._lock:
                    self._busy.discard(name)

        try:
            self._spawn(job)
        except Exception:  # noqa: BLE001 - e.g. no thread can be started
            with self._lock:
                self._busy.discard(name)
            log.debug("rediscover: cannot start a search for %s", name, exc_info=True)
            return False
        return True

    def _search(self, name, mac, ip, exclude, do_sweep):
        own = set(local_addresses(self._run) or ())
        exclude = set(exclude) | own
        found = find_mac(mac, neighbours(self._run), exclude)
        swept = None
        if found is None and do_sweep:
            targets = sweep_targets(ip, self._run, own=own)
            if targets is None:
                if name not in self._routed_logged:
                    self._routed_logged.add(name)
                    log.info("mac rediscovery %s: %s is not on a directly connected "
                             "network; only the neighbour table is searched", name, ip)
            else:
                answered = sweep(targets, self._run)
                swept = (len(targets), answered)
                found = find_mac(mac, neighbours(self._run), exclude)
        with self._lock:
            if found is not None:
                self._found[name] = found
                self._misses.pop(name, None)
            elif swept is not None:
                self._misses[name] = self._misses.get(name, 0) + 1
        if found is not None and swept is None:
            log.info("mac rediscovery %s: mac %s seen at %s (neighbour table, no sweep)",
                     name, mac, found)
        elif found is not None:
            log.info("mac rediscovery %s: mac %s seen at %s after a sweep "
                     "(%d addresses, %d answered)", name, mac, found, swept[0], swept[1])
        elif swept is not None:
            log.info("mac rediscovery %s: mac %s not found (swept %d addresses, %d answered)",
                     name, mac, swept[0], swept[1])
