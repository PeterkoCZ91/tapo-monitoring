"""Opt-in JSON status endpoint: the daemon's own state, served on request.

``GET /status`` answers with one JSON object assembled from what the daemon already
holds — package identity, process start time, the last tick's outcome and a per-camera
summary reduced from the redacted Digital Twin fleet. Nothing is probed on request and
nothing secret is ever included: no credentials, tokens, URLs or camera addresses.

The endpoint is off unless ``observability.status_port`` is set, and it binds
``observability.status_bind`` — ``127.0.0.1`` by default. That default is deliberate:
the endpoint has no authentication, so making it reachable beyond the host has to be
the operator's explicit decision, never something the package does on its own.

The server runs on a daemon thread and must never cost the monitor loop anything: a
failed bind is a warning instead of a crash, and a handler exception becomes a logged
500 instead of a dead server.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def status_snapshot(app, state, *, started_at, now=None):
    """Assemble the JSON payload ``/status`` serves. Reads state, contacts nothing.

    The per-camera block is a summary of the redacted twin entry the daemon already
    maintains (aggregate health with its layers, the open drift count, when it was
    captured) plus the latest reachability observation. A camera the twin has not
    probed yet reports ``None`` for the twin-derived fields rather than guessing.
    """
    now = time.time() if now is None else now
    try:
        from .cli import package_fingerprint
        fingerprint = package_fingerprint()
    except OSError:
        fingerprint = None
    from . import __version__

    cameras = {}
    for cfg in app.cameras:
        entry = state.twin_fleet.get(cfg.name)
        health = drift_count = probed_at = None
        if isinstance(entry, Mapping):
            raw_health = entry.get("health")
            if isinstance(raw_health, Mapping):
                health = {"status": raw_health.get("status", "unknown"),
                          "layers": dict(raw_health.get("layers") or {})}
            counts = (entry.get("drift") or {}).get("counts") or {}
            drift_count = int(counts.get("drift", 0))
            probed_at = entry.get("captured_at")
        cameras[cfg.name] = {
            "reachable": state.network_reachable.get(cfg.name),
            "health": health,
            "drift_count": drift_count,
            "probed_at": probed_at,
        }

    return {
        "package": {"version": __version__, "fingerprint": fingerprint},
        "started_at": float(started_at),
        "now": float(now),
        "tick": {"ok": state.last_tick_ok, "at": state.last_tick_at},
        "cameras": cameras,
    }


def make_server(snapshot_fn, port=0, bind="127.0.0.1"):
    """HTTP server: GET /status -> ``snapshot_fn()`` as JSON; anything else 404.

    Only GET is implemented, so other methods get http.server's own 501. A
    ``snapshot_fn`` exception is contained as a logged 500 — the server outlives it.
    """

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The client gave up and closed the socket; there is nobody left to
                # reply to, and writing into the dead socket would only crash the
                # handler a second time.
                log.debug("client closed the connection before the reply")

        def do_GET(self):
            if self.path != "/status":
                self._reply(404, {"error": "not found"})
                return
            try:
                payload = snapshot_fn()
            except Exception as exc:  # noqa: BLE001 - report, don't kill the server
                log.warning("status snapshot failed: %s", type(exc).__name__)
                self._reply(500, {"error": "status unavailable"})
                return
            self._reply(200, payload)

        def log_message(self, fmt, *args):  # route http.server chatter to logging
            log.debug(fmt, *args)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def start(app, state, *, started_at=None):
    """Start the endpoint on a daemon thread when configured; return it or ``None``.

    This is the daemon's whole wiring point: it decides the opt-in from the config
    (``status_port`` absent/0 means off) and never raises — an endpoint that cannot
    start is a log line, not a monitor that will not.
    """
    port = app.observability.status_port
    if not port:
        return None
    bind = app.observability.status_bind
    started_at = time.time() if started_at is None else started_at

    def snapshot_fn():
        return status_snapshot(app, state, started_at=started_at)

    try:
        server = make_server(snapshot_fn, port=port, bind=bind)
    except Exception as exc:  # noqa: BLE001 - observability must not block startup
        log.warning("status endpoint failed to bind %s:%d: %s",
                    bind, port, type(exc).__name__)
        return None
    threading.Thread(target=server.serve_forever, name="tapo-statusd",
                     daemon=True).start()
    log.info("status endpoint serving on %s:%d", bind, port)
    return server
