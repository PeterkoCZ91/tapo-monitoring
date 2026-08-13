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
