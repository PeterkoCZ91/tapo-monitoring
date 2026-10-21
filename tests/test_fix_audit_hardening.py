"""Hardening fixes from the audit: temp file perms, bearer on redirects, pixel bombs,
ffmpeg protocol whitelist."""

import io
import json
import os
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

DUMMY_VALUE = "-".join(["dummy", "value"])
AUTH = {"token": DUMMY_VALUE}

from tapo_monitor import recclip, scorer, scorer_service, sdclip, snapshot


def _segment(tmp_path, host):
    d = tmp_path / host / "2026-01-01" / "00"
    d.mkdir(parents=True)
    seg = d / "a.mkv"
    seg.write_bytes(b"x")
    os.utime(seg, (1000, 1000))


def test_recording_frame_output_is_private_and_unpredictable(tmp_path):
    host = "192.0.2.50"
    _segment(tmp_path, host)
    seen = []

    def fake_run(args, **kwargs):
        seen.append(os.stat(args[-1]).st_mode)       # pre-created by mkstemp, not by ffmpeg
        with open(args[-1], "wb") as f:
            f.write(b"jpeg")
        return subprocess.CompletedProcess(args, 0)

    out = snapshot.latest_recording_frame(host, root=str(tmp_path), out_dir=str(tmp_path),
                                          now=1040, max_age=60, _run=fake_run)
    assert out and stat.S_IMODE(seen[0]) == 0o600
    assert os.path.basename(out) != "snaprec_192_0_2_50_1040000.jpg"


def test_recording_frame_failure_unlinks_placeholder(tmp_path):
    host = "192.0.2.50"
    _segment(tmp_path, host)

    def boom(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    assert snapshot.latest_recording_frame(host, root=str(tmp_path), out_dir=str(tmp_path),
                                           now=1040, max_age=60, _run=boom) is None
    assert not list(tmp_path.glob("snaprec_*"))


class _Redirector(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        _Redirector.seen.append(self.headers.get("Authorization"))
        body = b'{"person": 0.5}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _redirect_server():
    _Redirector.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_shadow_call_does_not_replay_bearer_on_redirect():
    srv = _redirect_server()
    try:
        payload, err = scorer_service._shadow_call(
            f"http://127.0.0.1:{srv.server_port}/start", DUMMY_VALUE, b"x", 1, 5)
    finally:
        srv.shutdown()
    assert _Redirector.seen == [None], (payload, err)


def test_replay_zones_post_does_not_replay_bearer_on_redirect(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "replay_zones", os.path.join(os.path.dirname(__file__), "..", "tools", "replay_zones.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    srv = _redirect_server()
    try:
        mod._post(f"http://127.0.0.1:{srv.server_port}/start", str(img), 5, **AUTH)
    finally:
        srv.shutdown()
    assert _Redirector.seen == [None]


def test_auth_header_still_sent_to_original_url():
    req = scorer.keep_auth_off_redirects(urllib.request.Request(
        "http://x/", data=b"", headers={"Authorization": "Bearer t"}))
    assert req.get_header("Authorization") == "Bearer t"
    assert "Authorization" not in req.headers


def _big_jpeg():
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("L", (9000, 9000)).save(buf, "JPEG")      # 81 MP, a few KB of body
    return buf.getvalue()


def test_open_image_rejects_oversized_images():
    pytest.importorskip("numpy")
    with pytest.raises(scorer_service.ImageTooLarge):
        scorer_service.open_image(_big_jpeg())
    with pytest.raises(scorer_service.ImageTooLarge):
        scorer_service.preprocess(_big_jpeg(), 64)
    assert scorer_service.MAX_IMAGE_PIXELS == 64_000_000
    from PIL import Image
    assert Image.MAX_IMAGE_PIXELS == 64_000_000


def test_score_endpoint_answers_413_for_pixel_bomb():
    pytest.importorskip("PIL")
    calls = []
    srv = scorer_service.make_server(lambda b, tiles=1: calls.append(1) or {"person": 0},
                                     port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/score", data=_big_jpeg())
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 413
        assert json.loads(ei.value.read())["error"] == "image too large"
        with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
            assert "image_too_large" in r.read().decode()
    finally:
        srv.shutdown()
    assert calls == []


def _check_whitelist(argv):
    i = argv.index("-protocol_whitelist")
    assert argv[i + 1] == "file" and i < argv.index("-i")


def test_ffmpeg_argv_whitelists_file_protocol_only(tmp_path, monkeypatch):
    _check_whitelist(snapshot.ts_frame_args("c.mp4", "o.jpg"))

    argvs = []
    monkeypatch.setattr(subprocess, "run",
                        lambda a, **k: argvs.append(a) or subprocess.CompletedProcess(a, 0))
    sdclip._extract_frames("c.mp4", str(tmp_path), "b", 2, 1)
    assert argvs
    for a in argvs:
        _check_whitelist(a)

    argvs.clear()
    recclip.extract_frames("seg.mkv", 0, 0, 2, 1, str(tmp_path), "b",
                           runner=lambda a: argvs.append(a))
    assert argvs
    for a in argvs:
        _check_whitelist(a)
    _check_whitelist(snapshot.ts_frame_args("c.mp4", "o.jpg"))
