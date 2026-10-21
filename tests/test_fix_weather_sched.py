import importlib
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import scheduling, weather


@pytest.fixture(autouse=True)
def _clean_state():
    weather._fail_until.clear()
    scheduling._warned_env.clear()
    weather._warned_env.clear()
    yield
    weather._fail_until.clear()


# ── weather: outage backoff / stale expiry ───────────────────────────────────

def _failing(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("down")

    monkeypatch.setattr(weather, "_fetch_weather", boom)
    return calls


def test_outage_is_not_retried_every_pass(tmp_path, monkeypatch):
    calls = _failing(monkeypatch)
    cache = str(tmp_path / "c")
    for t in range(1000, 1040, 4):
        weather.is_raining_now(t, cache_path=cache, lat=1.0, lon=1.0)
    assert len(calls) == 1
    weather.is_raining_now(1000 + weather.FAIL_BACKOFF + 1, cache_path=cache, lat=1.0, lon=1.0)
    assert len(calls) == 2


def test_stale_rain_expires_during_outage(tmp_path, monkeypatch):
    _failing(monkeypatch)
    cache = str(tmp_path / "c")
    weather.write_cache(1000, True, cache)
    assert weather.is_raining_now(1000 + 1000, cache_path=cache, lat=1.0, lon=1.0) is True
    assert weather.is_raining_now(1000 + weather.STALE_MAX + 10, cache_path=cache,
                                  lat=1.0, lon=1.0) is False


# ── weather: cache file safety ───────────────────────────────────────────────

def test_cache_write_does_not_follow_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "cache"
    link.symlink_to(victim)
    assert weather.write_cache(5, True, str(link)) is True
    assert victim.read_text() == "keep"
    assert not link.is_symlink()
    assert weather.read_cache(str(link)) == (5.0, True)


def test_cache_read_refuses_symlink(tmp_path):
    real = tmp_path / "real"
    real.write_text("5,1")
    link = tmp_path / "cache"
    link.symlink_to(real)
    assert weather.read_cache(str(link)) == (None, None)


@pytest.mark.parametrize("content", ["garbage", "nan,1", "inf,1", "5,x", "\xff\xfe", ""])
def test_corrupt_cache_tolerated(tmp_path, content):
    p = tmp_path / "cache"
    p.write_bytes(content.encode("latin-1"))
    assert weather.read_cache(str(p)) == (None, None)


def test_cache_write_leaves_no_temp_files(tmp_path):
    weather.write_cache(1, False, str(tmp_path / "c"))
    weather.touch_rain(2, str(tmp_path / "l"))
    assert sorted(os.listdir(tmp_path)) == ["c", "l"]


def test_cache_fifo_does_not_block(tmp_path):
    p = tmp_path / "cache"
    os.mkfifo(p)
    assert weather.read_cache(str(p)) == (None, None)


# ── weather: env parsing at import ───────────────────────────────────────────

def test_bad_env_does_not_crash_import(monkeypatch, capsys):
    monkeypatch.setenv("RAIN_LAT", "north")
    monkeypatch.setenv("RAIN_PRECIP_THRESHOLD", "x")
    monkeypatch.setenv("RAIN_CLEAR_DELAY", "1.5h")
    monkeypatch.setenv("RAIN_POLL_INTERVAL", "")
    try:
        mod = importlib.reload(weather)
        assert mod.LAT is None
        assert mod.PRECIP_THRESHOLD == 0.1
        assert mod.CLEAR_DELAY == 1800
        assert mod.POLL_INTERVAL == 900
        err = capsys.readouterr().err
        assert err.count("RAIN_LAT") == 1
    finally:
        monkeypatch.undo()
        importlib.reload(weather)


# ── scheduling ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["abc", "25:99", "24", "12:60", "-1", "1:2:3"])
def test_parse_hhmm_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        scheduling._parse_hhmm(bad, 0)


def test_invalid_night_env_falls_back_and_logs_once(monkeypatch, capsys):
    monkeypatch.setenv("NIGHT_FORCE_HHMM", "1")
    monkeypatch.setenv("NIGHT_START", "abc")
    monkeypatch.setenv("NIGHT_END", "25:99")
    for _ in range(3):
        assert scheduling.is_night(datetime(2026, 1, 1, 23, 0)) is True
        assert scheduling.is_night(datetime(2026, 1, 1, 12, 0)) is False
    err = capsys.readouterr().err
    assert err.count("NIGHT_START") == 1
    assert err.count("NIGHT_END") == 1


@pytest.mark.parametrize("spec", ["-", " - ", "08:00-", "-08:00", "25:00-26:00", "abc"])
def test_parse_clock_window_invalid(spec):
    with pytest.raises(ValueError):
        scheduling.parse_clock_window(spec)


def test_parse_clock_window_valid_still_works():
    assert scheduling.parse_clock_window("22:00-05:00") == (22 * 60, 5 * 60)
