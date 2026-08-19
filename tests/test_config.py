import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg


def _minimal():
    return {
        "cameras": [
            {"name": "front", "host": "192.0.2.50"},
        ]
    }


# ── valid configs + defaults ─────────────────────────────────────────────────

def test_minimal_config_loads_with_defaults():
    app = cfg.load_config_from_dict(_minimal())
    assert len(app.cameras) == 1
    cam = app.cameras[0]
    assert cam.name == "front"
    assert cam.host == "192.0.2.50"
    # sensible defaults
    assert cam.role == "tracking"
    assert cam.schedule == "astral"
    assert cam.weather.strategy == "none"
    assert cam.weather.motion_normal == 60
    assert cam.weather.motion_rain == 20
    assert cam.detection.strict_people is True
    assert cam.enrich.snapshot == "rtsp"


def test_night_vision_defaults_none_and_parses():
    assert cfg.load_config_from_dict(_minimal()).cameras[0].night_vision is None
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "night_vision": "ir"}]}
    assert cfg.load_config_from_dict(data).cameras[0].night_vision == "ir"


def test_night_vision_rejects_bad_value():
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "night_vision": "sometimes"}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_snapshot_source_defaults_sd():
    assert cfg.load_config_from_dict(_minimal()).cameras[0].snapshot_source == "sd"


def test_snapshot_source_recording_requires_sd_snapshot():
    ok = {"cameras": [{"name": "f", "host": "192.0.2.50",
                       "sd_snapshot": True, "snapshot_source": "recording"}]}
    assert cfg.load_config_from_dict(ok).cameras[0].snapshot_source == "recording"
    bad = {"cameras": [{"name": "f", "host": "192.0.2.50", "snapshot_source": "recording"}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(bad)


def test_snapshot_source_rejects_bad_value():
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "snapshot_source": "cloud"}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_crop_to_subject_defaults_false_and_parses():
    assert cfg.load_config_from_dict(_minimal()).cameras[0].crop_to_subject is False
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "crop_to_subject": True}]}
    assert cfg.load_config_from_dict(data).cameras[0].crop_to_subject is True


def test_scorer_tiles_defaults_one_and_parses():
    assert cfg.load_config_from_dict(_minimal()).cameras[0].scorer.tiles == 1
    data = {"cameras": [{"name": "f", "host": "192.0.2.50",
                         "scorer": {"url": "http://x/score", "tiles": 2}}]}
    assert cfg.load_config_from_dict(data).cameras[0].scorer.tiles == 2


def test_scorer_tiles_rejects_below_one():
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "scorer": {"tiles": 0}}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_pan_limit_defaults_disabled():
    pl = cfg.load_config_from_dict(_minimal()).cameras[0].pan_limit
    assert pl.enabled is False and pl.margin == 0.01 and pl.poll_interval == 6


def test_pan_limit_parses():
    data = {"cameras": [{"name": "f", "host": "192.0.2.50", "pan_limit": {
        "enabled": True, "margin": 0.02, "poll_interval": 8, "onvif_port": 2020,
        "onvif_user_env": "ONVIF_USER", "onvif_password_env": "ONVIF_PASS"}}]}
    pl = cfg.load_config_from_dict(data).cameras[0].pan_limit
    assert pl.enabled is True and pl.margin == 0.02 and pl.poll_interval == 8
    assert pl.onvif_user_env == "ONVIF_USER" and pl.onvif_port == 2020


def test_pan_limit_rejects_bad_poll_interval():
    data = {"cameras": [{"name": "f", "host": "192.0.2.50",
                         "pan_limit": {"poll_interval": 0}}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_multiple_cameras():
    data = {"cameras": [
        {"name": "a", "host": "198.51.100.10"},
        {"name": "b", "host": "198.51.100.11", "role": "static"},
    ]}
    app = cfg.load_config_from_dict(data)
    assert [c.name for c in app.cameras] == ["a", "b"]
    assert app.cameras[1].role == "static"


def test_location_and_weather_parsed():
    data = {
        "location": {"lat": 50.0, "lon": 14.0, "tz": "Europe/Prague"},
        "cameras": [{
            "name": "yard", "host": "198.51.100.12",
            "weather": {"strategy": "lower_sensitivity", "motion_rain": 10},
        }],
    }
    app = cfg.load_config_from_dict(data)
    assert app.location.lat == 50.0
    assert app.cameras[0].weather.strategy == "lower_sensitivity"
    assert app.cameras[0].weather.motion_rain == 10
    assert app.cameras[0].weather.storm_park is False   # opt-in, off by default


def test_storm_park_parsed():
    data = {"cameras": [{
        "name": "yard", "host": "198.51.100.12",
        "weather": {"strategy": "lower_sensitivity", "storm_park": True},
    }]}
    app = cfg.load_config_from_dict(data)
    assert app.cameras[0].weather.storm_park is True


# ── validation errors ────────────────────────────────────────────────────────

def test_no_cameras_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": []})


def test_missing_host_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x"}]})


def test_missing_name_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"host": "198.51.100.10"}]})


def test_invalid_role_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x", "host": "203.0.113.10", "role": "spinning"}]})


def test_invalid_schedule_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{"name": "x", "host": "203.0.113.10", "schedule": "midnight"}]})


def test_invalid_weather_strategy_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "x", "host": "203.0.113.10", "weather": {"strategy": "make_it_rain"}}
        ]})


def test_invalid_detection_source_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "x", "host": "203.0.113.10", "detection": {"sources": ["telepathy"]}}
        ]})


def test_duplicate_camera_names_is_error():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [
            {"name": "dup", "host": "203.0.113.10"},
            {"name": "dup", "host": "203.0.113.11"},
        ]})


def test_error_message_names_the_camera():
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config_from_dict({"cameras": [{"name": "porch", "host": "203.0.113.10", "role": "bad"}]})
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
        "name": "front", "host": "192.0.2.50",
        "user_env": "CAM_USER", "password_env": "CAM_PASSWORD",
        "cloud_password_env": "CAM_CLOUD",
    }]}
    cam = cfg.load_config_from_dict(data).cameras[0]
    assert cam.user_env == "CAM_USER"
    assert cam.password_env == "CAM_PASSWORD"
    assert cam.cloud_password_env == "CAM_CLOUD"


def test_person_sensitivity_defaults_to_none():
    cam = cfg.load_config_from_dict(_minimal()).cameras[0]
    assert cam.person_sensitivity is None


def test_person_sensitivity_round_trips():
    data = {"cameras": [{
        "name": "front", "host": "192.0.2.50", "person_sensitivity": 40,
    }]}
    cam = cfg.load_config_from_dict(data).cameras[0]
    assert cam.person_sensitivity == 40


def test_alerts_defaults_when_absent():
    app = cfg.load_config_from_dict(_minimal())
    assert app.alerts.cooldown == 120
    assert app.alerts.outage_threshold == 900


def test_alerts_override():
    data = {"alerts": {"cooldown": 30, "outage_threshold": 60},
            "cameras": [{"name": "front", "host": "192.0.2.50"}]}
    app = cfg.load_config_from_dict(data)
    assert app.alerts.cooldown == 30
    assert app.alerts.outage_threshold == 60


def test_crop_from_native_requires_crop_to_subject():
    # On its own the flag buys a multi-second native grab per frame and crops nothing.
    data = {"cameras": [{"name": "front", "host": "192.0.2.50", "crop_from_native": True}]}
    with pytest.raises(cfg.ConfigError, match="crop_from_native"):
        cfg.load_config_from_dict(data)


def test_stall_threshold_defaults_and_overrides():
    assert cfg.load_config_from_dict(_minimal()).alerts.stall_threshold == 900
    data = {"alerts": {"stall_threshold": 300},
            "cameras": [{"name": "front", "host": "192.0.2.50"}]}
    assert cfg.load_config_from_dict(data).alerts.stall_threshold == 300


def test_event_watchdog_defaults_and_overrides():
    app = cfg.load_config_from_dict(_minimal())
    assert app.alerts.event_failure_threshold == 300
    assert app.alerts.event_restart_threshold == 900
    assert app.alerts.event_restart_enabled is True
    data = {"alerts": {"event_failure_threshold": 20, "event_restart_threshold": 40,
                         "event_restart_enabled": False},
            "cameras": [{"name": "front", "host": "192.0.2.50"}]}
    app = cfg.load_config_from_dict(data)
    assert app.alerts.event_failure_threshold == 20
    assert app.alerts.event_restart_threshold == 40
    assert app.alerts.event_restart_enabled is False


@pytest.mark.parametrize(
    ("alerts", "message"),
    [({"event_failure_threshold": 0}, "event_failure_threshold"),
     ({"event_failure_threshold": 60, "event_restart_threshold": 30},
      "event_restart_threshold")],
)
def test_event_watchdog_rejects_unsafe_ranges(alerts, message):
    data = {"alerts": alerts, "cameras": [{"name": "front", "host": "192.0.2.50"}]}
    with pytest.raises(cfg.ConfigError, match=message):
        cfg.load_config_from_dict(data)



def test_observability_defaults_are_safe_and_opt_in():
    obs = cfg.load_config_from_dict(_minimal()).observability
    assert obs.digital_twin is False
    assert obs.drift_alerts is False
    assert obs.ledger is False
    assert obs.probe_interval == 900
    assert obs.ledger_retention_days == 30
    assert obs.shadow_match_window == 20


def test_observability_overrides_round_trip():
    data = {
        "observability": {
            "digital_twin": True,
            "probe_interval": 600,
            "drift_alerts": True,
            "ledger": True,
            "ledger_retention_days": 14,
            "shadow_match_window": 12,
        },
        "cameras": [{"name": "front", "host": "192.0.2.50"}],
    }
    obs = cfg.load_config_from_dict(data).observability
    assert obs.digital_twin is True
    assert obs.probe_interval == 600
    assert obs.drift_alerts is True
    assert obs.ledger is True
    assert obs.ledger_retention_days == 14
    assert obs.shadow_match_window == 12


@pytest.mark.parametrize(
    ("key", "value"),
    [("probe_interval", 59), ("ledger_retention_days", 0), ("shadow_match_window", 0)],
)
def test_observability_rejects_unsafe_ranges(key, value):
    data = {
        "observability": {key: value},
        "cameras": [{"name": "front", "host": "192.0.2.50"}],
    }
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


# ── resolve_camera_credentials (monkeypatched env) ───────────────────────────

def _cred_cam(**overrides):
    base = {"name": "front", "host": "192.0.2.50"}
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
            {"name": "x", "host": "203.0.113.10", "rtsp_port": "not-a-number"}
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


# ── sd_snapshot flag ──────────────────────────────────────────────────────────

def test_camera_sd_snapshot_defaults_false():
    app = cfg.load_config_from_dict({"cameras": [{"name": "front", "host": "192.0.2.50"}]})
    assert app.cameras[0].sd_snapshot is False


def test_camera_sd_snapshot_parsed_true():
    app = cfg.load_config_from_dict({
        "cameras": [{"name": "front", "host": "192.0.2.50", "sd_snapshot": True}]})
    assert app.cameras[0].sd_snapshot is True


def test_loop_defaults():
    app = cfg.load_config_from_dict({"cameras": [{"name": "x", "host": "203.0.113.10"}]})
    assert app.loop.event_interval == 4
    assert app.loop.control_interval == 60


def test_loop_overrides():
    app = cfg.load_config_from_dict({
        "cameras": [{"name": "x", "host": "203.0.113.10"}],
        "loop": {"event_interval": 3, "control_interval": 90},
    })
    assert app.loop.event_interval == 3
    assert app.loop.control_interval == 90


def test_camera_config_parses_sd_span_cap():
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_span_cap": 120},
                     {"name": "b", "host": "203.0.113.11"}]})
    assert app.cameras[0].sd_span_cap == 120
    assert app.cameras[1].sd_span_cap is None     # default: package cap decides


def test_camera_config_parses_sd_motion_span_cap():
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10",
                      "sd_motion_span_cap": 48}]})
    assert app.cameras[0].sd_motion_span_cap == 48


def test_camera_config_parses_sd_motion():
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_motion": True},
                     {"name": "b", "host": "203.0.113.11"}]})
    assert app.cameras[0].sd_motion is True
    assert app.cameras[1].sd_motion is False    # opt-in: costs a download per burst


def test_camera_config_parses_sd_jobs_per_tick():
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "sd_jobs_per_tick": 1},
                     {"name": "b", "host": "203.0.113.11"}]})
    assert app.cameras[0].sd_jobs_per_tick == 1
    assert app.cameras[1].sd_jobs_per_tick is None


# ── sampler and scorer config ────────────────────────────────────────────────

def test_sampler_scorer_defaults_off():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    cam = app.cameras[0]
    assert cam.sampler.enabled is False
    assert (cam.sampler.interval, cam.sampler.max_frames, cam.sampler.group_gap) == (30, 6, 90)
    assert cam.sampler.stream is None
    assert cam.sampler.low_score_exit == 0
    assert cam.sampler.low_score == 0.15
    assert cam.scorer.url is None
    assert cam.scorer.threshold == 0.4
    assert cam.scorer.timeout == 10


def test_sampler_scorer_parsed():
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "a", "host": "203.0.113.10",
        "sampler": {"enabled": True, "interval": 20, "max_frames": 4,
                    "group_gap": 60, "stream": "stream1",
                    "low_score_exit": 3, "low_score": 0.2},
        "scorer": {"url": "http://127.0.0.1:8765/score", "threshold": 0.55, "timeout": 5},
    }]})
    cam = app.cameras[0]
    assert cam.sampler.enabled is True
    assert (cam.sampler.interval, cam.sampler.max_frames, cam.sampler.group_gap) == (20, 4, 60)
    assert cam.sampler.stream == "stream1"
    assert cam.sampler.low_score_exit == 3
    assert cam.sampler.low_score == 0.2
    assert cam.scorer.url == "http://127.0.0.1:8765/score"
    assert cam.scorer.threshold == 0.55
    assert cam.scorer.timeout == 5


def test_scorer_threshold_validated():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{
            "name": "a", "host": "203.0.113.10",
            "scorer": {"url": "http://x/score", "threshold": 1.5},
        }]})


def test_sampler_interval_validated():
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict({"cameras": [{
            "name": "a", "host": "203.0.113.10",
            "sampler": {"enabled": True, "interval": 0},
        }]})


def test_night_only_defaults_false():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    assert app.cameras[0].night_only is False


def test_night_only_parsed():
    app = cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10", "night_only": True}]})
    assert app.cameras[0].night_only is True


def test_scorer_motion_send_threshold_defaults_none():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10"}]})
    assert app.cameras[0].scorer.motion_send_threshold is None


def test_scorer_motion_send_threshold_parsed():
    data = {"cameras": [{"name": "a", "host": "203.0.113.10",
                         "scorer": {"url": "http://x/score", "threshold": 0.3,
                                    "motion_send_threshold": 0.6}}]}
    assert cfg.load_config_from_dict(data).cameras[0].scorer.motion_send_threshold == 0.6


def test_scorer_motion_send_threshold_must_exceed_threshold():
    data = {"cameras": [{"name": "a", "host": "203.0.113.10",
                         "scorer": {"threshold": 0.6, "motion_send_threshold": 0.5}}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_camera_rotate_defaults_zero_and_parses():
    assert cfg.load_config_from_dict(
        {"cameras": [{"name": "a", "host": "203.0.113.10"}]}).cameras[0].rotate == 0
    data = {"cameras": [{"name": "a", "host": "203.0.113.10", "rotate": 180}]}
    assert cfg.load_config_from_dict(data).cameras[0].rotate == 180


def test_camera_rotate_rejects_non_quarter_turn():
    data = {"cameras": [{"name": "a", "host": "203.0.113.10", "rotate": 45}]}
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(data)


def test_crop_min_frac_defaults_and_overrides():
    # The floor is a per-camera knob: how far a distant subject may be zoomed depends on
    # how far the scene reaches, which differs per site.
    assert cfg.load_config_from_dict(_minimal()).cameras[0].crop_min_frac == 0.22
    data = {"cameras": [{"name": "front", "host": "192.0.2.50",
                         "crop_to_subject": True, "crop_min_frac": 0.12}]}
    assert cfg.load_config_from_dict(data).cameras[0].crop_min_frac == 0.12


def test_crop_min_frac_rejects_a_value_outside_the_frame():
    for bad in (0, -0.1, 1.5):
        data = {"cameras": [{"name": "front", "host": "192.0.2.50",
                             "crop_to_subject": True, "crop_min_frac": bad}]}
        with pytest.raises(cfg.ConfigError, match="crop_min_frac"):
            cfg.load_config_from_dict(data)


# ── hubpoll: battery cameras whose detections come from the hub ───────────────

def _hubpoll_camera(**overrides):
    cam = {"name": "gate", "host": "192.0.2.50",
           "detection": {"sources": ["hubpoll"]},
           "hub_host": "192.0.2.60", "go2rtc_src": "gate"}
    cam.update(overrides)
    return {"cameras": [cam]}


def test_hubpoll_is_a_valid_detection_source():
    cam = cfg.load_config_from_dict(_hubpoll_camera()).cameras[0]
    assert cam.detection.sources == ["hubpoll"]


def test_hubpoll_camera_parses_hub_fields():
    data = _hubpoll_camera(hub_device_id="ABCDEF0123", hub_device_mac="AABBCCDDEEFF",
                           hub_poll_interval=30)
    cam = cfg.load_config_from_dict(data).cameras[0]
    assert cam.hub_host == "192.0.2.60"
    assert cam.hub_device_id == "ABCDEF0123"
    assert cam.hub_device_mac == "AABBCCDDEEFF"
    assert cam.go2rtc_src == "gate"
    assert cam.hub_poll_interval == 30


def test_hub_fields_absent_by_default_on_a_normal_camera():
    cam = cfg.load_config_from_dict(_minimal()).cameras[0]
    assert cam.hub_host is None
    assert cam.go2rtc_src is None
    # Addressing is discovered from the hub at runtime, so both stay unset until then.
    assert cam.hub_device_id is None
    assert cam.hub_device_mac is None
    assert cam.hub_poll_interval == 20


def test_hubpoll_requires_hub_host():
    # A battery camera has no event source of its own: without the hub there is nothing.
    data = _hubpoll_camera()
    del data["cameras"][0]["hub_host"]
    with pytest.raises(cfg.ConfigError, match="hub_host"):
        cfg.load_config_from_dict(data)


def test_hubpoll_requires_go2rtc_src():
    # These cameras have no usable RTSP; go2rtc is where the alert frame comes from.
    data = _hubpoll_camera()
    del data["cameras"][0]["go2rtc_src"]
    with pytest.raises(cfg.ConfigError, match="go2rtc_src"):
        cfg.load_config_from_dict(data)


def test_hub_poll_interval_must_be_a_positive_integer():
    for bad in (0, -5, "soon"):
        with pytest.raises(cfg.ConfigError, match="hub_poll_interval"):
            cfg.load_config_from_dict(_hubpoll_camera(hub_poll_interval=bad))


def test_hubpoll_rejects_a_rotation_it_cannot_apply():
    # go2rtc hands over a finished JPEG and there is no capture-time filter behind it, so
    # a configured rotation would be silently dropped — the scorer would see it sideways.
    with pytest.raises(cfg.ConfigError, match="rotate"):
        cfg.load_config_from_dict(_hubpoll_camera(rotate=180))


def test_hub_credential_env_names_parse_and_default_to_none():
    assert cfg.load_config_from_dict(_minimal()).cameras[0].hub_user_env is None
    assert cfg.load_config_from_dict(_minimal()).cameras[0].hub_password_env is None
    cam = cfg.load_config_from_dict(
        _hubpoll_camera(hub_user_env="HUB_EMAIL", hub_password_env="HUB_PASS")).cameras[0]
    assert (cam.hub_user_env, cam.hub_password_env) == ("HUB_EMAIL", "HUB_PASS")


def test_resolve_hub_credentials_reads_the_named_env_vars(monkeypatch):
    monkeypatch.setenv("HUB_EMAIL", "account@example.invalid")
    monkeypatch.setenv("HUB_PASS", "s3cret")
    cam = cfg.load_config_from_dict(
        _hubpoll_camera(hub_user_env="HUB_EMAIL", hub_password_env="HUB_PASS")).cameras[0]
    assert cfg.resolve_hub_credentials(cam) == ("account@example.invalid", "s3cret")


def test_resolve_hub_credentials_missing_env_is_empty():
    cam = cfg.load_config_from_dict(_hubpoll_camera()).cameras[0]
    assert cfg.resolve_hub_credentials(cam) == ("", "")


def test_hubpoll_rejects_crop_from_native_it_cannot_honour():
    # Both frame paths hand over a finished JPEG with no native twin attached, so the flag
    # would silently buy nothing while costing the crop its detail.
    with pytest.raises(cfg.ConfigError, match="crop_from_native"):
        cfg.load_config_from_dict(_hubpoll_camera(crop_to_subject=True,
                                                 crop_from_native=True))
