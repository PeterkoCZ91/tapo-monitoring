"""Ensemble mode of the scorer service: one combined person score from two detectors."""

import json
import logging
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("numpy")

from tapo_monitor import scorer_service  # noqa: E402

BEARER_VALUE = "ens-" + "x" * 24
SOURCE = "b" * 24
PBOX = [[1, 2, 3, 4]]
SBOX = [[10, 20, 30, 40]]


class Shadow:
    def __init__(self, person=0.1, delay=0.0, status=200, raw=None, require_bearer=None):
        outer = self
        self.person, self.delay, self.status, self.raw = person, delay, status, raw
        self.require_bearer = require_bearer
        self.received, self.paths = [], []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append(body)
                outer.paths.append(self.path)
                time.sleep(outer.delay)
                status = outer.status
                if (outer.require_bearer
                        and self.headers.get("Authorization") != f"Bearer {outer.require_bearer}"):
                    status = 401
                payload = outer.raw if outer.raw is not None else json.dumps({
                    "person": outer.person, "box": SBOX[0], "person_boxes": SBOX,
                    "person_scores": [outer.person], "animal": 0.7, "w": 1, "h": 1,
                }).encode()
                try:
                    self.send_response(status)
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
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/score"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _primary_result(person):
    return {"person": person, "animal": 0.05, "vehicle": 0.2, "classes": {"x": 1},
            "w": 640, "h": 480, "box": PBOX[0], "person_boxes": PBOX,
            "person_scores": [person]}


@pytest.fixture
def stack(tmp_path):
    made = []

    def build(shadow, person=0.9, ensemble=True, timeout=2.0, log=True,
              primary_delay=0.0):
        # positional: url, log_path, shared secret, disagreement directory
        cfg = scorer_service.ShadowConfig(
            shadow.url, str(tmp_path / "shadow.jsonl") if log else "", BEARER_VALUE,
            str(tmp_path / "dis"))
        ens = scorer_service.EnsembleConfig(timeout=timeout) if ensemble else None

        def score(body, tiles=1):
            time.sleep(primary_delay)
            return _primary_result(person)

        srv = scorer_service.make_server(score, port=0, shadow=cfg, ensemble=ens)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        made.append((srv, shadow))
        return f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "shadow.jsonl", srv

    yield build
    for srv, shadow in made:
        srv.shutdown()
        srv.server_close()
        srv.shadow.close()
        shadow.close()


def _post(base, query="", body=b"jpegbytes"):
    req = urllib.request.Request(base + "/score" + query, data=body,
                                 headers={"X-Tapo-Source-ID": SOURCE})
    t = time.monotonic()
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()), time.monotonic() - t


def _metrics(base):
    with urllib.request.urlopen(base + "/metrics", timeout=5) as resp:
        return json.loads(resp.read())


# (primary, shadow, expected person, used)
RULE = [
    (0.90, 0.60, 0.60, "shadow"),      # shadow high -> shadow wins even if lower
    (0.20, 0.45, 0.45, "shadow"),      # HIGH boundary inclusive
    (0.10, 0.80, 0.80, "shadow"),
    (0.90, 0.12, 0.90, "primary"),     # LOW boundary inclusive
    (0.45, 0.30, 0.45, "primary"),     # primary HIGH boundary inclusive
    (0.90, 0.1199, 0.44, "neither"),   # shadow below LOW vetoes primary, capped under HIGH
    (0.90, 0.0, 0.44, "neither"),
    (0.449, 0.40, 0.44, "neither"),    # both under HIGH: max, then the cap
    (0.43, 0.05, 0.43, "neither"),     # below the cap: max unchanged
    (0.30, 0.10, 0.30, "neither"),
    (0.0, 0.0, 0.0, "neither"),
]


@pytest.mark.parametrize("primary,shadow,expected,used", RULE)
def test_rule_branches(stack, primary, shadow, expected, used):
    sh = Shadow(person=shadow)
    base, _log, _srv = stack(sh, person=primary)
    reply, _ = _post(base)
    assert reply["person"] == pytest.approx(expected)
    assert reply["ensemble"]["used"] == used
    assert reply["ensemble"]["mode"] == "gate"
    assert reply["ensemble"]["primary"] == pytest.approx(primary)
    assert reply["ensemble"]["shadow"] == pytest.approx(shadow)


def test_a_vetoed_person_cannot_come_back_through_the_score_list():
    # The shadow denies a person the primary was sure of: every score in the reply must stay
    # below HIGH, or a client that recomputes ``person`` from ``person_scores`` (alert zones)
    # would revive the demoted person.
    cfg = scorer_service.EnsembleConfig()
    primary = {"person": 0.9, "person_scores": [0.9, 0.3], "box": [1, 2, 3, 4],
               "person_boxes": [[1, 2, 3, 4], [5, 6, 7, 8]]}
    reply, used = scorer_service.combine_ensemble(cfg, primary, {"person": 0.05})
    assert used == "neither"
    assert reply["person"] < cfg.high
    assert all(x < cfg.high for x in reply["person_scores"])
    assert reply["person_scores"] == [cfg.high - 0.01, 0.3]


def test_boxes_from_shadow_when_shadow_wins_and_other_fields_primary(stack):
    sh = Shadow(person=0.7)
    base, _, _ = stack(sh, person=0.2)
    reply, _ = _post(base)
    assert reply["box"] == SBOX[0] and reply["person_boxes"] == SBOX
    assert reply["person_scores"] == [0.7]
    assert reply["animal"] == 0.05 and reply["vehicle"] == 0.2
    assert reply["classes"] == {"x": 1} and reply["w"] == 640 and reply["h"] == 480


def test_boxes_from_primary_otherwise(stack):
    sh = Shadow(person=0.3)
    base, _, _ = stack(sh, person=0.9)
    reply, _ = _post(base)
    assert reply["ensemble"]["used"] == "primary"
    assert reply["box"] == PBOX[0] and reply["person_boxes"] == PBOX
    assert reply["person_scores"] == [0.9]


def test_same_image_and_tiles_forwarded(stack):
    sh = Shadow(person=0.3)
    base, _, _ = stack(sh)
    _post(base, "?tiles=3", body=b"the-frame")
    assert sh.received == [b"the-frame"]
    assert "tiles=3" in sh.paths[0]


def test_one_shadow_call_per_request_and_log_still_written(stack):
    sh = Shadow(person=0.3)
    base, log, _ = stack(sh, person=0.9)
    _post(base)
    end = time.monotonic() + 5
    while time.monotonic() < end and not (log.exists() and log.read_text().strip()):
        time.sleep(0.02)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    time.sleep(0.2)
    assert len(sh.received) == 1 and len(rows) == 1
    assert rows[0]["shadow_person"] == pytest.approx(0.3) and rows[0]["error"] is None


def test_disagreement_still_saved(stack, tmp_path):
    sh = Shadow(person=0.1)
    base, _, _ = stack(sh, person=0.9)
    _post(base)
    end = time.monotonic() + 5
    while time.monotonic() < end and not list((tmp_path / "dis").glob("*.jpg")):
        time.sleep(0.02)
    assert list((tmp_path / "dis").glob("*.jpg"))


def test_parallel_not_serial(stack):
    sh = Shadow(person=0.3, delay=0.4)
    base, _, _ = stack(sh, person=0.9, primary_delay=0.4)
    _, took = _post(base)
    assert took < 0.75


@pytest.mark.parametrize("make", [
    lambda: Shadow(status=500),
    lambda: Shadow(raw=b"not json"),
    lambda: Shadow(raw=b'{"person": "x"}'),
    lambda: Shadow(person=0.9, require_bearer="other-value-123456"),   # 401
])
def test_shadow_failure_returns_primary_unchanged(stack, make):
    sh = make()
    base, _, _ = stack(sh, person=0.9)
    reply, _ = _post(base)
    assert reply["person"] == 0.9 and reply["box"] == PBOX[0]
    assert reply["ensemble"] == {"mode": "gate", "primary": 0.9, "shadow": None,
                                 "used": "primary_only"}
    block = _metrics(base)["ensemble"]
    assert block["primary_only"] == 1 and block["failed"] == 1


def test_timeout_bounded(stack):
    sh = Shadow(person=0.9, delay=1.5)
    base, _, _ = stack(sh, person=0.2, timeout=0.3)
    reply, took = _post(base)
    assert took < 1.0
    assert reply["ensemble"]["used"] == "primary_only" and reply["person"] == 0.2
    assert _metrics(base)["ensemble"]["timeouts"] == 1


def test_disabled_reply_identical_and_no_block(stack):
    sh = Shadow(person=0.9)
    base, _, _ = stack(sh, person=0.3, ensemble=False)
    reply, _ = _post(base)
    assert reply == _primary_result(0.3)
    assert "ensemble" not in _metrics(base)


def test_metrics_counts(stack):
    sh = Shadow(person=0.6)
    base, _, _ = stack(sh, person=0.9)
    _post(base)
    _post(base)
    block = _metrics(base)["ensemble"]
    assert block["mode"] == "gate" and block["requests"] == 2 and block["shadow"] == 2


def test_token_sent_but_url_and_token_never_logged(stack, caplog):
    sh = Shadow(status=500, require_bearer=BEARER_VALUE)
    base, _, _ = stack(sh)
    with caplog.at_level(logging.DEBUG):
        _post(base)
    assert sh.received
    assert BEARER_VALUE not in caplog.text and str(sh.port) not in caplog.text


def test_error_warning_rate_limited(stack, caplog):
    sh = Shadow(status=500)
    base, _, _ = stack(sh)
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            _post(base)
    warns = [r for r in caplog.records if "ensemble" in r.getMessage()]
    assert len(warns) == 1


def test_ensemble_without_log_path_works(stack):
    sh = Shadow(person=0.6)
    base, _, _ = stack(sh, person=0.9, log=False)
    reply, _ = _post(base)
    assert reply["ensemble"]["used"] == "shadow"


class TestLoad:
    def test_off_by_default(self):
        assert scorer_service.load_ensemble({}, None) is None
        assert scorer_service.load_ensemble({"TAPO_SCORER_ENSEMBLE": ""}, None) is None

    def test_requires_shadow(self):
        with pytest.raises(scorer_service.EnsembleConfigError):
            scorer_service.load_ensemble({"TAPO_SCORER_ENSEMBLE": "gate"}, None)

    def test_unknown_mode(self):
        cfg = scorer_service.ShadowConfig(url="http://x/score", log_path="")
        with pytest.raises(scorer_service.EnsembleConfigError):
            scorer_service.load_ensemble({"TAPO_SCORER_ENSEMBLE": "vote"}, cfg)

    def test_values(self):
        cfg = scorer_service.ShadowConfig(url="http://x/score", log_path="")
        e = scorer_service.load_ensemble({
            "TAPO_SCORER_ENSEMBLE": "gate", "TAPO_SCORER_ENSEMBLE_LOW": "0.2",
            "TAPO_SCORER_ENSEMBLE_HIGH": "0.5", "TAPO_SCORER_ENSEMBLE_TIMEOUT": "1.5"}, cfg)
        assert (e.low, e.high, e.timeout) == (0.2, 0.5, 1.5)
        d = scorer_service.load_ensemble({"TAPO_SCORER_ENSEMBLE": "gate"}, cfg)
        assert (d.low, d.high, d.timeout) == (0.12, 0.45, 2.0)

    def test_low_must_be_below_high(self):
        cfg = scorer_service.ShadowConfig(url="http://x/score", log_path="")
        with pytest.raises(scorer_service.EnsembleConfigError):
            scorer_service.load_ensemble({"TAPO_SCORER_ENSEMBLE": "gate",
                                          "TAPO_SCORER_ENSEMBLE_LOW": "0.5"}, cfg)

    def test_shadow_loads_without_log_only_when_ensemble_on(self):
        env = {"TAPO_SCORER_SHADOW_URL": "http://x/score"}
        assert scorer_service.load_shadow(env) is None
        env["TAPO_SCORER_ENSEMBLE"] = "gate"
        assert scorer_service.load_shadow(env).log_path == ""
