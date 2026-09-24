"""Tests for the local frame-labeling tool (tapo_monitor.labeling)."""

import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import cli, labeling

# A few bytes are enough: the tool hashes and serves images, it never decodes them.
JPEG = b"\xff\xd8\xff\xe0" + b"frame" + b"\xff\xd9"


def _jpeg(tag):
    return JPEG + tag.encode()


def _write_log(folder, records):
    """One sent/review-log folder: a JPEG per record plus its index.jsonl."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.jsonl"), "w", encoding="utf-8") as idx:
        for record in records:
            with open(os.path.join(folder, record["file"]), "wb") as f:
                f.write(_jpeg(record["file"]))
            idx.write(json.dumps(record) + "\n")


def _sent(name, person=None, camera="front", ts=1000.0):
    record = {"ts": ts, "file": name, "caption": "Person", "delivered": True, "camera": camera}
    if person is not None:
        record["person"] = person
        record["animal"] = 0.0
    return record


def _review(name, person, camera="yard", ts=2000.0):
    return {"ts": ts, "file": name, "camera": camera, "verdict": "hold", "etype": "motion",
            "person": person, "animal": 0.1}


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "dataset"
    _write_log(str(root / "host-a" / "sent-log"), [
        _sent("s_gray.jpg", 0.50),
        _sent("s_high.jpg", 0.90),
        _sent("s_none.jpg", None),
    ])
    _write_log(str(root / "host-b" / "review-log"), [
        _review("r_low1.jpg", 0.10),
        _review("r_low2.jpg", 0.20),
        _review("r_gray.jpg", 0.40),
    ])
    return str(root)


def _labels(dataset_dir):
    path = os.path.join(dataset_dir, "labels.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── loading ──────────────────────────────────────────────────────────────────

def test_load_frames_finds_indexes_recursively_and_classifies_source(dataset):
    frames = {f.path: f for f in labeling.load_frames(dataset)}
    assert set(frames) == {
        "host-a/sent-log/s_gray.jpg", "host-a/sent-log/s_high.jpg",
        "host-a/sent-log/s_none.jpg", "host-b/review-log/r_low1.jpg",
        "host-b/review-log/r_low2.jpg", "host-b/review-log/r_gray.jpg",
    }
    assert frames["host-a/sent-log/s_gray.jpg"].source == "sent"
    assert frames["host-b/review-log/r_gray.jpg"].source == "review"
    assert frames["host-a/sent-log/s_none.jpg"].score is None
    assert frames["host-b/review-log/r_low1.jpg"].camera == "yard"
    expected = hashlib.sha256(_jpeg("s_gray.jpg")).hexdigest()
    assert frames["host-a/sent-log/s_gray.jpg"].sha256 == expected


def test_load_frames_skips_missing_files_garbage_lines_and_escapes(dataset):
    index = os.path.join(dataset, "host-a", "sent-log", "index.jsonl")
    with open(index, "a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"ts": 1.0, "file": "gone.jpg"}) + "\n")
        f.write(json.dumps({"ts": 1.0, "file": "../../../etc/passwd"}) + "\n")
        f.write(json.dumps({"ts": 1.0}) + "\n")
    paths = {f.path for f in labeling.load_frames(dataset)}
    assert len(paths) == 6


def test_band_edges():
    assert labeling.band(None) == "none"
    assert labeling.band(0.29) == "low"
    assert labeling.band(0.30) == "gray"
    assert labeling.band(0.65) == "gray"
    assert labeling.band(0.66) == "high"


# ── queue ────────────────────────────────────────────────────────────────────

def test_queue_orders_gray_then_low_then_high_then_unscored(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    order = [f.path.rsplit("/", 1)[1] for f in session.pending()]
    assert set(order[:2]) == {"s_gray.jpg", "r_gray.jpg"}
    assert set(order[2:4]) == {"r_low1.jpg", "r_low2.jpg"}
    assert order[4:] == ["s_high.jpg", "s_none.jpg"]


def test_low_sample_caps_the_low_band(dataset):
    session = labeling.LabelSession(dataset, seed=1, low_sample=1)
    bands = [labeling.band(f.score) for f in session.pending()]
    assert bands.count("low") == 1


def test_queue_skips_frames_already_labeled(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    first = session.next_frame()
    session.label(first.path, "person", now=5000.0)
    again = labeling.LabelSession(dataset, seed=1)
    assert first.path not in {f.path for f in again.pending()}
    assert len(again.pending()) == 5


# ── labeling, undo, skip ─────────────────────────────────────────────────────

def test_label_appends_a_full_record_and_never_touches_images(dataset):
    before = {p: open(os.path.join(dataset, p), "rb").read()
              for p in (f.path for f in labeling.load_frames(dataset))}
    session = labeling.LabelSession(dataset, seed=1)
    session.label("host-a/sent-log/s_gray.jpg", "no_person", now=5000.0)
    (line,) = _labels(dataset)
    assert line == {
        "path": "host-a/sent-log/s_gray.jpg",
        "sha256": hashlib.sha256(_jpeg("s_gray.jpg")).hexdigest(),
        "label": "no_person", "labeled_at": 5000.0, "score": 0.5,
        "camera": "front", "source": "sent",
    }
    after = {p: open(os.path.join(dataset, p), "rb").read() for p in before}
    assert after == before


def test_label_rejects_unknown_label_and_unknown_frame(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    with pytest.raises(ValueError):
        session.label("host-a/sent-log/s_gray.jpg", "cat")
    with pytest.raises(KeyError):
        session.label("host-a/sent-log/nope.jpg", "person")
    assert _labels(dataset) == []


def test_undo_appends_a_line_restoring_the_previous_state(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    path = "host-a/sent-log/s_gray.jpg"
    session.label(path, "person", now=1.0)
    session.label(path, "no_person", now=2.0)
    session.undo(now=3.0)
    lines = _labels(dataset)
    assert [line["label"] for line in lines] == ["person", "no_person", "person"]
    assert lines[-1]["undo"] is True
    session.undo(now=4.0)
    assert _labels(dataset)[-1]["label"] == labeling.UNLABELED
    assert labeling.latest_labels(dataset) == {}
    assert path in {f.path for f in session.pending()}
    assert session.next_frame().path == path


def test_undo_with_nothing_to_undo_is_a_no_op(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    assert session.undo() is None
    assert _labels(dataset) == []


def test_skip_moves_the_frame_to_the_back_and_writes_nothing(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    first = session.next_frame()
    session.skip(first.path)
    assert session.next_frame().path != first.path
    assert session.pending()[-1].path == first.path
    assert _labels(dataset) == []
    session.undo()
    assert session.next_frame().path == first.path


def test_latest_line_per_sha_wins(dataset):
    path = os.path.join(dataset, "labels.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"sha256": "a", "label": "person"}) + "\n")
        f.write("garbage\n")
        f.write(json.dumps({"sha256": "a", "label": "no_person"}) + "\n")
        f.write(json.dumps({"sha256": "b", "label": "unsure"}) + "\n")
    latest = labeling.latest_labels(dataset)
    assert {k: v["label"] for k, v in latest.items()} == {"a": "no_person", "b": "unsure"}


# ── stats ────────────────────────────────────────────────────────────────────

def _label_lines(dataset_dir, rows):
    with open(os.path.join(dataset_dir, "labels.jsonl"), "w", encoding="utf-8") as f:
        for i, (label, score, camera, source) in enumerate(rows):
            f.write(json.dumps({"path": f"x/{i}.jpg", "sha256": str(i), "label": label,
                                "labeled_at": float(i), "score": score,
                                "camera": camera, "source": source}) + "\n")


def test_stats_rates_bands_cameras_and_threshold(tmp_path):
    root = str(tmp_path)
    _label_lines(root, [
        ("person", 0.90, "front", "sent"),
        ("no_person", 0.70, "front", "sent"),
        ("person", 0.50, "front", "sent"),
        ("no_person", 0.35, "yard", "review"),
        ("person", 0.20, "yard", "review"),
        ("no_person", 0.10, "yard", "review"),
        ("unsure", 0.40, "yard", "review"),
        ("no_person", None, "yard", "sent"),
    ])
    stats = labeling.compute_stats(root)
    assert stats["counts"] == {"person": 3, "no_person": 4, "unsure": 1, "total": 8}
    fa = stats["false_alarms"]
    assert (fa["no_person"], fa["decided"]) == (2, 4)
    assert fa["rate"] == pytest.approx(0.5)
    miss = stats["misses"]
    assert (miss["person"], miss["decided"]) == (1, 3)
    assert stats["bands"]["high"]["false_alarms"]["no_person"] == 1
    assert stats["bands"]["low"]["misses"]["person"] == 1
    assert stats["bands"]["none"]["counts"]["no_person"] == 1
    assert stats["cameras"]["front"]["counts"]["person"] == 2
    assert stats["cameras"]["yard"]["misses"]["decided"] == 3
    best = stats["threshold"]
    assert best["n"] == 6
    assert best["errors"] == 2
    assert best["threshold"] is not None
    predicted = [s >= best["threshold"] for s in (0.90, 0.70, 0.50, 0.35, 0.20, 0.10)]
    truth = [True, False, True, False, True, False]
    assert sum(p != t for p, t in zip(predicted, truth, strict=True)) == 2


def test_stats_on_empty_dataset(tmp_path):
    stats = labeling.compute_stats(str(tmp_path))
    assert stats["counts"]["total"] == 0
    assert stats["false_alarms"]["rate"] is None
    assert stats["threshold"]["threshold"] is None


def test_stats_ignore_undone_labels(tmp_path):
    root = str(tmp_path)
    with open(os.path.join(root, "labels.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"sha256": "a", "label": "person", "score": 0.5,
                            "source": "sent", "camera": "front"}) + "\n")
        f.write(json.dumps({"sha256": "a", "label": labeling.UNLABELED, "undo": True}) + "\n")
    assert labeling.compute_stats(root)["counts"]["total"] == 0


def test_label_stats_cli_json_and_text(dataset, capsys):
    session = labeling.LabelSession(dataset, seed=1)
    session.label("host-a/sent-log/s_high.jpg", "no_person", now=1.0)
    session.label("host-b/review-log/r_low1.jpg", "person", now=2.0)
    assert cli.main(["label-stats", dataset, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["false_alarms"]["no_person"] == 1
    assert stats["misses"]["person"] == 1
    assert cli.main(["label-stats", dataset]) == 0
    out = capsys.readouterr().out
    assert "false alarms" in out and "misses" in out and "threshold" in out


def test_label_stats_cli_missing_dir_fails(tmp_path, capsys):
    assert cli.main(["label-stats", str(tmp_path / "nope")]) == 2


def test_label_cli_defaults_bind_to_loopback(monkeypatch, dataset):
    seen = {}

    def fake_serve(session, port, bind):
        seen.update(port=port, bind=bind, frames=len(session.pending()))
        return 0

    monkeypatch.setattr(labeling, "serve", fake_serve)
    assert cli.main(["label", dataset]) == 0
    assert seen == {"port": 8791, "bind": "127.0.0.1", "frames": 6}
    assert cli.main(["label", dataset, "--port", "9000", "--bind", "192.0.2.10"]) == 0
    assert seen["port"] == 9000 and seen["bind"] == "192.0.2.10"


# ── HTTP handler ─────────────────────────────────────────────────────────────

def _serve(session):
    srv = labeling.make_server(session, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers, resp.read()


def _post(url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


@pytest.fixture
def served(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    srv, url = _serve(session)
    yield session, url
    srv.shutdown()
    srv.server_close()


def test_index_page_has_the_controls(served):
    _, url = served
    status, headers, body = _get(url + "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    page = body.decode()
    assert "viewport" in page
    for key in ("person", "no_person", "unsure", "skip", "undo"):
        assert key in page


def test_next_label_undo_round_trip(served, dataset):
    session, url = served
    _, _, body = _get(url + "/api/next")
    state = json.loads(body)
    frame = state["frame"]
    assert frame["band"] == "gray"
    assert state["remaining"] == 6
    state = _post(url + "/api/label", {"path": frame["path"], "label": "person"})
    assert state["remaining"] == 5
    assert state["frame"]["path"] != frame["path"]
    assert _labels(dataset)[-1]["label"] == "person"
    state = _post(url + "/api/undo")
    assert state["frame"]["path"] == frame["path"]
    assert state["remaining"] == 6
    skipped = _post(url + "/api/skip", {"path": frame["path"]})
    assert skipped["frame"]["path"] != frame["path"]


def test_bad_label_is_400(served):
    _, url = served
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(url + "/api/label", {"path": "host-a/sent-log/s_gray.jpg", "label": "cat"})
    assert err.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(url + "/api/label", {"path": "nope.jpg", "label": "person"})
    assert err.value.code == 400


def test_image_is_served_as_jpeg(served):
    _, url = served
    status, headers, body = _get(url + "/image/host-a/sent-log/s_gray.jpg")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert body == _jpeg("s_gray.jpg")


@pytest.mark.parametrize("path", [
    "/image/../../../etc/passwd",
    "/image/%2e%2e/%2e%2e/etc/passwd",
    "/image/..%2F..%2Fetc%2Fpasswd",
    "/image//etc/passwd",
    "/image/host-a/sent-log/index.jsonl",
    "/image/labels.jsonl",
])
def test_image_path_traversal_is_refused(served, path):
    _, url = served
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(url + path)
    assert err.value.code == 404


def test_image_symlink_escaping_the_dataset_is_refused(dataset, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG)
    os.symlink(str(outside), os.path.join(dataset, "host-a", "sent-log", "leak.jpg"))
    with open(os.path.join(dataset, "host-a", "sent-log", "index.jsonl"), "a") as f:
        f.write(json.dumps(_sent("leak.jpg", 0.5)) + "\n")
    session = labeling.LabelSession(dataset, seed=1)
    assert "host-a/sent-log/leak.jpg" not in {f.path for f in session.pending()}
    srv, url = _serve(session)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(url + "/image/host-a/sent-log/leak.jpg")
        assert err.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_stats_page_and_json(served):
    session, url = served
    session.label("host-a/sent-log/s_high.jpg", "no_person", now=1.0)
    status, headers, body = _get(url + "/stats")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "False alarms" in body.decode()
    _, _, body = _get(url + "/api/stats")
    assert json.loads(body)["false_alarms"]["no_person"] == 1


def test_unknown_path_is_404(served):
    _, url = served
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(url + "/metrics")
    assert err.value.code == 404
