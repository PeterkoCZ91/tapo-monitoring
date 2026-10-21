"""Review fixes in the scorer service: request limits, tiles ceiling, ensemble, eviction."""

import http.client
import json
import threading
import time

import pytest

pytest.importorskip("numpy")

from tapo_monitor import scorer_service  # noqa: E402


@pytest.fixture
def server():
    seen = []

    def score(body, tiles=1):
        seen.append(tiles)
        return {"person": 0.8, "animal": 0.0, "tiles": tiles}

    srv = scorer_service.make_server(score, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()


def _post(port, path="/score", headers=None, body=b"x"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", path)
    for key, value in (headers or {}).items():
        conn.putheader(key, value)
    conn.endheaders(body)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _metrics(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/metrics")
    out = json.loads(conn.getresponse().read())
    conn.close()
    return out


def _wait_idle(port):
    deadline = time.monotonic() + 3
    while True:
        m = _metrics(port)
        if m["in_flight"] == 0 or time.monotonic() > deadline:
            return m
        time.sleep(0.02)


def test_oversized_content_length_is_413_and_accounted(server):
    port, _ = server
    status, _ = _post(port, headers={"Content-Length": str(scorer_service.MAX_BODY_BYTES + 1)},
                      body=b"")
    assert status == 413
    m = _wait_idle(port)
    assert m["in_flight"] == 0 and m["failed"] == 1


@pytest.mark.parametrize("value", ["-5", "abc"])
def test_negative_or_invalid_content_length_is_400(server, value):
    port, _ = server
    status, _ = _post(port, headers={"Content-Length": value}, body=b"")
    assert status == 400
    assert _wait_idle(port)["in_flight"] == 0


def test_handler_has_a_socket_timeout():
    srv = scorer_service.make_server(lambda b, tiles=1: {"person": 0.0}, port=0)
    try:
        assert srv.RequestHandlerClass.timeout
    finally:
        srv.server_close()


def test_tiles_query_is_clamped(server):
    port, seen = server
    status, _ = _post(port, path="/score?tiles=500")
    assert status == 200
    assert seen == [scorer_service.MAX_TILES]


def test_ensemble_survives_non_numeric_shadow_scores():
    cfg = scorer_service.EnsembleConfig()
    primary = {"person": 0.9, "person_scores": [0.9]}
    for person in (0.95, 0.5):          # shadow wins outright, and the marginal band
        shadow = {"person": person, "person_scores": [person, "oops"]}
        reply, used = scorer_service.combine_ensemble(cfg, primary, shadow)
        assert used == "primary"
        assert reply["person_scores"] == [0.9] and reply["person"] == 0.9


def test_missed_last_is_bounded(tmp_path):
    runner = scorer_service.ShadowRunner(scorer_service.ShadowConfig(
        url="http://127.0.0.1:9/score", log_path="", disagree_dir=str(tmp_path)))
    try:
        runner._save_disagreement = lambda *a, **k: True
        for i in range(scorer_service.MISSED_SOURCES_MAX + 50):
            runner._process(b"x", 1, f"s{i}", {"person": 0.05}, 0.1,
                            outcome=(0.3, None, 0.1, {"person": 0.3}))
        assert len(runner._missed_last) == scorer_service.MISSED_SOURCES_MAX
        assert "s299" in runner._missed_last and "s0" not in runner._missed_last
    finally:
        runner._stop.set()
