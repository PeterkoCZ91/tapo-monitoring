"""Detection pipeline: poll a camera's events, classify, enrich and notify.

The pure core is :func:`collect_detections` — given a batch of ``getEvents`` results and a
watermark, it returns the new alertable detections and the advanced watermark. Enrichment
(snapshot + Groq) and notification (Telegram) are thin I/O wired in :func:`run_monitor`,
with their side-effecting pieces injected so the orchestration stays testable.
"""

import collections
import inspect
import logging
import shlex
import time as _time
import weakref
from datetime import datetime

from . import camera, detection, enrich, notify, scheduling, sentlog, snapshot
from . import mqtt as mqtt_bridge
from .incident import incident_id
from .snapshot import album_lens

log = logging.getLogger(__name__)

_safe_unlink = snapshot.safe_unlink


_SHAPES: "weakref.WeakKeyDictionary[object, dict]" = weakref.WeakKeyDictionary()


def _shape(fn, nargs, min_args, kwnames):
    """Longest call shape (positional count, keyword names) ``fn`` accepts, or None.

    Decided from the signature, once per callable, so a callback is invoked exactly once:
    a TypeError raised inside its body is a real error, not a cue to call it again.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return nargs, kwnames
    for kw in (kwnames, ()):
        for n in range(nargs, min_args - 1, -1):
            try:
                sig.bind(*range(n), **{k: None for k in kw})
            except TypeError:
                continue
            return n, kw
    return nargs, kwnames


def _call_compat(fn, *args, min_args=None, **kw):
    """Call ``fn`` once with the richest signature it accepts.

    Drops optional trailing positionals down to ``min_args`` (default: all) and, failing
    that, the keywords, so an older ``cb(a, b)`` keeps working next to ``cb(a, b, c)``.
    """
    min_args = len(args) if min_args is None else min_args
    key = (len(args), min_args, tuple(kw))
    try:
        # Weak keys: a per-tick closure must not keep its camera or frames alive.
        per_fn = _SHAPES.setdefault(fn, {})
    except TypeError:                   # not weak-referenceable: no cache
        per_fn = {}
    if key not in per_fn:
        per_fn[key] = _shape(fn, *key)
    n, kwnames = per_fn[key]
    return fn(*args[:n], **{k: kw[k] for k in kwnames})


def _observe(observe, event, etype, sent, delivered=False):
    if observe is None:
        return
    _call_compat(observe, event, etype, sent, delivered, min_args=3)


def _health_observe(observe, ok, error=None):
    if observe is None:
        return
    _call_compat(observe, bool(ok), error, min_args=1)


def _latency_observe(observe, operation, seconds):
    if observe is not None:
        observe(operation, seconds)


def _can_alert(can_alert, etype, event):
    if can_alert is None:
        return True
    return _call_compat(can_alert, etype, event, min_args=1)


def _on_alert(on_alert, etype, event, score=None, threshold=None):
    """Arm the caller's gate. ``score``/``threshold`` are passed only for a delivery whose
    photo was scored, so a plain ``on_alert(etype[, event])`` callback keeps working."""
    if on_alert is None:
        return
    if score is not None and threshold is not None:
        on_alert(etype, event, score=score, threshold=threshold)
        return
    _call_compat(on_alert, etype, event, min_args=1)


_EVENT_FAILURES: "collections.OrderedDict[tuple, int]" = collections.OrderedDict()
_EVENT_FAILURES_MAX = 256
EVENT_FAILURE_LIMIT = 3


def _event_failed(cfg, event):
    """Count one more failure of this event; True once it reached the skip limit."""
    key = (cfg.name, event.get("start_time"))
    count = _EVENT_FAILURES.pop(key, 0) + 1
    _EVENT_FAILURES[key] = count
    while len(_EVENT_FAILURES) > _EVENT_FAILURES_MAX:
        _EVENT_FAILURES.popitem(last=False)
    return count >= EVENT_FAILURE_LIMIT


def vehicle_verdict(cfg, s):
    """``"vehicle"`` when a scored frame shows no person but a vehicle the camera opted in to.

    The person always has precedence: a frame at or above the person threshold is never a
    vehicle alert, so a vehicle can neither replace nor duplicate a person one. ``s`` is
    the :class:`scorer.SubjectScore` (None = scorer unavailable = no verdict). Pure.
    """
    if s is None or s >= cfg.scorer.threshold:
        return ""
    return detection.vehicle_alert(getattr(s, "vehicle", 0.0), cfg.scorer.vehicle_threshold,
                                   cfg.scorer.alert_vehicles)


def _keeps_sampling(cfg):
    """Whether an empty-live person defer leaves the burst open (see run_monitor)."""
    scfg = getattr(cfg, "sampler", None)
    return bool(getattr(scfg, "enabled", False)
                and getattr(scfg, "keep_sampling_on_defer", False))


def _defer(defer, event, etype, live_sent, **kw):
    """Queue a follow-up; False only when ``defer`` says it queued nothing (a duplicate).

    A ``defer`` that takes no keywords gets the plain call; one that returns nothing
    counts as queued, as every defer did before it could answer.
    """
    queued = _call_compat(defer, event, etype, live_sent, **kw)
    return queued is not False


def sample_drop(cfg, image, etype, score, path, event=None):
    """Offer one below-threshold frame to the review log's random drop sample.

    ``image`` is a path the caller still owns. A no-op unless ``TAPO_REVIEW_LOG_DIR`` is
    set; it never raises and never changes the drop it follows (see
    :func:`sentlog.archive_drop_sample_if_configured`).
    """
    return sentlog.archive_drop_sample_if_configured(
        image, {**sentlog.review_meta(cfg.name, "drop", etype, score, event), "path": path})


def _fmt_score(score):
    return "none" if score is None else f"{float(score):.4f}"


def _fmt_audit_value(value):
    return shlex.quote(str(value))


def audit_event(cfg, event, etype, path, action, *, score=None, threshold=None,
                telegram=None, reason=None, extra=None):
    """Log one structured ``audit`` line.

    ``extra`` is an optional mapping of additional ``key=value`` fields appended after the
    fixed ones (e.g. hub ``video_type``/``clip_s``). Parsers read audit lines as free-form
    key/value pairs and ignore keys they do not know, so this never breaks a consumer.
    ``None`` values and keys that are not plain identifiers are skipped.
    """
    parts = [
        f"camera={_fmt_audit_value(cfg.name)}",
        f"path={_fmt_audit_value(path)}",
        f"action={_fmt_audit_value(action)}",
        f"etype={_fmt_audit_value(etype)}",
        f"start={_fmt_audit_value(event.get('start_time', 0))}",
    ]
    incident = incident_id(cfg.name, event)
    if incident:
        parts.append(f"incident={_fmt_audit_value(incident)}")
    channels = detection.event_channels(event)
    if channels:
        # Which lenses of a multi-lens camera fired (see detection.normalize_event).
        parts.append(f"channels={_fmt_audit_value(','.join(map(str, channels)))}")
    try:
        event_age_s = max(0.0, _time.time() - float(event.get("start_time")))
    except (TypeError, ValueError):
        event_age_s = None
    if event_age_s is not None:
        parts.append(f"event_age_s={event_age_s:.3f}")
    if score is not None:
        parts.append(f"score={_fmt_score(score)}")
        if hasattr(score, "person"):
            parts.append(f"person_score={_fmt_score(score.person)}")
            parts.append(f"animal_score={_fmt_score(score.animal)}")
    if threshold is not None:
        parts.append(f"threshold={_fmt_score(threshold)}")
    if telegram is not None:
        parts.append(f"telegram={_fmt_audit_value(str(bool(telegram)).lower())}")
    if reason:
        parts.append(f"reason={_fmt_audit_value(reason)}")
    for key, value in (extra or {}).items():
        if value is not None and str(key).replace("_", "").isalnum():
            parts.append(f"{key}={_fmt_audit_value(value)}")
    log.info("audit %s", " ".join(parts))
    zone_observe = getattr(score, "zone_observe", None)
    if zone_observe and action == "send":
        # scorer.alert_zones_mode observe: the zone would have dropped the person of the
        # frame this alert went out on. A separate line, so the send itself stays as is.
        audit_event(cfg, event, etype, path, "would_drop_outside_alert_zone",
                    threshold=threshold,
                    extra={"persons_inside": zone_observe[0],
                           "persons_outside": zone_observe[1]})
    box_observe = getattr(score, "box_observe", None)
    if box_observe and action == "send":
        # scorer.ignore_boxes_mode observe: the box match would have dropped this person.
        audit_event(cfg, event, etype, path, "would_drop_ignore_box", threshold=threshold,
                    extra={"boxes": box_observe})


def audit_error(cfg, error, *, now=None):
    """Record a structured getEvents failure without storing a full exception.

    Only the exception class travels here. The audit line is what AuditLedgerHandler
    persists, and a pytapo/requests failure text carries the camera's address — and
    occasionally a session token — which the ledger is documented not to hold. The
    message itself is logged once by the caller, where the journal keeps it local.
    """
    now = _time.time() if now is None else now
    detail = type(error).__name__
    log.info(
        "audit camera=%s path=getevents action=error etype=system start=%s reason=%s",
        _fmt_audit_value(cfg.name), _fmt_audit_value(now), _fmt_audit_value(detail),
    )


def face_ids(event):
    info = event.get("event_info")
    if not isinstance(info, list):
        return []
    return [item["face_id"] for item in info
            if isinstance(item, dict) and item.get("face_id") is not None]


def has_known_face(event, face_names=None):
    """True only when the event contains a face ID mapped to a configured name."""
    face_names = face_names or {}
    return any(face_names.get(fid) for fid in face_ids(event))


def collect_detections(events, last_seen, strict_people=True, profile=None, event_seen=None):
    """Return (alertable, new_watermark).

    ``alertable`` is a list of (event, event_type) for events newer than ``last_seen``
    that classify as something worth alerting on (vehicles skipped; bare motion dropped
    under ``strict_people``). ``new_watermark`` advances to the newest seen start_time.

    Every event is first put through :func:`detection.normalize_event` under the camera's
    event ``profile``, so the watermark, classification and everything downstream of the
    returned events (audit, incident ID, ledger, sampler, SD follow-up) read one shape.
    ``event_seen(event)`` is called for each fresh normalized event, alertable or not.
    """
    events = [detection.normalize_event(ev, profile) for ev in (events or [])]
    fresh = camera.new_events(events, last_seen)
    alertable = []
    for ev in fresh:
        if event_seen is not None:
            event_seen(ev)
        faces = face_ids(ev)
        flags = detection.event_flags(ev)
        etype = detection.classify_getevent(
            ev.get("event_type") or ev.get("type"),
            has_face=bool(faces),
            strict_people=strict_people,
            events_1=ev.get("events_1"),
            profile=ev.get("event_profile"),
            alarm_type=ev.get("alarm_type"),
        )
        # Audit trail: log every camera event with its decoded signal and our verdict,
        # so "the camera is wrong" vs "our parsing is wrong" is always answerable and the
        # still-unmapped AI bits (alarm_type 4/8/9) can be ground-truthed from real traffic.
        # Multi-lens and non-default-profile cameras add which lenses fired and how the
        # bits were read; a single-lens default camera keeps the line exactly as it was.
        channels = detection.event_channels(ev)
        lens = f" channels={','.join(map(str, channels))}" if channels else ""
        if ev.get("event_profile"):
            lens += f" profile={ev['event_profile']}"
        log.info(
            "event t=%s events_1=%d motion=%d pir=%d person=%d unknown_bits=%s "
            "alarm_type=%s faces=%d%s -> %s",
            ev.get("start_time"), flags["raw"], flags["motion"], flags["pir"],
            flags["person"], flags["unknown_bits"], ev.get("alarm_type"),
            len(faces), lens, etype or "drop",
        )
        if etype:
            alertable.append((ev, etype))
    return alertable, (camera.newest_start(fresh) or last_seen)


def _hold_unscored(cfg, event, etype, image, *, defer, can_defer, on_alert, observe):
    """An unscored live frame at night under ``unscored_at_night: hold``: no photo.

    A camera with a card follow-up gets the event deferred to it (the read is scored a
    couple of minutes later, by when the scorer is often back); otherwise the frame goes
    to the review log, so the person can still be found afterwards. Not through the
    sampler's hold archive: a frame remembered there may be sent when the hold expires,
    which is exactly the unverified send this avoids.
    """
    if defer is not None and can_defer:
        log.warning("scorer unavailable at night; %s held for the card follow-up", etype)
        _on_alert(on_alert, etype, event)
        defer(event, etype, False)
        audit_event(cfg, event, etype, "live", "defer", reason="scorer_unavailable")
        _observe(observe, event, etype, True)
        return
    log.warning("scorer unavailable at night; %s frame held, not sent", etype)
    audit_event(cfg, event, etype, "live", "scorer_unavailable", reason="held_at_night")
    sentlog.archive_review_if_configured(
        image, sentlog.review_meta(cfg.name, "hold", etype, None, event))
    _observe(observe, event, etype, False)


TYPE_EMOJI = {"person": "👤", "vehicle": "🚗", "pet": "🐾", "tamper": "⚠️", "motion": "👁"}


def run_monitor(cam, cfg, last_seen, *, now, groq_key, telegram_token, telegram_chat,
                snapshot, time_str, can_alert=None, on_alert=None, face_names=None,
                ignore_known=False,
                defer=None, score=None, observe=None, poll_observe=None,
                media_observe=None, latency_observe=None, mute=False, corroborate=None,
                burst_sent=None, open_burst=None,
                send_alert=None, scene_alert=None, hold_archive=None,
                trigger_whitelamp=camera.trigger_whitelamp, event_seen=None,
                hold_unscored=False):
    """Poll one camera once and alert on new detections. Returns the new watermark.

    ``mute`` polls and advances the watermark but skips all grabbing/scoring/alerting.
    A night_only camera runs muted outside the astral night, a quiet_hours camera muted
    inside its window, so the backlog is drained silently and does not replay afterwards.

    Side-effecting collaborators are injected:
      snapshot(cam, event) -> image path or None
      time_str(event) -> caption time string
      can_alert(etype) -> bool gate (per-type cooldown / rate-limit); default always True
      on_alert(etype) -> called once after an alert is actually sent (record timestamp)
      defer(event, etype, live_sent) -> enqueue a detection for a deferred SD-frame
        follow-up. ``live_sent`` says whether a live photo already went out (True) or the
        live grab failed (False). Confirmed detections defer when the live frame was empty
        or failed; PIR-backed bare motion may defer only when ``cfg.sd_motion`` is enabled.
        With ``sampler.keep_sampling_on_defer`` an empty (scored) live person frame calls
        it with ``keep_sampling=True``: the cooldown is armed as always, but the sampler
        group stays open. It may return False when it queued nothing (a follow-up of the
        same passage is already pending).
      score(image_path) -> float|None — local scorer subject confidence; when passed it
        replaces Groq as the send/drop arbiter (Groq only captions what already passed)
        and None (scorer unreachable) degrades to raw passthrough, never a drop -
        unless ``hold_unscored`` (``scorer.unscored_at_night: hold`` at night): then a
        frame that could follow up on the card is deferred, any other is kept in the
        review log and not sent.
      observe(event, etype, sent, delivered) -> feeds the sampler's event grouping;
        ``sent`` is True when this event produced an alert or was handed to the SD
        follow-up, ``delivered`` only when a photo actually reached Telegram.
      burst_sent() -> bool — True when the camera's current event burst already
        produced a *delivered* alert; lets the empty-live defer skip queueing a
        duplicate SD follow-up. A queued follow-up or a failed send is not a delivery,
        so it never suppresses one.
      send_alert(image, caption, score) -> bool — delivers one alert frame. The daemon
        passes a sender that crops to the subject and archives the uncropped scene, the
        same route the sampler and the SD follow-up already take. The default posts the
        frame as-is, so a caller that does not care keeps the old behaviour. It also
        gets ``incident=`` and ``send_path="live"`` for the sent-log index.
      scene_alert(etype, event) -> bool — optional cross-camera group gate checked after
        the per-camera cooldown and before snapshot capture.
      hold_archive(image, etype, score, event) -> archives one held (corroboration-suppressed)
        frame, replacing the inline review-log write. The daemon passes one that also
        remembers the archived path on the sampler group, so an expiring hold broken by
        a pan-limit recall can still send its evidence.
      open_burst(event) -> bool — True while a follow-up of this event's passage is queued
        with ``sampler.keep_sampling_on_defer`` and nothing of it was delivered yet. A
        cooldown-blocked event is then scored first: a frame showing a person at the
        threshold may still go out; any other frame stays under the cooldown.
      event_seen(event) -> sees every fresh (normalized) event, muted or not, before any
        gate; the daemon uses it to know when a dual-lens camera's firmware is moving its
        pan/tilt lens (see detection.EventProfile.pt_channel).
    """
    started = _time.monotonic()
    try:
        events = cam.getEvents() or []
    except Exception as exc:
        # Camera exception text can contain a session URL (/stok=...), credentials
        # and addresses. Log only a fixed diagnosis, never a slice of that text.
        text = str(exc).lower()
        if "connecttimeout" in text or "connect timeout" in text:
            reason = "connect timeout"
        elif "readtimeout" in text or "read timed out" in text:
            reason = "read timeout"
        elif "invalid stok" in text or "unauthorized" in text or "authentication" in text:
            reason = "authentication failure"
        elif "connection refused" in text:
            reason = "connection refused"
        else:
            reason = "other"
        log.warning("getEvents failed: %s: %s", type(exc).__name__, reason)
        audit_error(cfg, exc, now=now)
        _health_observe(poll_observe, False, exc)
        return last_seen
    finally:
        _latency_observe(latency_observe, "getevents", _time.monotonic() - started)
    _health_observe(poll_observe, True, None)

    alertable, watermark = collect_detections(
        events, last_seen, cfg.detection.strict_people,
        profile=getattr(cfg, "event_profile", None), event_seen=event_seen)
    if mute:
        return watermark          # outside window: drain silently, no grab/score/alert
    def _process(event, etype):
        audit_event(cfg, event, etype, "getevents", "detect")
        mqtt_bridge.note_detect(cfg.name)       # no-op unless the mqtt: block is set
        lt = getattr(cfg, "light_trigger", None)
        if (lt is not None and lt.enabled and getattr(lt, "mode", "software") == "firmware"
                and cfg.enrich.light_status):
            # The firmware lights the lamp itself; read it while it is likely still lit
            # (~20 s after the event) so a later SD/sampler caption can still show it.
            camera.whitelamp_seen(cam, cfg.name, event.get("start_time"), now=now,
                                  force_time=getattr(cfg, "whitelamp_force_time", None))
        if (lt is not None and lt.enabled and etype in lt.types
                and getattr(lt, "mode", "software") == "software"):
            if lt.window is None or scheduling.in_clock_window(
                lt.window, datetime.fromtimestamp(now)
            ):
                if trigger_whitelamp is not None:
                    force_time = getattr(cfg, "whitelamp_force_time", None)
                    lamp_triggered = False
                    if force_time is not None:
                        lamp_triggered = _call_compat(trigger_whitelamp, cam,
                                                      force_time=force_time)
                    else:
                        lamp_triggered = trigger_whitelamp(cam)
                    if lamp_triggered:
                        camera.note_whitelamp(cfg.name, now,
                                              now + (force_time or camera.LAMP_DEFAULT_FORCE_TIME))
                        log.info("light_trigger: turned on white lamp for %s (%s)", cfg.name, etype)
        event_flags = detection.event_flags(event)
        defer_motion = (
            etype == "motion"
            and defer is not None
            and (
                # A local recorder is the configured alert-media source. Let it
                # inspect bare motion too; the recorder path still sends only a
                # frame that clears the normal scorer threshold.
                cfg.snapshot_source == "recording"
                or (cfg.sd_motion and event_flags["pir"])
            )
        )
        if ignore_known and etype != "motion" and has_known_face(event, face_names):
            label = enrich.face_label(face_ids(event), face_names)
            log.info("skip %s: known face present (ignored: %s)", etype, label)
            audit_event(cfg, event, etype, "live", "ignore_known", reason="known_face")
            return
        cooled_open = False
        if not _can_alert(can_alert, etype, event):
            if etype != "motion" and has_known_face(event, face_names):
                # A known face is new information, not a burst duplicate — the cooldown
                # must not eat it. Unknown face IDs are too noisy for this exception and
                # stay cooldown-gated.
                log.info("cooldown override %s: recognized face present", etype)
            elif open_burst is not None and open_burst(event):
                # The cooldown was armed by this passage's own empty-frame defer, and
                # nothing has reached the phone. Only a frame that shows the person may
                # pass it (decided once the frame is scored, below).
                cooled_open = True
            else:
                log.info("skip %s: cooldown active", etype)
                # Only this event is cooled down; a later event in the same poll may be new.
                audit_event(cfg, event, etype, "live", "cooldown")
                return
        if not _can_alert(scene_alert, etype, event):
            log.info("skip %s: scene duplicate", etype)
            audit_event(cfg, event, etype, "live", "scene_duplicate", reason="same_scene")
            return
        image = snapshot(cam, event)
        if not image:
            # RTSP capture on a slow Pi (e.g. Pi Zero) fails transiently — one retry
            # catches most of those so a confirmed person isn't lost to a single hiccup.
            image = snapshot(cam, event)
        _health_observe(media_observe, image is not None)
        if not image:
            # Confirmed person but no live frame: queue an SD follow-up that MUST send
            # (live_sent=False) so the person isn't lost. PIR-backed motion can opt into
            # the same second chance, but still must find a subject in SD before alerting.
            if defer is not None and etype != "motion":
                log.warning("defer %s: live snapshot failed, SD follow-up queued", etype)
                _on_alert(on_alert, etype, event)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", reason="snapshot_failed")
                _observe(observe, event, etype, True)
            elif defer_motion:
                log.warning("defer %s: live snapshot failed, SD follow-up queued", etype)
                _on_alert(on_alert, etype, event)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", reason="snapshot_failed")
                _observe(observe, event, etype, True)
            else:
                log.warning("skip %s: snapshot failed (after retry)", etype)
                audit_event(cfg, event, etype, "live", "snapshot_failed")
                _observe(observe, event, etype, False)
            return
        try:
            description = ""
            s = None
            if score is not None:
                # Local scorer is the arbiter; Groq no longer decides anything.
                s = score(image)
                if s is None and hold_unscored:
                    _hold_unscored(cfg, event, etype, image, defer=defer,
                                   can_defer=etype != "motion" or defer_motion,
                                   on_alert=on_alert, observe=observe)
                    return
                if s is None:
                    # Scorer unreachable: raw passthrough — degraded means spam,
                    # never a silent miss.
                    log.warning("scorer unavailable; passing %s frame through", etype)
                    audit_event(cfg, event, etype, "live", "scorer_unavailable")
                    empty = False
                else:
                    empty = s < cfg.scorer.threshold
            elif cfg.enrich.groq:
                description = enrich.groq_describe(groq_key, image)
                empty = notify.is_empty_scene(description)
            else:
                # Groq disabled = raw mode: there is no arbiter to declare a scene
                # empty, so nothing is — every live frame goes straight out.
                empty = False
            if vehicle_verdict(cfg, s):
                # Opt-in vehicle alert (scorer.alert_vehicles): its own cooldown, caption
                # and audit type. It is a side alert: the person handling below goes on
                # exactly as without it (a confirmed person with an empty frame still gets
                # its SD follow-up), and it never arms the person cooldown.
                if not _can_alert(can_alert, "vehicle", event):
                    log.info("skip vehicle: cooldown active")
                    audit_event(cfg, event, "vehicle", "live", "cooldown")
                elif cfg.scorer.vehicle_mode == "observe":
                    log.info("observe vehicle (score=%.2f): not sent", s.vehicle)
                    audit_event(cfg, event, "vehicle", "live", "would_send", score=s.vehicle,
                                threshold=cfg.scorer.vehicle_threshold,
                                reason="vehicle_observe")
                else:
                    v_caption = notify.build_caption(
                        TYPE_EMOJI["vehicle"], time_str(event), lens=album_lens(image))
                    v_incident = incident_id(cfg.name, event)
                    v_ok = (send_alert(image, v_caption, None, incident=v_incident,
                                       send_path="live", etype="vehicle")
                            if send_alert is not None
                            else notify.send_photo(telegram_token, telegram_chat, image,
                                                   v_caption, incident=v_incident,
                                                   send_path="live"))
                    audit_event(cfg, event, "vehicle", "live", "send", score=s.vehicle,
                                threshold=cfg.scorer.vehicle_threshold, telegram=v_ok)
                    if v_ok:
                        log.info("alert vehicle sent (score=%.2f)", s.vehicle)
                        _on_alert(on_alert, "vehicle", event)
            if cooled_open and (s is None or s < cfg.scorer.threshold):
                # Not a person photo (or the scorer could not tell): the cooldown the
                # defer armed holds, as it does with keep_sampling_on_defer off.
                log.info("skip %s: cooldown active (open burst, no person in frame)", etype)
                audit_event(cfg, event, etype, "live", "cooldown", score=s,
                            threshold=cfg.scorer.threshold if s is not None else None,
                            reason="open_burst_no_person")
                _observe(observe, event, etype, False)
                return
            if cooled_open:
                log.info("cooldown override %s: person in frame, follow-up still queued",
                         etype)
            if etype == "tamper":
                # Tamper events indicate camera blinding or covering; visual person scorer must not drop them.
                empty = False
            if etype == "motion" and empty and defer_motion:
                # This has to come before the corroborate gate. `empty` is
                # `score < scorer.threshold` and the gate is handed that same threshold as
                # its confirm level, so every frame that qualifies for a recorder look was
                # dropped one branch earlier and the look never happened. That is the case
                # it exists for: a live frame can score 0.05 on a subject the recording
                # shows at 0.83.
                log.info("defer %s: live empty, SD follow-up queued", etype)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", score=s,
                            threshold=cfg.scorer.threshold if score is not None else None,
                            reason="below_threshold" if score is not None else "empty")
                # Deliberately not "sent": the recorder look is extra evidence, not a
                # replacement, so the live sampler keeps working this burst instead of
                # standing down for a look that may find nothing. Both cannot reach the
                # phone - process_pending_sd asks the alert gate before it sends.
                _observe(observe, event, etype, False)
                return
            if etype == "motion" and s is not None and corroborate is not None:
                # Do not alert on a single marginal motion frame — an empty IR scene can
                # hallucinate a person once and not on the next, while a real subject
                # persists across the sampler window. PIR means the motion was physically
                # near, but it is not visual person confirmation and must not bypass this
                # gate. Camera-confirmed person events keep the immediate path above.
                verdict = corroborate(event, s)
                if verdict == "hold":
                    log.info("hold %s: score %.2f awaiting corroboration", etype, s)
                    audit_event(cfg, event, etype, "live", "hold", score=s,
                                threshold=cfg.scorer.threshold, reason="awaiting_corroboration")
                    if hold_archive is not None:
                        hold_archive(image, etype, s, event)
                    else:
                        sentlog.archive_review_if_configured(
                            image, sentlog.review_meta(cfg.name, "hold", etype, s, event))
                    _observe(observe, event, etype, False)
                    return
                if verdict == "drop":
                    log.info("drop %s: score %.2f below threshold %.2f",
                             etype, s, cfg.scorer.threshold)
                    audit_event(cfg, event, etype, "live", "drop", score=s,
                                threshold=cfg.scorer.threshold, reason="below_threshold")
                    sample_drop(cfg, image, etype, s, "live", event)
                    _observe(observe, event, etype, False)
                    return
                empty = False   # verdict == "send": fall through to the send block
            if etype == "motion":
                if empty:
                    # defer_motion was handled above, before the corroborate gate.
                    if score is not None:
                        # Keep the score in the trace: threshold calibration reads this.
                        log.info("drop %s: score %.2f below threshold %.2f",
                                 etype, s, cfg.scorer.threshold)
                        audit_event(cfg, event, etype, "live", "drop", score=s,
                                    threshold=cfg.scorer.threshold, reason="below_threshold")
                        sample_drop(cfg, image, etype, s, "live", event)
                    else:
                        log.info("drop %s: Groq reports empty scene", etype)
                        audit_event(cfg, event, etype, "live", "drop", reason="empty")
                    _observe(observe, event, etype, False)
                    return
            elif empty and defer is not None:
                if burst_sent is not None and burst_sent():
                    # A frame of this same passage already went out (e.g. a bare-motion
                    # frame seconds earlier scored as the subject); an SD follow-up
                    # would repeat it.
                    log.info("drop %s: live empty, burst already alerted", etype)
                    audit_event(cfg, event, etype, "live", "drop", score=s,
                                threshold=cfg.scorer.threshold if score is not None else None,
                                reason="burst_already_sent")
                    _observe(observe, event, etype, False)
                    return
                if _keeps_sampling(cfg) and score is not None:
                    # sampler.keep_sampling_on_defer: queue the follow-up and arm the
                    # cooldown as always, but leave the burst open. A group marked sent
                    # stops the sampler that often photographs the person seconds later,
                    # and the cooldown would skip the next live person frames of this
                    # very passage; the person then arrives minutes late from the card,
                    # or not at all. So the sampler keeps sampling, and a sampler or live
                    # frame showing the person at the threshold may pass the cooldown
                    # while this follow-up is queued (open_burst). Once one is delivered
                    # the follow-up is dropped; when none is it sends as before. Only
                    # with a scorer: without one nothing can tell a person photo.
                    _on_alert(on_alert, etype, event)
                    if not _defer(defer, event, etype, False, keep_sampling=True):
                        log.info("drop %s: live empty, follow-up of this passage already "
                                 "queued", etype)
                        audit_event(cfg, event, etype, "live", "drop", score=s,
                                    threshold=cfg.scorer.threshold,
                                    reason="followup_pending",
                                    extra={"keep_sampling": "true"})
                        _observe(observe, event, etype, False)
                        return
                    log.info("defer %s: live empty, SD follow-up queued, burst kept open",
                             etype)
                    audit_event(cfg, event, etype, "live", "defer", score=s,
                                threshold=cfg.scorer.threshold, reason="below_threshold",
                                extra={"keep_sampling": "true"})
                    _observe(observe, event, etype, False)
                    return
                # Camera confirmed a person but the frame shows nothing — hand it to
                # the SD follow-up (live_sent=False) instead of pinging a blank photo.
                # Still record the alert so the per-type cooldown sees this person.
                log.info("defer %s: live empty, SD follow-up queued (no live send)", etype)
                _on_alert(on_alert, etype, event)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", score=s,
                            threshold=cfg.scorer.threshold if score is not None else None,
                            reason="below_threshold" if score is not None else "empty")
                _observe(observe, event, etype, True)
                return
            elif empty:
                # No SD path (sd_snapshot off): keep the always-send safety net — a
                # confirmed person still goes out on a stale/empty frame so we never
                # miss one; just drop the misleading (empty) caption.
                description = ""
            if score is not None and cfg.enrich.groq and not description:
                # Caption-only Groq for an already-approved frame; the empty marker
                # would be a misleading caption, not a veto.
                description = enrich.groq_describe(groq_key, image)
                if notify.is_empty_scene(description):
                    description = ""
            label = enrich.face_label(face_ids(event), face_names)
            light = (camera.whitelamp_seen(cam, cfg.name, event.get("start_time"),
                                           force_time=cfg.whitelamp_force_time)
                     if cfg.enrich.light_status else None)
            caption = notify.build_caption(
                TYPE_EMOJI.get(etype, "👁"), time_str(event),
                description=description or None, detail=label or None, score=s,
                light=light, lens=album_lens(image),
            )
            incident = incident_id(cfg.name, event)
            ok = (send_alert(image, caption, s, incident=incident, send_path="live",
                             etype=etype)
                  if send_alert is not None
                  else notify.send_photo(telegram_token, telegram_chat, image, caption,
                                         incident=incident, send_path="live"))
            audit_event(cfg, event, etype, "live", "send", score=s,
                        threshold=cfg.scorer.threshold if score is not None else None,
                        telegram=ok)
            if ok:
                log.info("alert %s sent (faces=%r, desc=%r)", etype, label, description)
                _on_alert(on_alert, etype, event, s,
                          cfg.scorer.threshold if score is not None else None)
                _observe(observe, event, etype, True, delivered=True)
            else:
                # The event watermark has already advanced, so the live poll cannot
                # simply see this event again. Hand it to an available SD follow-up;
                # otherwise report it unsent so the sampler can keep the group open.
                if defer is not None:
                    log.warning("alert %s Telegram delivery failed; SD retry queued", etype)
                    defer(event, etype, False)
                    _observe(observe, event, etype, True)
                else:
                    log.warning("alert %s Telegram delivery failed", etype)
                    _observe(observe, event, etype, False)
        finally:
            _safe_unlink(image)

    for event, etype in alertable:
        try:
            _process(event, etype)
        except Exception as exc:
            # The daemon only advances its watermark when run_monitor returns, so the
            # error must not escape (events that already alerted would replay). But a
            # confirmed person must not be lost to a transient failure either: stop here,
            # return the watermark just before this event and retry it next poll. After
            # EVENT_FAILURE_LIMIT failures of the same event it is skipped. Class only:
            # the text can carry a camera address or token.
            name = type(exc).__name__
            skip = _event_failed(cfg, event)
            log.error("event processing failed (%s): %s %s event t=%s",
                      name, "skipping" if skip else "will retry", etype,
                      event.get("start_time"))
            try:
                audit_event(cfg, event, etype, "live", "error", reason=name)
            except Exception:
                pass
            # No poll_observe(False) here: the event API answered, and a False would be
            # reset by the next poll's True, so it could never build an outage duration.
            # The audit line and the journal carry this failure.
            if skip:
                continue
            try:
                return max(last_seen, event["start_time"] - 1)
            except (KeyError, TypeError):
                return last_seen
        else:
            _EVENT_FAILURES.pop((cfg.name, event.get("start_time")), None)
    return watermark
