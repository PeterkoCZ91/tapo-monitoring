import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import scorer


def _fake_urlopen(monkeypatch, payload=None, exc=None, capture=None):
    def fake(req, timeout=None):
        if capture is not None:
            capture.append((req.full_url, req.data, req.get_header("Content-type"), timeout))
        if exc is not None:
            raise exc
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(scorer.urllib.request, "urlopen", fake)


def test_score_image_posts_jpeg_and_parses_json(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8jpegbytes")
    calls = []
    _fake_urlopen(monkeypatch, payload={"person": 0.9, "animal": 0.1}, capture=calls)
    out = scorer.score_image("http://127.0.0.1:8765/score", str(img), timeout=5)
    assert out == {"person": 0.9, "animal": 0.1}
    url, body, ctype, timeout = calls[0]
    assert url == "http://127.0.0.1:8765/score"
    assert body == b"\xff\xd8jpegbytes"
    assert ctype == "image/jpeg"
    assert timeout == 5


def test_score_image_none_on_connection_error(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    _fake_urlopen(monkeypatch, exc=OSError("refused"))
    assert scorer.score_image("http://127.0.0.1:8765/score", str(img)) is None


def test_score_image_none_on_bad_json(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    def fake(req, timeout=None):
        return io.BytesIO(b"not json")
    monkeypatch.setattr(scorer.urllib.request, "urlopen", fake)
    assert scorer.score_image("http://127.0.0.1:8765/score", str(img)) is None


def test_score_image_none_on_missing_file(tmp_path):
    assert scorer.score_image("http://127.0.0.1:8765/score", str(tmp_path / "gone.jpg")) is None


def test_subject_score_uses_person_and_keeps_animal_telemetry():
    score = scorer.subject_score({"person": 0.3, "animal": 0.7})
    assert score == 0.3
    assert score.person == 0.3
    assert score.animal == 0.7
    assert scorer.subject_score({"person": 0.5}) == 0.5
    assert scorer.subject_score({}) == 0.0


def test_subject_score_rejects_malformed_or_nonfinite_values():
    assert scorer.subject_score([0.2]) is None
    assert scorer.subject_score({"person": "nope"}) is None
    assert scorer.subject_score({"person": float("nan")}) is None
    assert scorer.subject_score({"person": 1.1}) is None


def test_score_image_appends_tiles_query(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    calls = []
    _fake_urlopen(monkeypatch, payload={"person": 0.5}, capture=calls)
    scorer.score_image("http://h/score", str(img), tiles=2)
    assert calls[0][0] == "http://h/score?tiles=2"


def test_score_image_no_tiles_query_when_one(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    calls = []
    _fake_urlopen(monkeypatch, payload={"person": 0.5}, capture=calls)
    scorer.score_image("http://h/score", str(img), tiles=1)
    assert calls[0][0] == "http://h/score"


def test_score_image_sends_anonymous_source_id(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    seen = []

    def fake(req, timeout=None):
        seen.append(req.get_header("X-tapo-source-id"))
        return io.BytesIO(b'{"person": 0.5}')

    monkeypatch.setattr(scorer.urllib.request, "urlopen", fake)
    scorer.score_image("http://h/score", str(img), source_id="0123456789abcdef")
    assert seen == ["0123456789abcdef"]


def test_camera_source_id_is_stable_without_exposing_camera_name():
    source_id = scorer.source_id_for_camera("front")
    assert source_id == scorer.source_id_for_camera("front")
    assert source_id != scorer.source_id_for_camera("back")
    assert len(source_id) == 16
    assert all(character in "0123456789abcdef" for character in source_id)
    assert "front" not in source_id


def test_subject_box_parses_and_validates():
    assert scorer.subject_box({"box": [1, 2, 3, 4]}) == [1.0, 2.0, 3.0, 4.0]
    assert scorer.subject_box({"box": None}) is None
    assert scorer.subject_box({}) is None
    assert scorer.subject_box({"box": [1, 2, 3]}) is None


def test_person_count_counts_at_the_callers_threshold():
    result = {"person": 0.9, "animal": 0.0, "person_scores": [0.9, 0.45, 0.2]}
    assert scorer.person_count(result, 0.4) == 2
    assert scorer.person_count(result, 0.5) == 1
    assert scorer.subject_score(result, threshold=0.4).persons == 2


def test_person_count_is_unknown_for_an_older_or_malformed_reply():
    assert scorer.person_count({"person": 0.9}, 0.4) is None          # older scorer
    assert scorer.person_count({"person_scores": [0.9]}, None) is None
    assert scorer.person_count({"person_scores": "0.9"}, 0.4) is None
    assert scorer.person_count({"person_scores": [0.9, "x"]}, 0.4) is None
    assert scorer.person_count({"person_scores": [float("nan")]}, 0.4) is None
    assert scorer.person_count({"person_scores": [1.5]}, 0.4) is None
    assert scorer.subject_score({"person": 0.9}, threshold=0.4).persons is None
    assert scorer.subject_score({"person": 0.9, "person_scores": [0.9, 0.8]}).persons is None


# ── ignore zones ────────────────────────────────────────────────────────────

def _zoned(scores, boxes, person=None):
    return {"person": scores[0] if person is None else person, "animal": 0.01,
            "classes": {"person": scores[0] if person is None else person},
            "box": boxes[0] if boxes else None, "person_scores": scores,
            "person_boxes": boxes, "w": 1000, "h": 500}


SACKS = (0.0, 0.5, 0.1, 0.7)   # left edge, the lower middle of the frame


def test_ignore_zone_removes_a_static_object_and_keeps_the_real_person():
    result = _zoned([0.58, 0.54, 0.9], [[5, 260, 60, 330], [20, 270, 90, 340],
                                         [500, 100, 560, 300]], person=0.9)
    result["box"] = [500, 100, 560, 300]
    out = scorer.apply_ignore_zones(result, [SACKS])
    assert out["person"] == 0.9 and out["box"] == [500, 100, 560, 300]
    assert out["person_scores"] == [0.9] and out["ignored_persons"] == 2


def test_ignore_zone_with_only_the_object_scores_nobody():
    out = scorer.apply_ignore_zones(_zoned([0.58, 0.54], [[5, 260, 60, 330],
                                                          [20, 270, 90, 340]]), [SACKS])
    assert out["person"] == 0.0 and out["box"] is None
    assert out["classes"]["person"] == 0.0
    assert scorer.subject_score(out, threshold=0.45) < 0.45


def test_ignore_zone_keeps_the_next_best_person_as_the_score():
    out = scorer.apply_ignore_zones(_zoned([0.58, 0.5], [[5, 260, 60, 330],
                                                         [400, 50, 450, 200]]), [SACKS])
    assert out["person"] == 0.5 and out["box"] == [400, 50, 450, 200]


def test_a_person_mostly_outside_the_zone_still_counts():
    # A person standing in front of the sacks is taller than the zone: well under 80 % in.
    result = _zoned([0.9], [[10, 100, 90, 340]])
    assert scorer.apply_ignore_zones(result, [SACKS]) is result


def test_ignore_zones_leave_an_older_scorer_answer_alone():
    result = {"person": 0.58, "animal": 0.0, "box": [5, 260, 60, 330], "w": 1000, "h": 500}
    assert scorer.apply_ignore_zones(result, [SACKS]) is result


def test_score_image_applies_the_zones(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    body = json.dumps(_zoned([0.58], [[5, 260, 60, 330]])).encode()
    monkeypatch.setattr(scorer.urllib.request, "urlopen",
                        lambda req, timeout=10: io.BytesIO(body))
    assert scorer.score_image("http://x/score", str(img), ignore_zones=[SACKS])["person"] == 0.0
    assert scorer.score_image("http://x/score", str(img))["person"] == 0.58
