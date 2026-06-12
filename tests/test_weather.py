import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import weather

# ── parse_precip ─────────────────────────────────────────────────────────────

def test_parse_precip_rain_above_threshold():
    assert weather.parse_precip({"current": {"precipitation": 0.5}}, threshold=0.1) is True

def test_parse_precip_dry_below_threshold():
    assert weather.parse_precip({"current": {"precipitation": 0.0}}, threshold=0.1) is False

def test_parse_precip_boundary_is_rain():
    assert weather.parse_precip({"current": {"precipitation": 0.1}}, threshold=0.1) is True

def test_parse_precip_missing_current():
    assert weather.parse_precip({}, threshold=0.1) is False

def test_parse_precip_uses_showers_when_precip_missing():
    assert weather.parse_precip({"current": {"showers": 0.3}}, threshold=0.1) is True


# ── is_rain_active (hysteresis) ──────────────────────────────────────────────

def test_rain_active_false_when_never_rained():
    assert weather.is_rain_active(now=1000, last_seen=None, clear_delay=1800) is False

def test_rain_active_true_within_clear_delay():
    assert weather.is_rain_active(now=1600, last_seen=1000, clear_delay=1800) is True

def test_rain_active_false_after_clear_delay():
    assert weather.is_rain_active(now=3000, last_seen=1000, clear_delay=1800) is False

def test_rain_active_boundary_is_inactive():
    assert weather.is_rain_active(now=2800, last_seen=1000, clear_delay=1800) is False


# ── is_cache_fresh ───────────────────────────────────────────────────────────

def test_cache_fresh_within_interval():
    assert weather.is_cache_fresh(cache_ts=1000, now=1500, poll_interval=900) is True

def test_cache_fresh_false_after_interval():
    assert weather.is_cache_fresh(cache_ts=1000, now=2000, poll_interval=900) is False

def test_cache_fresh_false_without_cache():
    assert weather.is_cache_fresh(cache_ts=None, now=1500, poll_interval=900) is False


# ── is_raining_now (cache + fallback) ────────────────────────────────────────

def test_raining_now_uses_fresh_cache_without_api(monkeypatch, tmp_path):
    cache = str(tmp_path / "cache")
    weather.write_cache(1000, True, path=cache)
    def boom(*a, **k):
        raise AssertionError("API must not be called while cache is fresh")
    monkeypatch.setattr(weather, "_fetch_weather", boom)
    assert weather.is_raining_now(now=1200, poll_interval=900, cache_path=cache) is True

def test_raining_now_fetches_when_cache_stale(monkeypatch, tmp_path):
    cache = str(tmp_path / "cache")
    weather.write_cache(1000, False, path=cache)
    monkeypatch.setattr(weather, "_fetch_weather",
                        lambda *a, **k: {"current": {"precipitation": 0.4}})
    assert weather.is_raining_now(now=5000, poll_interval=900, cache_path=cache,
                                  lat=50.0, lon=14.0) is True

def test_raining_now_falls_back_to_cache_on_api_error(monkeypatch, tmp_path):
    cache = str(tmp_path / "cache")
    weather.write_cache(1000, True, path=cache)
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(weather, "_fetch_weather", boom)
    assert weather.is_raining_now(now=5000, poll_interval=900, cache_path=cache,
                                  lat=50.0, lon=14.0) is True

def test_raining_now_false_on_error_without_cache(monkeypatch, tmp_path):
    cache = str(tmp_path / "cache")
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(weather, "_fetch_weather", boom)
    assert weather.is_raining_now(now=5000, poll_interval=900, cache_path=cache,
                                  lat=50.0, lon=14.0) is False

def test_raining_now_skips_without_coordinates(monkeypatch, tmp_path):
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(weather, "_fetch_weather",
                        lambda *a, **k: {"current": {"precipitation": 9.9}})
    assert weather.is_raining_now(now=5000, poll_interval=900, cache_path=cache,
                                  lat=None, lon=None) is False
