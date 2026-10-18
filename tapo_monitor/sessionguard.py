"""Serialize a shared camera session and suppress forbidden native tracking."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from copy import deepcopy
from inspect import iscoroutinefunction
from typing import Any


class SessionRetiredError(RuntimeError):
    """An operation attempted to reuse a retired camera session."""


class _State:
    def __init__(self, client: Any, allowed: Callable[[], bool]) -> None:
        self.lock: Any = getattr(getattr(client, "asyncHandler", None), "_lock", None)
        if self.lock is None:
            self.lock = threading.RLock()
        self.allowed = allowed
        self.retired = False

    def outgoing(self, request: Any) -> Any:
        if self.retired:
            raise SessionRetiredError("Camera session has been retired")
        try:
            allowed_now = self.allowed()
        except Exception:
            allowed_now = False
        if allowed_now:
            return request
        outgoing = deepcopy(request)
        _rewrite(outgoing)
        return outgoing


def _disable_enables(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("enabled") == "on":
            value["enabled"] = "off"
        for child in value.values():
            _disable_enables(child)
    elif isinstance(value, list):
        for child in value:
            _disable_enables(child)


def _rewrite(value: Any) -> None:
    if isinstance(value, dict):
        method = value.get("method")
        if method in {"setTargetTrackConfig", "setAutoTrackTarget"}:
            params = value.get("params", value)
            for key in ("target_track", "auto_track_target"):
                if key in params:
                    _disable_enables(params[key])
        for child in value.values():
            _rewrite(child)
    elif isinstance(value, list):
        for child in value:
            _rewrite(child)


def protect_client(client: Any, allowed: Callable[[], bool]) -> None:
    """Wrap the existing request method, preserving earlier request restrictions.

    Install before sharing the client with another thread. Repeated installation
    updates the policy without stacking wrappers or reviving a retired session.
    """
    state = getattr(client, "_tapo_session_guard", None)
    if state is not None:
        with state.lock:
            state.allowed = allowed
        return
    state = _State(client, allowed)
    original = client.performRequest
    transport = getattr(client, "transport", None)
    original_send = getattr(transport, "send", None)
    if transport is not None and iscoroutinefunction(original_send):

        async def send(request: Any, *args: Any, **kwargs: Any) -> Any:
            # performRequest still owns the outer session lock across authentication
            # and this coroutine. Recheck after authentication, immediately before
            # entering the transport; already in-flight writes cannot be recalled.
            return await original_send(state.outgoing(request), *args, **kwargs)

        transport.send = send

    def perform_request(requestData: Any, *args: Any, **kwargs: Any) -> Any:
        with state.lock:
            return original(state.outgoing(requestData), *args, **kwargs)

    client._tapo_session_guard = state
    client.performRequest = perform_request


def retire_client(client: Any, close_fn: Callable[[], Any]) -> None:
    """Retire before closing; stale callbacks can no longer send requests."""
    if getattr(client, "_tapo_session_guard", None) is None:
        protect_client(client, lambda: False)
    state = client._tapo_session_guard
    with state.lock:
        if state.retired:
            return
        state.retired = True
        close_fn()


def native_tracking_off(client: Any, *, diagnostics: dict | None = None) -> bool:
    """Disable and verify native tracking atomically on a protected session.

    Failure is deliberately quiet: protocol errors can contain authentication
    material. The caller receives only whether off was positively verified.
    """
    state = getattr(client, "_tapo_session_guard", None)
    if state is None:
        if diagnostics is not None:
            diagnostics["result"] = "unprotected"
        return False
    started = time.monotonic()
    with state.lock:
        acquired = time.monotonic()
        if diagnostics is not None:
            diagnostics["lock_wait_s"] = acquired - started
        if state.retired:
            if diagnostics is not None:
                diagnostics["result"] = "retired"
            return False
        try:
            client.setAutoTrackTarget(False)
            if diagnostics is not None:
                diagnostics["set_s"] = time.monotonic() - acquired
            result = client.getAutoTrackTarget()
            ok = isinstance(result, dict) and result.get("enabled") == "off"
            if diagnostics is not None:
                diagnostics["result"] = "verified" if ok else "unverified"
            return ok
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.update(result="error", error=type(exc).__name__)
            return False
        finally:
            if diagnostics is not None:
                diagnostics["request_s"] = time.monotonic() - acquired
