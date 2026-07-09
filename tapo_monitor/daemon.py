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
import tempfile
import time as _time
from dataclasses import dataclass, field

from . import enrich, monitor, notify, sampler, scheduling, scorer, sdclip, snapshot, tracking, weather
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
    return CameraPlan(
        autotrack_on=autotrack_on,
        rain_parked=rain_parked,
        motion_sensitivity=sensitivity,
        smarttrack=tuple(cfg.tracking.smarttrack),
        preset=preset,
        person_sensitivity=cfg.person_sensitivity,
    )


def apply_plan(cam, plan: CameraPlan):
    """Apply a CameraPlan to a connected camera in firmware-safe order.

    SmartTrack / motion sensitivity / preset first; auto-track asserted LAST and verified.
    Returns True if auto-track ended in the intended state.
    """
    try:
        cam.setMotionDetection(sensitivity=int(plan.motion_sensitivity))
    except Exception:
        pass
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

    night = is_night()
    plans = {}
    for cfg in app.cameras:
        rain_active = False
        if cfg.weather.strategy != "none" or cfg.weather.storm_park:
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
    ``last_event_start``  camera event start time of the last alert/deferred alert.
    ``fail_since``        when the current connect outage began, or absent if up.
    ``outage_alerted``    cameras for which a 🔴 outage alert has already fired.
    ``connect_fails``     consecutive connect failures per camera (drives backoff).
    ``connect_backoff_until`` earliest time we may try connecting again per camera.
    ``groups``            open sampler event-groups per camera (see tapo_monitor.sampler).
    """
    last_seen: dict = field(default_factory=dict)
    last_alert: dict = field(default_factory=dict)
    last_event_start: dict = field(default_factory=dict)
    fail_since: dict = field(default_factory=dict)
    outage_alerted: dict = field(default_factory=dict)
    connect_fails: dict = field(default_factory=dict)
    connect_backoff_until: dict = field(default_factory=dict)
    pending_sd: list = field(default_factory=list)
    groups: dict = field(default_factory=dict)


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
        image = snapshot.capture_rtsp(url, timeout=cfg.rtsp_timeout)
        if image:
            return image
        if not recorder_fallback:
            return None
        image = snapshot.latest_recording_frame(cfg.host, timeout=cfg.rtsp_timeout)
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
        result = scorer.score_image(cfg.scorer.url, image_path, timeout=cfg.scorer.timeout)
        return None if result is None else scorer.subject_score(result)

    return score


def _caption_describe(cfg, groq_key, image):
    """Caption-only Groq for an already-approved frame; never blocks a send."""
    if not cfg.enrich.groq:
        return ""
    desc = enrich.groq_describe(groq_key, image)
    return "" if notify.is_empty_scene(desc) else desc


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

        def defer(event, etype, live_sent, _name=name, _cap=cfg.sd_span_cap):
            if etype == "motion" and any(
                e["camera"] == _name and e["etype"] == "motion" for e in state.pending_sd
            ):
                log.info("drop %s: SD follow-up already pending for %s", etype, _name)
                return
            state.pending_sd.append({
                "camera": _name,
                "etype": etype,
                "event": event,
                # Window and due time follow the camera's own event seconds (end_time),
                # bounded by the camera's hardware budget (sd_span_cap): a longer event
                # needs a wider window AND a later fetch, or the window end is still
                # inside pytapo's freshness guard and downloads empty.
                "due_at": (event.get("start_time") or now)
                          + sdclip.fresh_delay(sdclip.event_span(event, cap=_cap)),
                "live_sent": live_sent,
            })
        defer_fn = defer if cfg.sd_snapshot else None
        score = score_for(cfg)

        def observe(event, etype, sent, _name=name, _cfg=cfg):
            if _cfg.sampler.enabled:
                sampler.observe_event(state.groups, _name, event, etype, sent, now, _cfg.sampler)

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
            observe=observe,
            mute=cfg.night_only and not night,
        )
        state.last_seen[cfg.name] = watermark
    return state.last_seen


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
    fetch_frames = fetch_frames or sdclip.fetch_sd_frames_subprocess
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
        frames = fetch_frames(cfg, start_time,
                              span=sdclip.event_span(event, cap=cfg.sd_span_cap),
                              out_dir=job_dir)
        image, description, fallback_image = None, "", None
        selected_score = None
        score = score_for(cfg)
        try:
            for frame in frames:
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
                description = _caption_describe(cfg, secrets["groq_key"], image)
            label = enrich.face_label(monitor.face_ids(event), secrets.get("face_names"))
            caption = notify.build_caption(
                monitor.TYPE_EMOJI.get(etype, "👤"), time_str(event),
                description=description or None, detail=label or None,
            )
            ok = notify.send_photo(secrets["telegram_token"], secrets["telegram_chat"], image, caption)
            log.info("alert %s sent (faces=%r, desc=%r) [sd]", etype, label, description)
            monitor.audit_event(cfg, event, etype, "sd", "send", score=selected_score,
                                threshold=cfg.scorer.threshold if score is not None else None,
                                telegram=ok)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)   # frames + any orphaned mp4/partials
            _safe_unlink(fallback_image)
    state.pending_sd = remaining
    return state.pending_sd



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
            if score is not None and s is not None and s < cfg.scorer.threshold:
                log.info("sampler %s frame %d/%d: score %.2f below threshold %.2f",
                         cfg.name, group["frames"], scfg.max_frames, s, cfg.scorer.threshold)
                monitor.audit_event(cfg, group["event"], etype, "sampler", "drop", score=s,
                                    threshold=cfg.scorer.threshold, reason="below_threshold")
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
            ok = notify.send_photo(secrets["telegram_token"], secrets["telegram_chat"], image, caption)
            log.info("alert %s sent (faces=%r, desc=%r, score=%s) [sampler]",
                     etype, label, description, f"{s:.2f}" if s is not None else "n/a")
            monitor.audit_event(cfg, group["event"], etype, "sampler", "send", score=s,
                                threshold=cfg.scorer.threshold if score is not None else None,
                                telegram=ok)
            on_alert(etype)
            group["sent"] = True
        finally:
            _safe_unlink(image)

def control_due(last_control, now, interval):
    """True if the camera-control pass is due. Pure.

    ``last_control`` is the time the control pass last ran, or None if it never has
    (first tick is always due). Once run, it's due again after ``interval`` seconds.
    """
    return last_control is None or (now - last_control) >= interval


def loop_step(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets,
              last_control, control_interval,
              run_control=None, watchdog=None, monitor=None, drain=None, sample=None,
              connect_factory=None, is_night=None):
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
    connect_factory = connect_factory or _connect_camera
    is_night = is_night or scheduling.is_night
    night = is_night()                    # one source of truth for this tick's night gate
    if control_due(last_control, now, control_interval):
        cam_clients.clear()
        run_control(app, now=now, connect=connect_factory(cam_clients, state, now))
        watchdog(app, cam_clients, state, now=now, secrets=secrets, night=night)
        last_control = now
    monitor(app, cam_clients, state, now=now, secrets=secrets, night=night)
    sample(app, cam_clients, state, now=now, secrets=secrets, night=night)
    drain(app, cam_clients, state, now=now, secrets=secrets, night=night)
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
    secrets = resolve_secrets(app)
    log.info("loaded %d camera(s); poll events every %ds, control every %ds; face_names=%d known",
             len(app.cameras), poll_interval, control_interval, len(secrets.get("face_names") or {}))
    cam_clients = {}
    last_control = None
    while True:
        now = _time.time()
        try:
            last_control = loop_step(
                app, cam_clients, state, now=now, secrets=secrets,
                last_control=last_control, control_interval=control_interval,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("tick error: %s", e)
        _time.sleep(poll_interval)


def _watchdog_pass(app: AppConfig, cam_clients, state: MonitorState, *, now, secrets, night=True):
    """Advance per-camera outage state and send 🔴/🟢 alerts once per transition.

    A night_only camera is silent during the day (no 🔴/🟢), matching the rule that all
    of that camera's Telegram traffic — detection and operational alike — is night-only.
    """
    token = secrets["telegram_token"]
    chat = secrets["telegram_chat"]
    for cfg in app.cameras:
        if cfg.night_only and not night:
            continue
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
