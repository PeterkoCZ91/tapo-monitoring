import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

np = pytest.importorskip("numpy")  # service tests need numpy; core package does not

from tapo_monitor import scorer_service


@pytest.fixture
def server():
    srv = scorer_service.make_server(lambda body: {"person": 0.8, "animal": 0.0, "n": len(body)},
                                     port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_post_score_returns_json(server):
    req = urllib.request.Request(f"{server}/score", data=b"jpegbytes",
                                 headers={"Content-Type": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        out = json.load(resp)
    assert out == {"person": 0.8, "animal": 0.0, "n": 9}


def test_health(server):
    with urllib.request.urlopen(f"{server}/health", timeout=5) as resp:
        assert json.load(resp) == {"ok": True}


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/nope", timeout=5)
    assert e.value.code == 404


def test_score_fn_error_500():
    def boom(_body):
        raise ValueError("bad image")
    srv = scorer_service.make_server(boom, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/score", data=b"x")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 500
    finally:
        srv.shutdown()


def test_scores_from_output_person_and_animal():
    out = np.zeros((1, 100, 85), dtype=np.float32)
    out[0, 3, 4] = 0.9          # objectness
    out[0, 3, 5 + 0] = 0.8      # person class -> 0.72
    out[0, 7, 4] = 0.5
    out[0, 7, 5 + 16] = 0.6     # dog -> 0.30
    scores = scorer_service.scores_from_output(out)
    assert scores["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["animal"] == pytest.approx(0.30, abs=1e-4)


def test_scores_from_output_empty_is_zero():
    out = np.zeros((1, 10, 85), dtype=np.float32)
    assert scorer_service.scores_from_output(out) == {"person": 0.0, "animal": 0.0}
