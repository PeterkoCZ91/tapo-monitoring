"""Configuration model for tapo_monitor.

A single ``cameras.yaml`` drives the whole stack: shared location/secrets plus a list
of cameras, each opting into capabilities (detection sources, tracking, scheduling,
weather gating, enrichment, coordination). Parsing is pure and fully validated so the
daemon never starts with a malformed config.

No personal data lives here — coordinates and secrets come from the config file the
operator keeps outside the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ROLES = {"tracking", "static"}
SCHEDULES = {"astral", "always_night", "always_day"}
WEATHER_STRATEGIES = {"none", "disable_tracking", "lower_sensitivity"}
DETECTION_SOURCES = {"onvif", "getevents", "motion"}
SMARTTRACK_KINDS = {"people", "vehicle", "pet", "baby"}
SNAPSHOT_SOURCES = {"rtsp", "sd"}


class ConfigError(ValueError):
    """Raised when a configuration is malformed."""


@dataclass
class Location:
    lat: float | None = None
    lon: float | None = None
    tz: str | None = None


@dataclass
class WeatherConfig:
    strategy: str = "none"
    motion_normal: int = 60
    motion_rain: int = 20
    precip_threshold: float = 0.1
    clear_delay: int = 1800
    poll_interval: int = 900


@dataclass
class DetectionConfig:
    sources: list[str] = field(default_factory=lambda: ["getevents"])
    strict_people: bool = True


@dataclass
class TrackingConfig:
    smarttrack: list[str] = field(default_factory=lambda: ["people"])
    day_preset: str | None = "2"
    night_preset: str | None = None


@dataclass
class EnrichConfig:
    snapshot: str = "rtsp"
    groq: bool = True


@dataclass
class CoordinatorConfig:
    group: str | None = None
    handoff_preset: str | None = None


@dataclass
class CameraConfig:
    name: str
    host: str
    role: str = "tracking"
    schedule: str = "astral"
    # Names of env vars holding the camera credentials (never the secrets themselves).
    user_env: str | None = None
    password_env: str | None = None
    cloud_password_env: str | None = None
    # RTSP/ONVIF credentials are often a separate account from the pytapo login, so
    # they get their own env-var-name fields. Values below name env vars, not secrets.
    rtsp_user_env: str | None = None
    rtsp_password_env: str | None = None
    rtsp_port: int = 554
    rtsp_stream: str = "stream1"
    rtsp_timeout: int = 15  # seconds; slow cameras/Pis need >8s for the first keyframe
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    enrich: EnrichConfig = field(default_factory=EnrichConfig)
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)


@dataclass
class AlertsConfig:
    cooldown: int = 120         # min seconds between detection alerts per camera
    outage_threshold: int = 900  # seconds a camera must be unreachable before alerting


@dataclass
class AppConfig:
    location: Location = field(default_factory=Location)
    telegram: dict = field(default_factory=dict)
    groq: dict = field(default_factory=dict)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    cameras: list[CameraConfig] = field(default_factory=list)


def resolve_camera_credentials(cfg: CameraConfig):
    """Read (user, password, cloud_password) from the env vars named in ``cfg``.

    Pure-ish (env read only). Missing/unset env vars resolve to "" so callers never
    crash. ``cloud_password`` falls back to ``password`` when its env var is unset.
    """
    def env(name):
        return os.environ.get(name, "") if name else ""

    user = env(cfg.user_env)
    password = env(cfg.password_env)
    cloud = env(cfg.cloud_password_env) or password
    return user, password, cloud


def resolve_rtsp_credentials(cfg: CameraConfig):
    """Read (user, password) for RTSP from the env vars named in ``cfg``.

    Pure-ish (env read only). Missing/unset env vars resolve to "" so callers never
    crash. These are independent from the pytapo login credentials.
    """
    def env(name):
        return os.environ.get(name, "") if name else ""

    return env(cfg.rtsp_user_env), env(cfg.rtsp_password_env)


def _require(mapping, key, where):
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field '{key}'")
    return mapping[key]


def _check_enum(value, allowed, key, where):
    if value not in allowed:
        opts = ", ".join(sorted(allowed))
        raise ConfigError(f"{where}: '{key}' must be one of [{opts}], got {value!r}")
    return value


def _check_subset(values, allowed, key, where):
    if not isinstance(values, list):
        raise ConfigError(f"{where}: '{key}' must be a list")
    bad = [v for v in values if v not in allowed]
    if bad:
        opts = ", ".join(sorted(allowed))
        raise ConfigError(f"{where}: '{key}' has invalid {bad}; allowed: [{opts}]")
    return values


def _weather(data, where):
    d = data or {}
    strategy = _check_enum(d.get("strategy", "none"), WEATHER_STRATEGIES, "weather.strategy", where)
    return WeatherConfig(
        strategy=strategy,
        motion_normal=int(d.get("motion_normal", 60)),
        motion_rain=int(d.get("motion_rain", 20)),
        precip_threshold=float(d.get("precip_threshold", 0.1)),
        clear_delay=int(d.get("clear_delay", 1800)),
        poll_interval=int(d.get("poll_interval", 900)),
    )


def _detection(data, where):
    d = data or {}
    sources = d.get("sources", ["getevents"])
    _check_subset(sources, DETECTION_SOURCES, "detection.sources", where)
    return DetectionConfig(sources=list(sources), strict_people=bool(d.get("strict_people", True)))


def _tracking(data, where):
    d = data or {}
    smarttrack = d.get("smarttrack", ["people"])
    _check_subset(smarttrack, SMARTTRACK_KINDS, "tracking.smarttrack", where)
    return TrackingConfig(
        smarttrack=list(smarttrack),
        day_preset=d.get("day_preset", "2"),
        night_preset=d.get("night_preset"),
    )


def _enrich(data, where):
    d = data or {}
    snapshot = _check_enum(d.get("snapshot", "rtsp"), SNAPSHOT_SOURCES, "enrich.snapshot", where)
    return EnrichConfig(snapshot=snapshot, groq=bool(d.get("groq", True)))


def _camera(data, index):
    if not isinstance(data, dict):
        raise ConfigError(f"cameras[{index}]: must be a mapping")
    name = _require(data, "name", f"cameras[{index}]")
    where = f"camera {name!r}"
    host = _require(data, "host", where)
    role = _check_enum(data.get("role", "tracking"), ROLES, "role", where)
    schedule = _check_enum(data.get("schedule", "astral"), SCHEDULES, "schedule", where)
    coord = data.get("coordinator") or {}
    try:
        rtsp_port = int(data.get("rtsp_port", 554))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: 'rtsp_port' must be an integer") from None
    try:
        rtsp_timeout = int(data.get("rtsp_timeout", 15))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: 'rtsp_timeout' must be an integer") from None
    return CameraConfig(
        name=name,
        host=host,
        role=role,
        schedule=schedule,
        user_env=data.get("user_env"),
        password_env=data.get("password_env"),
        cloud_password_env=data.get("cloud_password_env"),
        rtsp_user_env=data.get("rtsp_user_env"),
        rtsp_password_env=data.get("rtsp_password_env"),
        rtsp_port=rtsp_port,
        rtsp_stream=data.get("rtsp_stream", "stream1"),
        rtsp_timeout=rtsp_timeout,
        detection=_detection(data.get("detection"), where),
        tracking=_tracking(data.get("tracking"), where),
        weather=_weather(data.get("weather"), where),
        enrich=_enrich(data.get("enrich"), where),
        coordinator=CoordinatorConfig(
            group=coord.get("group"),
            handoff_preset=coord.get("handoff_preset"),
        ),
    )


def load_config_from_dict(data) -> AppConfig:
    """Validate a parsed config mapping and return an AppConfig. Pure (no I/O)."""
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    raw_cameras = data.get("cameras")
    if not raw_cameras or not isinstance(raw_cameras, list):
        raise ConfigError("config must define a non-empty 'cameras' list")

    cameras = [_camera(c, i) for i, c in enumerate(raw_cameras)]

    seen = set()
    for cam in cameras:
        if cam.name in seen:
            raise ConfigError(f"duplicate camera name {cam.name!r}")
        seen.add(cam.name)

    loc = data.get("location") or {}
    location = Location(lat=loc.get("lat"), lon=loc.get("lon"), tz=loc.get("tz"))

    alerts_raw = data.get("alerts") or {}
    alerts = AlertsConfig(
        cooldown=int(alerts_raw.get("cooldown", 120)),
        outage_threshold=int(alerts_raw.get("outage_threshold", 900)),
    )

    return AppConfig(
        location=location,
        telegram=data.get("telegram") or {},
        groq=data.get("groq") or {},
        alerts=alerts,
        cameras=cameras,
    )


def load_config(path) -> AppConfig:
    """Load and validate a YAML config file."""
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    return load_config_from_dict(data)
