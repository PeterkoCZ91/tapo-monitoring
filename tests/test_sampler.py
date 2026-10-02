import os
import sys
from dataclasses import replace

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


def test_observe_records_delivered_and_never_clears_it():
    # ``sent`` also covers "queued for the SD follow-up"; only ``delivered`` means a
    # photo actually reached Telegram, and that fact must survive later events.
    groups = {}
    sampler.observe_event(groups, "a", _ev(1000), "motion", True, 1010, CFG)
    assert groups["a"]["delivered"] is False        # sent-but-not-delivered
    sampler.observe_event(groups, "a", _ev(1020), "motion", True, 1030, CFG,
                          delivered=True)
    assert groups["a"]["delivered"] is True
    sampler.observe_event(groups, "a", _ev(1040), "motion", True, 1050, CFG)
    assert groups["a"]["delivered"] is True         # once delivered, stays delivered


def test_ensure_group_starts_undelivered():
    groups = {}
    g = sampler.ensure_group(groups, "a", _ev(1000), "motion", 1010, CFG)
    assert g["delivered"] is False


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


def test_corroborate_hold_records_score_for_expiry_audit():
    g = {}
    assert sampler.corroborate_motion(g, 0.41, 0.3, 0.6) == "hold"
    assert g["last_hold_score"] == 0.41


def test_remember_held_frame_keeps_the_best_scoring_archive():
    g = {}
    sampler.remember_held_frame(g, None, 0.5, 1000)       # review log off: nothing to keep
    assert "hold_path" not in g
    sampler.remember_held_frame(g, "/rl/a.jpg", 0.45, 1000)
    sampler.remember_held_frame(g, "/rl/b.jpg", 0.41, 1030)   # weaker: the first stays
    assert (g["hold_path"], g["hold_at"], g["hold_score"]) == ("/rl/a.jpg", 1000, 0.45)
    sampler.remember_held_frame(g, "/rl/c.jpg", 0.45, 1060)   # a tie goes to the later
    assert (g["hold_path"], g["hold_at"]) == ("/rl/c.jpg", 1060)
    assert g["hold_lens"] is None


def test_remember_held_frame_keeps_the_lens_with_its_frame():
    g = {}
    sampler.remember_held_frame(g, "/rl/a.jpg", 0.45, 1000, lens="pan/tilt")
    sampler.remember_held_frame(g, "/rl/b.jpg", 0.41, 1030, lens="wide")   # weaker
    assert (g["hold_path"], g["hold_lens"]) == ("/rl/a.jpg", "pan/tilt")


def test_held_score_prefers_the_archived_frame():
    assert sampler.held_score({"last_hold_score": 0.41}) == 0.41
    assert sampler.held_score({"last_hold_score": 0.41, "hold_score": 0.52}) == 0.52
    assert sampler.held_score({}) is None


def test_hold_expiry_floor_follows_the_threshold_unless_set():
    assert sampler.hold_expiry_floor(CFG, 0.3) == 0.3
    assert sampler.hold_expiry_floor(replace(CFG, hold_expiry_min_score=0.5), 0.3) == 0.5


def test_ensure_group_creates_then_returns_same():
    groups = {}
    g1 = sampler.ensure_group(groups, "a", _ev(1000), "motion", 1010, CFG)
    g2 = sampler.ensure_group(groups, "a", _ev(1020), "motion", 1030, CFG)
    assert g1 is g2
    assert g1["started"] == 1000 and g1["sent"] is False


# --- V3 loitering -----------------------------------------------------------------

LOITER = replace(CFG, loiter_frames=4)


def _loiter_group():
    groups = {}
    return sampler.ensure_group(groups, "a", _ev(1000), "person", 1000, LOITER)


def _feed(group, presences, scfg=LOITER, start=1030):
    """Feed presence flags one interval apart; return the list of fire decisions."""
    return [sampler.note_loiter(group, p, start + i * scfg.interval, scfg)
            for i, p in enumerate(presences)]


def test_loiter_off_by_default_never_fires():
    g = _loiter_group()
    assert sampler.loiter_enabled(CFG) is False
    assert _feed(g, [True] * 10, scfg=CFG) == [False] * 10
    assert "loiter_streak" not in g


def test_loiter_fires_exactly_on_the_kth_consecutive_person_frame():
    g = _loiter_group()
    assert _feed(g, [True] * 4) == [False, False, False, True]


def test_loiter_repeated_frames_of_the_same_group_send_once():
    # Zero spam: a person who stays for a dozen frames yields exactly one notice.
    g = _loiter_group()
    assert sum(_feed(g, [True] * 12)) == 1


def test_loiter_gap_resets_the_streak():
    g = _loiter_group()
    assert sum(_feed(g, [True, True, True, False, True, True, True])) == 0
    assert sum(_feed(g, [True], start=1300)) == 1       # fourth in the new streak


def test_loiter_does_not_refire_after_a_gap_in_the_same_group():
    g = _loiter_group()
    assert sum(_feed(g, [True] * 4 + [False] + [True] * 6)) == 1


def test_loiter_miss_tolerance_bridges_a_ptz_hiccup():
    scfg = replace(LOITER, loiter_miss_tolerance=1)
    g = _loiter_group()
    fired = _feed(g, [True, True, False, True, True], scfg=scfg)
    assert fired == [False, False, False, False, True]   # the miss is not a frame
    g2 = _loiter_group()
    assert sum(_feed(g2, [True, False, False, True, True, True], scfg=scfg)) == 0


def test_loiter_out_of_zone_person_does_not_count():
    g = _loiter_group()
    fired = [sampler.note_loiter(g, True, 1030 + 30 * i, LOITER, in_zone=False)
             for i in range(6)]
    assert fired == [False] * 6


def test_loiter_minutes_rounds_half_up_and_is_at_least_one():
    g = _loiter_group()
    _feed(g, [True] * 4)
    assert sampler.loiter_minutes(g, 1030 + 90, LOITER) == 2   # 4 frames x 30 s = 2 min
    g2 = _loiter_group()
    sampler.note_loiter(g2, True, 1030, LOITER)
    assert sampler.loiter_minutes(g2, 1030, LOITER) == 1


def test_sent_group_keeps_sampling_while_loiter_watch_is_open():
    g = _loiter_group()
    g["sent"] = True
    assert sampler.due(g, 1040, LOITER) is True
    assert sampler.due(g, 1040, CFG) is False            # legacy: a sent group stops
    _feed(g, [True] * 4)
    assert sampler.due(g, 5000, LOITER) is False         # notice sent: watch is over


def test_loiter_watch_still_honours_the_frame_budget():
    g = _loiter_group()
    g["sent"] = True
    g["frames"] = LOITER.max_frames
    assert sampler.due(g, 5000, LOITER) is False
