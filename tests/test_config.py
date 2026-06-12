import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg


def _minimal():
    return {
        "cameras": [
            {"name": "front", "host": "192.168.1.50"},
        ]
    }


# ── valid configs + defaults ─────────────────────────────────────────────────

def test_minimal_config_loads_with_defaults():
    app = cfg.load_config_from_dict(_minimal())
    assert len(app.cameras) == 1
    cam = app.cameras[0]
    assert cam.name == "front"
    assert cam.host == "192.168.1.50"
    # sensible defaults
    assert cam.role == "tracking"
    assert cam.schedule == "astral"
    assert cam.weather.strategy == "none"
    assert cam.weather.motion_normal == 60
    assert cam.weather.motion_rain == 20
    assert cam.detection.strict_people is True
    assert cam.enrich.snapshot == "rtsp"


def test_multiple_cameras():
    data = {"cameras": [
        {"name": "a", "host": "10.0.0.1"},
        {"name": "b", "host": "10.0.0.2", "role": "static"},
    ]}
    app = cfg.load_config_from_dict(data)
    assert [c.name for c in app.cameras] == ["a", "b"]
    assert app.cameras[1].role == "static"


def test_location_and_weather_parsed():
    data = {
        "location": {"lat": 50.0, "lon": 14.0, "tz": "Europe/Prague"},
        "cameras": [{
            "name": "yard", "host": "10.0.0.3",
            "weather": {"strategy": "lower_sensitivity", "motion_rain": 10},
        }],
    }
    app = cfg.load_config_from_dict(data)
    assert app.location.lat == 50.0
    assert app.cameras[0].weather.strategy == "lower_sensitivity"
    assert app.cameras[0].weather.motion_rain == 10


# ── validation errors ────────────────────────────────────────────────────────

def test_no_cameras_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": []})


def test_missing_host_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x"}]})


def test_missing_name_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"host": "10.0.0.1"}]})


def test_invalid_role_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x", "host": "1.1.1.1", "role": "spinning"}]})


def test_invalid_schedule_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x", "host": "1.1.1.1", "schedule": "midnight"}]})


def test_invalid_weather_strategy_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "x", "host": "1.1.1.1", "weather": {"strategy": "make_it_rain"}}
        ]})


def test_invalid_detection_source_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "x", "host": "1.1.1.1", "detection": {"sources": ["telepathy"]}}
        ]})


def test_duplicate_camera_names_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "dup", "host": "1.1.1.1"},
            {"name": "dup", "host": "1.1.1.2"},
        ]})


def test_error_message_names_the_camera():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config_from_dict({"cameras": [{"name": "porch", "host": "1.1.1.1", "role": "bad"}]})
    assert "porch" in str(exc.value)


def test_shipped_example_config_is_valid():
    example = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cameras.example.yaml"
    )
    app = cfg.load_config(example)
    assert [c.name for c in app.cameras] == ["front", "yard"]
    assert app.telegram["token_env"] == "TELEGRAM_TOKEN"
