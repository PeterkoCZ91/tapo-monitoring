import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

np = pytest.importorskip("numpy")  # service tests need numpy; core package does not

from tapo_monitor import scorer_service


@pytest.fixture
def server():
    srv = scorer_service.make_server(
        lambda body, tiles=1: {"person": 0.8, "animal": 0.0, "n": len(body), "tiles": tiles},
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
    assert out == {"person": 0.8, "animal": 0.0, "n": 9, "tiles": 1}


def test_health(server):
    with urllib.request.urlopen(f"{server}/health", timeout=5) as resp:
        assert json.load(resp) == {"ok": True}


def _metrics_when_settled(server, timeout=3.0):
    """Read /metrics once the request has been accounted for.

    The server finishes a request before it books it, so a client holding the response can
    outrun the counter. That is fine in production — the metrics are aggregate and eventually
    consistent — but a test that reads them the microsecond after the response fails maybe
    one run in fifty. Poll instead of asserting on a race.
    """
    deadline = time.monotonic() + timeout
    while True:
        with urllib.request.urlopen(f"{server}/metrics", timeout=5) as resp:
            metrics = json.load(resp)
        if metrics["completed"] >= 1 or time.monotonic() > deadline:
            return metrics
        time.sleep(0.02)


def test_metrics_are_aggregate_only_and_count_tile_inference(server):
    req = urllib.request.Request(f"{server}/score?tiles=2", data=b"jpegbytes")
    source_id = "0123456789abcdef"
    req.add_header("X-Tapo-Source-ID", source_id)
    with urllib.request.urlopen(req, timeout=5):
        pass
    metrics = _metrics_when_settled(server)
    assert metrics["requests"] == 1
    assert metrics["completed"] == 1
    assert metrics["inference_runs"] == 5
    assert metrics["failed"] == 0
    assert metrics["score_successes"] == 1
    assert metrics["person_candidates"] == 1
    assert metrics["animal_candidates"] == 0
    assert metrics["malformed_responses"] == 0
    assert metrics["failure_reasons"] == {}
    assert metrics["score_seconds_max"] >= 0
    assert metrics["request_seconds_p50"] >= 0
    assert metrics["request_seconds_p95"] >= 0
    assert metrics["sources"][source_id]["requests"] == 1
    assert metrics["sources"][source_id]["score_successes"] == 1


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/nope", timeout=5)
    assert e.value.code == 404


def test_score_fn_error_500():
    def boom(_body, tiles=1):
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
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/metrics",
                                    timeout=5) as response:
            metrics = json.load(response)
        assert metrics["requests"] == 1
        assert metrics["completed"] == 1
        assert metrics["failed"] == 1
        assert metrics["in_flight"] == 0
        assert metrics["failure_reasons"] == {"inference_error": 1}
    finally:
        srv.shutdown()


def test_metrics_mark_malformed_score_result():
    srv = scorer_service.make_server(lambda _body, tiles=1: [tiles], port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/score",
            data=b"jpeg",
            timeout=5,
        ) as response:
            assert json.load(response) == [1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/metrics", timeout=5
        ) as response:
            metrics = json.load(response)
        assert metrics["completed"] == 1
        assert metrics["failed"] == 0
        assert metrics["score_successes"] == 0
        assert metrics["malformed_responses"] == 1
        assert metrics["failure_reasons"] == {"malformed_response": 1}
    finally:
        srv.shutdown()


def test_metrics_report_percentiles_from_completed_requests():
    metrics = scorer_service.ScorerMetrics()
    for request_seconds, score_seconds in ((1.0, 0.5), (2.0, 1.0), (3.0, 1.5)):
        metrics.begin(1)
        metrics.finish(
            request_seconds,
            score_seconds,
            True,
            result={"person": 0.0, "animal": 0.0},
        )

    snapshot = metrics.snapshot()
    assert snapshot["request_seconds_p50"] == pytest.approx(2.0)
    assert snapshot["request_seconds_p95"] == pytest.approx(2.9)
    assert snapshot["score_seconds_p50"] == pytest.approx(1.0)
    assert snapshot["score_seconds_p95"] == pytest.approx(1.45)


def test_invalid_state_counters_are_ignored(tmp_path):
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    state_file = metrics_file.with_name(metrics_file.name + ".state")
    state_file.write_text(json.dumps({"counters": []}), encoding="utf-8")

    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file))

    assert metrics.requests == 0
    assert metrics.completed == 0


def test_persistence_failure_does_not_break_scoring_accounting(tmp_path, monkeypatch):
    metrics = scorer_service.ScorerMetrics(
        metrics_file=str(tmp_path / "scorer-metrics.jsonl"), persist_seconds=0
    )

    def fail_persist():
        raise OSError("disk full")

    monkeypatch.setattr(metrics, "_persist_unlocked", fail_persist)
    metrics.begin(1)
    metrics.finish(0.1, 0.1, True, result={"person": 0.0, "animal": 0.0})

    assert metrics.snapshot()["completed"] == 1


def test_metrics_persist_across_restart(tmp_path):
    metrics_file = tmp_path / "scorer-metrics.jsonl"

    first = scorer_service.make_server(
        lambda _body, tiles=1: {"person": 0.8, "animal": 0.0},
        port=0,
        metrics_file=str(metrics_file),
        metrics_persist_seconds=0,
    )
    threading.Thread(target=first.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{first.server_address[1]}/score", data=b"jpeg"
            ),
            timeout=5,
        ):
            pass
        # Settle before shutting the server down. The response is written before the
        # request is booked, and it is booking that persists the counters, so tearing the
        # server down the moment the client returns can race the write the second server
        # is about to read. Once /metrics reports the request, finish() has released the
        # lock it persists under, so the file is on disk.
        _metrics_when_settled(f"http://127.0.0.1:{first.server_address[1]}")
    finally:
        first.shutdown()

    second = scorer_service.make_server(
        lambda _body, tiles=1: {"person": 0.0, "animal": 0.0},
        port=0,
        metrics_file=str(metrics_file),
        metrics_persist_seconds=0,
    )
    threading.Thread(target=second.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{second.server_address[1]}/metrics", timeout=5
        ) as response:
            metrics = json.load(response)
        assert metrics["requests"] == 1
        assert metrics["score_successes"] == 1
        assert metrics["person_candidates"] == 1
        assert metrics["in_flight"] == 0
        assert metrics["restart_count"] == 2
        assert len(metrics["instance_id"]) == 16
        assert metrics["started_at"].endswith("Z")
        assert metrics_file.exists()
        assert metrics_file.with_name(metrics_file.name + ".state").exists()
    finally:
        second.shutdown()


def test_metrics_journal_rotates_after_retention_window(tmp_path):
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    metrics_file.write_text('{"recorded_at":"old"}\n', encoding="utf-8")
    old = time.time() - (8 * 24 * 60 * 60)
    os.utime(metrics_file, (old, old))

    srv = scorer_service.make_server(
        lambda _body, tiles=1: {"person": 0.0, "animal": 0.0},
        port=0,
        metrics_file=str(metrics_file),
        metrics_persist_seconds=0,
        metrics_retention_days=7,
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/score", data=b"jpeg"
            ),
            timeout=5,
        ):
            pass
        deadline = time.monotonic() + 3
        rotated = []
        while time.monotonic() < deadline:
            rotated = [
                path for path in tmp_path.glob("scorer-metrics.jsonl.*")
                if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()
            ]
            # The rotated file appears one os.replace before the fresh journal is
            # appended, so wait for both rather than racing that window.
            if rotated and metrics_file.exists():
                break
            time.sleep(0.02)
        assert len(rotated) == 1
        assert rotated[0].read_text(encoding="utf-8") == '{"recorded_at":"old"}\n'
        assert metrics_file.read_text(encoding="utf-8").count("recorded_at") == 1
    finally:
        srv.shutdown()


def test_health_responds_while_scoring_in_flight():
    started, release = threading.Event(), threading.Event()

    def slow(_body, tiles=1):
        started.set()
        release.wait(5)
        return {"person": 0.0}

    srv = scorer_service.make_server(slow, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        post = threading.Thread(
            target=lambda: urllib.request.urlopen(
                urllib.request.Request(f"{url}/score", data=b"x"), timeout=10).read(),
            daemon=True)
        post.start()
        assert started.wait(5), "score request never reached score_fn"
        with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
            assert json.load(resp) == {"ok": True}
    finally:
        release.set()
        post.join(5)
        srv.shutdown()


def test_concurrent_score_requests_serialize_inference():
    gauge_lock = threading.Lock()
    gauge = {"in_flight": 0, "max": 0}

    def tracking(_body, tiles=1):
        with gauge_lock:
            gauge["in_flight"] += 1
            gauge["max"] = max(gauge["max"], gauge["in_flight"])
        time.sleep(0.2)
        with gauge_lock:
            gauge["in_flight"] -= 1
        return {"person": 0.0}

    srv = scorer_service.make_server(tracking, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/score"
    try:
        posts = [threading.Thread(
            target=lambda: urllib.request.urlopen(
                urllib.request.Request(url, data=b"x"), timeout=10).read(),
            daemon=True) for _ in range(2)]
        for p in posts:
            p.start()
        for p in posts:
            p.join(10)
        assert gauge["max"] == 1, "two inferences ran concurrently"
    finally:
        srv.shutdown()


def test_client_gone_before_reply_is_not_a_scoring_failure(caplog):
    started, client_gone = threading.Event(), threading.Event()

    def slow(_body, tiles=1):
        started.set()
        client_gone.wait(5)
        return {"person": 0.1}

    srv = scorer_service.make_server(slow, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        with caplog.at_level(logging.WARNING, logger="tapo_monitor.scorer_service"):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"POST /score HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: 1\r\n\r\nx")
            assert started.wait(5), "request never reached score_fn"
            # RST on close so the server's reply write fails immediately.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
            sock.close()
            client_gone.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
                    metrics = json.load(resp)
                if metrics["completed"] == 1:
                    break
                time.sleep(0.05)
        assert metrics["completed"] == 1
        assert metrics["failed"] == 0
        assert "scoring failed" not in caplog.text
        # The handler must survive to serve the next client.
        client_gone.set()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/score", data=b"y")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert json.load(resp) == {"person": 0.1}
    finally:
        client_gone.set()
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
    assert scorer_service.scores_from_output(out) == {"person": 0.0, "animal": 0.0, "classes": {}}

def test_scores_from_output_returns_named_classes():
    out = np.zeros((1, 4, 85), dtype=np.float32)
    out[0, 0, 4] = 0.9
    out[0, 0, 5 + 0] = 0.8       # person -> 0.72
    out[0, 1, 4] = 0.5
    out[0, 1, 5 + 16] = 0.6      # dog -> 0.30
    out[0, 2, 4] = 0.4
    out[0, 2, 5 + 2] = 0.02      # car -> 0.008, below floor

    scores = scorer_service.scores_from_output(out)

    assert scores["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["animal"] == pytest.approx(0.30, abs=1e-4)
    assert scores["classes"]["person"] == pytest.approx(0.72, abs=1e-4)
    assert scores["classes"]["dog"] == pytest.approx(0.30, abs=1e-4)
    assert "car" not in scores["classes"]

def test_best_person_box_decodes_grid_and_stride():
    # input 64 -> grids 8x8(s8)+4x4(s16)+2x2(s32) = 84 anchors. Anchor 0 = grid(0,0),
    # stride 8, raw box (0,0,0,0) -> cx=cy=0, w=h=exp(0)*8=8 -> xyxy (-4,-4,4,4).
    out = np.zeros((1, 84, 85), dtype=np.float32)
    out[0, 0, 4] = 0.9
    out[0, 0, 5 + 0] = 0.8                  # person conf 0.72 on anchor 0
    box = scorer_service.best_person_box(out, input_size=64)
    assert box == pytest.approx((-4.0, -4.0, 4.0, 4.0))


def test_best_person_box_none_below_floor():
    out = np.zeros((1, 84, 85), dtype=np.float32)
    out[0, 0, 4] = 0.1
    out[0, 0, 5 + 0] = 0.1                  # 0.01, below default floor 0.05
    assert scorer_service.best_person_box(out, input_size=64) is None


def test_best_person_box_falls_back_when_anchor_count_mismatch():
    # 3 anchors != any grid count -> treat head as already-decoded cx,cy,w,h
    out = np.zeros((1, 3, 85), dtype=np.float32)
    out[0, 1, :4] = [100, 80, 40, 60]
    out[0, 1, 4] = 0.9
    out[0, 1, 5 + 0] = 0.8
    assert scorer_service.best_person_box(out, input_size=640) == \
        pytest.approx((80.0, 50.0, 120.0, 110.0))


def test_tile_rects_whole_frame_only_when_tiles_1():
    assert scorer_service.tile_rects(400, 300, 1) == [(0.0, 0.0, 400.0, 300.0)]


def test_tile_rects_grid_count_and_bounds():
    rects = scorer_service.tile_rects(400, 300, 2, overlap=0.0)
    assert len(rects) == 5                   # whole frame + 2x2
    assert rects[0] == (0.0, 0.0, 400.0, 300.0)
    assert rects[1] == (0.0, 0.0, 200.0, 150.0)          # top-left cell
    assert rects[4] == (200.0, 150.0, 400.0, 300.0)      # bottom-right cell
    for x0, y0, x1, y1 in rects:             # all within the frame
        assert 0.0 <= x0 < x1 <= 400.0 and 0.0 <= y0 < y1 <= 300.0


def _rect(person, animal=0.0, box=None, classes=None):
    return {"person": person, "animal": animal,
            "classes": classes or ({"person": person} if person else {}), "box": box}


def test_combine_full_frame_score_decides_tile_only_adds_box():
    # Tile hallucinations must not raise the decision score: person/animal come
    # from the full frame; the best tile only contributes box + tile_person.
    combined = scorer_service.combine_rect_scores(
        [_rect(0.05), _rect(0.55, box=[10, 20, 30, 60]), _rect(0.40, box=[1, 2, 3, 4])])
    assert combined["person"] == 0.05
    assert combined["tile_person"] == 0.55
    assert combined["box"] == [10, 20, 30, 60]     # from the best-person tile


def test_combine_prefers_full_frame_box_when_present():
    combined = scorer_service.combine_rect_scores(
        [_rect(0.9, box=[100, 100, 200, 300]), _rect(0.95, box=[5, 5, 9, 9])])
    assert combined["person"] == 0.9
    assert combined["box"] == [100, 100, 200, 300]


def test_combine_animal_also_full_frame_only():
    combined = scorer_service.combine_rect_scores(
        [_rect(0.0, animal=0.1), _rect(0.0, animal=0.8)])
    assert combined["animal"] == 0.1


def test_combine_single_rect_has_no_tile_person():
    combined = scorer_service.combine_rect_scores([_rect(0.7, box=[1, 2, 3, 4])])
    assert combined["person"] == 0.7
    assert combined["box"] == [1, 2, 3, 4]
    assert "tile_person" not in combined


def test_scale_box_undoes_ratio_and_adds_tile_offset():
    # tile-input box at ratio 0.5 -> /0.5 = *2, then shift by the tile origin
    assert scorer_service.scale_box((10, 20, 30, 40), 0.5, 100, 200) == \
        pytest.approx((120.0, 240.0, 160.0, 280.0))


def test_scores_from_output_uses_darknet_coco_names():
    out = np.zeros((1, 1, 85), dtype=np.float32)
    out[0, 0, 4] = 0.5
    out[0, 0, 5 + 3] = 0.8       # darknet coco.names class 3: motorbike

    scores = scorer_service.scores_from_output(out)

    assert scores["classes"]["motorbike"] == pytest.approx(0.4, abs=1e-4)
    assert "motorcycle" not in scores["classes"]


def test_metrics_journal_rotates_while_it_is_still_being_appended(tmp_path):
    # The production shape the mtime-based check missed: the journal is written every
    # persist interval, so its mtime is always fresh while its oldest record ages out.
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    old_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 8 * 24 * 60 * 60))
    metrics_file.write_text(json.dumps({"recorded_at": old_stamp, "requests": 1}) + "\n",
                            encoding="utf-8")
    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7)

    metrics.flush()

    rotated = [path for path in tmp_path.glob("scorer-metrics.jsonl.*")
               if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()]
    assert len(rotated) == 1
    assert old_stamp in rotated[0].read_text(encoding="utf-8")
    assert old_stamp not in metrics_file.read_text(encoding="utf-8")


def test_metrics_journal_keeps_a_young_journal_with_an_old_mtime(tmp_path):
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    fresh_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metrics_file.write_text(json.dumps({"recorded_at": fresh_stamp}) + "\n",
                            encoding="utf-8")
    old = time.time() - (8 * 24 * 60 * 60)
    os.utime(metrics_file, (old, old))
    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7)

    metrics.flush()

    assert not [path for path in tmp_path.glob("scorer-metrics.jsonl.*")
                if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()]


def test_metrics_journal_rotates_on_size_before_age(tmp_path):
    # Age rotation alone lets a burst outgrow the disk between two age checks: the
    # journal is appended every persist interval, so seven quiet days cost ~14 MB but
    # seven noisy ones cost whatever the burst wrote. A journal past the size cap must
    # rotate even though its oldest record is still young.
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    fresh_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metrics_file.write_text(
        json.dumps({"recorded_at": fresh_stamp, "pad": "x" * 512}) + "\n",
        encoding="utf-8")
    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7,
                                          max_journal_bytes=256)

    metrics.flush()

    rotated = [path for path in tmp_path.glob("scorer-metrics.jsonl.*")
               if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()]
    assert len(rotated) == 1
    assert fresh_stamp in rotated[0].read_text(encoding="utf-8")
    assert "pad" not in metrics_file.read_text(encoding="utf-8")


def test_metrics_journal_keeps_a_young_small_journal_under_the_default_cap(tmp_path):
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    fresh_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metrics_file.write_text(json.dumps({"recorded_at": fresh_stamp}) + "\n",
                            encoding="utf-8")
    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7)

    metrics.flush()

    assert not [path for path in tmp_path.glob("scorer-metrics.jsonl.*")
                if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()]


def test_metrics_size_rotation_never_loses_counters_across_restart(tmp_path):
    # Rotation renames the journal, never the state file, so a size-triggered rotation
    # must leave the counters a restarted service loads exactly where they were.
    metrics_file = tmp_path / "scorer-metrics.jsonl"
    metrics = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7,
                                          max_journal_bytes=64)
    metrics.begin(1)
    metrics.finish(0.1, 0.1, True, result={"person": 0.9, "animal": 0.0})
    metrics.flush()
    metrics.flush()          # second persist finds the journal over the cap and rotates

    assert [path for path in tmp_path.glob("scorer-metrics.jsonl.*")
            if path.name[len("scorer-metrics.jsonl."):][:8].isdigit()]
    reborn = scorer_service.ScorerMetrics(metrics_file=str(metrics_file),
                                          persist_seconds=0, retention_days=7,
                                          max_journal_bytes=64)
    assert reborn.requests == 1
    assert reborn.completed == 1
    assert reborn.person_candidates == 1
    assert reborn.restart_count == metrics.restart_count + 1


def test_metrics_settings_ignore_unset_blank_and_invalid_environment():
    defaults = scorer_service.metrics_settings(env={})
    assert defaults == {"metrics_file": None, "metrics_persist_seconds": 60.0,
                        "metrics_retention_days": 7.0, "metrics_retention_files": 8,
                        "metrics_max_journal_bytes": 32 * 1024 * 1024}
    blank = scorer_service.metrics_settings(env={
        "TAPO_SCORER_METRICS_FILE": "  ",
        "TAPO_SCORER_METRICS_PERSIST_SECONDS": "",
        "TAPO_SCORER_METRICS_RETENTION_DAYS": "",
        "TAPO_SCORER_METRICS_RETENTION_FILES": "",
        "TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES": "",
    })
    assert blank == defaults
    mixed = scorer_service.metrics_settings(env={
        "TAPO_SCORER_METRICS_PERSIST_SECONDS": "not-a-number",
        "TAPO_SCORER_METRICS_RETENTION_FILES": "12",
    })
    assert mixed["metrics_persist_seconds"] == 60.0     # invalid -> default
    assert mixed["metrics_retention_files"] == 12       # valid -> honoured


def test_metrics_settings_read_the_environment():
    settings = scorer_service.metrics_settings(env={
        "TAPO_SCORER_METRICS_FILE": "/var/lib/tapo/scorer.jsonl",
        "TAPO_SCORER_METRICS_PERSIST_SECONDS": "30",
        "TAPO_SCORER_METRICS_RETENTION_DAYS": "2",
        "TAPO_SCORER_METRICS_RETENTION_FILES": "3",
        "TAPO_SCORER_METRICS_MAX_JOURNAL_BYTES": "1048576",
    })
    assert settings == {"metrics_file": "/var/lib/tapo/scorer.jsonl",
                        "metrics_persist_seconds": 30.0,
                        "metrics_retention_days": 2.0,
                        "metrics_retention_files": 3,
                        "metrics_max_journal_bytes": 1048576}


def test_cli_survives_a_unit_file_whose_metrics_variables_are_unset():
    # systemd drops an unset ${VAR} word entirely, so the flags arrive without values.
    # That used to exit 2 under Restart=always, i.e. an invisible crash loop.
    parser = scorer_service.build_parser(env={})
    args = parser.parse_args([
        "--model", "/models/yolox_m.onnx", "--port", "8766", "--input-size", "640",
        "--metrics-file", "--metrics-persist-seconds", "--metrics-retention-days",
        "--metrics-retention-files", "--metrics-max-journal-bytes",
    ])
    assert args.metrics_file is None
    assert args.metrics_persist_seconds == 60.0
    assert args.metrics_retention_days == 7.0
    assert args.metrics_retention_files == 8
    assert args.metrics_max_journal_bytes == 32 * 1024 * 1024


def test_cli_survives_quoted_empty_metrics_variables():
    parser = scorer_service.build_parser(env={})
    args = parser.parse_args([
        "--model", "/models/yolox_m.onnx",
        "--metrics-file", "", "--metrics-persist-seconds", "",
        "--metrics-retention-days", "", "--metrics-retention-files", "",
    ])
    assert args.metrics_file is None
    assert args.metrics_persist_seconds == 60.0
    assert args.metrics_retention_files == 8
