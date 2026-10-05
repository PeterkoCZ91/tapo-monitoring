"""``scorer.ignore_boxes``: drop a person whose box IS a known static box (size and place).

The case it exists for: a swing roof boxed as a person, 0.44 x 0.43 of the frame, while
people stand in front of it. An ``ignore_zones`` rect over the roof would hide those people
too (their boxes lie inside it); ``max_person_box`` low enough to catch it would hide any
large person anywhere.
"""
import io
import json

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import monitor, scorer
from tests import test_monitor as tm
from tests.test_scorer import _zoned

# 1000 x 500 frame (see _zoned). The roof: x 0..0.44, y 0.255..0.685 of the frame.
ROOF = (0.0, 0.255, 0.44, 0.685)
ROOF_PX = [0, 127.5, 440, 342.5]
PERSON_IN_FRONT = [150, 200, 200, 340]       # inside the roof rect, but a person-sized box
FAR_PERSON = [700, 100, 760, 300]


def _cam(**scorer_kw):
    return cfg.load_config_from_dict({"cameras": [{
        "name": "a", "host": "203.0.113.10", "scorer": {"url": "http://x", **scorer_kw}}]}
    ).cameras[0].scorer


# -- config --------------------------------------------------------------------------

def test_defaults_off_and_parse():
    c = _cam()
    assert c.ignore_boxes == () and c.ignore_boxes_mode == "enforce"
    c = _cam(ignore_boxes=[list(ROOF)], ignore_boxes_mode="observe")
    assert c.ignore_boxes == (ROOF,) and c.ignore_boxes_mode == "observe"


@pytest.mark.parametrize("value", ["roof", [[0.5, 0.5, 0.4, 0.6]], [[0, 0, 1]],
                                   [[0, 0, 1.2, 1]], [["a", 0, 1, 1]], [0, 0, 1, 1]])
def test_bad_rects_are_refused(value):
    with pytest.raises(cfg.ConfigError, match="ignore_boxes"):
        _cam(ignore_boxes=value)


def test_observe_needs_boxes_and_mode_is_checked():
    with pytest.raises(cfg.ConfigError, match="ignore_boxes_mode"):
        _cam(ignore_boxes_mode="observe")
    with pytest.raises(cfg.ConfigError, match="ignore_boxes_mode"):
        _cam(ignore_boxes=[list(ROOF)], ignore_boxes_mode="dry-run")


# -- filter --------------------------------------------------------------------------

def test_the_static_box_goes_and_a_person_in_front_stays():
    result = _zoned([0.55, 0.9], [ROOF_PX, PERSON_IN_FRONT], person=0.9)
    result["box"] = PERSON_IN_FRONT
    out = scorer.apply_ignore_boxes(result, [ROOF])
    assert out["person_boxes"] == [PERSON_IN_FRONT] and out["person"] == 0.9
    assert out["ignored_boxes"] == 1
    # The same frame under ignore_zones would lose the person in front as well.
    assert scorer.apply_ignore_zones(_zoned([0.9], [PERSON_IN_FRONT]), [ROOF])["person"] == 0.0


def test_only_the_static_box_scores_nobody_and_a_far_person_stays():
    out = scorer.apply_ignore_boxes(_zoned([0.55], [ROOF_PX]), [ROOF])
    assert out["person"] == 0.0 and out["box"] is None
    both = scorer.apply_ignore_boxes(_zoned([0.55, 0.5], [ROOF_PX, FAR_PERSON]), [ROOF])
    assert both["person"] == 0.5 and both["box"] == FAR_PERSON


def test_a_big_person_elsewhere_is_not_touched_unlike_max_person_box():
    big = [300, 50, 740, 270]                      # 0.44 x 0.44 of the frame, other place
    result = _zoned([0.8], [big])
    assert scorer.apply_ignore_boxes(result, [ROOF]) is result
    assert scorer.apply_max_person_box(result, 0.4)["person"] == 0.0


def test_a_person_with_the_roof_box_itself_is_dropped_known_limit():
    # What the option costs: someone whose box coincides with the rect cannot be told apart.
    assert scorer.apply_ignore_boxes(_zoned([0.9], [ROOF_PX]), [ROOF])["person"] == 0.0


@pytest.mark.parametrize("shift,dropped", [(0, True), (60, True), (66, False), (200, False)])
def test_the_iou_boundary(shift, dropped):
    # Same-size box moved right by `shift` px: IoU = (440 - s) / (440 + s); 0.75 at ~62.9.
    box = [ROOF_PX[0] + shift, ROOF_PX[1], ROOF_PX[2] + shift, ROOF_PX[3]]
    out = scorer.apply_ignore_boxes(_zoned([0.6], [box]), [ROOF])
    assert (out["person"] == 0.0) is dropped


def test_an_older_scorer_answer_is_left_alone():
    result = {"person": 0.58, "animal": 0.0, "box": ROOF_PX, "w": 1000, "h": 500}
    assert scorer.apply_ignore_boxes(result, [ROOF]) is result


# -- score_image: off / enforce / observe ----------------------------------------------

def _serve(monkeypatch, body):
    monkeypatch.setattr(scorer.urllib.request, "urlopen",
                        lambda req, timeout=10: io.BytesIO(json.dumps(body).encode()))


def _score(monkeypatch, tmp_path, body=None, **kw):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    _serve(monkeypatch, body or _zoned([0.55], [ROOF_PX]))
    return scorer.score_image("http://x/score", str(img), **kw)


def test_off_by_default(monkeypatch, tmp_path):
    out = _score(monkeypatch, tmp_path)
    assert out["person"] == 0.55 and "ignored_boxes" not in out


def test_enforce_drops(monkeypatch, tmp_path):
    out = _score(monkeypatch, tmp_path, ignore_boxes=[ROOF])
    assert out["person"] == 0.0 and out["ignored_boxes"] == 1


def test_observe_drops_nothing_and_reports(monkeypatch, tmp_path):
    out = _score(monkeypatch, tmp_path, ignore_boxes=[ROOF], ignore_boxes_mode="observe")
    assert out["person"] == 0.55 and out["person_boxes"] == [ROOF_PX]
    assert out["ignore_box_observe"] == {"dropped": 1, "person": 0.0}
    s = scorer.subject_score(out, threshold=0.45)
    assert s == 0.55 and s.box_observe == 1


def test_observe_reports_nothing_when_a_real_person_keeps_the_frame_above(monkeypatch, tmp_path):
    out = _score(monkeypatch, tmp_path, body=_zoned([0.9, 0.55], [FAR_PERSON, ROOF_PX]),
                 ignore_boxes=[ROOF], ignore_boxes_mode="observe")
    assert scorer.subject_score(out, threshold=0.45).box_observe is None


# -- audit ---------------------------------------------------------------------------

def test_audit_line_only_for_a_send(caplog):
    camera = tm._vehicle_cfg()
    s = scorer.subject_score({"person": 0.55, "animal": 0.0,
                              "ignore_box_observe": {"dropped": 1, "person": 0.0}},
                             threshold=0.45)
    with caplog.at_level("INFO"):
        monitor.audit_event(camera, {"start_time": 100}, "person", "live", "send",
                            score=s, threshold=0.45)
        monitor.audit_event(camera, {"start_time": 100}, "person", "live", "drop", score=s)
    lines = [r.getMessage() for r in caplog.records if "would_drop_ignore_box" in r.getMessage()]
    assert len(lines) == 1 and "boxes=1" in lines[0]
