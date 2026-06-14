"""Detection pipeline: poll a camera's events, classify, enrich and notify.

The pure core is :func:`collect_detections` — given a batch of ``getEvents`` results and a
watermark, it returns the new alertable detections and the advanced watermark. Enrichment
(snapshot + Groq) and notification (Telegram) are thin I/O wired in :func:`run_monitor`,
with their side-effecting pieces injected so the orchestration stays testable.
"""

import logging

from . import camera, detection, enrich, notify

log = logging.getLogger(__name__)


def _face_ids(event):
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
        faces = _face_ids(ev)
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


_TYPE_EMOJI = {"person": "👤", "vehicle": "🚗", "pet": "🐾", "tamper": "⚠️", "motion": "👁"}


def run_monitor(cam, cfg, last_seen, *, now, groq_key, telegram_token, telegram_chat,
                snapshot, time_str, can_alert=None, on_alert=None, face_names=None):
    """Poll one camera once and alert on new detections. Returns the new watermark.

    Side-effecting collaborators are injected:
      snapshot(cam, event) -> image path or None
      time_str(event) -> caption time string
      can_alert() -> bool gate (cooldown / rate-limit); default always True
      on_alert() -> called once after an alert is actually sent (record timestamp)
    """
    try:
        events = cam.getEvents() or []
    except Exception:
        return last_seen

    alertable, watermark = collect_detections(events, last_seen, cfg.detection.strict_people)
    for event, etype in alertable:
        if can_alert is not None and not can_alert():
            log.info("skip %s: cooldown active", etype)
            break
        image = snapshot(cam, event)
        if not image:
            log.warning("skip %s: snapshot failed", etype)
            continue
        description = enrich.groq_describe(groq_key, image) if cfg.enrich.groq else ""
        # Groq is the arbiter for both camera-confirmed people and bare-motion
        # candidates: the prompt makes it reply exactly "empty scene" for anything that
        # isn't a person/animal (vehicle, trees, empty), so a non-empty reply means a
        # living subject — even when it describes only clothing ("grey hoodie walking")
        # with no person noun. Word-matching the description here dropped real people.
        if notify.is_empty_scene(description):
            log.info("drop %s: Groq reports empty scene", etype)
            continue
        label = enrich.face_label(_face_ids(event), face_names)
        caption = notify.build_caption(
            _TYPE_EMOJI.get(etype, "👁"), time_str(event),
            description=description or None, detail=label or None,
        )
        notify.send_photo(telegram_token, telegram_chat, image, caption)
        log.info("alert %s sent (faces=%r, desc=%r)", etype, label, description)
        if on_alert is not None:
            on_alert()
    return watermark
