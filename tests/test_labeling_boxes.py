"""Tests for opt-in box labeling in tapo_monitor.labeling."""

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import cli, labeling
from tests.test_labeling import _labels, _post, _sent, _serve, _write_log

YOLO = [0.10, 0.10, 0.30, 0.60, 0.80, "yolox640"]
RF = [0.12, 0.12, 0.31, 0.58, 0.70, "rfdetr"]
LOW = [0.50, 0.50, 0.60, 0.90, 0.20, "yolox640"]
SENT = "h/sent-log/"


def _boxed(name, boxes, ts=1000.0, **kw):
    rec = _sent(name, 0.5, ts=ts, **kw)
    if boxes is not None:
        rec["boxes"] = boxes
    return rec


def _drawn(box, label="person"):
    return {"source": "drawn", "box": box, "label": label}


def _model(index, label):
    return {"source": "model", "index": index, "label": label}


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "ds"
    _write_log(str(folder / "h" / "sent-log"), [
        _boxed("b1.jpg", [YOLO, RF, LOW]),
        _boxed("b2.jpg", []),
        _boxed("plain.jpg", None),
        _boxed("bad.jpg", [YOLO, [1, 2], [0.5, 0.5, 0.4, 0.4, 0.9, ""], "x",
                           [0.7, 0.1, 0.2, 0.3, 2.0, "m"]]),
    ])
    return str(folder)


def _frames(root):
    return {f.path.split("/")[-1]: f for f in labeling.load_frames(root)}


def test_index_boxes_are_parsed_and_malformed_ones_dropped(root):
    f = _frames(root)
    assert f["plain.jpg"].boxes is None
    assert f["b2.jpg"].boxes == ()
    assert len(f["b1.jpg"].boxes) == 3 and f["b1.jpg"].boxes[0][5] == "yolox640"
    assert [b[5] for b in f["bad.jpg"].boxes] == ["yolox640", "m"]
    assert f["bad.jpg"].boxes[1][:5] == (0.2, 0.1, 0.7, 0.3, 1.0)
    assert labeling.parse_index_boxes("nope") is None


def test_state_exposes_boxes_only_for_boxed_frames(root):
    s = labeling.LabelSession(root, seed=1)
    views = {}
    for _ in range(4):
        v = s.state()["frame"]
        views[v["path"].split("/")[-1]] = v
        s.skip(v["path"])
    assert views["plain.jpg"]["boxes"] is None
    assert views["b2.jpg"]["boxes"] == []
    assert views["b1.jpg"]["boxes"][1] == RF


def test_suggest_label():
    assert labeling.suggest_label([]) is None
    assert labeling.suggest_label([{"label": "no_person"}]) == "no_person"
    assert labeling.suggest_label([{"label": "no_person"}, {"label": "person"}]) == "person"


def test_label_with_boxes_stores_them_and_keeps_explicit_label(root):
    s = labeling.LabelSession(root, seed=1)
    rec = s.label(SENT + "b1.jpg", "no_person", boxes=[
        _model(0, "person"), _model(1, "no_person"), _drawn([0.9, 0.9, 0.7, 0.5])])
    assert rec["label"] == "no_person"          # explicit label never overridden
    assert rec["boxes"][0] == {"box": [0.1, 0.1, 0.3, 0.6], "label": "person",
                               "source": "model", "model": "yolox640", "score": 0.8}
    assert rec["boxes"][2] == {"box": [0.7, 0.5, 0.9, 0.9], "label": "person",
                               "source": "drawn"}
    assert _labels(root)[-1]["boxes"] == rec["boxes"]


def test_label_none_takes_the_suggestion_or_fails(root):
    s = labeling.LabelSession(root, seed=1)
    assert s.label(SENT + "b1.jpg", None, boxes=[_model(0, "person")])["label"] == "person"
    rec = s.label(SENT + "b2.jpg", None, boxes=[_drawn([0, 0, 0.5, 0.5], "no_person")])
    assert rec["label"] == "no_person"
    with pytest.raises(ValueError):
        s.label(SENT + "plain.jpg", None)
    with pytest.raises(ValueError):
        s.label(SENT + "plain.jpg", None, boxes=[])


@pytest.mark.parametrize("boxes", [
    "x", [1], [_model(9, "person")], [_model(True, "person")], [_model(0, "unsure")],
    [{"source": "other", "index": 0, "label": "person"}],
    [_drawn([0.1, 0.1, 0.1, 0.5])], [_drawn([0.1, "a", 0.3, 0.5])],
    [{"source": "drawn", "label": "person"}],
])
def test_bad_box_payload_is_rejected_and_nothing_written(root, boxes):
    s = labeling.LabelSession(root, seed=1)
    with pytest.raises(ValueError):
        s.label(SENT + "b1.jpg", "person", boxes=boxes)
    assert _labels(root) == []


def test_old_format_is_unchanged(root):
    s = labeling.LabelSession(root, seed=1)
    assert "boxes" not in s.label(SENT + "plain.jpg", "person")
    assert "boxes" not in s.label(SENT + "b2.jpg", "unsure", boxes=[])


def test_undo_restores_previous_boxes(root):
    s = labeling.LabelSession(root, seed=1)
    path = SENT + "b1.jpg"
    s.label(path, "person", boxes=[_model(0, "person")])
    s.label(path, "no_person")
    s.undo()
    assert _labels(root)[-1]["boxes"][0]["label"] == "person"
    assert labeling.latest_labels(root)[_frames(root)["b1.jpg"].sha256]["boxes"]


def test_http_label_with_boxes_and_suggestion(root):
    srv, url = _serve(labeling.LabelSession(root, seed=1))
    try:
        _post(url + "/api/label", {"path": SENT + "b1.jpg", "label": None,
                                   "boxes": [_model(1, "person")]})
        assert _labels(root)[-1]["label"] == "person"
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(url + "/api/label", {"path": SENT + "b2.jpg", "label": "person",
                                       "boxes": [{"source": "x"}]})
        assert err.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_has_box_keys_and_czech_texts():
    en, cs = labeling.page("en"), labeling.page("cs")
    for code in ("KeyP", "KeyN", "KeyX", "Tab", "Enter"):
        assert code in en
    assert "Tab další box" in cs and "Tab next box" in en
    assert "KeyU" in cs and "Digit1" in cs


def test_iou():
    assert labeling.iou([0, 0, 1, 1], [0, 0, 1, 1]) == 1.0
    assert labeling.iou([0, 0, 0.5, 0.5], [0.5, 0.5, 1, 1]) == 0.0
    assert labeling.iou([0, 0, 1, 1], [0.5, 0, 1, 1]) == pytest.approx(0.5)


def _label_all(root, spec):
    s = labeling.LabelSession(root, seed=1)
    for name, boxes in spec.items():
        s.label(SENT + name, "person", boxes=boxes)


def test_stats_per_model_counts_and_recall(tmp_path):
    folder = str(tmp_path / "ds")
    small_yolo = [0.50, 0.50, 0.55, 0.58, 0.9, "yolox640"]   # height 0.08
    _write_log(os.path.join(folder, "h", "sent-log"), [
        _boxed("a.jpg", [YOLO, RF, LOW]),
        _boxed("b.jpg", [small_yolo, RF]),
        _boxed("c.jpg", []),        # no model data: drawn persons are not misses
        _boxed("d.jpg", [RF]),      # only rfdetr has data
    ])
    _label_all(folder, {
        "a.jpg": [_model(0, "person"), _model(2, "no_person"), _model(1, "no_person"),
                  _drawn([0.1, 0.1, 0.3, 0.6]),            # found by both
                  _drawn([0.8, 0.1, 0.95, 0.5])],          # found by none
        "b.jpg": [_drawn([0.50, 0.50, 0.55, 0.58]),        # small, yolox finds it
                  _drawn([0.1, 0.9, 0.15, 0.97])],         # small, both miss
        "c.jpg": [_drawn([0.1, 0.1, 0.3, 0.6])],
        "d.jpg": [_drawn([0.1, 0.1, 0.3, 0.6])],
    })
    st = labeling.compute_stats(folder)["boxes"]
    assert st["frames"] == 4
    assert st["drawn"] == {"person": 6, "no_person": 0}
    y, r = st["recall"]["yolox640"], st["recall"]["rfdetr"]
    assert (y["people"], y["found"], y["missed"]) == (4, 2, 2)    # frames a, b only
    assert (y["small"], y["small_found"], y["small_missed"]) == (2, 1, 1)
    assert y["false_boxes"] == 0 and y["true_boxes"] == 1  # LOW (.2) is below the threshold
    assert (r["people"], r["found"], r["missed"]) == (5, 2, 3)    # frames a, b, d
    assert r["false_boxes"] == 1
    assert st["models"]["rfdetr"] == {"person": 0, "no_person": 1}
    lowered = labeling.compute_stats(folder, box_score=0.1)["boxes"]["recall"]["yolox640"]
    assert lowered["false_boxes"] == 1
    high = labeling.compute_stats(folder, box_score=0.85)["boxes"]["recall"]["yolox640"]
    assert high["found"] == 1
    tight = labeling.compute_stats(folder, box_iou=0.99)["boxes"]["recall"]["yolox640"]
    assert tight["found"] == 2       # only the two exact copies overlap that well


def test_stats_output_and_cli_options(tmp_path, capsys):
    folder = str(tmp_path / "ds")
    _write_log(os.path.join(folder, "h", "sent-log"), [_boxed("a.jpg", [YOLO])])
    _label_all(folder, {"a.jpg": [_model(0, "person"), _drawn([0.6, 0.6, 0.9, 0.9])]})
    stats = labeling.compute_stats(folder)
    text = labeling.format_stats(stats)
    assert "box labels on 1 frames" in text and "yolox640" in text and "recall" in text
    assert "(drawn by hand)" in text
    assert "Recall osob podle modelu" in labeling.format_stats(stats, "cs")
    assert "<table>" in labeling.stats_page(stats) and "yolox640" in labeling.stats_page(stats)
    assert cli.main(["label-stats", folder, "--json", "--box-score", "0.9", "--box-iou",
                     "0.5"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["boxes"]["params"] == {"score": 0.9, "iou": 0.5, "small_height": 0.1}
    assert data["boxes"]["recall"]["yolox640"]["missed"] == 1
    assert cli.main(["label-stats", folder, "--box-iou", "0"]) == 2


def test_stats_without_boxes_have_no_box_section(root):
    labeling.LabelSession(root, seed=1).label(SENT + "plain.jpg", "person")
    stats = labeling.compute_stats(root)
    assert "boxes" not in stats
    assert "box labels" not in labeling.format_stats(stats)


def _models_stats(tmp_path, records):
    folder = str(tmp_path / "ds")
    _write_log(os.path.join(folder, "h", "sent-log"), records)
    _label_all(folder, {r["file"]: [_drawn([0.1, 0.1, 0.3, 0.6])] for r in records})
    return labeling.compute_stats(folder)["boxes"]


def test_models_field_decides_which_models_count(tmp_path):
    def rec(name, boxes, models):
        r = _boxed(name, boxes)
        r["models"] = models
        return r

    st = _models_stats(tmp_path, [
        rec("a.jpg", [YOLO], ["yolox640", "rfdetr"]),   # rfdetr ran, found nothing: a miss
        rec("b.jpg", [], ["rfdetr"]),                   # yolox640 not listed: not counted
        rec("c.jpg", [RF], ["rfdetr", "yolox640"]),
    ])
    y, r = st["recall"]["yolox640"], st["recall"]["rfdetr"]
    assert (y["people"], y["found"], y["missed"], y["inferred"]) == (2, 1, 1, 0)
    assert (r["people"], r["found"], r["missed"], r["inferred"]) == (3, 1, 2, 0)
    assert "overstated" not in labeling._box_recall_note(st)
    assert labeling._box_recall_rows(st)[0][0] == "rfdetr"


def test_old_format_without_models_is_marked_overstated(tmp_path):
    st = _models_stats(tmp_path, [_boxed("a.jpg", [YOLO]), _boxed("b.jpg", [])])
    y = st["recall"]["yolox640"]
    assert (y["people"], y["found"], y["inferred"]) == (1, 1, 1)   # b.jpg: no data, no count
    assert labeling._box_recall_rows(st)[0][0] == "yolox640 *"
    assert "overstated" in labeling._box_recall_note(st)
    assert "nadsazen" in labeling._box_recall_note(st, "cs")


def test_parse_index_models():
    assert labeling.parse_index_models(None) is None
    assert labeling.parse_index_models(["a", " b ", "", 3]) == {"a", "b"}
    assert labeling.parse_index_models([]) == frozenset()
