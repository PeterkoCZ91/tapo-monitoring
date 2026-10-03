"""Shadow run of a second detector inside the scorer service."""

import json
import os
import stat
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("numpy")

from tapo_monitor import scorer_service  # noqa: E402

DUMMY_SHADOW_TOKEN = "dummy-shadow-auth-0123456789"
SOURCE = "a" * 24


class Shadow:
    """Local stand-in for the second scorer service."""

    def __init__(self, person=0.1, delay=0.0, status=200):
        outer = self
        self.person, self.delay, self.status = person, delay, status
        self.received = []
        self.auth = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                time.sleep(outer.delay)
                payload = json.dumps({"person": outer.person}).encode()
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/score"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _primary(shadow_cfg=None, person=0.9):
    result = {"person": person, "animal": 0.0, "vehicle": 0.2}
    srv = scorer_service.make_server(lambda body, tiles=1: dict(result), port=0,
                                     shadow=shadow_cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(base, body=b"jpegbytes"):
    req = urllib.request.Request(base + "/score", data=body,
                                 headers={"X-Tapo-Source-ID": SOURCE})
    t = time.monotonic()
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read(), time.monotonic() - t


def _metrics(base):
    with urllib.request.urlopen(base + "/metrics", timeout=5) as resp:
        return json.loads(resp.read())


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def stack(tmp_path):
    made = []

    def build(shadow=None, person=0.9, disagree=True, **kw):
        log = str(tmp_path / "shadow.jsonl")
        dis = str(tmp_path / "dis") if disagree else None
        cfg = scorer_service.ShadowConfig(
            url=(shadow.url if shadow else "http://127.0.0.1:9/score"),
            token=kw.pop("token", DUMMY_SHADOW_TOKEN), log_path=log, disagree_dir=dis, **kw)
        srv, base = _primary(cfg, person)
        made.append((srv, shadow))
        return base, log, dis, srv

    yield build
    for srv, shadow in made:
        srv.shutdown()
        srv.server_close()
        if shadow:
            shadow.close()


def test_reply_identical_with_and_without_shadow(stack):
    sh = Shadow(person=0.1)
    base, _log, _, _ = stack(sh)
    plain, pbase = _primary(None)
    try:
        assert _post(base)[1] == _post(pbase)[1]
    finally:
        plain.shutdown()
        plain.server_close()


def test_log_line_has_both_scores_and_no_secrets(stack):
    sh = Shadow(person=0.33)
    base, log, _, _ = stack(sh)
    _post(base)
    assert _wait(lambda: _lines(log))
    row = _lines(log)[0]
    assert row["source_id"] == SOURCE
    assert row["primary_person"] == 0.9 and row["primary_vehicle"] == 0.2
    assert row["shadow_person"] == 0.33
    assert row["error"] is None
    assert row["primary_seconds"] >= 0 and row["shadow_seconds"] >= 0
    assert "ts" in row
    raw = open(log, encoding="utf-8").read()
    assert DUMMY_SHADOW_TOKEN not in raw and sh.url not in raw and "127.0.0.1" not in raw
    assert sh.auth[0] == f"Bearer {DUMMY_SHADOW_TOKEN}"
    assert sh.received[0] == b"jpegbytes"


def test_slow_shadow_does_not_slow_the_primary(stack):
    sh = Shadow(person=0.1, delay=1.5)
    base, log, _, _ = stack(sh, timeout=0.3)
    status, _, took = _post(base)
    assert status == 200 and took < 1.0
    assert _wait(lambda: _lines(log))
    assert _lines(log)[0]["shadow_person"] is None
    assert _lines(log)[0]["error"] == "timeout"


def test_dead_shadow_is_counted_not_a_primary_failure(stack):
    base, log, _, _ = stack(None)           # nothing listens on port 9
    assert _post(base)[0] == 200
    assert _wait(lambda: _lines(log))
    assert _lines(log)[0]["error"]
    assert _wait(lambda: _metrics(base)["shadow"]["failed"] == 1)
    assert _metrics(base)["failed"] == 0


def test_401_from_shadow(stack):
    sh = Shadow(status=401)
    base, log, _, _ = stack(sh)
    _post(base)
    assert _wait(lambda: _lines(log))
    assert _lines(log)[0]["error"] == "http_401"


def test_disagreement_saved_with_modes_and_name(stack):
    sh = Shadow(person=0.1)
    base, log, dis, _ = stack(sh, person=0.9)
    _post(base)
    assert _wait(lambda: os.path.isdir(dis) and os.listdir(dis))
    (name,) = os.listdir(dis)
    assert name.endswith("_p0.90_s0.10.jpg")
    assert stat.S_IMODE(os.stat(os.path.join(dis, name)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(dis).st_mode) == 0o700
    assert open(os.path.join(dis, name), "rb").read() == b"jpegbytes"
    assert _wait(lambda: _metrics(base)["shadow"]["disagree"] == 1)


def test_agreement_and_gray_band_save_nothing(stack):
    sh = Shadow(person=0.3)         # 0.9 vs 0.3: not < 0.25
    base, log, dis, _ = stack(sh, person=0.9)
    _post(base)
    assert _wait(lambda: _lines(log))
    assert not os.path.isdir(dis) or not os.listdir(dis)
    assert _metrics(base)["shadow"]["disagree"] == 0


def test_no_disagree_dir_means_no_images(stack, tmp_path):
    sh = Shadow(person=0.1)
    base, log, _, _ = stack(sh, disagree=False)
    _post(base)
    assert _wait(lambda: _lines(log))
    assert [p for p in os.listdir(tmp_path) if p.endswith(".jpg")] == []


def test_disagree_limit_deletes_oldest(stack):
    sh = Shadow(person=0.1)
    base, log, dis, _ = stack(sh, person=0.9, disagree_max=3)
    for _ in range(6):
        _post(base)
        time.sleep(0.02)
    assert _wait(lambda: len(_lines(log)) == 6)
    assert len(os.listdir(dis)) == 3


def test_full_queue_drops_and_counts(stack):
    sh = Shadow(person=0.1, delay=0.5)
    base, log, _, _ = stack(sh, queue_size=2, timeout=2)
    for _ in range(8):
        assert _post(base)[0] == 200
    m = _metrics(base)["shadow"]
    assert m["dropped"] >= 4
    assert set(m) == {"sent", "failed", "dropped", "disagree"}


def test_off_means_no_shadow_block():
    srv, base = _primary(None)
    try:
        _post(base)
        assert "shadow" not in _metrics(base)
    finally:
        srv.shutdown()
        srv.server_close()


def test_load_shadow_env(tmp_path):
    assert scorer_service.load_shadow({}) is None
    assert scorer_service.load_shadow({"TAPO_SCORER_SHADOW_URL": "http://x/score"}) is None
    tok = tmp_path / "t"
    tok.write_text("abc\n")
    tok.chmod(0o600)
    env = {
        "TAPO_SCORER_SHADOW_URL": "http://x/score",
        "TAPO_SCORER_SHADOW_LOG": str(tmp_path / "l.jsonl"),
        "TAPO_SCORER_SHADOW_TOKEN_FILE": str(tok),
        "TAPO_SCORER_SHADOW_DISAGREE_DIR": str(tmp_path / "d"),
    }
    cfg = scorer_service.load_shadow(env)
    assert cfg.token == "abc" and cfg.disagree_max == 300 and cfg.timeout == 8.0
    tok.chmod(0o644)
    with pytest.raises(scorer_service.TokenConfigError):
        scorer_service.load_shadow(env)


def test_repeated_failures_log_once_a_minute(stack, caplog):
    base, log, _, _ = stack(None)
    with caplog.at_level("WARNING"):
        for _ in range(4):
            _post(base)
        assert _wait(lambda: len(_lines(log)) == 4)
    assert len([r for r in caplog.records if "shadow" in r.getMessage()]) == 1
