import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import panlimit


def test_bounds_from_presets_picks_extremes():
    # Vstup 0.39 (left), Zahrada 0.58, Maximalne 0.61 (right)
    b = panlimit.bounds_from_presets([("1", 0.39), ("2", 0.58), ("3", 0.61)])
    assert b == (0.39, "1", 0.61, "3")


def test_bounds_from_presets_none_when_too_few():
    assert panlimit.bounds_from_presets([("1", 0.39)]) is None
    assert panlimit.bounds_from_presets([("1", None), ("2", None)]) is None


def test_limit_target_recalls_right_bound_on_overshoot():
    b = (0.39, "1", 0.61, "3")
    assert panlimit.limit_target(0.63, b, margin=0.01) == "3"      # past the wall (right)


def test_limit_target_recalls_left_bound():
    b = (0.39, "1", 0.61, "3")
    assert panlimit.limit_target(0.37, b, margin=0.01) == "1"      # past the left edge


def test_limit_target_none_within_bounds_and_margin():
    b = (0.39, "1", 0.61, "3")
    assert panlimit.limit_target(0.58, b, margin=0.01) is None     # mid-range
    assert panlimit.limit_target(0.615, b, margin=0.01) is None    # within margin of max
    assert panlimit.limit_target(0.385, b, margin=0.01) is None    # within margin of min


def test_limit_target_none_without_bounds_or_position():
    assert panlimit.limit_target(0.7, None) is None
    assert panlimit.limit_target(None, (0.39, "1", 0.61, "3")) is None


def test_bounds_window_keeps_a_stray_preset_from_becoming_the_bound():
    # Real presets of a tracking camera, tilt axis: five sit at working heights and one
    # points at the sky. Unfiltered it spans 1.79 of the 2.0 the motor can reach, which
    # bounds nothing. The window drops that one and leaves a bound made of real presets.
    tilts = [("1", -1.0), ("2", -1.0), ("3", -0.253333),
             ("4", -0.866667), ("5", 0.786667), ("6", -1.0)]

    wide = panlimit.bounds_from_presets(tilts)
    assert wide == (-1.0, "1", 0.786667, "5")
    assert wide[2] - wide[0] > 1.7

    windowed = panlimit.bounds_from_presets(tilts, low=-1.0, high=-0.6)
    assert windowed == (-1.0, "1", -0.866667, "4")


def test_bounds_window_that_leaves_one_preset_gives_no_bounds():
    # Better no guard than a guard clamping to a single point it inferred from nothing.
    tilts = [("1", -1.0), ("5", 0.786667)]

    assert panlimit.bounds_from_presets(tilts, low=-1.0, high=-0.6) is None


def test_bounds_window_is_inclusive_at_its_edges():
    tilts = [("1", -1.0), ("2", -0.6), ("3", 0.5)]

    assert panlimit.bounds_from_presets(tilts, low=-1.0, high=-0.6) == (-1.0, "1", -0.6, "2")
