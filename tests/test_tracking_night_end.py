"""``tracking.night_end``: an opt-in fixed clock end for the night of auto-tracking."""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg
from tapo_monitor import daemon, scheduling

PRAGUE = ZoneInfo("Europe/Prague")
MORNING_END = 4 * 60 + 30   # 04:30


def _cam(tracking=None, **overrides):
    base = {"name": "c", "host": "203.0.113.10",
            "tracking": {"day_preset": "2", "night_preset": "1", **(tracking or {})}}
    base.update(overrides)
    return cfg.load_config_from_dict({"cameras": [base]}).cameras[0]


def _at(hour, minute=0):
    return datetime(2026, 11, 20, hour, minute)


# ── config ───────────────────────────────────────────────────────────────────

def test_night_end_is_unset_by_default():
    assert _cam().tracking.night_end is None


def test_night_end_parses_to_minutes():
    assert _cam({"night_end": "04:30"}).tracking.night_end == MORNING_END
    assert _cam({"night_end": "00:00"}).tracking.night_end == 0
    assert _cam({"night_end": "23:59"}).tracking.night_end == 23 * 60 + 59


@pytest.mark.parametrize("bad", ["4:30", "24:00", "04:60", "04:30:00", "soon", "", 270, True])
def test_night_end_rejects_anything_but_quoted_hhmm(bad):
    with pytest.raises(cfg.ConfigError, match=r"camera 'c'.*tracking\.night_end.*HH:MM"):
        _cam({"night_end": bad})


def test_unquoted_yaml_clock_time_gets_the_quoting_hint():
    # YAML 1.1 reads an unquoted 14:30 as the sexagesimal integer 870.
    with pytest.raises(cfg.ConfigError, match="quoted"):
        _cam({"night_end": 870})


@pytest.mark.parametrize("schedule", ["always_day", "always_night"])
def test_night_end_needs_the_astral_schedule(schedule):
    with pytest.raises(cfg.ConfigError, match=r"tracking\.night_end.*schedule"):
        _cam({"night_end": "04:30"}, schedule=schedule)


# ── the window (pure) ────────────────────────────────────────────────────────

@pytest.mark.parametrize(("clock", "shared", "expected"), [
    (_at(1), True, True),              # after midnight, still night
    (_at(4, 29), True, True),          # last minute before the end
    (_at(4, 30), True, False),         # the end, although sunrise is hours away
    (_at(7, 0), True, False),          # after the fixed end, before the shared one
    (_at(11, 59), False, False),       # day
    (_at(15, 0), False, False),        # afternoon: the evening start is not fixed
    (_at(17, 0), True, True),          # the evening follows the shared night
    (_at(23, 59), True, True),         # across midnight
    (_at(0, 0), True, True),
])
def test_morning_end(clock, shared, expected):
    assert scheduling.night_with_fixed_end(shared, MORNING_END, clock) is expected


def test_morning_end_later_than_sunrise_keeps_tracking_until_the_clock_says_so():
    # In June the shared night is over by 05:30; the end stays where it was configured.
    assert scheduling.night_with_fixed_end(False, 7 * 60, _at(6, 59)) is True
    assert scheduling.night_with_fixed_end(False, 7 * 60, _at(7, 0)) is False


def test_end_at_midnight_ends_with_the_day():
    assert scheduling.night_with_fixed_end(True, 0, _at(23, 59)) is True
    assert scheduling.night_with_fixed_end(True, 0, _at(0, 0)) is False
    assert scheduling.night_with_fixed_end(True, 0, _at(5, 0)) is False


@pytest.mark.parametrize(("clock", "shared", "expected"), [
    (_at(18, 0), True, True),
    (_at(23, 29), True, True),
    (_at(23, 30), True, False),
    (_at(0, 15), True, False),         # already over last evening
    (_at(5, 0), True, False),
    (_at(15, 0), False, False),
])
def test_evening_end_does_not_cross_midnight(clock, shared, expected):
    assert scheduling.night_with_fixed_end(shared, 23 * 60 + 30, clock) is expected


# ── clock changes ────────────────────────────────────────────────────────────

def test_end_holds_the_wall_clock_on_both_sides_of_the_season():
    winter = datetime(2026, 12, 15, 4, 30, tzinfo=PRAGUE)
    summer = datetime(2026, 6, 15, 4, 30, tzinfo=PRAGUE)
    assert scheduling.night_with_fixed_end(True, MORNING_END, winter) is False
    assert scheduling.night_with_fixed_end(True, MORNING_END, summer) is False


def test_end_inside_the_repeated_hour_does_not_come_back_on():
    """Autumn: 02:00-03:00 happens twice, and an end at 02:30 must not reopen the night."""
    end = 2 * 60 + 30
    first_before = datetime(2026, 10, 25, 2, 15, fold=0, tzinfo=PRAGUE)
    first_after = datetime(2026, 10, 25, 2, 45, fold=0, tzinfo=PRAGUE)
    second_early = datetime(2026, 10, 25, 2, 15, fold=1, tzinfo=PRAGUE)
    assert scheduling.night_with_fixed_end(True, end, first_before) is True
    assert scheduling.night_with_fixed_end(True, end, first_after) is False
    assert scheduling.night_with_fixed_end(True, end, second_early) is False


def test_end_after_the_repeated_hour_is_not_cut_short():
    second_pass = datetime(2026, 10, 25, 2, 15, fold=1, tzinfo=PRAGUE)
    assert scheduling.night_with_fixed_end(True, MORNING_END, second_pass) is True


def test_end_inside_the_skipped_hour_takes_effect_when_the_clock_jumps():
    """Spring: 02:30 does not exist; the first moment after it (03:00) ends the night."""
    end = 2 * 60 + 30
    before = datetime(2027, 3, 28, 1, 59, tzinfo=PRAGUE)
    after = datetime(2027, 3, 28, 3, 0, tzinfo=PRAGUE)
    assert scheduling.night_with_fixed_end(True, end, before) is True
    assert scheduling.night_with_fixed_end(True, end, after) is False


# ── the plan ─────────────────────────────────────────────────────────────────

def test_plan_parks_after_the_fixed_end():
    plan = daemon.plan_camera(_cam({"night_end": "04:30", "back_time": 180}),
                              True, False, _at(5, 0))
    assert plan.autotrack_on is False
    assert plan.preset == "2"
    assert plan.back_time is None


def test_plan_tracks_before_the_fixed_end():
    plan = daemon.plan_camera(_cam({"night_end": "04:30", "back_time": 180}),
                              True, False, _at(4, 0))
    assert plan.autotrack_on is True
    assert plan.preset == "1"
    assert plan.back_time == 180


def test_plan_without_the_option_follows_the_shared_night_at_any_hour():
    for hour in range(24):
        for night in (True, False):
            plan = daemon.plan_camera(_cam(), night, False, _at(hour))
            assert plan.autotrack_on is night
            assert plan.preset == ("1" if night else "2")


def test_static_camera_is_not_changed_by_the_option():
    plan = daemon.plan_camera(_cam({"night_end": "04:30"}, role="static"),
                              False, False, _at(4, 0))
    assert plan.autotrack_on is False
    assert plan.preset == "2"


def test_rain_still_parks_before_the_fixed_end():
    cam = _cam({"night_end": "04:30"}, weather={"storm_park": True})
    plan = daemon.plan_camera(cam, True, True, _at(4, 0))
    assert plan.autotrack_on is False
    assert plan.rain_parked is True


# ── the rest of the night is untouched ───────────────────────────────────────

def test_forced_ir_keeps_the_shared_night():
    cam = _cam({"night_end": "04:30"}, night_vision="ir")
    assert daemon.plan_camera(cam, True, False, _at(5, 0)).night_vision == "on"
    assert daemon.plan_camera(cam, False, False, _at(4, 0)).night_vision == "off"


def test_firmware_lamp_mode_is_not_changed_by_the_option():
    light = {"enabled": True, "mode": "firmware", "window": "00:30-06:00"}
    with_end = daemon.plan_camera(_cam({"night_end": "04:30"}, light_trigger=light),
                                  True, False, _at(5, 0))
    without = daemon.plan_camera(_cam(light_trigger=light), True, False, _at(5, 0))
    assert with_end.night_vision_mode == without.night_vision_mode == "md_night_vision"


def test_scorer_threshold_and_mute_keep_the_shared_night():
    cam = _cam({"night_end": "04:30"}, night_only=True,
               scorer={"url": "http://203.0.113.20:8766/score", "threshold": 0.3,
                       "night_threshold": 0.5, "unscored_at_night": "hold"})
    assert daemon.scorer_threshold(cam, True) == 0.5
    assert daemon.holds_unscored(cam, True) is True
    assert daemon.camera_muted(cam, True, _at(5, 0).timestamp()) is False


def test_control_pass_uses_the_host_clock_across_the_autumn_change(monkeypatch):
    """The real control pass, with the real repeated hour of the host's own clock."""
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    try:
        app = cfg.load_config_from_dict({"cameras": [{
            "name": "c", "host": "203.0.113.10",
            "tracking": {"day_preset": "2", "night_preset": "1", "night_end": "02:30"},
        }]})
        utc = ZoneInfo("UTC")
        expected = [
            (datetime(2026, 10, 25, 0, 15, tzinfo=utc), True),    # 02:15 first pass (CEST)
            (datetime(2026, 10, 25, 0, 45, tzinfo=utc), False),   # 02:45 first pass
            (datetime(2026, 10, 25, 1, 15, tzinfo=utc), False),   # 02:15 second pass (CET)
        ]
        for moment, tracks in expected:
            plans = daemon.run_once(app, now=moment.timestamp(), is_night=lambda: True)
            assert plans["c"].autotrack_on is tracks, moment
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
