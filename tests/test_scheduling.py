import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import scheduling


def test_parse_hhmm_colon():
    assert scheduling._parse_hhmm("22:30", 22) == 22 * 60 + 30

def test_parse_hhmm_hour_only():
    assert scheduling._parse_hhmm("6", 22) == 6 * 60

def test_parse_hhmm_empty_uses_default():
    assert scheduling._parse_hhmm("", 22) == 22 * 60


def test_hhmm_night_before_morning_end(monkeypatch):
    monkeypatch.setenv("NIGHT_START", "22")
    monkeypatch.setenv("NIGHT_END", "6")
    # 03:00 is night (before 06:00 end)
    assert scheduling._is_night_hhmm(datetime(2026, 1, 1, 3, 0)) is True

def test_hhmm_day_midafternoon(monkeypatch):
    monkeypatch.setenv("NIGHT_START", "22")
    monkeypatch.setenv("NIGHT_END", "6")
    assert scheduling._is_night_hhmm(datetime(2026, 1, 1, 15, 0)) is False

def test_hhmm_night_after_evening_start(monkeypatch):
    monkeypatch.setenv("NIGHT_START", "22")
    monkeypatch.setenv("NIGHT_END", "6")
    assert scheduling._is_night_hhmm(datetime(2026, 1, 1, 23, 0)) is True


def test_is_night_force_hhmm_skips_astral(monkeypatch):
    monkeypatch.setenv("NIGHT_FORCE_HHMM", "1")
    monkeypatch.setenv("NIGHT_START", "22")
    monkeypatch.setenv("NIGHT_END", "6")
    # even with coords set, FORCE_HHMM must use the HH:MM window
    monkeypatch.setenv("NIGHT_LAT", "50.0")
    monkeypatch.setenv("NIGHT_LON", "14.0")
    monkeypatch.setenv("NIGHT_TZ", "Europe/Prague")
    assert scheduling.is_night(datetime(2026, 1, 1, 15, 0)) is False


def test_is_night_falls_back_to_hhmm_without_coords(monkeypatch):
    for var in ("NIGHT_LAT", "NIGHT_LON", "NIGHT_TZ", "NIGHT_FORCE_HHMM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NIGHT_START", "22")
    monkeypatch.setenv("NIGHT_END", "6")
    # no coords -> astral raises -> HH:MM fallback; 15:00 is day
    assert scheduling.is_night(datetime(2026, 1, 1, 15, 0)) is False


def test_parse_clock_window_hhmm():
    assert scheduling.parse_clock_window("00:30-04:30") == (30, 270)


def test_parse_clock_window_rejects_bad_spec():
    with pytest.raises(ValueError):
        scheduling.parse_clock_window("not a window")


def test_in_clock_window_inside_and_outside():
    window = scheduling.parse_clock_window("00:30-04:30")
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 2, 0)) is True
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 0, 0)) is False
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 12, 0)) is False


def test_in_clock_window_boundaries_start_inclusive_end_exclusive():
    window = scheduling.parse_clock_window("00:30-04:30")
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 0, 30)) is True
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 4, 30)) is False


def test_in_clock_window_wraps_past_midnight():
    window = scheduling.parse_clock_window("22:00-05:00")
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 23, 0)) is True
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 2, 0)) is True
    assert scheduling.in_clock_window(window, datetime(2026, 1, 1, 12, 0)) is False


def test_is_night_uses_config_location_before_environment(monkeypatch):
    from tapo_monitor.config import Location

    monkeypatch.delenv("NIGHT_FORCE_HHMM", raising=False)
    monkeypatch.setenv("NIGHT_LAT", "0")
    monkeypatch.setenv("NIGHT_LON", "0")
    monkeypatch.setenv("NIGHT_TZ", "UTC")
    location = Location(lat=50.0, lon=14.0, tz="Europe/Prague")
    assert scheduling.is_night(datetime(2026, 7, 26, 12, 0), location=location) is False
