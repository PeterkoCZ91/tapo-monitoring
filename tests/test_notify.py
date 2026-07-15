import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import notify

# ── is_empty_scene ───────────────────────────────────────────────────────────

def test_empty_scene_detected():
    assert notify.is_empty_scene("empty scene") is True

def test_empty_scene_with_real_description():
    assert notify.is_empty_scene("a person in a dark jacket walking left") is False

def test_empty_scene_none_or_blank_is_empty():
    # No description = vision model returned nothing (timeout/failure); we can't confirm
    # a subject, so treat it as empty rather than sending a content-free alert.
    assert notify.is_empty_scene(None) is True
    assert notify.is_empty_scene("") is True
    assert notify.is_empty_scene("   ") is True


# ── build_caption ────────────────────────────────────────────────────────────

def test_caption_minimal():
    assert notify.build_caption("👤", "23:14") == "👤 23:14"

def test_caption_with_description():
    cap = notify.build_caption("👤", "23:14", description="person walking")
    assert '"person walking"' in cap
    assert cap.startswith("👤 23:14")

def test_caption_with_count_and_since():
    cap = notify.build_caption("👤", "23:14", description="x", count=3, minutes_since_last=12)
    assert "detection #3 today" in cap
    assert "last 12 min ago" in cap

def test_caption_with_detail():
    cap = notify.build_caption("👤", "23:14", detail="Jana")
    assert cap.startswith("👤 Jana 23:14")


# ── outage_alert_due ─────────────────────────────────────────────────────────

def test_outage_no_failure():
    assert notify.outage_alert_due(None, 1000.0, False, threshold=900) is False

def test_outage_below_threshold():
    assert notify.outage_alert_due(1000.0, 1100.0, False, threshold=900) is False

def test_outage_above_threshold():
    assert notify.outage_alert_due(1000.0, 1901.0, False, threshold=900) is True

def test_outage_already_alerted_suppressed():
    assert notify.outage_alert_due(1000.0, 5000.0, True, threshold=900) is False


# ── format_duration ──────────────────────────────────────────────────────────

def test_format_duration_compact_two_units():
    assert notify.format_duration(0) == "0s"
    assert notify.format_duration(65) == "1m 5s"
    assert notify.format_duration(3 * 3600 + 61) == "3h 1m"
    assert notify.format_duration(2 * 86400 + 3 * 3600 + 60) == "2d 3h"


# ── should_send_alert (detection cooldown) ───────────────────────────────────

def test_alert_first_ever_allowed():
    assert notify.should_send_alert(None, 1000.0, cooldown=120) is True

def test_alert_within_cooldown_suppressed():
    assert notify.should_send_alert(1000.0, 1050.0, cooldown=120) is False

def test_alert_at_cooldown_boundary_allowed():
    assert notify.should_send_alert(1000.0, 1120.0, cooldown=120) is True

def test_alert_after_cooldown_allowed():
    assert notify.should_send_alert(1000.0, 2000.0, cooldown=120) is True
