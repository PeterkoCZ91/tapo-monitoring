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


# ── low-score early exit (motion-only groups) ────────────────────────────────

EXIT_CFG = SamplerConfig(enabled=True, interval=30, max_frames=6, group_gap=90,
                         low_score_exit=3, low_score=0.15)


def _motion_group(groups=None, etype="motion"):
    groups = {} if groups is None else groups
    sampler.observe_event(groups, "a", _ev(1000), etype, False, 1010, EXIT_CFG)
    return groups["a"]


def test_consecutive_low_scores_close_motion_group_early():
    g = _motion_group()
    assert sampler.note_score(g, 0.05, EXIT_CFG) is False
    assert sampler.note_score(g, 0.11, EXIT_CFG) is False
    assert sampler.note_score(g, 0.07, EXIT_CFG) is True
    assert sampler.due(g, 1030, EXIT_CFG) is False
    # closed group only lingers group_gap past the last event, not the full window
    assert sampler.expired(g, 1010 + 91, EXIT_CFG) is True


def test_high_score_resets_low_streak():
    g = _motion_group()
    assert sampler.note_score(g, 0.05, EXIT_CFG) is False
    assert sampler.note_score(g, 0.05, EXIT_CFG) is False
    assert sampler.note_score(g, 0.20, EXIT_CFG) is False   # resets the streak
    assert sampler.note_score(g, 0.05, EXIT_CFG) is False
    assert sampler.note_score(g, 0.05, EXIT_CFG) is False
    assert sampler.note_score(g, 0.05, EXIT_CFG) is True


def test_person_group_keeps_sampling_on_low_scores():
    g = _motion_group(etype="person")
    for _ in range(6):
        assert sampler.note_score(g, 0.01, EXIT_CFG) is False
    assert sampler.due(g, 1030, EXIT_CFG) is True


def test_pir_backed_motion_group_keeps_sampling_on_low_scores():
    groups = {}
    event = _ev(1000)
    event["events_1"] = sampler.PIR_BIT
    sampler.observe_event(groups, "a", event, "motion", False, 1010, EXIT_CFG)
    g = groups["a"]
    for _ in range(6):
        assert sampler.note_score(g, 0.01, EXIT_CFG) is False
    assert g["pir_backed"] is True
    assert sampler.due(g, 1030, EXIT_CFG) is True


def test_person_upgrade_reopens_early_exited_group():
    groups = {}
    g = _motion_group(groups)
    for score in (0.05, 0.05, 0.05):
        sampler.note_score(g, score, EXIT_CFG)
    assert sampler.due(g, 1030, EXIT_CFG) is False
    sampler.observe_event(groups, "a", _ev(1040), "person", False, 1050, EXIT_CFG)
    assert groups["a"] is g                       # same burst, same group
    assert sampler.due(g, 1060, EXIT_CFG) is True


def test_low_score_exit_off_by_default():
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", False, 1010, CFG)
    g = groups["a"]
    for _ in range(10):
        assert sampler.note_score(g, 0.01, CFG) is False
    assert sampler.due(g, 1030, CFG) is True


# ── multi-frame corroboration (non-PIR bare motion) ──────────────────────────

def test_corroborate_sends_high_score_immediately():
    g = {"motion_candidates": 0}
    assert sampler.corroborate_motion(g, 0.7, 0.3, 0.6) == "send"
    assert g["motion_candidates"] == 0          # high score doesn't need a candidate


def test_corroborate_holds_first_marginal_then_sends_second():
    g = {}
    assert sampler.corroborate_motion(g, 0.35, 0.3, 0.6) == "hold"
    assert g["motion_candidates"] == 1
    assert sampler.corroborate_motion(g, 0.42, 0.3, 0.6) == "send"
    assert g["motion_candidates"] == 2


def test_corroborate_drops_below_confirm():
    g = {"motion_candidates": 1}
    assert sampler.corroborate_motion(g, 0.2, 0.3, 0.6) == "drop"
    assert g["motion_candidates"] == 1          # a low frame does not reset the count


def test_ensure_group_creates_then_returns_same():
    groups = {}
    g1 = sampler.ensure_group(groups, "a", _ev(1000), "motion", 1010, CFG)
    g2 = sampler.ensure_group(groups, "a", _ev(1020), "motion", 1030, CFG)
    assert g1 is g2
    assert g1["started"] == 1000 and g1["sent"] is False
