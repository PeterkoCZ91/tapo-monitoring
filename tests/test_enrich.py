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


# ── pick_sharpest ────────────────────────────────────────────────────────────

def test_pick_sharpest_returns_highest_score():
    scored = [("a.jpg", 10.0), ("b.jpg", 99.0), ("c.jpg", 50.0)]
    assert enrich.pick_sharpest(scored) == "b.jpg"

def test_pick_sharpest_single():
    assert enrich.pick_sharpest([("only.jpg", 1.0)]) == "only.jpg"

def test_pick_sharpest_empty():
    assert enrich.pick_sharpest([]) is None
