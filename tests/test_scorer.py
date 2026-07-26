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


def test_subject_box_parses_and_validates():
    assert scorer.subject_box({"box": [1, 2, 3, 4]}) == [1.0, 2.0, 3.0, 4.0]
    assert scorer.subject_box({"box": None}) is None
    assert scorer.subject_box({}) is None
    assert scorer.subject_box({"box": [1, 2, 3]}) is None
