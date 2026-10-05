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
        self.payload = None

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                time.sleep(outer.delay)
                payload = json.dumps(outer.payload or {"person": outer.person}).encode()
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


def _primary(shadow_cfg=None, person=0.9, extra=None):
    result = {"person": person, "animal": 0.0, "vehicle": 0.2, **(extra or {})}
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

    def build(shadow=None, person=0.9, disagree=True, extra=None, **kw):
        log = str(tmp_path / "shadow.jsonl")
        dis = str(tmp_path / "dis") if disagree else None
        cfg = scorer_service.ShadowConfig(
            url=(shadow.url if shadow else "http://127.0.0.1:9/score"),
            token=kw.pop("token", DUMMY_SHADOW_TOKEN), log_path=log, disagree_dir=dis, **kw)
        srv, base = _primary(cfg, person, extra)
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


def test_log_line_carries_normalized_person_boxes(stack):
    sh = Shadow(person=0.33)
    sh.payload = {"person": 0.33, "w": 200, "h": 100, "person_scores": [0.33],
                  "person_boxes": [[20, 10, 100, 90]]}
    extra = {"w": 100, "h": 50,
             "person_scores": [0.5 + i / 100 for i in range(12)],
             "person_boxes": [[float(i), 0.0, float(i) + 10, 50.0] for i in range(12)]}
    base, log, _, _ = stack(sh, extra=extra)
    reply = json.loads(_post(base)[1])
    assert "primary_boxes" not in reply and "shadow_boxes" not in reply
    assert _wait(lambda: _lines(log))
    row = _lines(log)[0]
    assert len(row["primary_boxes"]) == 10
    assert row["primary_boxes"][0] == [0.11, 0.0, 0.21, 1.0, 0.61]
    assert row["shadow_boxes"] == [[0.1, 0.1, 0.5, 0.9, 0.33]]


def test_log_line_without_boxes_in_replies_has_no_box_fields(stack):
    sh = Shadow(person=0.33)
    base, log, _, _ = stack(sh)
    _post(base)
    assert _wait(lambda: _lines(log))
    row = _lines(log)[0]
    assert "primary_boxes" not in row and "shadow_boxes" not in row


def test_log_line_carries_a_short_frame_fingerprint(stack):
    import hashlib
    sh = Shadow(person=0.33)
    base, log, _, _ = stack(sh)
    _post(base, body=b"frame-one")
    _post(base, body=b"frame-one")
    _post(base, body=b"frame-two")
    assert _wait(lambda: len(_lines(log)) == 3)
    frames = [r["frame"] for r in _lines(log)]
    assert frames[0] == hashlib.sha256(b"frame-one").hexdigest()[:16]
    assert frames[0] == frames[1] != frames[2]
    assert "frame-one" not in open(log).read()


def test_no_frame_hash_without_a_shadow_log(monkeypatch):
    seen = []
    monkeypatch.setattr(scorer_service.hashlib, "sha256",
                        lambda *a, **k: seen.append(1) or None)
    runner = scorer_service.ShadowRunner(scorer_service.ShadowConfig(
        url="http://127.0.0.1:9/score", token=DUMMY_SHADOW_TOKEN, log_path=None))
    try:
        runner._process(b"x", 1, SOURCE, {"person": 0.1}, 0.01,
                        outcome=(None, "timeout", 0.0, None))
    finally:
        runner.close()
    assert seen == []


def test_log_boxes_helper():
    f = scorer_service._log_boxes
    assert f(None) is None and f({"person": 0.5}) is None
    assert f({"person_boxes": [[0, 0, 1, 1]], "person_scores": [0.5]}) is None   # no w/h
    assert f({"person_boxes": [], "person_scores": [], "w": 10, "h": 10}) == []
    assert f({"person_boxes": [[-5, 0, 20, 5]], "person_scores": [0.12345], "w": 10,
              "h": 10}) == [[0.0, 0.0, 1.0, 0.5, 0.123]]


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
    assert cfg.queue_size == scorer_service.SHADOW_QUEUE_SIZE
    assert scorer_service.load_shadow({**env, "TAPO_SCORER_SHADOW_QUEUE": "64"}).queue_size == 64
    assert scorer_service.load_shadow({**env, "TAPO_SCORER_SHADOW_QUEUE": "0"}).queue_size == 1
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


# --- optional second shadow target (TAPO_SCORER_SHADOW2_*) -------------------------------

OLD_KEYS = {"ts", "source_id", "primary_person", "primary_vehicle", "shadow_person",
            "primary_seconds", "shadow_seconds", "error", "frame"}


def _two(tmp_path, sh1, sh2, name="yolox960", **kw2):
    cfg1 = scorer_service.ShadowConfig(url=sh1.url, log_path=str(tmp_path / "s1.jsonl"))
    cfg2 = scorer_service.ShadowConfig(
        url=(sh2.url if sh2 else "http://127.0.0.1:9/score"),
        log_path=str(tmp_path / "s2.jsonl"), name=name, **kw2)
    srv = scorer_service.make_server(lambda body, tiles=1: {"person": 0.9, "vehicle": 0.2},
                                     port=0, shadow=cfg1, shadow2=cfg2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", cfg1.log_path, cfg2.log_path


def _stop(srv, *shadows):
    for runner in (srv.shadow, srv.shadow2):
        if runner is not None:
            runner.close()      # end the worker threads: later tests count live threads
    srv.shutdown()
    srv.server_close()
    for s in shadows:
        if s:
            s.close()


def test_first_shadow_rows_unchanged_without_shadow2(stack):
    sh = Shadow(person=0.3)
    base, log, _, _ = stack(sh)
    _post(base)
    assert _wait(lambda: _lines(log))
    assert set(_lines(log)[0]) == OLD_KEYS     # no shadow_name
    assert "shadow2" not in _metrics(base)


def test_second_shadow_logs_separately_with_name(tmp_path):
    sh1, sh2 = Shadow(person=0.3), Shadow(person=0.7)
    srv, base, log1, log2 = _two(tmp_path, sh1, sh2)
    try:
        _post(base, body=b"frame-x")
        assert _wait(lambda: _lines(log1) and _lines(log2))
        r1, r2 = _lines(log1)[0], _lines(log2)[0]
        assert set(r1) == OLD_KEYS                      # first shadow: format unchanged
        assert set(r2) == OLD_KEYS | {"shadow_name"}
        assert r2["shadow_name"] == "yolox960" and r2["shadow_person"] == 0.7
        assert r1["shadow_person"] == 0.3 and r1["frame"] == r2["frame"]
        assert sh2.received == [b"frame-x"]
        assert _wait(lambda: _metrics(base)["shadow2"]["sent"] == 1)
        m = _metrics(base)
        assert set(m["shadow2"]) == {"sent", "failed", "dropped", "disagree"}
        assert m["shadow"]["sent"] == 1
    finally:
        _stop(srv, sh1, sh2)


def test_reply_unchanged_by_second_shadow(tmp_path):
    sh1, sh2 = Shadow(), Shadow()
    srv, base, _, _ = _two(tmp_path, sh1, sh2)
    try:
        assert json.loads(_post(base)[1]) == {"person": 0.9, "vehicle": 0.2}
    finally:
        _stop(srv, sh1, sh2)


def test_dead_second_shadow_does_not_affect_first_or_reply(tmp_path):
    sh1 = Shadow(person=0.3)
    srv, base, log1, log2 = _two(tmp_path, sh1, None)
    try:
        assert _post(base)[0] == 200
        assert _wait(lambda: _lines(log1) and _lines(log2))
        assert _lines(log1)[0]["error"] is None
        assert _lines(log2)[0]["error"] and _lines(log2)[0]["shadow_person"] is None
        assert _wait(lambda: _metrics(base)["shadow2"]["failed"] == 1)
        assert _metrics(base)["shadow"]["failed"] == 0 and _metrics(base)["failed"] == 0
    finally:
        _stop(srv, sh1)


def test_slow_second_shadow_blocks_neither_reply_nor_first_shadow(tmp_path):
    sh1, sh2 = Shadow(person=0.3), Shadow(person=0.7, delay=1.5)
    srv, base, log1, log2 = _two(tmp_path, sh1, sh2, timeout=3)
    try:
        for _ in range(3):
            status, _, took = _post(base)
            assert status == 200 and took < 1.0
        assert _wait(lambda: len(_lines(log1)) == 3, timeout=2.0)
        assert len(_lines(log2)) < 3
    finally:
        _stop(srv, sh1, sh2)


def test_full_queue_of_second_shadow_drops_only_there(tmp_path):
    sh1, sh2 = Shadow(person=0.3), Shadow(person=0.7, delay=0.5)
    srv, base, log1, _ = _two(tmp_path, sh1, sh2, queue_size=2, timeout=2)
    try:
        for _ in range(8):
            assert _post(base)[0] == 200
        assert _wait(lambda: len(_lines(log1)) == 8)
        m = _metrics(base)
        assert m["shadow2"]["dropped"] >= 4 and m["shadow"]["dropped"] == 0
    finally:
        _stop(srv, sh1, sh2)


def test_load_shadow2_env(tmp_path):
    assert scorer_service.load_shadow2({}) is None
    assert scorer_service.load_shadow2({"TAPO_SCORER_SHADOW2_URL": "http://x/score"}) is None
    assert scorer_service.load_shadow2({"TAPO_SCORER_SHADOW2_LOG": "l"}) is None
    # the first shadow's variables never switch the second one on
    assert scorer_service.load_shadow2({"TAPO_SCORER_SHADOW_URL": "http://x/score",
                                        "TAPO_SCORER_SHADOW_LOG": "l"}) is None
    tok = tmp_path / "t"
    tok.write_text("abc\n")
    tok.chmod(0o600)
    env = {"TAPO_SCORER_SHADOW2_URL": "http://x/score",
           "TAPO_SCORER_SHADOW2_LOG": str(tmp_path / "l2.jsonl"),
           "TAPO_SCORER_SHADOW2_TOKEN_FILE": str(tok),
           "TAPO_SCORER_SHADOW2_QUEUE": "4"}
    cfg = scorer_service.load_shadow2(env)
    assert cfg.name == "shadow2" and cfg.token == "abc" and cfg.queue_size == 4
    assert scorer_service.load_shadow2({**env, "TAPO_SCORER_SHADOW2_NAME": "yolox960"}
                                       ).name == "yolox960"
