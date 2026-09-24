"""Tests for the local frame-labeling tool (tapo_monitor.labeling)."""

import hashlib
import json
import os
import sys
import threading
import time
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


# ── stats: backwards compatibility, hold vs drop, day/night ──────────────────

STATS_FIXTURE = [
    ("person", 0.90, "front", "sent"),
    ("no_person", 0.70, "front", "sent"),
    ("person", 0.50, "front", "sent"),
    ("no_person", 0.35, "yard", "review"),
    ("person", 0.20, "yard", "review"),
    ("no_person", 0.10, "yard", "review"),
    ("unsure", 0.40, "yard", "review"),
    ("no_person", None, "yard", "sent"),
]

# label-stats output for STATS_FIXTURE before day/night and verdicts existed.
STATS_FIXTURE_TEXT = """\
group         labeled  person  no_person  unsure  false alarms (sent)  misses (review)
------------  -------  ------  ---------  ------  -------------------  ---------------
all           8        3       4          1       2/4 (50.0%)          1/3 (33.3%)
band gray     3        1       1          1       0/1 (0.0%)           0/1 (0.0%)
band low      2        1       1          0       0/0 (n/a)            1/2 (50.0%)
band high     2        1       1          0       1/2 (50.0%)          0/0 (n/a)
band none     1        0       1          0       1/1 (100.0%)         0/0 (n/a)
camera front  3        2       1          0       1/3 (33.3%)          0/0 (n/a)
camera yard   5        1       3          1       1/1 (100.0%)         1/3 (33.3%)

labels: 8 by a person, 0 automatic (both models agreed)
best threshold: person >= 0.20 -> 2 errors of 6 (2 false alarms, 0 misses, accuracy 66.7%)
false alarms = sent frames labeled no_person; misses = review (held) frames labeled \
person; unsure excluded
"""


def _old_block(total, person, no_person, unsure, fa, fa_decided, miss, miss_decided):
    def rate(part, whole):
        return part / whole if whole else None

    return {"counts": {"person": person, "no_person": no_person, "unsure": unsure,
                       "total": total},
            "false_alarms": {"no_person": fa, "decided": fa_decided,
                             "rate": rate(fa, fa_decided)},
            "misses": {"person": miss, "decided": miss_decided,
                       "rate": rate(miss, miss_decided)}}


def _without_new_keys(stats):
    """The stats as the JSON looked before verdicts, threshold support and incidents."""
    def strip(block):
        return {k: v for k, v in block.items() if k not in ("verdicts", "incidents")}

    old = strip(stats)
    old["bands"] = {k: strip(v) for k, v in stats["bands"].items()}
    old["cameras"] = {k: strip(v) for k, v in stats["cameras"].items()}
    old["threshold"] = {k: v for k, v in stats["threshold"].items()
                        if k not in ("person", "no_person", "supported")}
    return old


def test_stats_without_config_keep_the_previous_text_and_json(tmp_path, capsys):
    root = str(tmp_path)
    _label_lines(root, STATS_FIXTURE)
    assert cli.main(["label-stats", root]) == 0
    out = capsys.readouterr().out
    # The incident section is appended; everything before it is unchanged.
    assert out.startswith(STATS_FIXTURE_TEXT + "\nincidents: 0 from 0 frames")
    assert cli.main(["label-stats", root, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert "day_night" not in stats
    expected = _old_block(8, 3, 4, 1, 2, 4, 1, 3)
    expected["bands"] = {"gray": _old_block(3, 1, 1, 1, 0, 1, 0, 1),
                         "low": _old_block(2, 1, 1, 0, 0, 0, 1, 2),
                         "high": _old_block(2, 1, 1, 0, 1, 2, 0, 0),
                         "none": _old_block(1, 0, 1, 0, 1, 1, 0, 0)}
    expected["cameras"] = {"front": _old_block(3, 2, 1, 0, 1, 3, 0, 0),
                           "yard": _old_block(5, 1, 3, 1, 1, 1, 1, 3)}
    expected["by"] = {"auto": 0, "human": 8}
    expected["threshold"] = {"threshold": 0.2, "errors": 2, "false_alarms": 2, "misses": 0,
                             "accuracy": pytest.approx(2 / 3), "n": 6}
    assert _without_new_keys(stats) == expected


def test_load_frames_exposes_verdict_and_sample_rate(tmp_path):
    root = tmp_path / "dataset"
    _write_log(str(root / "h" / "review-log"), [
        _review("held.jpg", 0.4),
        {**_review("hub.jpg", 0.2), "verdict": "drop"},
        {**_review("sampled.jpg", 0.1), "verdict": "drop", "sample_rate": 0.1},
        {**_review("bad_rate.jpg", 0.1), "verdict": "drop", "sample_rate": 7},
    ])
    _write_log(str(root / "h" / "sent-log"), [_sent("sent.jpg", 0.9)])
    frames = {f.path.rsplit("/", 1)[1]: f for f in labeling.load_frames(str(root))}
    assert (frames["held.jpg"].verdict, frames["held.jpg"].sample_rate) == ("hold", None)
    assert (frames["hub.jpg"].verdict, frames["hub.jpg"].sample_rate) == ("drop", None)
    assert frames["sampled.jpg"].sample_rate == 0.1
    assert frames["bad_rate.jpg"].sample_rate is None
    assert (frames["sent.jpg"].source, frames["sent.jpg"].verdict) == ("sent", None)
    assert all(f.source == "review" for n, f in frames.items() if n != "sent.jpg")


def test_stats_split_misses_by_hold_and_drop(tmp_path):
    root = tmp_path / "dataset"
    _write_log(str(root / "h" / "review-log"), [
        _review("h1.jpg", 0.5), _review("h2.jpg", 0.45), _review("h3.jpg", 0.4),
        {**_review("hub.jpg", 0.2), "verdict": "drop"},
        {**_review("s1.jpg", 0.1), "verdict": "drop", "sample_rate": 0.1},
        {**_review("s2.jpg", 0.05), "verdict": "drop", "sample_rate": 0.1},
    ])
    session = labeling.LabelSession(str(root), seed=1)
    for name, label in [("h1", "person"), ("h2", "person"), ("h3", "no_person"),
                        ("hub", "no_person"), ("s1", "person"), ("s2", "no_person")]:
        session.label(f"h/review-log/{name}.jpg", label, now=1.0)
    stats = labeling.compute_stats(str(root))
    assert (stats["misses"]["person"], stats["misses"]["decided"]) == (3, 6)
    hold, drop = stats["verdicts"]["hold"], stats["verdicts"]["drop"]
    assert (hold["person"], hold["decided"], hold["sampled"]) == (2, 3, 0)
    assert hold["estimated_person"] is None
    assert (drop["person"], drop["decided"], drop["sampled"]) == (1, 3, 2)
    # One sampled person at 10 % stands for ten; the hub drop was kept whole.
    assert drop["estimated_person"] == pytest.approx(10.0)
    assert drop["estimated_decided"] == pytest.approx(21.0)
    assert stats["cameras"]["yard"]["verdicts"]["drop"]["person"] == 1
    text = labeling.format_stats(stats)
    assert "misses by review verdict" in text
    assert "hold: 2/3 (66.7%)" in text
    assert "drop: 1/3 (33.3%), 2 of them sampled -> estimated 10 person of 21" in text
    assert text.index("hold: ") < text.index("drop: ")


def test_stats_hide_the_verdict_split_while_only_holds_were_labeled(dataset):
    session = labeling.LabelSession(dataset, seed=1)
    session.label("host-b/review-log/r_low1.jpg", "person", now=1.0)
    stats = labeling.compute_stats(dataset)
    assert stats["verdicts"]["hold"]["person"] == 1
    assert "misses by review verdict" not in labeling.format_stats(stats)


def test_best_threshold_minimum_support():
    def pairs(persons, others):
        return [(0.9, True)] * persons + [(0.1, False)] * others

    assert labeling.MIN_SUPPORT_DECIDED == 20 and labeling.MIN_SUPPORT_PER_CLASS == 5
    assert labeling.best_threshold(pairs(5, 15))["supported"] is True
    assert labeling.best_threshold(pairs(5, 14))["supported"] is False    # 19 decided
    assert labeling.best_threshold(pairs(4, 30))["supported"] is False    # 4 persons
    assert labeling.best_threshold(pairs(30, 4))["supported"] is False    # 4 no_person
    best = labeling.best_threshold(pairs(8, 0))
    assert (best["person"], best["no_person"], best["supported"]) == (8, 0, False)
    assert labeling.best_threshold([])["supported"] is False


# Site night from ts 5000 on; "porch" is always night whatever the sun does.
DAY_TS, NIGHT_TS = 1000.0, 6000.0


def _site_night(ts):
    return ts >= 5000


def _night_app():
    from tapo_monitor import config
    return config.load_config_from_dict({"cameras": [
        {"name": "front", "host": "192.0.2.10"},
        {"name": "porch", "host": "192.0.2.11", "schedule": "always_night"},
    ]})


@pytest.fixture
def day_night_dataset(tmp_path):
    root = tmp_path / "dataset"
    sent = [_sent("front_day.jpg", 0.9, "front", DAY_TS),
            _sent("front_night.jpg", 0.8, "front", NIGHT_TS),
            _sent("porch_day.jpg", 0.7, "porch", DAY_TS),
            _sent("yard_night.jpg", 0.6, "yard", NIGHT_TS),
            _sent("yard_day.jpg", 0.5, "yard", DAY_TS)]
    untimed = _sent("untimed.jpg", 0.4, "front")
    del untimed["ts"]
    _write_log(str(root / "h" / "sent-log"), sent + [untimed])
    session = labeling.LabelSession(str(root), seed=1)
    for frame in session.pending():
        session.label(frame.path, "person", now=1.0)
    return str(root)


def test_night_classifier_applies_site_night_and_camera_schedule():
    night = labeling.night_classifier(_night_app(), is_night=_site_night)
    assert night("front", DAY_TS) is False and night("front", NIGHT_TS) is True
    assert night("porch", DAY_TS) is True                  # always_night
    assert night("unknown-cam", NIGHT_TS) is True          # not in the config: site night
    assert night(None, DAY_TS) is False


def test_stats_split_by_day_and_night(day_night_dataset):
    night = labeling.night_classifier(_night_app(), is_night=_site_night)
    stats = labeling.compute_stats(day_night_dataset, night=night)
    parts = stats["day_night"]
    assert parts["day"]["counts"]["total"] == 2        # front_day, yard_day
    assert parts["night"]["counts"]["total"] == 3      # front_night, porch (always), yard
    assert parts["unknown"]["counts"]["total"] == 1    # no timestamp
    assert parts["cameras"]["porch"]["night"]["counts"]["total"] == 1
    assert parts["cameras"]["porch"]["day"]["counts"]["total"] == 0
    assert parts["cameras"]["front"]["day"]["threshold"]["n"] == 1
    assert parts["night"]["threshold"]["n"] == 3
    assert parts["night"]["threshold"]["supported"] is False
    assert parts["min_support"] == {"decided": 20, "per_class": 5}
    text = labeling.format_stats(stats)
    assert "day/night of the labeled frames (2 day, 3 night, 1 unknown" in text
    assert "camera porch night" in text and "too few labels" in text
    assert "camera porch day" not in text              # no labels in that slice
    # The part before the day/night section is what label-stats printed without it.
    plain = labeling.format_stats(labeling.compute_stats(day_night_dataset))
    assert text.startswith(plain[:plain.index("\nincidents:")])


def test_day_night_threshold_per_slice(tmp_path):
    root = str(tmp_path)
    rows = []
    for i in range(10):          # day: persons score high, clean cut at 0.60
        rows.append({"label": "person", "score": 0.6 + i / 100, "ts": DAY_TS})
        rows.append({"label": "no_person", "score": 0.2 + i / 100, "ts": DAY_TS})
    for i in range(10):          # night: persons score lower, clean cut at 0.40
        rows.append({"label": "person", "score": 0.4 + i / 100, "ts": NIGHT_TS})
        rows.append({"label": "no_person", "score": 0.1 + i / 100, "ts": NIGHT_TS})
    with open(os.path.join(root, "labels.jsonl"), "w", encoding="utf-8") as f:
        for i, row in enumerate(rows):
            # Label lines whose frame is not indexed fall back to their own fields.
            f.write(json.dumps({"path": f"x/{i}.jpg", "sha256": str(i), "camera": "front",
                                "source": "sent", **row}) + "\n")
    night = labeling.night_classifier(_night_app(), is_night=_site_night)
    parts = labeling.compute_stats(root, night=night)["day_night"]
    day, dark = parts["day"]["threshold"], parts["night"]["threshold"]
    assert (day["threshold"], day["errors"], day["supported"]) == (0.6, 0, True)
    assert (dark["threshold"], dark["errors"], dark["supported"]) == (0.4, 0, True)
    assert (day["n"], day["person"], day["no_person"]) == (20, 10, 10)


def test_label_stats_cli_config(day_night_dataset, tmp_path, monkeypatch, capsys):
    from tapo_monitor import replay
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: porch\n    host: 192.0.2.11\n"
                           "    schedule: always_night\n", encoding="utf-8")
    monkeypatch.setattr(replay, "default_is_night", lambda app: _site_night)
    assert cli.main(["label-stats", day_night_dataset, "--config", str(config_path),
                     "--json"]) == 0
    parts = json.loads(capsys.readouterr().out)["day_night"]
    assert [parts[name]["counts"]["total"] for name in ("day", "night", "unknown")] \
        == [2, 3, 1]
    assert cli.main(["label-stats", day_night_dataset, "--config", str(config_path)]) == 0
    assert "day/night of the labeled frames" in capsys.readouterr().out
    missing = str(tmp_path / "nope.yaml")
    assert cli.main(["label-stats", day_night_dataset, "--config", missing]) == 1
    assert "config" in capsys.readouterr().err


def test_stats_page_shows_day_and_night(day_night_dataset):
    night = labeling.night_classifier(_night_app(), is_night=_site_night)
    session = labeling.LabelSession(day_night_dataset, seed=1, night=night)
    srv, url = _serve(session)
    try:
        _, _, body = _get(url + "/stats")
        assert "Day and night" in body.decode() and "too few labels" in body.decode()
        _, _, body = _get(url + "/api/stats")
        assert json.loads(body)["day_night"]["unknown"]["counts"]["total"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
    plain = labeling.LabelSession(day_night_dataset, seed=1)
    srv, url = _serve(plain)
    try:
        _, _, body = _get(url + "/stats")
        assert "Day and night" not in body.decode()
    finally:
        srv.shutdown()
        srv.server_close()


# ── incidents ────────────────────────────────────────────────────────────────

def _f(ts, *, source="sent", camera="front", host="h", incident=None, event_start=None,
       label=None, delivered=True, verdict="hold", caption_start=None):
    """One frame as labeling.incident_frames returns it."""
    return {"path": f"{host}/{source}-log/{ts}.jpg", "host": host, "camera": camera,
            "ts": ts, "incident": incident, "event_start": event_start,
            "caption_start": caption_start, "source": source,
            "verdict": verdict if source == "review" else None,
            "delivered": source == "sent" and delivered, "label": label}


def _groups(frames, gap=labeling.INCIDENT_GAP):
    return [[f["ts"] for f in inc["frames"]]
            for inc in labeling.group_incidents(frames, gap)]


def test_incidents_group_by_id_even_across_a_long_gap():
    frames = [_f(1000.0, incident="front-990"), _f(1500.0, incident="front-990"),
              _f(1100.0, incident="front-1090")]
    incidents = labeling.group_incidents(frames)
    assert sorted((i["id"], len(i["frames"])) for i in incidents) \
        == [("front-1090", 1), ("front-990", 2)]


def test_incidents_group_by_gap_per_host_and_camera():
    gap = labeling.INCIDENT_GAP
    frames = [_f(1000.0), _f(1000.0 + gap), _f(1000.0 + 2 * gap + 1),
              _f(1010.0, camera="yard"),                 # another camera: its own incident
              _f(1020.0, host="h2")]                     # same name on another host
    assert sorted(_groups(frames)) == [[1000.0, 1000.0 + gap], [1010.0], [1020.0],
                                       [1000.0 + 2 * gap + 1]]


def test_incidents_mixed_ids_and_gap_are_not_double_counted():
    frames = [_f(1000.0, source="review"),                      # old path: no ID yet
              _f(1030.0, incident="front-995"),                 # adopts the open incident
              _f(1060.0, source="review"),                      # no ID, within the gap
              _f(1100.0, incident="front-1090"),                # a new camera event
              _f(1120.0),                                       # joins the latest one
              _f(5000.0)]                                       # much later: new
    incidents = labeling.group_incidents(frames)
    assert [(i["id"], [f["ts"] for f in i["frames"]]) for i in incidents] == [
        ("front-995", [1000.0, 1030.0, 1060.0]),
        ("front-1090", [1100.0, 1120.0]),
        (None, [5000.0])]


def test_incident_status_alert_and_delay():
    inc = {"id": "front-990", "host": "h", "camera": "front", "frames": [
        _f(1000.0, source="review", label="no_person"),
        _f(1010.0, delivered=False, label="person"),     # failed send: not an alert
        _f(1030.0), _f(1060.0)]}
    s = labeling.summarize_incident(inc)
    assert (s["status"], s["alerted"], s["start"], s["start_source"]) \
        == ("person", True, 990.0, "incident_id")
    assert s["delay"] == 40.0                            # 990 -> first delivered, 1030
    assert s["verdicts"] == {"hold": {"frames": 1, "person": 0}}
    inc["frames"][0]["event_start"] = 985.0              # an explicit start wins
    assert labeling.summarize_incident(inc)["delay"] == 45.0
    unsent = {"id": None, "host": "h", "camera": "front",
              "frames": [_f(1010.0, delivered=False, label="person")]}
    s = labeling.summarize_incident(unsent)
    assert (s["alerted"], s["delay"], s["start_source"]) == (False, None, "first_frame")
    captioned = {"id": None, "host": "h", "camera": "front",
                 "frames": [_f(1000.0, caption_start=950.0), _f(1030.0)]}
    assert labeling.summarize_incident(captioned)["delay"] == 50.0


@pytest.mark.parametrize("labels,status", [
    ([None, None], "unlabeled"), (["no_person", None], "no_person"),
    (["no_person", "unsure"], "unsure"), (["unsure"], "unsure"),
    (["no_person", "person"], "person")])
def test_incident_status_from_labels(labels, status):
    inc = {"id": None, "host": "h", "camera": "front",
           "frames": [_f(1000.0 + i, label=label) for i, label in enumerate(labels)]}
    assert labeling.summarize_incident(inc)["status"] == status


def test_caption_start_reads_the_event_time_and_rejects_implausible_ones():
    start = time.mktime(time.strptime("2026-01-01 22:00:00", "%Y-%m-%d %H:%M:%S"))
    assert labeling.caption_start("Person 2026-01-01 22:00:00", start + 30) == start
    assert labeling.caption_start("Person 2026-01-01 22:00:00", start - 5) is None
    assert labeling.caption_start("Person 2026-01-01 22:00:00", start + 7200) is None
    assert labeling.caption_start("Person", start) is None


def _incident_dataset(root):
    """Five visits on "front" and one on "yard", labeled."""
    _write_log(str(root / "h" / "sent-log"), [
        # A: person, alerted 20 s after the event start in its ID.
        {**_sent("a1.jpg", 0.9, ts=1000.0), "incident": "front-980"},
        {**_sent("a2.jpg", 0.9, ts=1030.0), "incident": "front-980"},
        # C: an alert that failed to deliver, and a held person: missed.
        {**_sent("c1.jpg", 0.8, ts=3000.0), "delivered": False},
        # D: alerted, labeled no_person: a false alarm.
        {**_sent("d1.jpg", 0.7, ts=4000.0), "event_start": 3950.0},
        # yard at night: person, alerted 60 s after its event start.
        {**_sent("y1.jpg", 0.9, camera="yard", ts=NIGHT_TS), "event_start": NIGHT_TS - 60},
    ])
    _write_log(str(root / "h" / "review-log"), [
        _review("a0.jpg", 0.5, camera="front", ts=990.0),
        # B: held and dropped frames, nobody alerted: missed.
        _review("b1.jpg", 0.5, camera="front", ts=2000.0),
        {**_review("b2.jpg", 0.1, camera="front", ts=2030.0), "verdict": "drop",
         "sample_rate": 0.05},
        _review("c2.jpg", 0.5, camera="front", ts=3020.0),
        # E: never labeled.
        _review("e1.jpg", 0.4, camera="front", ts=8000.0),
    ])
    session = labeling.LabelSession(str(root), seed=1)
    for name, label in (("sent-log/a1.jpg", "person"), ("review-log/b2.jpg", "person"),
                        ("review-log/b1.jpg", "no_person"), ("review-log/c2.jpg", "person"),
                        ("sent-log/d1.jpg", "no_person"), ("sent-log/y1.jpg", "person")):
        session.label(f"h/{name}", label, now=1.0)
    return str(root)


def test_incident_stats_missed_false_alarms_and_delay(tmp_path):
    root = _incident_dataset(tmp_path / "dataset")
    section = labeling.compute_stats(root)["incidents"]
    assert section["frames"] == 10
    block = section["all"]
    assert block["total"] == 6 and block["labeled"] == 5
    assert block["counts"] == {"person": 4, "no_person": 1, "unsure": 0, "unlabeled": 1}
    assert block["person_alerted"] == 2
    assert block["missed"] == {"count": 2, "person": 4, "rate": 0.5}
    assert block["false_alarms"] == {"count": 1, "decided": 3, "rate": pytest.approx(1 / 3)}
    assert block["delay"] == {"n": 2, "from_event_start": 2, "median": 40.0, "p90": 60.0}
    # B had a held frame and a person in a dropped one; C a person in a held frame.
    assert block["missed_verdicts"] == {
        "hold": {"incidents": 2, "frames": 2, "person_incidents": 1},
        "drop": {"incidents": 1, "frames": 1, "person_incidents": 1}}
    assert [m["start"] for m in section["missed"]] == [2000.0, 3000.0]
    assert section["cameras"]["yard"]["missed"]["count"] == 0
    assert section["cameras"]["front"]["total"] == 5
    assert "day_night" not in section
    text = labeling.format_stats(labeling.compute_stats(root))
    assert "incidents: 6 from 10 frames" in text
    assert "2/4 (50.0%)" in text and "40 s" in text
    assert "hold 2 (person labeled in 1), drop 1 (person labeled in 1)" in text


def test_incident_stats_by_day_and_night(tmp_path):
    root = _incident_dataset(tmp_path / "dataset")
    night = labeling.night_classifier(_night_app(), is_night=_site_night)
    parts = labeling.compute_stats(root, night=night)["incidents"]["day_night"]
    assert [parts[name]["total"] for name in ("day", "night", "unknown")] == [4, 2, 0]
    assert parts["night"]["counts"]["person"] == 1          # yard; E is unlabeled
    assert parts["night"]["delay"]["median"] == 60.0
    assert parts["day"]["missed"]["count"] == 2


def test_incident_stats_without_labels_say_so(dataset, capsys):
    assert cli.main(["label-stats", dataset]) == 0
    assert "no incident has a labeled frame yet" in capsys.readouterr().out
    assert cli.main(["label-stats", dataset, "--json"]) == 0
    section = json.loads(capsys.readouterr().out)["incidents"]
    assert section["all"]["total"] == 2 and section["all"]["labeled"] == 0


def test_stats_page_shows_incidents(tmp_path):
    root = _incident_dataset(tmp_path / "dataset")
    body = labeling.stats_page(labeling.compute_stats(root))
    assert "<h2>Incidents</h2>" in body and "person alerted" in body
