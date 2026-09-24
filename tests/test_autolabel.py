import json
import os

import pytest

from tapo_monitor import autolabel, labeling


def _dataset(tmp_path, frames):
    """frames: [(source, name, person_score_or_None)] -> dataset dir with index + JPEGs."""
    for source, name, score in frames:
        log = tmp_path / "site" / ("sent-log" if source == "sent" else "review-log")
        log.mkdir(parents=True, exist_ok=True)
        (log / name).write_bytes(b"jpeg " + name.encode())
        record = {"ts": 1.0, "file": name, "camera": "front"}
        if score is not None:
            record["person"] = score
            record["animal"] = 0.0
        if source == "review":
            record["verdict"] = "hold"
        with open(log / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    return str(tmp_path)


def _teacher(scores):
    """A score_fn returning the teacher score keyed by the JPEG's name."""
    def score(jpeg_bytes):
        return scores[jpeg_bytes.decode().split(" ", 1)[1]]
    return score


def test_decide_rules():
    d = autolabel.decide
    assert d(0.9, 0.8) == "person"                  # both confident: a person
    assert d(0.9, 0.05) is None                     # sent, teacher sees nobody: ask
    assert d(0.5, 0.9) is None                      # gray zone always goes to a human
    assert d(0.1, 0.05) == "no_person"              # both see nothing
    assert d(0.1, 0.9) is None                      # possible miss: ask
    assert d(None, 0.95) == "person"                # no production score: teacher alone,
    assert d(None, 0.02) == "no_person"             # but only when it is very sure
    assert d(None, 0.5) is None


def test_run_labels_agreements_and_leaves_disagreements_for_a_human(tmp_path):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9), ("sent", "b.jpg", 0.8),
                               ("review", "c.jpg", 0.4)])
    summary = autolabel.run(root, _teacher({"a.jpg": 0.88, "b.jpg": 0.03, "c.jpg": 0.7}),
                            model="teacher-x", now=5.0)
    assert summary == {"scored": 3, "cached": 0, "auto_person": 1, "auto_no_person": 0,
                       "for_human": 2}
    labels = labeling.latest_labels(root)
    [only] = labels.values()
    assert only["label"] == "person" and only["by"] == "auto:teacher-x"
    assert only["teacher"] == 0.88


def test_teacher_scores_are_cached_and_never_recomputed(tmp_path):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9)])
    autolabel.run(root, _teacher({"a.jpg": 0.9}), model="teacher-x")
    again = autolabel.run(root, lambda _b: pytest.fail("teacher re-run"), model="teacher-x")
    assert again["scored"] == 0 and again["cached"] == 1
    assert len(labeling.latest_labels(root)) == 1   # no duplicate label line either


def test_a_human_label_is_never_overwritten(tmp_path):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9)])
    session = labeling.LabelSession(root)
    session.label("site/sent-log/a.jpg", "no_person", now=1.0)
    autolabel.run(root, _teacher({"a.jpg": 0.95}), model="teacher-x")
    [rec] = labeling.latest_labels(root).values()
    assert rec["label"] == "no_person" and "by" not in rec


def test_the_human_queue_puts_the_biggest_disagreement_first(tmp_path):
    root = _dataset(tmp_path, [("sent", "small.jpg", 0.70), ("sent", "big.jpg", 0.95),
                               ("review", "gray.jpg", 0.45)])
    autolabel.run(root, _teacher({"small.jpg": 0.40, "big.jpg": 0.02, "gray.jpg": 0.5}),
                  model="teacher-x")
    session = labeling.LabelSession(root)
    order = [f.path.rsplit("/", 1)[1] for f in session.pending()]
    assert order[0] == "big.jpg"                    # sent at 0.95, teacher sees nobody
    assert session.state()["frame"]["teacher"] == 0.02


def test_stats_say_how_many_labels_were_automatic(tmp_path):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9), ("sent", "b.jpg", 0.9)])
    autolabel.run(root, _teacher({"a.jpg": 0.9, "b.jpg": 0.9}), model="teacher-x")
    labeling.LabelSession(root)  # untouched: both auto-labeled
    stats = labeling.compute_stats(root)
    assert stats["by"] == {"auto": 2, "human": 0}


def test_missing_image_is_skipped_not_fatal(tmp_path):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9)])
    os.remove(os.path.join(root, "site", "sent-log", "a.jpg"))
    assert autolabel.run(root, _teacher({}), model="teacher-x")["scored"] == 0


def test_cli_rejects_a_missing_model(tmp_path, capsys):
    root = _dataset(tmp_path, [("sent", "a.jpg", 0.9)])
    assert autolabel.main([root, "--model", str(tmp_path / "none.onnx")]) == 2
