"""Bearer token handling of tools/recording_audit.py: no plain http, no redirect leaks."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("recording_audit_tok", ROOT / "tools" / "recording_audit.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

TOKEN = "-".join(["dummy", "value"])
AUTH = {"token": TOKEN}


def _serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_refuses_token_over_plain_http_to_remote(tmp_path):
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")
    with pytest.raises(ValueError, match="plain http"):
        ra.score_jpeg("http://192.0.2.5:8767/score", jpg, **AUTH)


def test_allow_insecure_gets_past_the_guard(tmp_path):
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")
    # 192.0.2.0/24 is TEST-NET: the guard passes, then the connection fails (not ValueError).
    with pytest.raises(OSError):
        ra.score_jpeg("http://192.0.2.5:1/score", jpg, **AUTH, allow_insecure=True, timeout=0.2)


def test_no_token_no_guard(tmp_path):
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")
    with pytest.raises(OSError):
        ra.score_jpeg("http://192.0.2.5:1/score", jpg, token=None, timeout=0.2)


def test_authorization_not_resent_on_redirect(tmp_path):
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):  # urllib turns a redirected POST into a GET
            seen.append(self.headers.get("Authorization"))
            out = json.dumps({"person": 0.5}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    target = _serve(Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/score")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    redir = _serve(Redirector)
    try:
        jpg = tmp_path / "a.jpg"
        jpg.write_bytes(b"x")
        url = f"http://127.0.0.1:{redir.server_port}/score"
        assert ra.score_jpeg(url, jpg, **AUTH, force_token=True) == 0.5
        assert seen == [None]
    finally:
        redir.shutdown()
        target.shutdown()
