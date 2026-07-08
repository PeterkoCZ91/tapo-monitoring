import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import sampler
from tapo_monitor.config import SamplerConfig

CFG = SamplerConfig(enabled=True, interval=30, max_frames=6, group_gap=90)


def _ev(t):
    return {"start_time": t}


def test_observe_creates_group():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    g = groups["a"]
    assert g["started"] == 1000
    assert g["etype"] == "motion"
    assert g["frames"] == 0
    assert g["next_due"] == 1000 + 30
    assert g["sent"] is False


def test_observe_extends_group_and_upgrades_type():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    sampler.observe_event(groups, "a", _ev(1050), "person", True, 1060, CFG)
    g = groups["a"]
    assert g["started"] == 1000            # same group
    assert g["etype"] == "person"          # confirmed upgrade sticks
    assert g["event"]["start_time"] == 1050
    assert g["last_event_at"] == 1060
    assert g["sent"] is True               # once sent, stays sent


def test_observe_after_gap_starts_new_group():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", True, 1010, CFG)
    sampler.observe_event(groups, "a", _ev(1200), "motion", False, 1210, CFG)  # 200s later
    g = groups["a"]
    assert g["started"] == 1200
    assert g["sent"] is False


def test_due_and_record_grab_schedule():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    g = groups["a"]
    assert sampler.due(g, 1029, CFG) is False
    assert sampler.due(g, 1030, CFG) is True
    sampler.record_grab(g, 1032, CFG)
    assert g["frames"] == 1
    assert g["next_due"] == 1032 + 30
    assert sampler.due(g, 1040, CFG) is False


def test_due_false_when_sent_or_exhausted():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    g = groups["a"]
    g["sent"] = True
    assert sampler.due(g, 1030, CFG) is False
    g["sent"] = False
    g["frames"] = 6
    assert sampler.due(g, 1030, CFG) is False


def test_expiry_covers_full_sampling_window_without_new_events():
    # The live miss: one event at t=0, person at +172s. Window = 6*30 = 180s, so the
    # group must stay alive past +172 even though no further event arrived.
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    g = groups["a"]
    assert sampler.expired(g, 1010 + 172, CFG) is False
    assert sampler.expired(g, 1000 + 180 + 91, CFG) is True


def test_expiry_sent_group_dies_after_gap():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "person", True, 1010, CFG)
    g = groups["a"]
    assert sampler.expired(g, 1010 + 89, CFG) is False
    assert sampler.expired(g, 1010 + 91, CFG) is True
