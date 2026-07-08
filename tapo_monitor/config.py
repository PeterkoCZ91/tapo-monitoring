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
    storm_park: bool = False


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
class SamplerConfig:
    """Follow-up frame sampling across one event group. Off by default."""
    enabled: bool = False
    interval: int = 30      # seconds between follow-up grabs
    max_frames: int = 6     # follow-up grabs per group (interval*max_frames = window)
    group_gap: int = 90     # events closer than this belong to the same group
    stream: str | None = None  # RTSP stream for follow-up grabs; None = camera default


@dataclass
class ScorerConfig:
    """Local object-detection scoring service. Disabled when url is None."""
    url: str | None = None
    threshold: float = 0.4  # min subject confidence to send a frame
    timeout: int = 10       # seconds per scoring request


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
    sd_snapshot: bool = False  # pull the event-time frame from SD instead of live RTSP
    # Optional per-camera ceiling (seconds) for the SD follow-up window. None uses the
    # package default (Pi Zero-safe). Camera events run ~2 min; hardware that can afford
    # the download (Pi 4) may raise this to scan the whole event for the subject.
    sd_span_cap: int | None = None
    # Opt-in SD follow-up for PIR-backed bare motion whose live frame was empty (people
    # the camera never confirmed as person). Costs an SD download per motion burst, so
    # keep it off on weak hardware; never sends without a subject-bearing frame.
    sd_motion: bool = False
    # Optional per-camera backpressure for slow hosts: process at most this many due
    # SD follow-ups for this camera per daemon loop. None drains all due work.
    sd_jobs_per_tick: int | None = None
    # Optional AI person-detection sensitivity (0-100) re-asserted every control tick.
    # None leaves the camera's value unchanged; lower = fewer false AI-person detections.
    person_sensitivity: int | None = None
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    enrich: EnrichConfig = field(default_factory=EnrichConfig)
    sampler: SamplerConfig = field(default_factory=SamplerConfig)
    scorer: ScorerConfig = field(default_factory=ScorerConfig)
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)


@dataclass
class AlertsConfig:
    cooldown: int = 120         # min seconds between detection alerts per camera
    outage_threshold: int = 900  # seconds a camera must be unreachable before alerting


@dataclass
class LoopConfig:
    # How often to poll getEvents for new detections. Kept small (seconds) so the live
    # snapshot is taken while the person is still in frame — the wide control interval
    # used to mean the snapshot was a near-minute-stale empty scene.
    event_interval: int = 4
    # How often to reconnect and re-apply camera control (tracking/sensitivity/preset).
    # Wide on purpose: these change slowly and frequent re-logins risk the C560WS lockout.
    control_interval: int = 60


@dataclass
class AppConfig:
    location: Location = field(default_factory=Location)
    telegram: dict = field(default_factory=dict)
    groq: dict = field(default_factory=dict)
    # Optional face-id labelling. ``names_env`` names an env var holding
    # "face_id:name,face_id:name"; absent it, recognized faces show as "unknown face".
    faces: dict = field(default_factory=dict)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
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
        storm_park=bool(d.get("storm_park", False)),
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


def _sampler(data, where):
    d = data or {}
    try:
        interval = int(d.get("interval", 30))
        max_frames = int(d.get("max_frames", 6))
        group_gap = int(d.get("group_gap", 90))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: sampler interval/max_frames/group_gap must be integers") from None
    if interval < 1 or max_frames < 1 or group_gap < 1:
        raise ConfigError(f"{where}: sampler interval/max_frames/group_gap must be >= 1")
    return SamplerConfig(
        enabled=bool(d.get("enabled", False)),
        interval=interval,
        max_frames=max_frames,
        group_gap=group_gap,
        stream=d.get("stream"),
    )


def _scorer(data, where):
    d = data or {}
    try:
        threshold = float(d.get("threshold", 0.4))
        timeout = int(d.get("timeout", 10))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: scorer threshold/timeout must be numbers") from None
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError(f"{where}: scorer threshold must be between 0 and 1")
    return ScorerConfig(url=d.get("url"), threshold=threshold, timeout=timeout)


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
    sd_jobs_per_tick = None
    if data.get("sd_jobs_per_tick") is not None:
        try:
            sd_jobs_per_tick = int(data["sd_jobs_per_tick"])
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: 'sd_jobs_per_tick' must be an integer") from None
        if sd_jobs_per_tick < 1:
            raise ConfigError(f"{where}: 'sd_jobs_per_tick' must be >= 1")
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
        sd_snapshot=bool(data.get("sd_snapshot", False)),
        sd_span_cap=int(data["sd_span_cap"]) if data.get("sd_span_cap") is not None else None,
        sd_motion=bool(data.get("sd_motion", False)),
        sd_jobs_per_tick=sd_jobs_per_tick,
        person_sensitivity=int(data["person_sensitivity"]) if data.get("person_sensitivity") is not None else None,
        detection=_detection(data.get("detection"), where),
        tracking=_tracking(data.get("tracking"), where),
        weather=_weather(data.get("weather"), where),
        enrich=_enrich(data.get("enrich"), where),
        sampler=_sampler(data.get("sampler"), where),
        scorer=_scorer(data.get("scorer"), where),
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

    loop_raw = data.get("loop") or {}
    loop = LoopConfig(
        event_interval=int(loop_raw.get("event_interval", 4)),
        control_interval=int(loop_raw.get("control_interval", 60)),
    )

    return AppConfig(
        location=location,
        telegram=data.get("telegram") or {},
        groq=data.get("groq") or {},
        faces=data.get("faces") or {},
        alerts=alerts,
        loop=loop,
        cameras=cameras,
    )


def load_config(path) -> AppConfig:
    """Load and validate a YAML config file."""
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    return load_config_from_dict(data)
