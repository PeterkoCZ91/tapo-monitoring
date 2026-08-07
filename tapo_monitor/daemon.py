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
import shutil
import subprocess
import tempfile
import time as _time
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import (
    capabilities,
    drift,
    enrich,
    health,
    ledger,
    monitor,
    notify,
    panlimit,
    recclip,
    sampler,
    scheduling,
    scorer,
    sdclip,
    sentlog,
    snapshot,
    tracking,
    twin,
    weather,
)
from .config import (
    AppConfig,
    CameraConfig,
    resolve_camera_credentials,
    resolve_rtsp_credentials,
)

log = logging.getLogger(__name__)

# Drop a queued SD fetch once its event ages past the getEvents poll window; bounds the
# queue if a camera stays offline so it can never grow without limit.
PENDING_MAX_AGE = 600

# A single transient scorer timeout would otherwise flip the whole frame to passthrough
# (unfiltered spam). Retry once after this delay before degrading.
SCORER_RETRY_DELAY = 0.5


def _safe_unlink(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to remove temp file %s", path, exc_info=True)


@dataclass
class CameraPlan:
    autotrack_on: bool
    rain_parked: bool
    motion_sensitivity: int
    smarttrack: tuple
    preset: str | None
    person_sensitivity: int | None = None
    # Day/night mode to assert this tick ("on" = IR/B&W, "off" = day/colour, "auto"), or
    # None to leave the camera's day/night mode untouched.
    night_vision: str | None = None


def plan_camera(cfg: CameraConfig, night: bool, rain_active: bool) -> CameraPlan:
    """Pure: decide the camera-control actions for one tick."""
    autotrack_on, rain_parked = tracking.decide_tracking(
        cfg.role, night, rain_active, cfg.weather.strategy, cfg.weather.storm_park
    )
    sensitivity = tracking.decide_motion_sensitivity(
        rain_active, cfg.weather.motion_normal, cfg.weather.motion_rain, cfg.weather.strategy
    )
    if not autotrack_on:
        preset = cfg.tracking.day_preset          # parked (day or rain) -> day preset
    else:
        preset = cfg.tracking.night_preset        # tracking at night -> optional night preset
    night_vision = None
    if cfg.night_vision == "ir":
        night_vision = "on" if night else "off"   # IR/B&W at night, day/colour by day
    elif cfg.night_vision == "auto":
        night_vision = "auto"                      # re-assert the camera's own auto switch
    return CameraPlan(
        autotrack_on=autotrack_on,
        rain_parked=rain_parked,
        motion_sensitivity=sensitivity,
        smarttrack=tuple(cfg.tracking.smarttrack),
        preset=preset,
        person_sensitivity=cfg.person_sensitivity,
        night_vision=night_vision,
    )


def effective_night(cfg: CameraConfig, astronomical_night: bool) -> bool:
    """Apply the camera's explicit schedule to the shared astronomical decision."""
    if cfg.schedule == "always_day":
        return False
    if cfg.schedule == "always_night":
        return True
    return astronomical_night


def apply_plan(cam, plan: CameraPlan):
    """Apply a CameraPlan to a connected camera in firmware-safe order.

    SmartTrack / motion sensitivity / preset first; auto-track asserted LAST and verified.
    Returns True if auto-track ended in the intended state.
    """
    try:
        cam.setMotionDetection(sensitivity=int(plan.motion_sensitivity))
    except Exception:
        pass
    # Force IR night vision when configured (night_vision: ir). The camera otherwise stays
    # in colour mode under a streetlight, whose slow shutter smears any moving subject; IR
    # runs a faster shutter, so the event frame is sharper. Re-asserted every tick so it
    # follows the day/night schedule. Left before apply_smarttrack (safe zone): SmartTrack
    # and auto-track re-assert last, so this cannot leave the tracking filter wiped.
    if plan.night_vision is not None:
        try:
            cam.setDayNightMode(plan.night_vision)
        except Exception as exc:  # noqa: BLE001 - a camera control failure must not stop polling
            log.warning("failed to set day/night mode %s: %s", plan.night_vision, exc)
    # Self-heal AI person detection (events_1 bit 19). It silently went 'off'
    # after a daemon restart (2026-06-15), demoting people to bare motion that the
    # funnel then dropped. Re-assert ON every tick. When a per-camera
    # person_sensitivity is configured, re-assert it too (lower = fewer false
    # AI-person detections on an empty yard); otherwise leave sensitivity untouched.
    try:
        if plan.person_sensitivity is not None:
            cam.setPersonDetection(True, sensitivity=plan.person_sensitivity)
        else:
            cam.setPersonDetection(True)
    except Exception:
        pass
    # Keep the camera following people, not cars: the C560WS auto-track swings after any
    # AI-detected target, so vehicle detection is re-asserted OFF every tick (SmartTrack
    # already excludes vehicles, but that alone doesn't stop the detector feeding track).
    try:
        cam.setVehicleDetection(False)
    except Exception:
        pass
    if plan.preset:
        try:
            cam.setPreset(plan.preset)
        except Exception:
            pass
    # apply_smarttrack MUST be the LAST configuration call before ensure_autotrack.
    # Live evidence (2026-06-23) showed one of the calls above resets smart_track_info
    # to ALL-OFF; running SmartTrack first let those calls wipe the night people-only
    # filter, so auto-track followed any motion (including cars). Nothing may run between
    # apply_smarttrack and ensure_autotrack — setSmartTrackConfig clears the auto-track
    # master switch, so ensure_autotrack (setAutoTrackTarget) has to stay truly last.
    if plan.autotrack_on:
        try:
            tracking.apply_smarttrack(cam, plan.smarttrack)
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

    night = is_night() if is_night is not scheduling.is_night else scheduling.is_night(location=app.location)
    plans = {}
    for cfg in app.cameras:
        rain_active = False
        if cfg.weather.strategy != "none" or cfg.weather.storm_park:
            rain_active = is_raining(
                now,
                threshold=cfg.weather.precip_threshold,
                poll_interval=cfg.weather.poll_interval,
            )
        plan = plan_camera(cfg, effective_night(cfg, night), rain_active)
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
    ``last_event_start``  camera event start time of the last alert/deferred alert.
    ``fail_since``        when the current network outage began, or absent if up.
    ``outage_alerted``    cameras for which a 🔴 outage alert has already fired.
    ``online_since``      beginning of the current observed-online interval.
    ``last_success``      most recent successful health observation.
    ``last_outage_duration`` duration of the most recently completed outage.
    ``last_observed_uptime`` observed uptime immediately before the current/last outage.
    ``reconnect_count``   completed observed offline -> online transitions.
    ``recovery_pending``  recovery notifications awaiting confirmed delivery.
    ``total_observed_online`` accumulated completed online intervals.
    ``total_observed_offline`` accumulated completed offline intervals.
    ``network_reachable`` latest unauthenticated ICMP observation per camera.
    ``network_fails``     consecutive failed ICMP observations per camera.
    ``connect_fails``     consecutive API failures per camera (drives auth backoff).
    ``connect_backoff_until`` earliest time we may try connecting again per camera.
    ``health_path``       optional durable health-state path used by the daemon.
    ``events_reachable``  latest getEvents poll outcome per camera.
    ``rtsp_reachable``    latest event-triggered RTSP snapshot outcome per camera.
    ``twin_fleet``        latest redacted Digital Twin entry per camera.
    ``groups``            open sampler event-groups per camera (see tapo_monitor.sampler).
    """
    last_seen: dict = field(default_factory=dict)
    last_alert: dict = field(default_factory=dict)
    last_event_start: dict = field(default_factory=dict)
    fail_since: dict = field(default_factory=dict)
    outage_alerted: dict = field(default_factory=dict)
    online_since: dict = field(default_factory=dict)
    last_success: dict = field(default_factory=dict)
    last_outage_duration: dict = field(default_factory=dict)
    last_observed_uptime: dict = field(default_factory=dict)
    reconnect_count: dict = field(default_factory=dict)
    recovery_pending: dict = field(default_factory=dict)
    total_observed_online: dict = field(default_factory=dict)
    total_observed_offline: dict = field(default_factory=dict)
    network_reachable: dict = field(default_factory=dict)
    network_fails: dict = field(default_factory=dict)
    connect_fails: dict = field(default_factory=dict)
    connect_backoff_until: dict = field(default_factory=dict)
    health_path: str | None = None
    events_reachable: dict = field(default_factory=dict)
    rtsp_reachable: dict = field(default_factory=dict)
    desired_plans: dict = field(default_factory=dict)
    twin_last_probe: dict = field(default_factory=dict)
    twin_fleet: dict = field(default_factory=dict)
    twin_alerted: dict = field(default_factory=dict)
    twin_path: str | None = None
    event_ledger: object | None = None
    tick_fail_since: float | None = None
    stall_alerted: bool = False
    ledger_handler: object | None = None
    pending_sd: list = field(default_factory=list)
    groups: dict = field(default_factory=dict)
    pan_guard: dict = field(default_factory=dict)   # per-camera ONVIF pan-limit state


def backoff_seconds(fails, base=60, cap=1800):
    """Exponential connect backoff: base, 2·base, 4·base … capped at ``cap``.

    Pure. ``fails`` is the count of *consecutive* failures (1 after the first).
    The C560WS locks out a source IP for ~30 min after repeated failed logins, so
    backing off (rather than retrying every 60 s tick) avoids deepening a lockout.
    """
    if fails < 1:
        return 0
    return min(base * (2 ** (fails - 1)), cap)


def update_stall(state: "MonitorState", ok, now, threshold):
    """Pure transition for the daemon's own liveness. Returns ("alert"|"recovered"|None, state).

    ``ok`` is whether the last tick completed. Emits "alert" once when ticks have been
    failing continuously for ``threshold`` seconds, "recovered" once when one succeeds
    after that alert.
    """
    if ok:
        was_alerted = state.stall_alerted
        state.tick_fail_since = None
        state.stall_alerted = False
        return ("recovered", state) if was_alerted else (None, state)

    if state.tick_fail_since is None:
        state.tick_fail_since = now
        return None, state
    if not state.stall_alerted and now - state.tick_fail_since >= threshold:
        state.stall_alerted = True
        return "alert", state
    return None, state


def stall_watchdog(app: AppConfig, state: "MonitorState", secrets, *, ok, now):
    """Alert when the daemon's own loop has been failing for too long.

    The per-camera outage watchdog runs inside ``loop_step``; when that call is what
    breaks, it never runs at all — the daemon just logs a tick error every poll and goes
    quiet, which is indistinguishable from a calm night. This one lives in the outer
    loop, so a daemon throwing on every tick still says so out loud.

    Best-effort by construction: it must never be the reason the surviving loop dies.
    """
    try:
        event, _ = update_stall(state, ok, now, app.alerts.stall_threshold)
        if event == "alert":
            down = notify.format_duration(max(0, now - (state.tick_fail_since or now)))
            if not notify.send_text(secrets["telegram_token"], secrets["telegram_chat"],
                                    f"🔴 monitor stalled: no successful tick for {down}"):
                # Delivery is part of the transition; an outage whose symptom is silence
                # must not be muted by a swallowed message.
                state.stall_alerted = False
                log.warning("stall notification delivery failed")
        elif event == "recovered":
            notify.send_text(secrets["telegram_token"], secrets["telegram_chat"],
                             "🟢 monitor recovered: ticks completing again")
    except Exception:  # noqa: BLE001 - the watchdog may never kill the loop it guards
        log.debug("stall watchdog failed", exc_info=True)


def update_outage(state: "MonitorState", name, ok, now, threshold):
    """Pure per-camera outage state transition.

    Returns ("alert"|"recovered"|None, state) and mutates the outage bookkeeping in
    ``state``. ``ok`` is the latest unauthenticated network-health observation.
    Emits "alert" once when a continuous outage reaches ``threshold`` seconds, and
    "recovered" once when a previously-alerted camera comes back.
    """
    if ok:
        state.last_success[name] = now
        fail_since = state.fail_since.pop(name, None)
        was_alerted = state.outage_alerted.get(name, False)
        state.outage_alerted.pop(name, None)
        if fail_since is None:
            state.online_since.setdefault(name, now)
        else:
            state.last_outage_duration[name] = max(0, now - fail_since)
            state.total_observed_offline[name] = (
                state.total_observed_offline.get(name, 0)
                + state.last_outage_duration[name]
            )
            state.online_since[name] = now
            state.reconnect_count[name] = state.reconnect_count.get(name, 0) + 1
        if was_alerted:
            state.recovery_pending[name] = state.last_outage_duration.get(name, 0)
        if name in state.recovery_pending:
            return "recovered", state
        return None, state

    fail_since = state.fail_since.get(name)
    if fail_since is None:
        state.fail_since[name] = now
        fail_since = now
        online_since = state.online_since.get(name)
        if online_since is not None:
            state.last_observed_uptime[name] = max(0, now - online_since)
            state.total_observed_online[name] = (
                state.total_observed_online.get(name, 0)
                + state.last_observed_uptime[name]
            )
    already = state.outage_alerted.get(name, False)
    if notify.outage_alert_due(fail_since, now, already, threshold):
        state.outage_alerted[name] = True
        return "alert", state
    return None, state


def _default_snapshot(cfg: CameraConfig, stream=None, recorder_fallback=False):
    """Build a snapshot(cam, event) callable for one camera (RTSP only for now).

    Credentials and stream/port come from the config (resolved from the environment),
    NOT from the pytapo ``cam`` object, whose login is often a different account.
    ``stream`` overrides the camera's default RTSP stream (the sampler's follow-up
    grabs may want the high-res stream even where the hot path uses the fast one).
    """
    user, password = resolve_rtsp_credentials(cfg)

    def snap(_cam, _event):
        url = snapshot.rtsp_url(
            cfg.host, user, password, stream=stream or cfg.rtsp_stream, port=cfg.rtsp_port
        )
        # crop_to_subject needs the detail: a crop from an already-downscaled frame is
        # exactly as coarse as the frame it came from. Everyone else downscales at
        # capture, which is where it is cheapest.
        image = snapshot.capture_rtsp(url, timeout=cfg.rtsp_timeout, rotate=cfg.rotate,
                                      scale=not getattr(cfg, "crop_from_native", False))
        if image:
            return image
        if not recorder_fallback:
            return None
        image = snapshot.latest_recording_frame(cfg.host, timeout=cfg.rtsp_timeout,
                                                rotate=cfg.rotate)
        if image:
            log.info("snapshot %s: live RTSP failed, using recorder fallback", cfg.name)
        return image
    return snap


def _sampler_snapshot(cfg: CameraConfig):
    return _default_snapshot(cfg, stream=cfg.sampler.stream)


def _default_time_str(event):  # pragma: no cover - trivial formatting
    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(event.get("start_time", _time.time())))


def alert_gate(state, name, cooldown, now):
    """Build (can_alert, on_alert) for one camera at ``now``.

    Per-type cooldown: a confirmed detection (person/pet/tamper) is gated only by other
    confirmed alerts; bare motion is quieted by either a recent confirmed or motion alert.
    So motion never eats a real person, but a person does suppress a same-walk motion.

    The gate also compares camera event start times. Tapo can deliver several near-identical
    person events seconds apart, and a later one may only be processed minutes later after
    an SD follow-up. Wall-clock cooldown alone then expires even though the camera event is
    the same passage.
    """
    def key_for(etype):
        return "motion" if etype == "motion" else "confirmed"

    def event_start(event):
        if not isinstance(event, dict):
            return None
        try:
            return float(event.get("start_time"))
        except (TypeError, ValueError):
            return None

    def event_time_allowed(keys, event):
        start = event_start(event)
        if start is None:
            return True
        for key in keys:
            last_start = state.last_event_start.get((name, key))
            if last_start is not None and abs(start - last_start) < cooldown:
                return False
        return True

    def can_alert(etype, event=None):
        confirmed_ts = state.last_alert.get((name, "confirmed"))
        if etype != "motion":
            return (notify.should_send_alert(confirmed_ts, now, cooldown)
                    and event_time_allowed(("confirmed",), event))
        motion_ts = state.last_alert.get((name, "motion"))
        recent = [t for t in (confirmed_ts, motion_ts) if t is not None]
        return (notify.should_send_alert(max(recent) if recent else None, now, cooldown)
                and event_time_allowed(("confirmed", "motion"), event))

    def on_alert(etype, event=None):
        key = key_for(etype)
        state.last_alert[(name, key)] = now
        start = event_start(event)
        if start is not None:
            state.last_event_start[(name, key)] = start

    return can_alert, on_alert


def score_for(cfg: CameraConfig):
    """Build score(image_path) -> float|None for a camera, or None if no scorer configured."""
    if not cfg.scorer.url:
        return None

    def score(image_path):
        started = _time.monotonic()
        result = scorer.score_image(cfg.scorer.url, image_path, timeout=cfg.scorer.timeout,
                                    tiles=cfg.scorer.tiles)
        if result is None:
            elapsed = _time.monotonic() - started
            if elapsed >= cfg.scorer.timeout:
                # The service hung rather than refused: a retry costs another full timeout
                # for every scored frame of every tick, which stalls the whole pass. Give
                # up now and let the caller degrade to passthrough.
                log.warning("scorer for %s hung (%.1fs); skipping retry", cfg.name, elapsed)
                return None
            # A fast failure is a blip (connection refused, restart) — one retry keeps a
            # whole burst from being spammed through passthrough.
            log.info("scorer retry for %s after %.2fs failure", cfg.name, elapsed)
            _time.sleep(SCORER_RETRY_DELAY)
            result = scorer.score_image(cfg.scorer.url, image_path, timeout=cfg.scorer.timeout,
                                        tiles=cfg.scorer.tiles)
        return None if result is None else scorer.subject_score(result)

    return score


def compute_crop(box, w, h, pad=0.4, min_frac=0.22, skip_frac=0.55):
    """Padded, clamped integer crop rect ``(x, y, cw, ch)`` around a person box, or None.

    Returns None when the subject already fills >= ``skip_frac`` of the frame (nothing to
    zoom to). Otherwise pads the box by ``pad`` on each side and enforces a minimum size
    (``min_frac`` of the frame) so a distant, tiny box still yields a readable photo with
    context rather than a postage stamp; the rect is centred on the box and clamped inside
    the frame. Pure.
    """
    x1, y1, x2, y2 = box
    bw, bh = max(0.0, x2 - x1), max(0.0, y2 - y1)
    if bw <= 0 or bh <= 0 or w <= 0 or h <= 0:
        return None
    if bw * bh >= skip_frac * w * h:
        return None
    cw = min(max(bw * (1 + 2 * pad), min_frac * w), float(w))
    ch = min(max(bh * (1 + 2 * pad), min_frac * h), float(h))
    ccx, ccy = (x1 + x2) / 2, (y1 + y2) / 2
    x = min(max(0.0, ccx - cw / 2), w - cw)
    y = min(max(0.0, ccy - ch / 2), h - ch)
    return (int(x), int(y), int(cw), int(ch))


def _run_crop_ffmpeg(image, out_path, rect):  # pragma: no cover - subprocess I/O
    x, y, cw, ch = rect
    subprocess.run(
        ["ffmpeg", "-y", "-i", image, "-vf", f"crop={cw}:{ch}:{x}:{y}", "-q:v", "2", out_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=True,
    )


def crop_for_subject(cfg, image, out_dir, secrets=None, score_result=None, run_ffmpeg=None,
                     source=None, source_width=None):
    """Crop ``image`` to the detected person (a zoom) for ``crop_to_subject`` cameras.

    Re-scores the chosen frame with tiling to get the person box + frame dims, then
    ffmpeg-crops to a padded box. Returns the crop path, or ``image`` unchanged when
    cropping is off, there is no box, the subject already fills the frame, or ffmpeg fails
    — the alert always goes out with *some* image.

    ``source`` (with its ``source_width``) crops a higher-resolution copy of the same
    frame instead of the scored one, scaling the rect into its coordinate space. Scoring
    stays on the small frame: the service resizes to its input size regardless, so a 4K
    frame buys no accuracy and costs the *shared* scorer 2-3x per request. Only the crop
    needs the pixels. Both frames come from one grab, so there is no time skew between
    where the subject was scored and where it is cropped.
    """
    if not getattr(cfg, "crop_to_subject", False) or not cfg.scorer.url:
        return image
    result = score_result
    if result is None:
        result = scorer.score_image(cfg.scorer.url, image, timeout=cfg.scorer.timeout,
                                    tiles=cfg.scorer.tiles)
    if not result:
        return image
    box = scorer.subject_box(result)
    w, h = result.get("w"), result.get("h")
    if not box or not w or not h:
        return image
    rect = compute_crop(box, w, h)
    if rect is None:
        return image
    crop_target = image
    if source and source_width and w:
        factor = source_width / w
        if factor > 1:
            rect = tuple(round(v * factor) for v in rect)
            crop_target = source
    out_path = os.path.join(out_dir or "/tmp", f"crop_{int(_time.time() * 1000)}.jpg")
    try:
        (run_ffmpeg or _run_crop_ffmpeg)(crop_target, out_path, rect)
    except Exception:
        log.debug("crop_to_subject: ffmpeg failed for %s", crop_target, exc_info=True)
        return image
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        log.info("crop_to_subject %s: zoomed to %s", cfg.name, rect)
        return out_path
    return image


def _run_downscale(src, out_path):  # pragma: no cover - subprocess I/O
    subprocess.run(snapshot.downscale_args(src, out_path),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=20, check=True)


def _reduced(src, out_dir, run):
    """Downscaled copy of ``src`` for delivery, or None if it could not be made."""
    out_path = os.path.join(out_dir or "/tmp", f"small_{int(_time.time() * 1000000)}.jpg")
    try:
        (run or _run_downscale)(src, out_path)
    except Exception:
        log.debug("downscale failed for %s", src, exc_info=True)
        return None
    return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 0 else None


def send_alert_photo(cfg, secrets, image, caption, downscale=None):
    """Send one alert frame: the zoom goes to Telegram, the whole scene to the sent log.

    ``crop_to_subject`` cameras push a close-up, which is what the user wants to look at
    but useless for reviewing a false positive later — a cropped empty yard is just a
    blurry patch. So the archive keeps the frame as it was before cropping.

    Those cameras are also handed a *native-resolution* frame (see ``_default_snapshot``),
    so the reduction to delivery width happens here instead: once on the crop, once on the
    scene, after there is something worth cropping. Full-resolution bytes never leave.
    """
    token, chat = secrets["telegram_token"], secrets["telegram_chat"]
    out_dir = os.path.dirname(image)
    if not getattr(cfg, "crop_from_native", False):
        cropped = crop_for_subject(cfg, image, out_dir, secrets)
        args = (token, chat, cropped, caption)
        if cropped == image:        # nothing was cropped away, nothing extra to keep
            return notify.send_photo(*args)
        try:
            return notify.send_photo(*args, archive_path=image)
        finally:
            _safe_unlink(cropped)   # our own temp zoom; ``image`` stays the caller's

    # Native frame in: reduce once, then score that copy but crop the full-detail one.
    scene = _reduced(image, out_dir, downscale) or image
    native = image if scene != image else None
    cropped = crop_for_subject(cfg, scene, out_dir, secrets, source=native,
                               source_width=snapshot.image_width(native) if native else None)
    to_send = scene if cropped == scene else (_reduced(cropped, out_dir, downscale) or cropped)
    try:
        return notify.send_photo(token, chat, to_send, caption, archive_path=scene)
    finally:
        for temp in (cropped if cropped != scene else None,
                     to_send if to_send not in (scene, cropped) else None,
                     scene if scene != image else None):
            _safe_unlink(temp)


def _caption_describe(cfg, groq_key, images):
    """Caption-only Groq for already-approved frame(s); never blocks a send.

    ``images`` may be a single path or a chronological sequence — a sequence lets the
    caption describe movement and direction across the event, not one frozen pose.
    """
    if not cfg.enrich.groq:
        return ""
    desc = enrich.groq_describe(groq_key, images)
    return "" if notify.is_empty_scene(desc) else desc


def sd_followup_spans(cfg, event, etype):
    """Return the initial and maximum SD windows for a follow-up."""
    full_span = sdclip.event_span(event, cap=cfg.sd_span_cap)
    if etype != "motion" or not cfg.sd_motion:
        return full_span, full_span
    first_cap = cfg.sd_motion_span_cap
    if first_cap is None:
        first_cap = min(cfg.sd_span_cap or sdclip.SD_SPAN_CAP, sdclip.SD_SPAN_CAP)
    if cfg.sd_span_cap is not None:
        first_cap = min(first_cap, cfg.sd_span_cap)

    return sdclip.event_span(event, cap=first_cap), full_span

def run_monitor_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets,
                     snapshot_for=None, time_str=None, night=True):
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

        can_alert, on_alert = alert_gate(state, name, cooldown, now)

        def defer(event, etype, live_sent, _name=name, _cfg=cfg,
                  _source=cfg.snapshot_source):
            key = "motion" if etype == "motion" else "confirmed"
            first_span, full_span = sd_followup_spans(_cfg, event, etype)
            try:
                start = float(event.get("start_time"))
            except (TypeError, ValueError):
                start = None
            for pending in state.pending_sd:
                if pending["camera"] != _name:
                    continue
                pending_key = "motion" if pending["etype"] == "motion" else "confirmed"
                if pending_key != key:
                    continue
                if key == "motion":
                    log.info("drop %s: SD follow-up already pending for %s", etype, _name)
                    return
                try:
                    pending_start = float(pending["event"].get("start_time"))
                except (TypeError, ValueError):
                    pending_start = None
                if start is not None and pending_start is not None:
                    if abs(start - pending_start) < cooldown:
                        log.info("drop %s: duplicate SD follow-up already pending for %s", etype, _name)
                        return
                elif pending["etype"] == etype:
                    log.info("drop %s: SD follow-up already pending for %s", etype, _name)
                    return
            state.pending_sd.append({
                "camera": _name,
                "etype": etype,
                "event": event,
                "span": first_span,
                "full_span": full_span,
                # Window and due time follow the camera's own event seconds (end_time),
                # bounded by the camera's hardware budget (sd_span_cap): a longer event
                # needs a wider window AND a later fetch, or the window end is still
                # inside pytapo's freshness guard and downloads empty.
                "due_at": (event.get("start_time") or now)
                          + (recclip.fresh_delay(first_span)
                             if _source == "recording"
                             else sdclip.fresh_delay(first_span)),
                "live_sent": live_sent,
            })
        defer_fn = defer if cfg.sd_snapshot else None
        score = score_for(cfg)

        corroborate = None
        if cfg.sampler.enabled and cfg.scorer.motion_send_threshold is not None:
            def corroborate(event, s, _name=name, _cfg=cfg):
                g = sampler.ensure_group(state.groups, _name, event, "motion", now, _cfg.sampler)
                return sampler.corroborate_motion(
                    g, s, _cfg.scorer.threshold, _cfg.scorer.motion_send_threshold)

        def observe(event, etype, sent, delivered=False, _name=name, _cfg=cfg):
            if _cfg.sampler.enabled:
                sampler.observe_event(state.groups, _name, event, etype, sent, now,
                                      _cfg.sampler, delivered=delivered)

        def burst_sent(_name=name, _cfg=cfg):
            # Only a real delivery suppresses a follow-up: "sent" also means "queued for
            # SD" or "Telegram refused it", and dropping a confirmed person on either
            # would lose it for good.
            g = state.groups.get(_name)
            return bool(g and g.get("delivered")
                        and (now - g["last_event_at"]) <= _cfg.sampler.group_gap)

        def poll_observe(ok, _name=name):
            state.events_reachable[_name] = bool(ok)

        def media_observe(ok, _name=name):
            state.rtsp_reachable[_name] = bool(ok)

        # night_only camera during the day: mute (drain the watermark, alert nothing).
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
            defer=defer_fn,
            score=score,
            corroborate=corroborate,
            observe=observe,
            burst_sent=burst_sent,
            poll_observe=poll_observe,
            media_observe=media_observe,
            mute=cfg.night_only and not night,
        )
        state.last_seen[cfg.name] = watermark
    return state.last_seen


def _select_recording_frame(cfg, event, etype, frames, score, blur_score=None):
    """Score every recorder frame; return the sharpest above-threshold one and its score.

    The SD path stops at the first above-threshold frame (each download is expensive). A
    local recording hands us the whole high-res buffer for free, so we rank every hit by
    sharpness (ffmpeg blurdetect) and pick the clearest — night motion smears single
    frames. Returns ``(frame, score)`` or ``(None, None)`` when nothing clears the bar; if
    the scorer is unavailable it passes the offending frame through (``(frame, None)``).
    """
    blur_score = blur_score or recclip.blur_score
    above = []
    for frame in frames:
        s = score(frame)
        if s is None:
            return frame, s                       # scorer down -> pass this frame through
        if s >= cfg.scorer.threshold:
            above.append((frame, s))
        else:
            monitor.audit_event(cfg, event, etype, "sd", "drop", score=s,
                                threshold=cfg.scorer.threshold, reason="below_threshold")
    if not above:
        return None, None
    above.sort(key=lambda fs: fs[1], reverse=True)          # best score first
    ranked = [(f, blur_score(f)) for f, _ in above]
    image = recclip.select_sharpest(ranked)
    selected = next(s for f, s in above if f == image)
    return image, selected


def process_pending_sd(app, cam_clients, state, *, now, secrets, snapshot_for=None,
                       time_str=None, fetch_frames=None, night=True):
    """Send queued confirmed-person SD follow-ups whose segment is now downloadable.

    Each entry waits SD_FRESH_DELAY past its event, then we pull candidate frames spanning
    the event from SD (via a fresh subprocess) and let Groq pick the one showing the
    subject — the camera fires on motion start, so the person is often only in view a few
    seconds in. The follow-up is sent only when it's *better* than what already went out:

      * a frame shows the subject -> send it (the accurate, in-frame photo);
      * frames were pulled but none shows the subject -> send nothing: Groq checked the
        whole event window, and every live such case (2026-07-02..06) was a false
        positive (passing car at night) — a blank photo helps nobody;
      * SD produced no frames at all and no live went out -> send a live-RTSP grab
        (zero checked frames is no evidence of absence; trust the camera).

    There is no cooldown gate here: the follow-up belongs to an already-alerted event (and
    the inline path emits at most one defer per cooldown window). Entries past
    PENDING_MAX_AGE are dropped; entries for a camera not reachable this tick are kept.
    """
    snapshot_for = snapshot_for or (lambda cfg: _default_snapshot(cfg, recorder_fallback=True))
    time_str = time_str or _default_time_str
    fetch_override = fetch_frames   # tests inject one fetch for every source
    cfg_by_name = {c.name: c for c in app.cameras}
    remaining = []
    processed_by_camera = {}
    for entry in state.pending_sd:
        cfg = cfg_by_name.get(entry["camera"])
        event = entry["event"]
        etype = entry["etype"]
        start_time = event.get("start_time") or 0
        if cfg is None:
            log.warning("drop %s: SD follow-up for unknown camera %r", etype, entry["camera"])
            continue
        if cfg.night_only and not night:
            continue                          # night_only by day: drop, won't replay at night
        if now - start_time > PENDING_MAX_AGE:
            # Past the getEvents poll window: usually a stale event re-queued after a
            # daemon restart (its segment is long gone). Log it so the queue isn't a
            # silent black hole — an unexplained missing [sd] alert traces back here.
            log.info("drop %s: SD follow-up too old (age=%ds > %ds), no segment",
                     etype, int(now - start_time), PENDING_MAX_AGE)
            continue
        if now < entry["due_at"]:
            remaining.append(entry)               # segment not fresh yet -> keep
            continue
        cam = cam_clients.get(entry["camera"])
        if cam is None:
            remaining.append(entry)               # camera offline this tick -> keep
            continue

        limit = cfg.sd_jobs_per_tick
        if limit is not None and processed_by_camera.get(entry["camera"], 0) >= limit:
            remaining.append(entry)               # slow-host backpressure -> next tick
            continue
        processed_by_camera[entry["camera"]] = processed_by_camera.get(entry["camera"], 0) + 1

        # Pull SD frames in a fresh subprocess; pick the frame Groq sees a subject in.
        # The window is sized from the event's own seconds (see sdclip.event_span),
        # bounded by the camera's hardware budget (sd_span_cap).
        # Give the job its own temp dir and drop the whole tree in `finally`: a slow host
        # (Pi Zero) can blow the subprocess timeout, and the killed child never reaches its
        # own cleanup -> the segment mp4 + partial frames orphan in /tmp until the tmpfs
        # fills. Owning the dir here means we clean up even when the child returns nothing.
        job_dir = tempfile.mkdtemp(prefix="sdjob_")
        if fetch_override is not None:
            fetch = fetch_override
        elif cfg.snapshot_source == "recording":
            fetch = recclip.fetch_recording_frames   # reads RECORDING_ROOT for the tree
        else:
            fetch = sdclip.fetch_sd_frames_subprocess
        span = entry.get("span")
        if span is None:  # backwards-compatible with entries queued by older daemons
            span = sdclip.event_span(event, cap=cfg.sd_span_cap)
        full_span = entry.get("full_span", sdclip.event_span(event, cap=cfg.sd_span_cap))
        frames = fetch(cfg, start_time, span=span, out_dir=job_dir)
        image, description, fallback_image = None, "", None
        selected_score = None
        score = score_for(cfg)
        try:
            recording_pick = cfg.snapshot_source == "recording" and score is not None
            if recording_pick:
                image, selected_score = _select_recording_frame(cfg, event, etype, frames, score)
            # SD path: first above-threshold frame wins. Skipped (empty) when the recording
            # path already made the pick above.
            for frame in (() if recording_pick else frames):
                if score is not None:
                    # Local scorer is the arbiter (Groq captions later, at send time).
                    s = score(frame)
                    selected_score = s
                    if s is None:
                        monitor.audit_event(cfg, event, etype, "sd", "scorer_unavailable")
                        image = frame
                        break
                    if s >= cfg.scorer.threshold:
                        image = frame
                        break
                    monitor.audit_event(cfg, event, etype, "sd", "drop", score=s,
                                        threshold=cfg.scorer.threshold, reason="below_threshold")
                    continue
                desc = enrich.groq_describe(secrets["groq_key"], frame) if cfg.enrich.groq else ""
                # Raw mode (groq off): no subject arbiter -> first frame wins as-is.
                if not cfg.enrich.groq or not notify.is_empty_scene(desc):
                    image, description = frame, desc          # subject found in this frame
                    break
            if image is None:
                if frames:
                    if (etype == "motion" and span < full_span
                            and not entry.get("extended_retry")):
                        entry["span"] = full_span
                        entry["extended_retry"] = True
                        entry["due_at"] = now
                        remaining.append(entry)
                        monitor.audit_event(
                            cfg, event, etype, "sd", "retry",
                            reason=f"extend_span={span}->{full_span}",
                        )
                        log.info("retry %s: no subject in %ss SD window; extending to %ss",
                                 etype, span, full_span)
                        continue
                    # Groq saw nobody in any frame spanning the whole event. Live
                    # 2026-07-02..06 every such case was a false positive (passing car
                    # at night), so a blank photo helps nobody — drop, keep the trace.
                    log.info("drop %s: SD found no subject in %d frames%s", etype,
                             len(frames), ", live already sent" if entry.get("live_sent") else "")
                    continue
                if entry.get("live_sent") or etype == "motion":
                    # The (empty) live frame already went out, or this is unconfirmed
                    # motion with no subject-bearing evidence. Do not send a blind ping.
                    log.info("drop %s: SD produced no frames%s", etype,
                             ", live already sent" if entry.get("live_sent") else "")
                    continue
                snap = snapshot_for(cfg)                      # SD download failed -> live RTSP
                image = fallback_image = snap(cam, event) or snap(cam, event)
            if not image:
                log.warning("skip %s: snapshot failed (after retry)", etype)
                continue                              # drop
            if not description:
                # Caption from the whole (thinned) frame sequence when the chosen frame
                # came from SD; the RTSP fallback has no sequence to offer.
                images = (enrich.select_frames(frames, keep=image)
                          if image in frames else image)
                description = _caption_describe(cfg, secrets["groq_key"], images)
            label = enrich.face_label(monitor.face_ids(event), secrets.get("face_names"))
            caption = notify.build_caption(
                monitor.TYPE_EMOJI.get(etype, "👤"), time_str(event),
                description=description or None, detail=label or None,
            )
            ok = send_alert_photo(cfg, secrets, image, caption)
            # SD follow-up is a real user-visible alert. Record it in the same gate as
            # live sends, otherwise a person rescued from SD can be followed minutes
            # later by a duplicate motion SD alert from the same passage.
            _, on_alert = alert_gate(state, cfg.name, app.alerts.cooldown, now)
            if ok:
                log.info("alert %s sent (faces=%r, desc=%r) [sd]", etype, label, description)
                on_alert(etype, event)
                open_group = state.groups.get(entry["camera"])
                if open_group is not None:
                    # This burst has now really been alerted on: a later empty-live frame
                    # of the same passage may skip its duplicate follow-up.
                    open_group["delivered"] = True
            else:
                log.warning("alert %s Telegram delivery failed [sd]; retry queued", etype)
                entry["due_at"] = now + 60
                remaining.append(entry)
            monitor.audit_event(cfg, event, etype, "sd", "send", score=selected_score,
                                threshold=cfg.scorer.threshold if score is not None else None,
                                telegram=ok)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)   # frames + any orphaned mp4/partials
            _safe_unlink(fallback_image)
    state.pending_sd = remaining
    return state.pending_sd



def _suppress_sampler_frame(cfg, group, etype, s, scfg, image, verdict):
    """Log/audit one sampler frame that will not be sent and fold it into the group.

    ``verdict`` is "drop" (below threshold) or "hold" (marginal, awaiting corroboration);
    a held frame is archived for review. Either way the score updates the low-score
    streak, so a marginal frame keeps a motion-only group alive exactly like a good one.
    """
    if verdict == "hold":
        log.info("sampler %s frame %d/%d: score %.2f awaiting corroboration",
                 cfg.name, group["frames"], scfg.max_frames, s)
        monitor.audit_event(cfg, group["event"], etype, "sampler", "hold", score=s,
                            threshold=cfg.scorer.threshold, reason="awaiting_corroboration")
        sentlog.archive_review_if_configured(
            image, sentlog.review_meta(cfg.name, "hold", etype, s))
    else:
        log.info("sampler %s frame %d/%d: score %.2f below threshold %.2f",
                 cfg.name, group["frames"], scfg.max_frames, s, cfg.scorer.threshold)
        monitor.audit_event(cfg, group["event"], etype, "sampler", "drop", score=s,
                            threshold=cfg.scorer.threshold, reason="below_threshold")
    if sampler.note_score(group, s, scfg):
        log.info("sampler %s: early exit after %d consecutive low frames",
                 cfg.name, group["low_streak"])
        monitor.audit_event(cfg, group["event"], etype, "sampler", "early_exit",
                            score=s, threshold=scfg.low_score, reason="low_score_streak")


def process_sampler(app, cam_clients, state, *, now, secrets, snapshot_for=None,
                    time_str=None, night=True):
    """Advance sampler groups: follow-up grabs across each open event window.

    A group exists because its burst never produced a subject-bearing alert. Every
    ``sampler.interval`` seconds we grab another frame (optionally from the high-res
    stream) and let the scorer decide; the first frame above threshold is sent and
    closes the group. A scorer failure sends the frame through unfiltered but still
    closes the group — degraded mode is bounded spam (one ping), never a silent miss.
    """
    time_str = time_str or _default_time_str
    for cfg in app.cameras:
        group = state.groups.get(cfg.name)
        if group is None:
            continue
        if not cfg.sampler.enabled:
            del state.groups[cfg.name]
            continue
        if cfg.night_only and not night:
            continue                          # night_only by day: leave the group alone
        scfg = cfg.sampler
        if sampler.expired(group, now, scfg):
            if group.get("motion_candidates") and not group["sent"]:
                # A held marginal motion never got its corroborating frame: make the discard
                # visible so threshold tuning can count expired holds like any other outcome.
                monitor.audit_event(cfg, group["event"], "motion", "sampler", "drop",
                                    score=group.get("last_hold_score"),
                                    threshold=cfg.scorer.threshold, reason="hold_expired")
            log.info("close group %s: %d follow-up frame(s), sent=%s",
                     cfg.name, group["frames"], group["sent"])
            del state.groups[cfg.name]
            continue
        if not sampler.due(group, now, scfg):
            continue
        cam = cam_clients.get(cfg.name)
        if cam is None:
            continue                          # camera offline this tick; retry next tick
        snap = (snapshot_for or _sampler_snapshot)(cfg)
        image = snap(cam, group["event"])
        sampler.record_grab(group, now, scfg)
        if not image:
            continue                          # grab hiccup: the schedule already moved on
        try:
            etype = group["etype"]
            score = score_for(cfg)
            s = score(image) if score is not None else None
            motion_corr = (cfg.scorer.motion_send_threshold is not None
                           and etype == "motion" and not group.get("pir_backed"))
            verdict = "send"
            if score is not None and s is not None:
                verdict = (sampler.corroborate_motion(
                               group, s, cfg.scorer.threshold, cfg.scorer.motion_send_threshold)
                           if motion_corr
                           else ("drop" if s < cfg.scorer.threshold else "send"))
            if verdict != "send":
                _suppress_sampler_frame(cfg, group, etype, s, scfg, image, verdict)
                continue
            if score is not None and s is None:
                log.warning("scorer unavailable; sampler passes %s frame through", cfg.name)
                monitor.audit_event(cfg, group["event"], etype, "sampler", "scorer_unavailable")
            can_alert, on_alert = alert_gate(state, cfg.name, app.alerts.cooldown, now)
            if not can_alert(etype):
                # A confirmed alert for this walk already went out elsewhere; this
                # group's job is done.
                log.info("skip %s: cooldown active [sampler]", etype)
                monitor.audit_event(cfg, group["event"], etype, "sampler", "cooldown")
                group["sent"] = True
                continue
            description = _caption_describe(cfg, secrets["groq_key"], image)
            label = enrich.face_label(monitor.face_ids(group["event"]), secrets.get("face_names"))
            caption = notify.build_caption(
                monitor.TYPE_EMOJI.get(etype, "👁"), time_str(group["event"]),
                description=description or None, detail=label or None,
            )
            ok = send_alert_photo(cfg, secrets, image, caption)
            monitor.audit_event(cfg, group["event"], etype, "sampler", "send", score=s,
                                threshold=cfg.scorer.threshold if score is not None else None,
                                telegram=ok)
            if ok:
                log.info("alert %s sent (faces=%r, desc=%r, score=%s) [sampler]",
                         etype, label, description, f"{s:.2f}" if s is not None else "n/a")
                on_alert(etype)
                group["sent"] = True
                group["delivered"] = True
            else:
                log.warning("alert %s Telegram delivery failed [sampler]", etype)
        finally:
            _safe_unlink(image)

def control_due(last_control, now, interval):
    """True if the camera-control pass is due. Pure.

    ``last_control`` is the time the control pass last ran, or None if it never has
    (first tick is always due). Once run, it's due again after ``interval`` seconds.
    """
    return last_control is None or (now - last_control) >= interval


def process_digital_twin(app, cam_clients, state, *, now, secrets, probe=None):
    """Refresh the opt-in Camera Digital Twin using already-connected clients only."""
    if not app.observability.digital_twin:
        return state.twin_fleet
    probe = probe or capabilities.collect_snapshot
    changed = False
    for cfg in app.cameras:
        cam = cam_clients.get(cfg.name)
        if cam is None:
            continue
        last_probe = state.twin_last_probe.get(cfg.name)
        if last_probe is not None and now - last_probe < app.observability.probe_interval:
            continue

        try:
            snapshot_data = probe(cam)
            layers = capabilities.derive_health(
                snapshot_data,
                network=state.network_reachable.get(cfg.name),
                events=state.events_reachable.get(cfg.name),
                rtsp=state.rtsp_reachable.get(cfg.name),
            )
            aggregate = drift.aggregate_health(layers)
            plan = state.desired_plans.get(cfg.name)
            if plan is None:
                empty_report = drift.evaluate_drift({}, {}, scope=cfg.name)
                evaluation = {
                    "desired": {},
                    "actual": {},
                    "drift": empty_report.to_dict(),
                }
            else:
                evaluation = twin.evaluate_snapshot(cfg.name, plan, snapshot_data)
        except Exception as exc:  # noqa: BLE001 - observability must not block alerts
            log.warning("digital twin %s probe failed: %s", cfg.name, type(exc).__name__)
            state.twin_last_probe[cfg.name] = now
            continue

        current_keys = twin.alertable_keys(evaluation)
        alerted = set(state.twin_alerted.get(cfg.name, set()))
        if app.observability.drift_alerts:
            new_results = [item for item in twin.alertable_results(evaluation)
                           if item["key"] not in alerted]
            if new_results:
                details = ", ".join(
                    f"{item['path']} expected={item['expected']} actual={item['actual']}"
                    for item in new_results
                )
                if notify.send_text(
                    secrets["telegram_token"], secrets["telegram_chat"],
                    f"⚠️ camera '{cfg.name}' configuration drift: {details}",
                ):
                    alerted.update(item["key"] for item in new_results)
            recovered = alerted - current_keys
            if recovered and notify.send_text(
                secrets["telegram_token"], secrets["telegram_chat"],
                f"✅ camera '{cfg.name}' configuration drift recovered",
            ):
                alerted.difference_update(recovered)
        state.twin_alerted[cfg.name] = alerted

        entry = twin.fleet_entry(
            captured_at=now,
            snapshot=snapshot_data,
            health=aggregate,
            evaluation=evaluation,
        )
        entry["alerted_keys"] = sorted(alerted)
        state.twin_fleet[cfg.name] = entry
        state.twin_last_probe[cfg.name] = now
        changed = True
        log.info(
            "digital twin %s: health=%s drift=%d unknown=%d",
            cfg.name,
            aggregate.status,
            evaluation["drift"]["counts"]["drift"],
            evaluation["drift"]["counts"]["unknown"],
        )
    if changed and state.twin_path:
        twin.save_state(state.twin_path, state.twin_fleet, logger=log)
    return state.twin_fleet


def _pan_guard_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets,
                    night=True):
    """Soft pan-limit: recall a camera that auto-tracked past its preset span (see
    :mod:`tapo_monitor.panlimit`). Runs every tick, throttled per camera by
    ``pan_limit.poll_interval``. ONVIF is independent of the pytapo ``cam_clients`` and its
    own client/bounds are cached in ``state.pan_guard``; any ONVIF error is logged and the
    client rebuilt next poll — the guard must never kill the loop.
    """
    for cfg in app.cameras:
        pl = cfg.pan_limit
        if not pl.enabled:
            continue
        g = state.pan_guard.setdefault(cfg.name, {})
        if g.get("last_poll") is not None and now - g["last_poll"] < pl.poll_interval:
            continue
        g["last_poll"] = now
        try:
            if "ptz" not in g:
                user = os.getenv(pl.onvif_user_env or "", "")
                password = os.getenv(pl.onvif_password_env or "", "")
                g["ptz"], g["token"] = panlimit.build_ptz(cfg.host, pl.onvif_port, user, password)
                g["bounds"] = panlimit.read_preset_bounds(g["ptz"], g["token"])
                log.info("pan_limit %s: preset bounds %s", cfg.name, g["bounds"])
            x = panlimit.read_pan_x(g["ptz"], g["token"])
            target = panlimit.limit_target(x, g.get("bounds"), pl.margin)
            if target is not None:
                panlimit.goto_preset(g["ptz"], g["token"], target)
                log.info("pan_limit %s: pan x=%.4f out of bounds -> recall preset %s",
                         cfg.name, x, target)
        except Exception as e:  # noqa: BLE001 - an ONVIF hiccup must not kill the loop
            log.warning("pan_limit %s: %s", cfg.name, e)
            g.pop("ptz", None)          # force a clean rebuild on the next poll


def loop_step(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets,
              last_control, control_interval,
              run_control=None, watchdog=None, monitor=None, drain=None, sample=None,
              connect_factory=None, is_night=None, guard=None, inspect=None):
    """One loop iteration with control decoupled from event polling.

    The slow, rarely-changing work (camera tracking/sensitivity/preset + the per-tick
    reconnect) runs only when :func:`control_due`; the fast detection poll + SD-queue
    drain run every tick on the *already-connected* clients. This shrinks the gap between
    a person's passage and the live snapshot from one control interval (~60s, which almost
    always snapped an empty scene) to one poll interval (~seconds), without re-logging in
    each poll — repeated logins are what risk the C560WS lockout.

    ``cam_clients`` is rebuilt by the control pass and reused (not cleared) on the fast
    polls between control passes. Collaborators are injectable for testing. Returns the
    (possibly advanced) ``last_control``.
    """
    run_control = run_control or run_once
    watchdog = watchdog or _watchdog_pass
    monitor = monitor or run_monitor_pass
    drain = drain or process_pending_sd
    sample = sample or process_sampler
    guard = guard or _pan_guard_pass
    inspect = inspect or process_digital_twin
    connect_factory = connect_factory or _connect_camera
    is_night = is_night or scheduling.is_night
    night = is_night()                    # one source of truth for this tick's night gate
    if control_due(last_control, now, control_interval):
        cam_clients.clear()
        plans = run_control(app, now=now, connect=connect_factory(cam_clients, state, now))
        if isinstance(plans, Mapping):
            state.desired_plans.update(plans)
        watchdog(app, cam_clients, state, now=now, secrets=secrets, night=night)
        inspect(app, cam_clients, state, now=now, secrets=secrets)
        last_control = now
    monitor(app, cam_clients, state, now=now, secrets=secrets, night=night)
    sample(app, cam_clients, state, now=now, secrets=secrets, night=night)
    drain(app, cam_clients, state, now=now, secrets=secrets, night=night)
    guard(app, cam_clients, state, now=now, secrets=secrets, night=night)
    return last_control


def main(argv=None):  # pragma: no cover - thin entry point
    import sys

    from .config import load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    path = (argv or sys.argv[1:] or ["cameras.yaml"])[0]
    app = load_config(path)
    poll_interval = app.loop.event_interval
    control_interval = app.loop.control_interval
    state = MonitorState()
    state.health_path = health.default_state_path()
    restored = health.load_state(state.health_path, state, logger=log)
    state.twin_path = twin.default_state_path()
    state.twin_fleet = twin.load_state(state.twin_path, logger=log)
    state.twin_alerted = {
        name: set(entry.get("alerted_keys", []))
        for name, entry in state.twin_fleet.items()
    }
    if app.observability.ledger:
        try:
            state.event_ledger = ledger.EventLedger()
            state.event_ledger.cleanup(
                app.observability.ledger_retention_days * 86400,
                now=_time.time(),
            )
            state.ledger_handler = ledger.AuditLedgerHandler(state.event_ledger)
            monitor.log.addHandler(state.ledger_handler)
            log.info("event ledger initialized: %s", state.event_ledger.path)
        except Exception as exc:  # noqa: BLE001 - observability must not block startup
            log.warning("event ledger initialization failed: %s", type(exc).__name__)
    secrets = resolve_secrets(app)
    log.info("loaded %d camera(s); poll events every %ds, control every %ds; face_names=%d known",
             len(app.cameras), poll_interval, control_interval, len(secrets.get("face_names") or {}))
    log.info("health state %s: %s", "restored" if restored else "initialized", state.health_path)
    cam_clients = {}
    last_control = None
    while True:
        now = _time.time()
        tick_ok = True
        try:
            last_control = loop_step(
                app, cam_clients, state, now=now, secrets=secrets,
                last_control=last_control, control_interval=control_interval,
            )
        except Exception as e:  # noqa: BLE001
            tick_ok = False
            log.exception("tick error: %s", e)
        stall_watchdog(app, state, secrets, ok=tick_ok, now=now)
        _time.sleep(poll_interval)


def _watchdog_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets, night=True):
    """Advance per-camera outage state and send 🔴/🟢 alerts once per transition.

    A night_only camera is silent during the day (no 🔴/🟢), matching the rule that all
    of that camera's Telegram traffic — detection and operational alike — is night-only.
    """
    token = secrets["telegram_token"]
    chat = secrets["telegram_chat"]
    previous = health.snapshot(state)
    for cfg in app.cameras:
        if cfg.night_only and not night:
            continue
        # Network uptime is intentionally independent from API/auth health. The connector
        # records a fresh ping result on every control pass; fall back to the historical
        # client-based behaviour only for injected/test connectors that do not.
        ok = state.network_reachable.get(cfg.name, cam_clients.get(cfg.name) is not None)
        event, _ = update_outage(state, cfg.name, ok, now, app.alerts.outage_threshold)
        if event == "alert":
            uptime = state.last_observed_uptime.get(cfg.name)
            detail = (f" after {notify.format_duration(uptime)} observed uptime"
                      if uptime is not None else "")
            delivered = notify.send_text(
                token, chat, f"🔴 camera '{cfg.name}' unreachable{detail}")
            if not delivered:
                # Delivery is part of the transition: retry while the outage remains due.
                state.outage_alerted.pop(cfg.name, None)
                log.warning("outage notification delivery failed for %s", cfg.name)
        elif event == "recovered":
            duration = state.last_outage_duration.get(cfg.name)
            detail = (f" after {notify.format_duration(duration)} outage"
                      if duration is not None else "")
            delivered = notify.send_text(
                token, chat, f"🟢 camera '{cfg.name}' back online{detail}")
            if delivered:
                state.recovery_pending.pop(cfg.name, None)
            else:
                log.warning("recovery notification delivery failed for %s", cfg.name)
    if state.health_path and health.snapshot(state) != previous:
        health.save_state(state.health_path, state, logger=log)


def _connect_camera(cam_clients, state=None, now=None):
    """Return a connect(cfg) that builds a pytapo client and caches it for the monitor pass.

    A cheap ping separates a disconnected camera from an API or authentication failure.
    Offline devices never reach the login path. Ping runs on every control pass, including
    while API login is backed off, so physical availability remains independently visible.
    """
    from . import camera

    def connect(cfg: CameraConfig):
        name = cfg.name
        reachable = camera.ping_reachable(cfg.host)
        if state is not None:
            was_reachable = state.network_reachable.get(name)
            state.network_reachable[name] = reachable
            if reachable:
                state.network_fails.pop(name, None)
                if was_reachable is False:
                    log.info("network %s reachable again", name)
            else:
                fails = state.network_fails.get(name, 0) + 1
                state.network_fails[name] = fails
                if was_reachable is not False:
                    log.warning("network %s unreachable", name)
        if not reachable:
            return None, ConnectionError("camera did not answer ping")

        if state is not None and now is not None:
            if now < state.connect_backoff_until.get(name, 0):
                return None, "backoff"

        user, password, cloud = resolve_camera_credentials(cfg)
        factory = camera.tapo_factory(cfg.host, user, password, cloud)
        client, err = camera.connect(factory)
        if state is not None:
            if client is not None:
                fails = state.connect_fails.pop(name, 0)
                state.connect_backoff_until.pop(name, None)
                if fails:
                    log.info("connect %s succeeded after %d failure(s)", name, fails)
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
