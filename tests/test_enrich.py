import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import enrich

# ── has_person ───────────────────────────────────────────────────────────────

def test_has_person_true():
    assert enrich.has_person("a man in a dark jacket walking left") is True

def test_has_person_pedestrian():
    assert enrich.has_person("pedestrian crossing the street") is True

def test_has_person_false_for_vehicle():
    assert enrich.has_person("a white car parked by the fence") is False

def test_has_person_empty_scene():
    assert enrich.has_person("empty scene") is False

def test_has_person_none():
    assert enrich.has_person(None) is False


# ── parse_face_names / face_label ────────────────────────────────────────────

def test_parse_face_names_basic():
    assert enrich.parse_face_names("101:alice,202:bob") == {101: "alice", 202: "bob"}

def test_parse_face_names_skips_garbage():
    assert enrich.parse_face_names("nope, 12:ok ,bad:name") == {12: "ok"}

def test_parse_face_names_empty():
    assert enrich.parse_face_names("") == {}
    assert enrich.parse_face_names(None) == {}

def test_face_label_known():
    assert enrich.face_label([101], {101: "alice"}) == "alice"

def test_face_label_unknown_single():
    assert enrich.face_label([123], {}) == "unknown face"

def test_face_label_unknown_multiple():
    assert enrich.face_label([1, 2, 3], {}) == "3 unknown faces"

def test_face_label_known_plus_unknown():
    assert enrich.face_label([1, 2], {1: "alice"}) == "alice + unknown face"

def test_face_label_no_faces():
    assert enrich.face_label([], {1: "alice"}) == ""


# ── pick_sharpest ────────────────────────────────────────────────────────────

def test_pick_sharpest_returns_highest_score():
    scored = [("a.jpg", 10.0), ("b.jpg", 99.0), ("c.jpg", 50.0)]
    assert enrich.pick_sharpest(scored) == "b.jpg"

def test_pick_sharpest_single():
    assert enrich.pick_sharpest([("only.jpg", 1.0)]) == "only.jpg"

def test_pick_sharpest_empty():
    assert enrich.pick_sharpest([]) is None


# ── select_frames ────────────────────────────────────────────────────────────

def test_select_frames_under_limit_returns_all():
    paths = ["a.jpg", "b.jpg", "c.jpg"]
    assert enrich.select_frames(paths, keep="b.jpg") == paths

def test_select_frames_over_limit_spans_sequence():
    paths = [f"f{i}.jpg" for i in range(9)]
    out = enrich.select_frames(paths)
    assert len(out) == 5
    assert out[0] == "f0.jpg" and out[-1] == "f8.jpg"   # whole event covered
    assert out == [p for p in paths if p in out]        # chronological subsequence

def test_select_frames_always_includes_keep():
    paths = [f"f{i}.jpg" for i in range(9)]
    out = enrich.select_frames(paths, keep="f3.jpg")
    assert "f3.jpg" in out
    assert len(out) <= 5

def test_select_frames_empty():
    assert enrich.select_frames([]) == []


# ── groq_describe ────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body):
        self._body = body
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def read(self):
        return self._body


def _vision_json(person=False, animal=None, description=""):
    import json as _json
    content = _json.dumps({"person": person, "animal": animal, "description": description})
    return _json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _fake_urlopen(monkeypatch, body, captured=None):
    def fake_urlopen(req, timeout=None):
        if captured is not None:
            captured["req"] = req
            captured["timeout"] = timeout
            captured["payload"] = __import__("json").loads(req.data.decode())
        return _FakeResp(body)
    monkeypatch.setattr(enrich.urllib.request, "urlopen", fake_urlopen)


def _img(tmp_path, name="frame.jpg"):
    img = tmp_path / name
    img.write_bytes(b"\xff\xd8\xff\xe0" + name.encode())   # distinct per frame
    return str(img)


def test_groq_describe_sends_browser_user_agent(monkeypatch, tmp_path):
    """Groq sits behind Cloudflare, which 403s the default Python-urllib agent.

    A real (non-urllib) User-Agent must be sent or every call fails (HTTP 403,
    error 1010) and we silently lose all scene descriptions.
    """
    captured = {}
    _fake_urlopen(monkeypatch, _vision_json(person=True, description="a man walking"), captured)
    out = enrich.groq_describe("key", _img(tmp_path))

    assert out == "a man walking"
    ua = captured["req"].get_header("User-agent")
    assert ua
    assert "python-urllib" not in ua.lower()


def test_groq_describe_uses_generous_timeout(monkeypatch, tmp_path):
    # A 10 s timeout was too tight on a slow Pi Zero (base64 upload), so Groq returned ''
    # and the caller sent a blank/false alert. The ceiling must stay generous (>= 20 s).
    captured = {}
    _fake_urlopen(monkeypatch, _vision_json(person=True, description="x"), captured)
    enrich.groq_describe("key", _img(tmp_path))
    assert captured["timeout"] >= 20


def test_groq_describe_requests_json_mode(monkeypatch, tmp_path):
    # Structured output instead of the fragile "reply exactly 'empty scene'" convention.
    captured = {}
    _fake_urlopen(monkeypatch, _vision_json(person=True, description="x"), captured)
    enrich.groq_describe("key", _img(tmp_path))
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_groq_describe_no_subject_maps_to_empty_scene(monkeypatch, tmp_path):
    # Callers (notify.is_empty_scene, the SD arbiter loop) keep the old contract:
    # nothing of interest -> 'empty scene'.
    _fake_urlopen(monkeypatch, _vision_json(person=False, animal=None, description=""))
    assert enrich.groq_describe("key", _img(tmp_path)) == "empty scene"


def test_groq_describe_animal_returns_description(monkeypatch, tmp_path):
    _fake_urlopen(monkeypatch, _vision_json(animal="cat", description="cat sits on the wall"))
    assert enrich.groq_describe("key", _img(tmp_path)) == "cat sits on the wall"


def test_groq_describe_subject_with_blank_description_stays_non_empty(monkeypatch, tmp_path):
    # person=true with a blank description must not read as "empty scene" downstream —
    # fall back to a minimal marker so the alert still carries the confirmation.
    _fake_urlopen(monkeypatch, _vision_json(person=True, description=""))
    out = enrich.groq_describe("key", _img(tmp_path))
    assert out
    assert "empty" not in out.lower()


def test_groq_describe_malformed_json_returns_blank(monkeypatch, tmp_path):
    # Non-JSON reply = model failure, same contract as a timeout: '' (never a false
    # "empty scene", never a made-up description).
    body = b'{"choices":[{"message":{"content":"sorry, I cannot help"}}]}'
    _fake_urlopen(monkeypatch, body)
    assert enrich.groq_describe("key", _img(tmp_path)) == ""


def test_groq_describe_multi_image_payload_in_order(monkeypatch, tmp_path):
    import base64
    captured = {}
    _fake_urlopen(monkeypatch, _vision_json(person=True, description="x"), captured)
    frames = [_img(tmp_path, f"f{i}.jpg") for i in range(3)]
    enrich.groq_describe("key", frames)

    content = captured["payload"]["messages"][0]["content"]
    sent = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
    expected = ["data:image/jpeg;base64,"
                + base64.b64encode(open(f, "rb").read()).decode() for f in frames]
    assert sent == expected                     # all frames, chronological order


def test_groq_describe_caps_images_at_groq_limit(monkeypatch, tmp_path):
    # Groq vision accepts at most 5 images per request; more must be thinned, not fail.
    captured = {}
    _fake_urlopen(monkeypatch, _vision_json(person=True, description="x"), captured)
    frames = [_img(tmp_path, f"f{i}.jpg") for i in range(8)]
    enrich.groq_describe("key", frames)

    content = captured["payload"]["messages"][0]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 5
