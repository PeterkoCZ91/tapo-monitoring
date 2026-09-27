"""Deny-list for camera API methods that take the local API down.

Some methods do not fail, they knock the camera's HTTPS API over: on a C545D
``checkDetectEventState`` and ``getInfLampCapability`` each closed port 443 for about
12 s and invalidated the session (reproduced against an idle baseline with no refused
connections). A C560WS answers both harmlessly, but nothing here needs either method,
so the deny-list is global: every client this package builds refuses them, whatever
the model. There is no per-camera opt-in in the config on purpose — a research script
that really wants to send one passes ``allow=`` to :func:`guard_client` explicitly.

The check sits at pytapo's lowest JSON choke point, ``Tapo.performRequest``: every
public getter and setter, ``executeFunction`` and a hand-built ``multipleRequest``
batch all pass through it, so a denied method is caught however it is reached, also
inside a batch or a hub ``controlChild`` envelope. A refused request is never sent;
the caller gets :class:`DeniedMethodError` and an error line in the log.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

log = logging.getLogger(__name__)

# method name -> why it is refused (shown in the error and the log).
DENIED_METHODS: Mapping[str, str] = {
    "checkDetectEventState": "closed the local API for ~12 s on a C545D",
    "getInfLampCapability": "closed the local API for ~12 s on a C545D",
}

_GUARD_MARK = "_tapo_monitor_api_guard"


class DeniedMethodError(RuntimeError):
    """A request named a method on the deny-list; nothing was sent."""

    def __init__(self, method):
        self.method = method
        super().__init__(f"refusing to send {method!r}: {DENIED_METHODS.get(method, 'denied')}")


def is_denied(method, allow: Iterable[str] = ()):
    """Whether ``method`` may not be sent. Pure."""
    return method in DENIED_METHODS and method not in set(allow)


def requested_methods(request):
    """Every ``method`` name anywhere in a request envelope, outermost first. Pure.

    Walks nested mappings and lists, so the methods inside a ``multipleRequest`` batch
    and inside a ``controlChild`` ``request_data`` are all found.
    """
    found: list[str] = []
    stack = [request]
    while stack:
        item = stack.pop(0)
        if isinstance(item, Mapping):
            method = item.get("method")
            if isinstance(method, str):
                found.append(method)
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


def check_request(request, allow: Iterable[str] = ()):
    """Raise :class:`DeniedMethodError` when ``request`` carries a denied method."""
    allowed = set(allow)
    for method in requested_methods(request):
        if is_denied(method, allowed):
            log.error("camera API guard: %s", DeniedMethodError(method))
            raise DeniedMethodError(method)


def guard_client(client, allow: Iterable[str] = ()):
    """Make ``client.performRequest`` refuse denied methods; returns ``client``.

    Idempotent, and a client without ``performRequest`` is returned unchanged.
    """
    original = getattr(client, "performRequest", None)
    if not callable(original) or getattr(original, _GUARD_MARK, False):
        return client
    allowed = frozenset(allow)

    def performRequest(requestData, *args, **kwargs):
        check_request(requestData, allowed)
        return original(requestData, *args, **kwargs)

    setattr(performRequest, _GUARD_MARK, True)
    client.performRequest = performRequest
    return client
