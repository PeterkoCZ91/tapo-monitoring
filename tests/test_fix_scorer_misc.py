"""Review fixes: autolabel races, enrich frame selection, ONVIF person-vs-car, pool locking."""

import json
import os

from tapo_monitor import autolabel, enrich, labeling, scorer


def _dataset(tmp_path, names):
    log = tmp_path / "site" / "sent-log"
    log.mkdir(parents=True)
    for name in names:
        (log / name).write_bytes(b"jpeg " + name.encode())
        with open(log / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1.0, "file": name, "camera": "c",
                                 "person": 0.9, "animal": 0.0}) + "\n")
    return str(tmp_path)


def test_autolabel_never_shadows_a_label_made_during_the_run(tmp_path):
    root = _dataset(tmp_path, ["a.jpg"])
    [frame] = labeling.load_frames(root)

    def score(_jpeg):
        # A person labels the frame while the teacher is still scoring it.
        with open(os.path.join(root, labeling.LABELS_NAME), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"sha256": frame.sha256, "path": frame.path,
                                 "label": "no_person", "by": "human"}) + "\n")
        return 0.95

    autolabel.run(root, score, model="m")
    labels = labeling.latest_labels(root)
    assert labels[frame.sha256]["label"] == "no_person"
    assert labels[frame.sha256]["by"] == "human"


def test_autolabel_ignores_cache_from_another_model(tmp_path):
    root = _dataset(tmp_path, ["a.jpg"])
    [frame] = labeling.load_frames(root)
    with open(os.path.join(root, labeling.TEACHER_NAME), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"sha256": frame.sha256, "model": "old", "person": 0.01}) + "\n")
    summary = autolabel.run(root, lambda _j: 0.95, model="new")
    assert summary["scored"] == 1 and summary["cached"] == 0
    assert labeling.latest_labels(root)[frame.sha256]["label"] == "person"
    again = autolabel.run(root, lambda _j: 0.0, model="new")
    assert again["cached"] == 1 and again["scored"] == 0


def test_select_frames_keeps_first_and_last_when_swapping_keep():
    paths = [f"f{i}.jpg" for i in range(20)]
    for keep in ("f1.jpg", "f18.jpg", "f10.jpg"):
        out = enrich.select_frames(paths, limit=4, keep=keep)
        assert out[0] == "f0.jpg" and out[-1] == "f19.jpg"
        assert keep in out


def test_false_string_is_not_a_person():
    assert enrich._content_to_description(json.dumps({"person": "false", "description": "x"})) \
        == enrich.EMPTY_SCENE
    assert enrich._content_to_description(json.dumps({"person": "true", "description": "x"})) == "x"
    assert enrich._content_to_description(json.dumps({"person": True})) == "person"


def test_pool_record_does_not_hold_lock_during_health_probe(monkeypatch):
    urls = ("http://203.0.113.1:8766/score", "http://203.0.113.2:8766/score")
    pool = scorer.ScorerPool(urls)
    held = []

    def probe(url, timeout=None):
        held.append(pool.lock.locked())
        return True

    monkeypatch.setattr(scorer, "_health_ok", probe)
    for _ in range(scorer.FAIL_THRESHOLD):
        pool.record(urls[0], True)
    assert held == [False]
    assert pool.index == 1


def test_autolabel_rereads_labels_only_when_the_file_changed(tmp_path, monkeypatch):
    root = _dataset(tmp_path, [f"{i}.jpg" for i in range(6)])
    with open(os.path.join(root, labeling.LABELS_NAME), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"sha256": "x", "label": "person"}) + "\n")
    calls = []
    real = labeling.latest_labels
    monkeypatch.setattr(labeling, "latest_labels", lambda d: calls.append(d) or real(d))
    # teacher disagrees everywhere: nothing is appended, so the file never changes
    autolabel.run(root, lambda _j: 0.0, model="m")
    assert len(calls) == 2          # the initial read + one re-read, not one per frame


def test_autolabel_warns_about_legacy_teacher_rows_and_does_not_duplicate(tmp_path, caplog):
    root = _dataset(tmp_path, ["a.jpg"])
    [frame] = labeling.load_frames(root)
    path = os.path.join(root, labeling.TEACHER_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"sha256": frame.sha256, "person": 0.5}) + "\n")
        fh.write(json.dumps({"sha256": frame.sha256, "model": "m", "person": 0.9}) + "\n")
    with caplog.at_level("WARNING"):
        autolabel.run(root, lambda _j: 0.9, model="m")
    assert any("without a model" in r.getMessage() and "1 rows" in r.getMessage()
               for r in caplog.records)
    with open(path, encoding="utf-8") as fh:
        assert len(fh.readlines()) == 2
