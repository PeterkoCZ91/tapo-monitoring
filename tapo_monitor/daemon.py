"""Config-driven daemon: one per-camera state machine for the whole fleet.

Replaces the trio of standalone scripts (camera_automation / person_monitor / groq_watch)
with a single loop that, each tick and per camera:

1. determines day/night (``scheduling.is_night``) and rain (``weather.is_raining_now``);
2. builds a pure :class:`CameraPlan` (what tracking / sensitivity / preset should be);
3. applies it in the firmware-safe order (SmartTrack/sensitivity/preset first, auto-track
   asserted last — see :mod:`tapo_monitor.tracking`).

The planning step is pure and tested; the apply/run steps are thin I/O orchestration.

Alongside camera control (:func:`run_once`), the daemon runs the detection pipeline each
tick (:func:`run_monitor_pass`): for every camera polling ``getevents`` it advances a
per-camera watermark held in :class:`MonitorState` and fires the enrich/notify steps via
:func:`monitor.run_monitor`. Secrets are resolved from the environment once per tick by
:func:`resolve_secrets`.
"""

import logging
import os
import time as _time
from dataclasses import dataclass, field

from . import enrich, monitor, notify, scheduling, snapshot, tracking, weather
from .config import (
    AppConfig,
    CameraConfig,
    resolve_camera_credentials,
    resolve_rtsp_credentials,
)

log = logging.getLogger(__name__)


@dataclass
class CameraPlan:
    autotrack_on: bool
    rain_parked: bool
    motion_sensitivity: int
    smarttrack: tuple
    preset: str | None


def plan_camera(cfg: CameraConfig, night: bool, rain_active: bool) -> CameraPlan:
    """Pure: decide the camera-control actions for one tick."""
    autotrack_on, rain_parked = tracking.decide_tracking(
        cfg.role, night, rain_active, cfg.weather.strategy
    )
    sensitivity = tracking.decide_motion_sensitivity(
        rain_active, cfg.weather.motion_normal, cfg.weather.motion_rain, cfg.weather.strategy
    )
    if not autotrack_on:
        preset = cfg.tracking.day_preset          # parked (day or rain) -> day preset
    else:
        preset = cfg.tracking.night_preset        # tracking at night -> optional night preset
    return CameraPlan(
        autotrack_on=autotrack_on,
        rain_parked=rain_parked,
        motion_sensitivity=sensitivity,
        smarttrack=tuple(cfg.tracking.smarttrack),
        preset=preset,
    )


def apply_plan(cam, plan: CameraPlan):
    """Apply a CameraPlan to a connected camera in firmware-safe order.

    SmartTrack / motion sensitivity / preset first; auto-track asserted LAST and verified.
    Returns True if auto-track ended in the intended state.
    """
    if plan.autotrack_on:
        try:
            tracking.apply_smarttrack(cam, plan.smarttrack)
        except Exception:
            pass
    try:
        cam.setMotionDetection(sensitivity=int(plan.motion_sensitivity))
    except Exception:
        pass
    if plan.preset:
        try:
            cam.setPreset(plan.preset)
        except Exception:
            pass
    return tracking.ensure_autotrack(cam, plan.autotrack_on)


def run_once(app: AppConfig, now=None, connect=None, is_night=None, is_raining=None):
    """One pass over all cameras. Dependencies injectable for testing.

    Returns a dict {camera_name: CameraPlan} of what was planned.
    """
    now = now if now is not None else _time.time()
    is_night = is_night or scheduling.is_night
    is_raining = is_raining or weather.is_raining_now

    night = is_night()
    plans = {}
    for cfg in app.cameras:
        rain_active = False
        if cfg.weather.strategy != "none":
            rain_active = is_raining(
                now,
                threshold=cfg.weather.precip_threshold,
                poll_interval=cfg.weather.poll_interval,
            )
        plan = plan_camera(cfg, night, rain_active)
        plans[cfg.name] = plan
        if connect is not None:
            cam, _err = connect(cfg)
            if cam is not None:
                apply_plan(cam, plan)
    return plans


def resolve_secrets(app: AppConfig) -> dict:
    """Read secret values from the env vars named in the config. Pure-ish (env read only).

    Missing or unset env vars resolve to an empty string so callers never crash.
    Returns {telegram_token, telegram_chat, groq_key, face_names}.
    """
    def env(mapping, key):
        name = (mapping or {}).get(key)
        return os.environ.get(name, "") if name else ""

    return {
        "telegram_token": env(app.telegram, "token_env"),
        "telegram_chat": env(app.telegram, "chat_id_env"),
        "groq_key": env(app.groq, "api_key_env"),
        "face_names": enrich.parse_face_names(env(app.faces, "names_env")),
    }


@dataclass
class MonitorState:
    """Per-camera state carried across ticks.

    ``last_seen``         detection watermark (newest start_time alerted).
    ``last_alert``        timestamp of the last detection alert sent (cooldown gate).
    ``fail_since``        when the current connect outage began, or absent if up.
    ``outage_alerted``    cameras for which a 🔴 outage alert has already fired.
    ``connect_fails``     consecutive connect failures per camera (drives backoff).
    ``connect_backoff_until`` earliest time we may try connecting again per camera.
    """
    last_seen: dict = field(default_factory=dict)
    last_alert: dict = field(default_factory=dict)
    fail_since: dict = field(default_factory=dict)
    outage_alerted: dict = field(default_factory=dict)
    connect_fails: dict = field(default_factory=dict)
    connect_backoff_until: dict = field(default_factory=dict)


def backoff_seconds(fails, base=60, cap=1800):
    """Exponential connect backoff: base, 2·base, 4·base … capped at ``cap``.

    Pure. ``fails`` is the count of *consecutive* failures (1 after the first).
    The C560WS locks out a source IP for ~30 min after repeated failed logins, so
    backing off (rather than retrying every 60 s tick) avoids deepening a lockout.
    """
    if fails < 1:
        return 0
    return min(base * (2 ** (fails - 1)), cap)


def update_outage(state: "MonitorState", name, ok, now, threshold):
    """Pure per-camera outage state transition.

    Returns ("alert"|"recovered"|None, state) and mutates the outage bookkeeping in
    ``state``. ``ok`` is True when the camera connected this tick. Emits "alert" once
    when a continuous outage reaches ``threshold`` seconds, and "recovered" once when a
    previously-alerted camera comes back.
    """
    if ok:
        was_alerted = state.outage_alerted.get(name, False)
        state.fail_since.pop(name, None)
        state.outage_alerted.pop(name, None)
        if was_alerted:
            return "recovered", state
        return None, state

    fail_since = state.fail_since.get(name)
    if fail_since is None:
        state.fail_since[name] = now
        fail_since = now
    already = state.outage_alerted.get(name, False)
    if notify.outage_alert_due(fail_since, now, already, threshold):
        state.outage_alerted[name] = True
        return "alert", state
    return None, state


def _default_snapshot(cfg: CameraConfig):
    """Build a snapshot(cam, event) callable for one camera (RTSP only for now).

    Credentials and stream/port come from the config (resolved from the environment),
    NOT from the pytapo ``cam`` object, whose login is often a different account.
    """
    user, password = resolve_rtsp_credentials(cfg)

    def snap(_cam, _event):
        url = snapshot.rtsp_url(
            cfg.host, user, password, stream=cfg.rtsp_stream, port=cfg.rtsp_port
        )
        return snapshot.capture_rtsp(url, timeout=cfg.rtsp_timeout)
    return snap


def _default_time_str(event):  # pragma: no cover - trivial formatting
    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(event.get("start_time", _time.time())))


def run_monitor_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets,
                     snapshot_for=None, time_str=None):
    """Poll the detection pipeline once per camera that uses ``getevents``.

    ``cam_clients`` maps camera name -> connected client (anything exposing ``getEvents()``);
    cameras without a client are skipped. Advances ``state.last_seen[name]`` per camera and
    returns the updated mapping. Collaborators (snapshot / time_str) are injectable.

    A per-camera cooldown (``app.alerts.cooldown``) rate-limits detection alerts so a
    burst of detections within the window produces at most one notification.
    """
    snapshot_for = snapshot_for or _default_snapshot
    time_str = time_str or _default_time_str
    cooldown = app.alerts.cooldown
    for cfg in app.cameras:
        if "getevents" not in cfg.detection.sources:
            continue
        cam = cam_clients.get(cfg.name)
        if cam is None:
            continue
        last_seen = state.last_seen.get(cfg.name, 0)
        name = cfg.name

        def can_alert(etype, _name=name):
            # Per-type cooldown so a motion alert never eats a real person. A confirmed
            # detection (person/pet/tamper) is gated only by other confirmed alerts;
            # bare motion is quieted by either a recent confirmed alert (same walk, no
            # duplicate) or a recent motion alert.
            confirmed_ts = state.last_alert.get((_name, "confirmed"))
            if etype != "motion":
                return notify.should_send_alert(confirmed_ts, now, cooldown)
            motion_ts = state.last_alert.get((_name, "motion"))
            recent = [t for t in (confirmed_ts, motion_ts) if t is not None]
            return notify.should_send_alert(max(recent) if recent else None, now, cooldown)

        def on_alert(etype, _name=name):
            state.last_alert[(_name, "motion" if etype == "motion" else "confirmed")] = now

        watermark = monitor.run_monitor(
            cam, cfg, last_seen,
            now=now,
            groq_key=secrets["groq_key"],
            telegram_token=secrets["telegram_token"],
            telegram_chat=secrets["telegram_chat"],
            snapshot=snapshot_for(cfg),
            time_str=time_str,
            can_alert=can_alert,
            on_alert=on_alert,
            face_names=secrets.get("face_names"),
        )
        state.last_seen[cfg.name] = watermark
    return state.last_seen


def main(argv=None):  # pragma: no cover - thin entry point
    import sys

    from .config import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    path = (argv or sys.argv[1:] or ["cameras.yaml"])[0]
    app = load_config(path)
    interval = 60
    state = MonitorState()
    secrets = resolve_secrets(app)
    log.info("loaded %d camera(s); tick every %ds; face_names=%d known",
             len(app.cameras), interval, len(secrets.get("face_names") or {}))
    while True:
        now = _time.time()
        cam_clients = {}
        try:
            run_once(app, now=now, connect=_connect_camera(cam_clients, state, now))
            _watchdog_pass(app, cam_clients, state, now=now, secrets=secrets)
            run_monitor_pass(app, cam_clients, state, now=now, secrets=secrets)
        except Exception as e:  # noqa: BLE001
            log.exception("tick error: %s", e)
        _time.sleep(interval)


def _watchdog_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets):
    """Advance per-camera outage state and send 🔴/🟢 alerts once per transition."""
    token = secrets["telegram_token"]
    chat = secrets["telegram_chat"]
    for cfg in app.cameras:
        ok = cam_clients.get(cfg.name) is not None
        event, _ = update_outage(state, cfg.name, ok, now, app.alerts.outage_threshold)
        if event == "alert":
            notify.send_text(token, chat, f"🔴 camera '{cfg.name}' unreachable")
        elif event == "recovered":
            notify.send_text(token, chat, f"🟢 camera '{cfg.name}' back online")


def _connect_camera(cam_clients, state=None, now=None):  # pragma: no cover - thin I/O glue
    """Return a connect(cfg) that builds a pytapo client and caches it for the monitor pass.

    When ``state`` and ``now`` are supplied, failed connects accrue an exponential
    backoff (:func:`backoff_seconds`) so a struggling/locked-out camera is not retried
    every tick. A successful connect clears the backoff. The normal (succeeding) path is
    unchanged: no failure, no backoff.
    """
    from . import camera

    def connect(cfg: CameraConfig):
        name = cfg.name
        if state is not None and now is not None:
            if now < state.connect_backoff_until.get(name, 0):
                return None, "backoff"
        user, password, cloud = resolve_camera_credentials(cfg)
        factory = camera.tapo_factory(cfg.host, user, password, cloud)
        client, err = camera.connect(factory)
        if state is not None:
            if client is not None:
                state.connect_fails.pop(name, None)
                state.connect_backoff_until.pop(name, None)
            else:
                fails = state.connect_fails.get(name, 0) + 1
                state.connect_fails[name] = fails
                wait = backoff_seconds(fails)
                if now is not None:
                    state.connect_backoff_until[name] = now + wait
                log.warning("connect %s failed (#%d): %s; backing off %ds", name, fails, err, wait)
        if client is not None:
            cam_clients[name] = client
        return client, err

    return connect
