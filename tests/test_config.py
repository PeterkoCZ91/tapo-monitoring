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
    # credential env references parsed from the example
    assert app.cameras[0].user_env == "CAM_USER"
    assert app.cameras[0].password_env == "CAM_PASSWORD"
    # RTSP credential env references parsed from the example
    assert app.cameras[0].rtsp_user_env == "CAM_RTSP_USER"
    assert app.cameras[0].rtsp_password_env == "CAM_RTSP_PASSWORD"
    # alerts block parsed
    assert app.alerts.cooldown == 120
    assert app.alerts.outage_threshold == 900


# ── credential env fields ─────────────────────────────────────────────────────

def test_config_without_credential_fields_validates():
    app = cfg.load_config_from_dict(_minimal())
    cam = app.cameras[0]
    assert cam.user_env is None
    assert cam.password_env is None
    assert cam.cloud_password_env is None


def test_config_with_credential_fields_validates():
    data = {"cameras": [{
        "name": "front", "host": "192.168.1.50",
        "user_env": "CAM_USER", "password_env": "CAM_PASSWORD",
        "cloud_password_env": "CAM_CLOUD",
    }]}
    cam = cfg.load_config_from_dict(data).cameras[0]
    assert cam.user_env == "CAM_USER"
    assert cam.password_env == "CAM_PASSWORD"
    assert cam.cloud_password_env == "CAM_CLOUD"


def test_alerts_defaults_when_absent():
    app = cfg.load_config_from_dict(_minimal())
    assert app.alerts.cooldown == 120
    assert app.alerts.outage_threshold == 900


def test_alerts_override():
    data = {"alerts": {"cooldown": 30, "outage_threshold": 60},
            "cameras": [{"name": "front", "host": "192.168.1.50"}]}
    app = cfg.load_config_from_dict(data)
    assert app.alerts.cooldown == 30
    assert app.alerts.outage_threshold == 60


# ── resolve_camera_credentials (monkeypatched env) ───────────────────────────

def _cred_cam(**overrides):
    base = {"name": "front", "host": "192.168.1.50"}
    base.update(overrides)
    return cfg.load_config_from_dict({"cameras": [base]}).cameras[0]


def test_resolve_credentials_present(monkeypatch):
    monkeypatch.setenv("CAM_USER", "admin")
    monkeypatch.setenv("CAM_PASSWORD", "pw")
    monkeypatch.setenv("CAM_CLOUD", "cloudpw")
    cam = _cred_cam(user_env="CAM_USER", password_env="CAM_PASSWORD",
                    cloud_password_env="CAM_CLOUD")
    assert cfg.resolve_camera_credentials(cam) == ("admin", "pw", "cloudpw")


def test_resolve_credentials_missing_is_empty(monkeypatch):
    monkeypatch.delenv("NOPE_USER", raising=False)
    monkeypatch.delenv("NOPE_PW", raising=False)
    cam = _cred_cam(user_env="NOPE_USER", password_env="NOPE_PW")
    assert cfg.resolve_camera_credentials(cam) == ("", "", "")


def test_resolve_credentials_cloud_falls_back_to_password(monkeypatch):
    monkeypatch.setenv("CAM_USER", "admin")
    monkeypatch.setenv("CAM_PASSWORD", "pw")
    monkeypatch.delenv("CAM_CLOUD", raising=False)
    cam = _cred_cam(user_env="CAM_USER", password_env="CAM_PASSWORD",
                    cloud_password_env="CAM_CLOUD")
    assert cfg.resolve_camera_credentials(cam) == ("admin", "pw", "pw")


def test_resolve_credentials_no_env_names(monkeypatch):
    cam = _cred_cam()
    assert cfg.resolve_camera_credentials(cam) == ("", "", "")


# ── RTSP credential / stream fields ──────────────────────────────────────────

def test_rtsp_fields_default_when_absent():
    cam = _cred_cam()
    assert cam.rtsp_user_env is None
    assert cam.rtsp_password_env is None
    assert cam.rtsp_port == 554
    assert cam.rtsp_stream == "stream1"
    assert cam.rtsp_timeout == 15


def test_rtsp_timeout_parsed():
    assert _cred_cam(rtsp_timeout=25).rtsp_timeout == 25


def test_rtsp_fields_parsed():
    cam = _cred_cam(
        rtsp_user_env="CAM_RTSP_USER",
        rtsp_password_env="CAM_RTSP_PASSWORD",
        rtsp_port=8554,
        rtsp_stream="stream2",
    )
    assert cam.rtsp_user_env == "CAM_RTSP_USER"
    assert cam.rtsp_password_env == "CAM_RTSP_PASSWORD"
    assert cam.rtsp_port == 8554
    assert cam.rtsp_stream == "stream2"


def test_rtsp_port_as_string_is_coerced():
    cam = _cred_cam(rtsp_port="8554")
    assert cam.rtsp_port == 8554


def test_invalid_rtsp_port_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "x", "host": "1.1.1.1", "rtsp_port": "not-a-number"}
        ]})


def test_resolve_rtsp_credentials_present(monkeypatch):
    monkeypatch.setenv("CAM_RTSP_USER", "rtspadmin")
    monkeypatch.setenv("CAM_RTSP_PASSWORD", "rtsppw")
    cam = _cred_cam(rtsp_user_env="CAM_RTSP_USER", rtsp_password_env="CAM_RTSP_PASSWORD")
    assert cfg.resolve_rtsp_credentials(cam) == ("rtspadmin", "rtsppw")


def test_resolve_rtsp_credentials_missing_is_empty(monkeypatch):
    monkeypatch.delenv("NOPE_RTSP_USER", raising=False)
    monkeypatch.delenv("NOPE_RTSP_PW", raising=False)
    cam = _cred_cam(rtsp_user_env="NOPE_RTSP_USER", rtsp_password_env="NOPE_RTSP_PW")
    assert cfg.resolve_rtsp_credentials(cam) == ("", "")


def test_resolve_rtsp_credentials_no_env_names():
    cam = _cred_cam()
    assert cfg.resolve_rtsp_credentials(cam) == ("", "")
