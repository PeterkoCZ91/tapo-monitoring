"""Telegram text fallback and review-digest robustness fixes."""

import io
import json
import time
import urllib.error
import urllib.parse

from tapo_monitor import notify, reviewdigest


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "bad", {}, io.BytesIO(b""))


def test_send_text_falls_back_to_plain_on_unparseable_html(monkeypatch):
    sent = []

    def fake(req, timeout=None):
        body = req.data.decode()
        sent.append(body)
        if "parse_mode=HTML" in body:
            raise _http_error(400)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    assert notify.send_text("t", "c", "down <urlopen error timed out>") is True
    assert len(sent) == 2
    assert "parse_mode" not in sent[1]
    assert "urlopen+error" in sent[1]


def test_send_text_does_not_double_escape_and_other_errors_fail(monkeypatch):
    sent = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append(req.data.decode()))
    assert notify.send_text("t", "c", "a &amp; b <b>x</b>") is True
    assert "a+%26amp%3B+b" in sent[0]  # sent verbatim, once

    def boom(req, timeout=None):
        raise _http_error(500)

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send_text("t", "c", "x") is False


def test_send_text_exact_payloads_for_special_characters(monkeypatch):
    sent = []

    def fake(req, timeout=None):
        fields = urllib.parse.parse_qs(req.data.decode())
        sent.append({k: v[0] for k, v in fields.items()})
        if "parse_mode" in fields:
            raise _http_error(400)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    text = "a & b <c> &lt;d&gt;"
    assert notify.send_text("t", "42", text) is True
    assert sent == [
        {"chat_id": "42", "text": "a & b <c> &lt;d&gt;", "parse_mode": "HTML"},
        {"chat_id": "42", "text": "a & b <c> <d>"},       # entities decoded, no parse_mode
    ]


def test_send_text_non_400_error_is_not_retried_and_fails(monkeypatch):
    for code in (401, 403, 429, 500):
        attempts = []

        def boom(req, timeout=None, _a=attempts, _c=code):
            _a.append(req.data.decode())
            raise _http_error(_c)

        monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
        assert notify.send_text("t", "c", "x < y") is False
        assert len(attempts) == 1 and "parse_mode=HTML" in attempts[0]


def test_send_text_plain_fallback_failure_returns_false(monkeypatch):
    attempts = []

    def boom(req, timeout=None):
        attempts.append(req.data.decode())
        raise _http_error(400)

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send_text("t", "c", "x < y") is False
    assert len(attempts) == 2


def _digest_env(tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    return review, {"TAPO_REVIEW_DIGEST_TIME": "00:00",
                    "TAPO_REVIEW_LOG_DIR": str(review)}


def _write_index(review, records):
    with open(review / "index.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_photo_failure_still_marks_sent(tmp_path):
    review, env = _digest_env(tmp_path)
    now = time.time()
    (review / "a.jpg").write_bytes(b"x")
    _write_index(review, [{"ts": now - 60, "file": "a.jpg", "person": 0.9,
                           "camera": "c", "verdict": "hold"}])

    def bad_photo(*a, **k):
        raise RuntimeError("boom")

    assert reviewdigest.run_if_due(env=env, now=now, send_text=lambda t: True,
                                   send_photo=bad_photo) is True
    assert reviewdigest.last_sent_day(str(review)) is not None


def test_photo_caption_tolerates_missing_event_ts():
    assert "c" in reviewdigest.photo_caption(
        {"camera": "c", "event_ts": None, "ts": time.time()})
    assert reviewdigest.photo_caption({"camera": "c", "event_ts": None}).endswith("?")


def test_mark_sent_is_atomic_and_warns(tmp_path, caplog):
    reviewdigest.mark_sent(str(tmp_path), time.time())
    assert not (tmp_path / (reviewdigest.STATE_NAME + ".tmp")).exists()
    assert reviewdigest.last_sent_day(str(tmp_path)) is not None
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with caplog.at_level("WARNING"):
        reviewdigest.mark_sent(str(blocker), time.time())
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_pick_photos_rejects_paths_outside_review_dir(tmp_path):
    review = tmp_path / "review"
    review.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"x")
    (review / "ok.jpg").write_bytes(b"x")
    entries = [{"file": str(outside), "person": 0.99}, {"file": "../outside.jpg", "person": 0.98},
               {"file": "ok.jpg", "person": 0.5}]
    picked = reviewdigest.pick_photos(entries, str(review), 5)
    assert [e["file"] for e in picked] == ["ok.jpg"]


def test_network_summary_tolerates_small_clock_skew(tmp_path):
    now = time.time()
    path = tmp_path / "summary.json"

    def lines(at):
        path.write_text(json.dumps({"at": at, "window_hours": 24, "targets": []}))
        return reviewdigest.network_context_lines(now, path=str(path))

    assert "no measurement history" in lines(now + 3)[0]
    assert "unavailable" in lines(now + 300)[0]


def test_radio_snapshot_tolerates_small_clock_skew():
    snap = {"at": 1000.0 + 3, "interfaces": []}
    assert reviewdigest.radio_context_lines(snap, 1000.0) == ["Wi-Fi watcher host: unavailable"]
