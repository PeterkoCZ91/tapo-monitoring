"""Exercise the hub session against an HTTP peer that refuses connection reuse."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from kasa import DeviceConfig, Discover
from kasa.httpclient import HttpClient
from yarl import URL

from tapo_monitor.hubclient import kasa_session


@pytest.mark.parametrize("close_fails", [False, True])
def test_hub_queries_survive_peer_refusing_connection_reuse(monkeypatch, close_fails):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.request_count = 0

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.request_count += 1
            requests.append(self.request_count)
            body = json.dumps({"error_code": 0}).encode()
            self.send_response(200 if self.request_count == 1 else 409)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = DeviceConfig(host="127.0.0.1")
    client = None
    url = URL(f"http://127.0.0.1:{server.server_port}/")

    class Transport:
        async def send(self, payload):
            # Two HTTP exchanges in a logical request reproduce a multi-step login.
            for _ in range(2):
                status, result = await client.post(url, json=json.loads(payload))
                if status != 200:
                    raise ConnectionError("peer refused reused HTTP connection")
            return result

    async def close():
        if close_fails:
            raise RuntimeError("protocol close failed")
        await client.close()

    async def discover(*args, **kwargs):
        nonlocal client
        client = HttpClient(config)
        return SimpleNamespace(config=config, protocol=SimpleNamespace(
            _transport=Transport(), close=close))

    monkeypatch.setattr(Discover, "discover_single", discover)
    session = None
    try:
        session = kasa_session("127.0.0.1", "test@example.invalid", "fixture")
        for _ in range(3):
            assert session.send("getDeviceInfo", {"device_info": {"name": ["basic_info"]}}) == {
                "error_code": 0}
        assert requests == [1] * 6
    finally:
        if session:
            session.close()
            session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert config.http_client.closed
    assert not session._thread.is_alive()
    assert session._loop.is_closed()


def test_failed_discovery_releases_session_thread(monkeypatch):
    threads_before = set(threading.enumerate())

    async def discover(*args, **kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(Discover, "discover_single", discover)
    with pytest.raises(RuntimeError, match="discovery failed"):
        kasa_session("127.0.0.1", "test@example.invalid", "fixture")
    assert not [t for t in threading.enumerate()
                if t not in threads_before and t.name == "hub-session"]


def test_discovery_timeout_cancels_work_and_releases_loop(monkeypatch):
    loops = []
    cancelled = threading.Event()
    threads_before = set(threading.enumerate())

    async def discover(*args, **kwargs):
        loops.append(asyncio.get_running_loop())
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(Discover, "discover_single", discover)
    with pytest.raises(TimeoutError):
        kasa_session("127.0.0.1", "test@example.invalid", "fixture", timeout=0.1)
    assert cancelled.is_set()
    assert loops[0].is_closed()
    assert not [t for t in threading.enumerate()
                if t not in threads_before and t.name == "hub-session"]
