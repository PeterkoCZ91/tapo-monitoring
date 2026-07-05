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


def test_groq_describe_sends_browser_user_agent(monkeypatch, tmp_path):
    """Groq sits behind Cloudflare, which 403s the default Python-urllib agent.

    A real (non-urllib) User-Agent must be sent or every call fails (HTTP 403,
    error 1010) and we silently lose all scene descriptions.
    """
    img = tmp_path / "frame.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(b'{"choices":[{"message":{"content":" a man walking "}}]}')

    monkeypatch.setattr(enrich.urllib.request, "urlopen", fake_urlopen)
    out = enrich.groq_describe("key", str(img))

    assert out == "a man walking"
    ua = captured["req"].get_header("User-agent")
    assert ua
    assert "python-urllib" not in ua.lower()


def test_groq_describe_uses_generous_timeout(monkeypatch, tmp_path):
    # A 10 s timeout was too tight on a slow Pi Zero (base64 upload), so Groq returned ''
    # and the caller sent a blank/false alert. The ceiling must stay generous (>= 20 s).
    img = tmp_path / "frame.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeResp(b'{"choices":[{"message":{"content":"x"}}]}')

    monkeypatch.setattr(enrich.urllib.request, "urlopen", fake_urlopen)
    enrich.groq_describe("key", str(img))
    assert captured["timeout"] >= 20
