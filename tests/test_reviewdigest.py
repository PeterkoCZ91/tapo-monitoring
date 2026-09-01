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


def test_expected_fingerprint_strips_and_is_off_when_unset():
    assert reviewdigest.expected_fingerprint_from_env(
        {"TAPO_EXPECTED_FINGERPRINT": " abc123def456 \n"}) == "abc123def456"
    assert reviewdigest.expected_fingerprint_from_env({}) is None
    assert reviewdigest.expected_fingerprint_from_env(
        {"TAPO_EXPECTED_FINGERPRINT": "   "}) is None


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


# ── photo captions ───────────────────────────────────────────────────────────

def test_photo_caption_shadow_prefers_event_ts_over_scan_ts():
    scan_ts = _local_ts(2026, 8, 13, 3, 0)
    event_ts = _local_ts(2026, 8, 13, 13, 45)
    caption = reviewdigest.photo_caption({
        "verdict": "shadow", "camera": "front", "person": 0.81,
        "ts": scan_ts, "event_ts": event_ts})
    assert "13:45" in caption
    assert "03:0" not in caption


def test_photo_caption_hold_without_event_ts_uses_ts_as_before():
    ts = _local_ts(2026, 8, 13, 20, 45)
    caption = reviewdigest.photo_caption({
        "verdict": "hold", "camera": "front", "person": 0.42, "ts": ts})
    assert caption == "hold front p0.42 20:45"


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


def test_run_if_due_includes_the_fleet_health_and_alert_sections(tmp_path):
    # The point of the whole feature: one message a day that says the fleet is alive, not
    # only what it suppressed. Silence used to be the only "all good" signal there was.
    review_dir = str(tmp_path / "review")
    sent_dir = str(tmp_path / "sent")
    os.makedirs(review_dir)
    os.makedirs(sent_dir)
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(review_dir, "e0.jpg", now - 60, "front", 0.64)
    with open(os.path.join(sent_dir, "index.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now - 300, "file": "s.jpg", "camera": "front",
                            "delivered": True}) + "\n")
    env = {"TAPO_REVIEW_DIGEST_TIME": "20:45",
           sentlog.ENV_REVIEW_DIR: review_dir,
           sentlog.ENV_DIR: sent_dir}
    texts = []

    assert reviewdigest.run_if_due(
        env=env, now=now,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: True,
        health={"cameras": {"front": {"reachable": True, "events": True}},
                "tick": {"ok": True}, "scorer": {"ok": True, "requests": 10, "failed": 0,
                                                 "p95": 0.4},
                "recorder": None, "repairs": {}}) is True

    assert len(texts) == 1
    assert "Fleet OK" in texts[0]
    assert "1 sent" in texts[0]
    assert "suppressed frame(s)" in texts[0]


def test_run_if_due_indents_alerts_under_the_fleet_header(tmp_path):
    review_dir = str(tmp_path / "review")
    sent_dir = str(tmp_path / "sent")
    os.makedirs(review_dir)
    os.makedirs(sent_dir)
    now = _local_ts(2026, 8, 13, 21, 0)
    with open(os.path.join(sent_dir, "index.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now - 300, "file": "s.jpg", "camera": "front",
                            "delivered": True}) + "\n")
    env = {"TAPO_REVIEW_DIGEST_TIME": "20:45",
           sentlog.ENV_REVIEW_DIR: review_dir, sentlog.ENV_DIR: sent_dir}
    texts = []

    reviewdigest.run_if_due(
        env=env, now=now, send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: True,
        health={"cameras": {"front": {"reachable": True, "events": True}},
                "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {}})

    assert "\n   alerts 24h: 1 sent" in texts[0]


def test_run_if_due_without_health_keeps_the_old_message(tmp_path):
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(review_dir, "e0.jpg", now - 60, "front", 0.64)
    texts = []

    assert reviewdigest.run_if_due(
        env=_env(tmp_path), now=now,
        send_text=lambda t: texts.append(t) or True,
        send_photo=lambda p, c: True) is True

    assert "Fleet" not in texts[0]


def test_run_if_due_says_when_a_digest_went_out(tmp_path, caplog):
    # Only failures were logged, so a delivered digest left no trace in the journal at all
    # and the only evidence it still worked was the .digest-sent stamp. A telemetry channel
    # that is silent when healthy cannot be distinguished from one that died.
    review_dir = str(tmp_path)
    now = _local_ts(2026, 8, 13, 21, 0)
    _write_entry(review_dir, "e0.jpg", now - 60, "front", 0.64)

    with caplog.at_level("INFO", logger="tapo_monitor.reviewdigest"):
        assert reviewdigest.run_if_due(
            env=_env(tmp_path), now=now,
            send_text=lambda t: True,
            send_photo=lambda p, c: True) is True

    assert any("digest" in message for message in caplog.messages)


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


def test_fleet_lines_reports_ok_and_names_what_it_checked():
    # The whole point of a positive heartbeat: it has to say which subsystems it actually
    # verified, so "OK" cannot be read as covering something nobody looked at.
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True},
                    "yard": {"reachable": True, "events": True}},
        "tick": {"ok": True},
        "scorer": {"ok": True, "requests": 4180, "failed": 0, "p95": 0.73},
        "recorder": {"status": "ok", "age_s": 47.0},
        "repairs": {},
    })
    text = "\n".join(lines)
    assert text.startswith("\U0001f49a Fleet OK")
    assert "front" in text and "yard" in text
    assert "4180" in text and "0 failed" in text
    assert "47s" in text


def test_fleet_lines_never_says_ok_when_a_camera_is_unreachable():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": False, "events": True},
                    "yard": {"reachable": True, "events": True}},
        "tick": {"ok": True},
        "scorer": None, "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert "Fleet OK" not in text
    assert "front" in text and "unreachable" in text


def test_fleet_lines_distinguishes_not_yet_checked_from_unreachable():
    # Before the first control pass a camera's reachability is simply unknown. Calling
    # that "unreachable" cries wolf; hiding it lets "Fleet OK" imply a camera nobody
    # looked at is fine. It has to be named as unchecked.
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True},
                    "yard": {"reachable": None, "events": None}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert "unreachable" not in text
    assert "yard" in text
    assert "not checked" in text


def test_fleet_lines_never_says_ok_when_the_scorer_is_down():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True},
        "scorer": {"ok": False, "error": "ConnectionRefused"},
        "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert "Fleet OK" not in text
    assert "scorer" in text


def test_fleet_lines_stays_silent_about_subsystems_it_could_not_check():
    # A host with no recorder must not get a recorder line at all, the same way the
    # shadow-scan line is omitted there. Claiming nothing beats claiming "ok".
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert "recorder" not in text
    assert "scorer" not in text
    assert text.startswith("\U0001f49a Fleet OK")


def test_fleet_lines_reports_a_camera_refusing_its_self_heal():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None,
        "repairs": {"person_detection": 12},
    })
    text = "\n".join(lines)
    assert "Fleet OK" not in text
    assert "person_detection" in text and "12" in text


def test_fleet_lines_reports_a_stalled_daemon():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": False, "stalled_for": 900.0},
        "scorer": None, "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert "Fleet OK" not in text
    assert "tick" in text or "stalled" in text


def test_fleet_lines_reports_the_running_package_fingerprint():
    # Which code a host runs has twice been established only by manual inventory, after
    # the drift had already cost something. The digest is the one message the fleet sends
    # every day, so it is where the running fingerprint belongs. Without an expectation
    # the line is informational and must not touch the OK headline.
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
        "package": {"running": "abc123def456", "expected": None},
    })
    text = "\n".join(lines)
    assert text.startswith("\U0001f49a Fleet OK")
    assert "package abc123def456" in text


def test_fleet_lines_fails_the_check_on_fingerprint_drift():
    # An expectation that is set and missed is a failed check like any other: it takes
    # the OK headline away and the drift line names running vs expected.
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
        "package": {"running": "abc123def456", "expected": "987fedcba654"},
    })
    text = "\n".join(lines)
    assert "Fleet OK" not in text
    assert "abc123def456" in lines[0]
    assert "987fedcba654" in lines[0]
    assert "expected" in lines[0]


def test_fleet_lines_compares_fingerprints_case_insensitively():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
        "package": {"running": "abc123def456", "expected": "ABC123DEF456"},
    })
    assert lines[0].startswith("\U0001f49a Fleet OK")


def test_fleet_lines_stays_silent_about_an_unknown_fingerprint():
    # Same rule as the recorder and the scorer: a fingerprint nobody computed gets no
    # line, because an invented one would be vouched for by the OK headline.
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
        "package": None,
    })
    assert "package" not in "\n".join(lines)


def test_alert_lines_counts_what_actually_went_out(tmp_path):
    # "Alerts sent" has to come from the sent log, not from a counter that a restart
    # resets: the index is the only durable record of what reached the phone.
    sent = str(tmp_path)
    now = _local_ts(2026, 8, 13, 20, 45)
    rows = [
        {"ts": now - 60, "file": "a.jpg", "camera": "front", "delivered": True},
        {"ts": now - 120, "file": "b.jpg", "camera": "front", "delivered": True},
        {"ts": now - 180, "file": "c.jpg", "camera": "yard", "delivered": False},
        {"ts": now - 40 * 3600, "file": "old.jpg", "camera": "front", "delivered": True},
    ]
    with open(os.path.join(sent, "index.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    lines = reviewdigest.alert_lines(sent, now)
    text = "\n".join(lines)
    assert "2 sent" in text              # the 40h-old one is outside the window
    assert "front 2" in text
    assert "1 undelivered" in text       # a refused send is not a sent alert


def test_alert_lines_without_a_sent_log_is_silent(tmp_path):
    assert reviewdigest.alert_lines(str(tmp_path), 1000.0) == []
    assert reviewdigest.alert_lines(None, 1000.0) == []


def test_scan_context_line_says_when_the_scan_skipped_part_of_the_day(tmp_path):
    # A scan that ran out of decode budget reports the same segment total as a complete
    # one, so the digest read as full coverage while a camera had 74 % of its day
    # unscanned (2026-08-25). The skipped count is the whole point of the summary flag.
    now = _local_ts(2026, 8, 13, 20, 45)
    review = str(tmp_path)
    summary = {"date": "2026-08-12", "generated_at": now - 3600,
               "extract_exhausted": True,
               "cameras": {
                   "front": {"segments": 96, "frames_scored": 700, "observations": 3,
                             "matched": 2, "shadow_only": 1, "segments_skipped": 0},
                   "yard": {"segments": 96, "frames_scored": 99, "observations": 1,
                            "matched": 1, "shadow_only": 0, "segments_skipped": 71}}}
    with open(os.path.join(review, ".shadow-scan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)

    line = reviewdigest.scan_context_line(review, now)
    assert "121 of 192 segments" in line     # covered, not merely present
    assert "71 skipped" in line


def test_scan_context_line_complete_run_does_not_mention_skipping(tmp_path):
    now = _local_ts(2026, 8, 13, 20, 45)
    review = str(tmp_path)
    summary = {"date": "2026-08-12", "generated_at": now - 3600,
               "cameras": {"front": {"segments": 96, "frames_scored": 700,
                                     "observations": 3, "matched": 2,
                                     "shadow_only": 1, "segments_skipped": 0}}}
    with open(os.path.join(review, ".shadow-scan.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)

    line = reviewdigest.scan_context_line(review, now)
    assert "96 of 96 segments" in line
    assert "skipped" not in line


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
