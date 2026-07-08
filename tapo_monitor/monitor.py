"""Detection pipeline: poll a camera's events, classify, enrich and notify.

The pure core is :func:`collect_detections` — given a batch of ``getEvents`` results and a
watermark, it returns the new alertable detections and the advanced watermark. Enrichment
(snapshot + Groq) and notification (Telegram) are thin I/O wired in :func:`run_monitor`,
with their side-effecting pieces injected so the orchestration stays testable.
"""

import logging
import os

from . import camera, detection, enrich, notify

log = logging.getLogger(__name__)


def _safe_unlink(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to remove temp file %s", path, exc_info=True)


def face_ids(event):
    info = event.get("event_info")
    if not isinstance(info, list):
        return []
    return [item["face_id"] for item in info
            if isinstance(item, dict) and item.get("face_id") is not None]


def collect_detections(events, last_seen, strict_people=True):
    """Return (alertable, new_watermark).

    ``alertable`` is a list of (event, event_type) for events newer than ``last_seen``
    that classify as something worth alerting on (vehicles skipped; bare motion dropped
    under ``strict_people``). ``new_watermark`` advances to the newest seen start_time.
    """
    fresh = camera.new_events(events, last_seen)
    alertable = []
    for ev in fresh:
        faces = face_ids(ev)
        flags = detection.decode_events_1(ev.get("events_1"))
        etype = detection.classify_getevent(
            ev.get("event_type") or ev.get("type"),
            has_face=bool(faces),
            strict_people=strict_people,
            events_1=ev.get("events_1"),
        )
        # Audit trail: log every camera event with its decoded signal and our verdict,
        # so "the camera is wrong" vs "our parsing is wrong" is always answerable and the
        # still-unmapped AI bits (alarm_type 4/8/9) can be ground-truthed from real traffic.
        log.info(
            "event t=%s events_1=%d motion=%d pir=%d person=%d unknown_bits=%s "
            "alarm_type=%s faces=%d -> %s",
            ev.get("start_time"), flags["raw"], flags["motion"], flags["pir"],
            flags["person"], flags["unknown_bits"], ev.get("alarm_type"),
            len(faces), etype or "drop",
        )
        if etype:
            alertable.append((ev, etype))
    return alertable, (camera.newest_start(fresh) or last_seen)


TYPE_EMOJI = {"person": "👤", "vehicle": "🚗", "pet": "🐾", "tamper": "⚠️", "motion": "👁"}


def run_monitor(cam, cfg, last_seen, *, now, groq_key, telegram_token, telegram_chat,
                snapshot, time_str, can_alert=None, on_alert=None, face_names=None,
                defer=None):
    """Poll one camera once and alert on new detections. Returns the new watermark.

    Side-effecting collaborators are injected:
      snapshot(cam, event) -> image path or None
      time_str(event) -> caption time string
      can_alert(etype) -> bool gate (per-type cooldown / rate-limit); default always True
      on_alert(etype) -> called once after an alert is actually sent (record timestamp)
      defer(event, etype, live_sent) -> enqueue a detection for a deferred SD-frame
        follow-up. ``live_sent`` says whether a live photo already went out (True) or the
        live grab failed (False). Confirmed detections defer when the live frame was empty
        or failed; PIR-backed bare motion may defer only when ``cfg.sd_motion`` is enabled.
    """
    try:
        events = cam.getEvents() or []
    except Exception:
        return last_seen

    alertable, watermark = collect_detections(events, last_seen, cfg.detection.strict_people)
    for event, etype in alertable:
        event_flags = detection.decode_events_1(event.get("events_1"))
        defer_motion = (
            etype == "motion"
            and cfg.sd_motion
            and event_flags["motion"]
            and event_flags["pir"]
            and defer is not None
        )
        if can_alert is not None and not can_alert(etype):
            if etype != "motion" and face_ids(event):
                # A recognized face is new information, not a burst duplicate — the
                # cooldown must not eat it (live 2026-07-06 18:37: the camera named 3
                # known faces 40 s after a person alert and the alert was skipped).
                log.info("cooldown override %s: recognized face present", etype)
            else:
                log.info("skip %s: cooldown active", etype)
                break
        image = snapshot(cam, event)
        if not image:
            # RTSP capture on a slow Pi (e.g. Pi Zero) fails transiently — one retry
            # catches most of those so a confirmed person isn't lost to a single hiccup.
            image = snapshot(cam, event)
        if not image:
            # Confirmed person but no live frame: queue an SD follow-up that MUST send
            # (live_sent=False) so the person isn't lost. PIR-backed motion can opt into
            # the same second chance, but still must find a subject in SD before alerting.
            if defer is not None and etype != "motion":
                log.warning("defer %s: live snapshot failed, SD follow-up queued", etype)
                defer(event, etype, False)
            elif defer_motion:
                log.warning("defer %s: live snapshot failed, SD follow-up queued", etype)
                defer(event, etype, False)
            else:
                log.warning("skip %s: snapshot failed (after retry)", etype)
            continue
        try:
            description = enrich.groq_describe(groq_key, image) if cfg.enrich.groq else ""
            # Groq disabled = raw mode: there is no arbiter to declare a scene empty, so
            # nothing is — every live frame goes straight out (the human is the filter).
            empty = notify.is_empty_scene(description) if cfg.enrich.groq else False
            if etype == "motion":
                # Bare motion is an unconfirmed candidate: Groq is the arbiter. The prompt
                # makes it reply exactly "empty scene" for anything that isn't a person or
                # animal, so a non-empty reply means a living subject — even one described
                # only by clothing ("grey hoodie walking") with no person noun.
                if empty:
                    if defer_motion:
                        log.info("defer %s: live empty, SD follow-up queued", etype)
                        defer(event, etype, False)
                        continue
                    log.info("drop %s: Groq reports empty scene", etype)
                    continue
            elif empty and defer is not None:
                # Camera confirmed a person (AI bit / face) but the live frame is empty —
                # a mistimed grab or a phantom fire. Don't ping an empty photo now: hand it
                # to the SD follow-up (live_sent=False), which sends the real event-time
                # frame if it finds the subject and an event-time fallback otherwise. This
                # drops the duplicate empty-then-real ping the always-send rule produced.
                # Still record the alert so the per-type cooldown sees this person.
                log.info("defer %s: live empty, SD follow-up queued (no live send)", etype)
                if on_alert is not None:
                    on_alert(etype)
                defer(event, etype, False)
                continue
            elif empty:
                # No SD path (sd_snapshot off): keep the always-send safety net — a
                # confirmed person still goes out on a stale/empty frame so we never miss
                # one; just drop the misleading (empty) caption.
                description = ""
            label = enrich.face_label(face_ids(event), face_names)
            caption = notify.build_caption(
                TYPE_EMOJI.get(etype, "👁"), time_str(event),
                description=description or None, detail=label or None,
            )
            notify.send_photo(telegram_token, telegram_chat, image, caption)
            log.info("alert %s sent (faces=%r, desc=%r)", etype, label, description)
            if on_alert is not None:
                on_alert(etype)
        finally:
            _safe_unlink(image)
    return watermark
