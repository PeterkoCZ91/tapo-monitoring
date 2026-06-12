"""Config-driven daemon: one per-camera state machine for the whole fleet.

Replaces the trio of standalone scripts (camera_automation / person_monitor / groq_watch)
with a single loop that, each tick and per camera:

1. determines day/night (``scheduling.is_night``) and rain (``weather.is_raining_now``);
2. builds a pure :class:`CameraPlan` (what tracking / sensitivity / preset should be);
3. applies it in the firmware-safe order (SmartTrack/sensitivity/preset first, auto-track
   asserted last — see :mod:`tapo_monitor.tracking`).

The planning step is pure and tested; the apply/run steps are thin I/O orchestration.

NOTE: the detection → enrich → notify pipeline (ONVIF / getEvents polling) is wired in a
follow-up; this module currently owns the camera-control automation.
"""

import time as _time
from dataclasses import dataclass

from . import scheduling, tracking, weather
from .config import AppConfig, CameraConfig


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


def main(argv=None):  # pragma: no cover - thin entry point
    import sys

    from .config import load_config

    path = (argv or sys.argv[1:] or ["cameras.yaml"])[0]
    app = load_config(path)
    interval = 60
    print(f"[tapo-monitor] loaded {len(app.cameras)} camera(s); tick every {interval}s")
    while True:
        try:
            run_once(app)
        except Exception as e:  # noqa: BLE001
            print(f"[tapo-monitor] tick error: {e}")
        _time.sleep(interval)
