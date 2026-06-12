import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import notify


# ── is_empty_scene ───────────────────────────────────────────────────────────

def test_empty_scene_detected():
    assert notify.is_empty_scene("empty scene") is True

def test_empty_scene_with_real_description():
    assert notify.is_empty_scene("a person in a dark jacket walking left") is False

def test_empty_scene_none():
    assert notify.is_empty_scene(None) is False


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
