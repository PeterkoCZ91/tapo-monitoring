"""Pin a hostname to a fixed address when the local resolver will not answer for it.

Some sites sit behind a resolver that filters specific names. Measured on one deployment:
``api.telegram.org`` returns nothing while the network route to it is fine and every other
name resolves — so the daemon could reach Telegram but could not look it up, and every
alert would have died at DNS.

``DNS_OVERRIDES`` (``host=ip[ ip…][,host=ip…]``) makes those names resolvable for this
process only. TLS is untouched: the connection still asks for the original hostname, so
certificate verification and SNI behave exactly as before — this replaces the lookup, not
the identity check.

A pinned address is a liability of its own (the provider may move it), so it is a
workaround: fixing the resolver, or the filter in front of it, is the durable answer.
Unset the variable and this module does nothing at all.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

ENV_VAR = "DNS_OVERRIDES"


def parse_overrides(text):
    """Parse ``"host=ip ip,host2=ip"`` into ``{host: [ip, …]}``. Pure.

    Malformed entries are dropped rather than raised on: a typo in an environment variable
    must never be the reason a daemon refuses to start.
    """
    overrides = {}
    for chunk in str(text or "").split(","):
        host, sep, addresses = chunk.partition("=")
        host = host.strip().lower()
        if not sep or not host:
            continue
        ips = [ip.strip() for ip in addresses.split() if ip.strip()]
        if ips:
            overrides[host] = ips
    return overrides


def _port_number(port):
    if isinstance(port, int):
        return port
    try:
        return int(port)
    except (TypeError, ValueError):
        pass
    try:
        return socket.getservbyname(str(port))
    except OSError:
        return 0


def resolver(overrides, getaddrinfo=None):
    """Build a ``getaddrinfo`` replacement honouring ``overrides``. Pure-ish.

    Any host not listed goes to the real resolver untouched.
    """
    real = getaddrinfo or socket.getaddrinfo

    def resolve(host, port, family=0, socktype=0, proto=0, flags=0):
        pinned = overrides.get(str(host or "").lower())
        if not pinned:
            return real(host, port, family, socktype, proto, flags)
        number = _port_number(port)
        return [(socket.AF_INET, socktype or socket.SOCK_STREAM, proto, "", (ip, number))
                for ip in pinned]

    return resolve


def install_from_env(env=None):
    """Patch this process's resolver from ``DNS_OVERRIDES``. Returns what was pinned."""
    env = os.environ if env is None else env
    overrides = parse_overrides(env.get(ENV_VAR))
    if not overrides:
        return {}
    socket.getaddrinfo = resolver(overrides, getaddrinfo=socket.getaddrinfo)
    log.info("pinned %d hostname(s) past the local resolver: %s",
             len(overrides), ", ".join(sorted(overrides)))
    return overrides
