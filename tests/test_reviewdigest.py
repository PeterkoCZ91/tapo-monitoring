import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import reviewdigest, sentlog


def _local_ts(year, month, day, hour, minute, second=0):
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def _write_entry(review_dir, name, ts, camera, person, body=b"\xff\xd8JPG"):
    os.makedirs(review_dir, exist_ok=True)
    with open(os.path.join(review_dir, name), "wb") as f:
        f.write(body)
    record = {"ts": ts, "file": name, "camera": camera, "verdict": "hold",
              "etype": "motion", "person": person, "animal": 0.0}
    with open(os.path.join(review_dir, sentlog.INDEX_NAME), "a", encoding="utf-8") as idx:
        idx.write(json.dumps(record) + "\n")


# ── env parsing ──────────────────────────────────────────────────────────────

def test_digest_time_parses_hh_mm():
    assert reviewdigest.digest_time_from_env({"TAPO_REVIEW_DIGEST_TIME": "20:45"}) == (20, 45)


def test_digest_time_missing_or_garbage_is_off():
    assert reviewdigest.digest_time_from_env({}) is None
    assert reviewdigest.digest_time_from_env({"TAPO_REVIEW_DIGEST_TIME": ""}) is None
    assert reviewdigest.digest_time_from_env({"TAPO_REVIEW_DIGEST_TIME": "later"}) is None
    assert reviewdigest.digest_time_from_env({"TAPO_REVIEW_DIGEST_TIME": "25:00"}) is None


def test_max_photos_default_and_override():
    assert reviewdigest.max_photos_from_env({}) == reviewdigest.DEFAULT_MAX_PHOTOS
    assert reviewdigest.max_photos_from_env({"TAPO_REVIEW_DIGEST_MAX_PHOTOS": "2"}) == 2
    assert reviewdigest.max_photos_from_env(
        {"TAPO_REVIEW_DIGEST_MAX_PHOTOS": "junk"}) == reviewdigest.DEFAULT_MAX_PHOTOS


# ── due / sent-state ─────────────────────────────────────────────────────────

def test_due_only_after_configured_time():
    before = _local_ts(2026, 8, 13, 20, 44)
    after = _local_ts(2026, 8, 13, 20, 45)
    assert reviewdigest.due(before, (20, 45), None) is False
    assert reviewdigest.due(after, (20, 45), None) is True


def test_due_not_twice_the_same_day():
    now = _local_ts(2026, 8, 13, 21, 0)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    assert reviewdigest.due(now, (20, 45), today) is False
    assert reviewdigest.due(now, (20, 45), "2026-08-12") is True


def test_mark_sent_roundtrip(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    assert reviewdigest.last_sent_day(review_dir) is None
    reviewdigest.mark_sent(review_dir, now)
    assert reviewdigest.last_sent_day(review_dir) == "2026-08-13"


# ── collecting entries ───────────────────────────────────────────────────────

def test_collect_keeps_last_window_only(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(review_dir, "old.jpg", now - 90000, "front", 0.31)
    _write_entry(review_dir, "new.jpg", now - 3600, "front", 0.64)
    entries = reviewdigest.collect(review_dir, now)
    assert [e["file"] for e in entries] == ["new.jpg"]


def test_collect_skips_garbage_lines_and_missing_index(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    assert reviewdigest.collect(review_dir, now) == []
    _write_entry(review_dir, "a.jpg", now - 60, "front", 0.59)
    with open(os.path.join(review_dir, sentlog.INDEX_NAME), "a", encoding="utf-8") as idx:
        idx.write("not json\n")
        idx.write(json.dumps({"file": "no-ts.jpg"}) + "\n")
    entries = reviewdigest.collect(review_dir, now)
    assert [e["file"] for e in entries] == ["a.jpg"]


# ── summary + photo selection ────────────────────────────────────────────────

def test_summary_counts_per_camera_with_max_score():
    entries = [
        {"ts": 1.0, "file": "a.jpg", "camera": "front", "person": 0.64},
        {"ts": 2.0, "file": "b.jpg", "camera": "front", "person": 0.59},
        {"ts": 3.0, "file": "c.jpg", "camera": "yard", "person": 0.55},
    ]
    text = reviewdigest.build_summary(entries)
    assert "3 suppressed frame(s)" in text
    assert "front: 2 (max p0.64)" in text
    assert "yard: 1 (max p0.55)" in text


def test_summary_when_quiet_says_so():
    text = reviewdigest.build_summary([])
    assert "no suppressed frames" in text


def test_summary_breaks_out_shadow_verdict():
    entries = [
        {"ts": 1.0, "file": "a.jpg", "camera": "front", "person": 0.64},
        {"ts": 2.0, "file": "b.jpg", "camera": "front", "person": 0.81,
         "verdict": "shadow"},
    ]
    text = reviewdigest.build_summary(entries)
    assert "1 suppressed frame(s)" in text            # holds counted without shadow
    assert "shadow: 1 miss candidate(s)" in text
    assert "front: 1 (max p0.81)" in text


def test_pick_photos_highest_scores_capped_existing_only(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(review_dir, "low.jpg", now - 300, "front", 0.31)
    _write_entry(review_dir, "top.jpg", now - 200, "front", 0.64)
    _write_entry(review_dir, "mid.jpg", now - 100, "yard", 0.55)
    entries = reviewdigest.collect(review_dir, now)
    entries.append({"ts": now, "file": "gone.jpg", "camera": "front", "person": 0.99})
    picked = reviewdigest.pick_photos(entries, review_dir, limit=2)
    assert [e["file"] for e in picked] == ["top.jpg", "mid.jpg"]


# ── orchestration ────────────────────────────────────────────────────────────

def _env(tmp_path, hhmm="20:45"):
    return {"TAPO_REVIEW_LOG_DIR": str(tmp_path), "TAPO_REVIEW_DIGEST_TIME": hhmm}


def test_run_if_due_off_without_config(tmp_path):
    sent = []
    assert reviewdigest.run_if_due(
        env={}, now=_local_ts(2026, 8, 13, 21, 0),
        send_text=lambda t: sent.append(t) or True,
        send_photo=lambda p, c: True) is False
    assert sent == []


def test_run_if_due_sends_text_and_capped_photos_once(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    for i, score in enumerate((0.64, 0.59, 0.55, 0.41, 0.33)):
        _write_entry(review_dir, f"e{i}.jpg", now - 60 * (i + 1), "front", score)
    texts, photos = [], []
    env = dict(_env(tmp_path), TAPO_REVIEW_DIGEST_MAX_PHOTOS="3")

    assert reviewdigest.run_if_due(
        env=env, now=now,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: photos.append((p, c)) or True) is True
    assert len(texts) == 1
    assert "5 suppressed frame(s)" in texts[0]
    assert len(photos) == 3
    assert photos[0][0] == os.path.join(review_dir, "e0.jpg")
    assert "p0.64" in photos[0][1]

    # Same day again: already sent, nothing more goes out.
    assert reviewdigest.run_if_due(
        env=env, now=now + 600,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: photos.append((p, c)) or True) is False
    assert len(texts) == 1 and len(photos) == 3


def test_run_if_due_failed_text_send_retries_next_tick(tmp_path):
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(str(tmp_path), "a.jpg", now - 60, "front", 0.59)
    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now,
        send_text=lambda t: False, send_photo=lambda p, c: True) is False
    sent = []
    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now + 4,
        send_text=lambda t: sent.append(t) or True,
        send_photo=lambda p, c: True) is True
    assert len(sent) == 1


def test_run_if_due_never_raises(tmp_path):
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(str(tmp_path), "a.jpg", now - 60, "front", 0.59)

    def boom(_t):
        raise RuntimeError("telegram exploded")

    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now,
        send_text=boom, send_photo=lambda p, c: True) is False


# ── shadow-scan context line ─────────────────────────────────────────────────

def test_scan_context_line_absent_fresh_and_stale(tmp_path):
    now = _local_ts(2026, 8, 13, 20, 45)
    review = str(tmp_path)
    assert reviewdigest.scan_context_line(review, now) is None
    summary = {"date": "2026-08-12", "generated_at": now - 3600,
               "cameras": {"front": {"segments": 96, "frames_scored": 700,
                                      "observations": 3, "matched": 2,
                                      "shadow_only": 1}}}
    with open(os.path.join(review, ".shadow-scan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)
    line = reviewdigest.scan_context_line(review, now)
    assert "2026-08-12" in line and "96 segments" in line
    assert "2 matched" in line and "1 candidate(s)" in line
    summary["generated_at"] = now - 40 * 3600
    with open(os.path.join(review, ".shadow-scan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)
    assert reviewdigest.scan_context_line(review, now) == "shadow scan: no recent run"


def test_run_if_due_appends_scan_context(tmp_path):
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(str(tmp_path), "a.jpg", now - 60, "front", 0.59)
    with open(os.path.join(str(tmp_path), ".shadow-scan.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": "2026-08-12", "generated_at": now - 3600,
                   "cameras": {}}, f)
    texts = []
    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: True) is True
    assert "shadow scan 2026-08-12" in texts[0]


def test_scan_context_line_malformed_cameras_returns_none(tmp_path):
    now = _local_ts(2026, 8, 13, 20, 45)
    review = str(tmp_path)
    summary = {"date": "2026-08-12", "generated_at": now - 3600,
               "cameras": ["not", "a", "dict"]}
    with open(os.path.join(review, ".shadow-scan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)
    assert reviewdigest.scan_context_line(review, now) is None


def test_run_if_due_delivers_digest_despite_malformed_scan_summary(tmp_path):
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(str(tmp_path), "a.jpg", now - 60, "front", 0.59)
    with open(os.path.join(str(tmp_path), ".shadow-scan.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": "2026-08-12", "generated_at": now - 3600,
                   "cameras": ["not", "a", "dict"]}, f)
    texts = []
    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: True) is True
    assert len(texts) == 1
    assert "1 suppressed frame(s)" in texts[0]
