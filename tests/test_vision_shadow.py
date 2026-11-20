import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import enrich
from tools import vision_shadow


def _fake(reply):
    calls = []

    def ask(api_key, images, **kw):
        calls.append({"images": list(images), "kw": kw, "exists": os.path.exists(images[0])})
        if isinstance(reply, Exception):
            raise reply
        return reply
    return ask, calls


def test_person_reply_gives_a_verdict_score_and_logs_the_description(tmp_path):
    ask, calls = _fake('{"person": true, "animal": null, "description": "man in red jacket"}')
    log = tmp_path / "v.jsonl"
    shadow = vision_shadow.VisionShadow("http://x/v1", "m", ask=ask, log_path=str(log))
    status, payload = shadow.score(b"\xff\xd8jpeg\xff\xd9")
    assert status == 200
    assert payload["person"] == 0.9 and payload["animal"] == 0.0
    assert payload["description"] == "man in red jacket"
    row = json.loads(log.read_text().splitlines()[0])
    assert row["error"] is None and row["person"] == 0.9 and len(row["frame"]) == 16
    assert calls[0]["exists"] is True                      # the model saw a real file
    assert not os.path.exists(calls[0]["images"][0])       # and it is gone afterwards


def test_empty_and_animal_replies():
    ask, _ = _fake('{"person": false, "animal": null, "description": ""}')
    s = vision_shadow.VisionShadow("u", "m", ask=ask)
    assert s.score(b"x")[1]["person"] == 0.0
    ask, _ = _fake('{"person": false, "animal": "fox", "description": "a fox"}')
    s = vision_shadow.VisionShadow("u", "m", ask=ask)
    payload = s.score(b"x")[1]
    assert payload["person"] == 0.0 and payload["animal"] == 0.9


def test_broken_json_is_still_read():
    ask, _ = _fake('{"person": true, "animal": null, "description": "Person in suit points.}')
    s = vision_shadow.VisionShadow("u", "m", ask=ask)
    status, payload = s.score(b"x")
    assert status == 200 and payload["person"] == 0.9
    assert payload["description"] == "Person in suit points."


def test_free_text_and_server_errors_are_502_not_a_score(tmp_path):
    ask, _ = _fake("There is a man")
    log = tmp_path / "v.jsonl"
    s = vision_shadow.VisionShadow("u", "m", ask=ask, log_path=str(log))
    assert s.score(b"x") == (502, {"error": "bad_reply"})
    ask, _ = _fake(urllib.error.URLError("down"))
    s = vision_shadow.VisionShadow("u", "m", ask=ask, log_path=str(log))
    status, payload = s.score(b"x")
    assert status == 502 and payload == {"error": "URLError"}
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["error"] for r in rows] == ["bad_reply", "URLError"]


def test_log_rotates_and_never_fails_a_request(tmp_path):
    ask, _ = _fake('{"person": false, "animal": null, "description": ""}')
    log = tmp_path / "v.jsonl"
    s = vision_shadow.VisionShadow("u", "m", ask=ask, log_path=str(log), log_max_bytes=10)
    s.score(b"x")
    s.score(b"y")
    assert (tmp_path / "v.jsonl.1").exists()
    s = vision_shadow.VisionShadow("u", "m", ask=ask, log_path=str(tmp_path / "no" / "v.jsonl"))
    assert s.score(b"x")[0] == 200


def test_http_contract_matches_what_the_scorer_shadow_call_expects():
    ask, _ = _fake('{"person": true, "animal": null, "description": "man"}')
    shadow = vision_shadow.VisionShadow("u", "m", ask=ask)
    server = vision_shadow.serve(shadow, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from tapo_monitor import scorer_service
        payload, err = scorer_service._shadow_call(f"http://127.0.0.1:{port}/score", None,
                                                   b"\xff\xd8x\xff\xd9", 1, 10)
        assert err is None and payload["person"] == 0.9
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health"))
        assert health["ok"] is True and health["scored"] == 1 and health["persons"] == 1
        req = urllib.request.Request(f"http://127.0.0.1:{port}/score", data=b"",
                                     method="POST")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("empty body must be refused")
    finally:
        server.shutdown()


def test_parse_verdict_free_text_is_unknown():
    assert enrich.parse_verdict("a man walks")["person"] is None
    assert enrich.parse_verdict("")["person"] is None
