"""Detection pipeline: poll a camera's events, classify, enrich and notify.

The pure core is :func:`collect_detections` — given a batch of ``getEvents`` results and a
watermark, it returns the new alertable detections and the advanced watermark. Enrichment
(snapshot + Groq) and notification (Telegram) are thin I/O wired in :func:`run_monitor`,
with their side-effecting pieces injected so the orchestration stays testable.
"""

from . import camera, detection, enrich, notify


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
        etype = detection.classify_getevent(
            ev.get("event_type") or ev.get("type"),
            has_face=bool(_face_ids(ev)),
            strict_people=strict_people,
        )
        if etype:
            alertable.append((ev, etype))
    return alertable, (camera.newest_start(fresh) or last_seen)


_TYPE_EMOJI = {"person": "👤", "vehicle": "🚗", "pet": "🐾", "tamper": "⚠️", "motion": "👁"}


def run_monitor(cam, cfg, last_seen, *, now, groq_key, telegram_token, telegram_chat,
                snapshot, time_str):
    """Poll one camera once and alert on new detections. Returns the new watermark.

    Side-effecting collaborators are injected:
      snapshot(cam, event) -> image path or None
      time_str(event) -> caption time string
    """
    try:
        events = cam.getEvents() or []
    except Exception:
        return last_seen

    alertable, watermark = collect_detections(events, last_seen, cfg.detection.strict_people)
    for event, etype in alertable:
        image = snapshot(cam, event)
        if not image:
            continue
        description = enrich.groq_describe(groq_key, image) if cfg.enrich.groq else ""
        if notify.is_empty_scene(description):
            continue
        caption = notify.build_caption(
            _TYPE_EMOJI.get(etype, "👁"), time_str(event), description=description or None
        )
        notify.send_photo(telegram_token, telegram_chat, image, caption)
    return watermark
