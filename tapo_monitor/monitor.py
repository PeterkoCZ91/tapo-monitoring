"""Detection pipeline: poll a camera's events, classify, enrich and notify.

The pure core is :func:`collect_detections` — given a batch of ``getEvents`` results and a
watermark, it returns the new alertable detections and the advanced watermark. Enrichment
(snapshot + Groq) and notification (Telegram) are thin I/O wired in :func:`run_monitor`,
with their side-effecting pieces injected so the orchestration stays testable.
"""

import logging
import os
import shlex

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


def _observe(observe, event, etype, sent):
    if observe is not None:
        observe(event, etype, sent)


def _fmt_score(score):
    return "none" if score is None else f"{float(score):.4f}"


def _fmt_audit_value(value):
    return shlex.quote(str(value))


def audit_event(cfg, event, etype, path, action, *, score=None, threshold=None,
                telegram=None, reason=None):
    parts = [
        f"camera={_fmt_audit_value(cfg.name)}",
        f"path={_fmt_audit_value(path)}",
        f"action={_fmt_audit_value(action)}",
        f"etype={_fmt_audit_value(etype)}",
        f"start={_fmt_audit_value(event.get('start_time', 0))}",
    ]
    if score is not None:
        parts.append(f"score={_fmt_score(score)}")
    if threshold is not None:
        parts.append(f"threshold={_fmt_score(threshold)}")
    if telegram is not None:
        parts.append(f"telegram={_fmt_audit_value(str(bool(telegram)).lower())}")
    if reason:
        parts.append(f"reason={_fmt_audit_value(reason)}")
    log.info("audit %s", " ".join(parts))


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
                defer=None, score=None, observe=None, mute=False):
    """Poll one camera once and alert on new detections. Returns the new watermark.

    ``mute`` polls and advances the watermark but skips all grabbing/scoring/alerting.
    A night_only camera runs muted during the day so the daytime backlog is drained
    silently and does not replay when night begins.

    Side-effecting collaborators are injected:
      snapshot(cam, event) -> image path or None
      time_str(event) -> caption time string
      can_alert(etype) -> bool gate (per-type cooldown / rate-limit); default always True
      on_alert(etype) -> called once after an alert is actually sent (record timestamp)
      defer(event, etype, live_sent) -> enqueue a detection for a deferred SD-frame
        follow-up. ``live_sent`` says whether a live photo already went out (True) or the
        live grab failed (False). Confirmed detections defer when the live frame was empty
        or failed; PIR-backed bare motion may defer only when ``cfg.sd_motion`` is enabled.
      score(image_path) -> float|None — local scorer subject confidence; when passed it
        replaces Groq as the send/drop arbiter (Groq only captions what already passed)
        and None (scorer unreachable) degrades to raw passthrough, never a drop.
      observe(event, etype, sent) -> feeds the sampler's event grouping; ``sent`` is
        True when this event produced an alert or was handed to the SD follow-up.
    """
    try:
        events = cam.getEvents() or []
    except Exception:
        return last_seen

    alertable, watermark = collect_detections(events, last_seen, cfg.detection.strict_people)
    if mute:
        return watermark          # night_only by day: drain silently, no grab/score/alert
    for event, etype in alertable:
        audit_event(cfg, event, etype, "getevents", "detect")
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
                audit_event(cfg, event, etype, "live", "cooldown")
                _observe(observe, event, etype, False)
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
                audit_event(cfg, event, etype, "live", "defer", reason="snapshot_failed")
                _observe(observe, event, etype, True)
            elif defer_motion:
                log.warning("defer %s: live snapshot failed, SD follow-up queued", etype)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", reason="snapshot_failed")
                _observe(observe, event, etype, True)
            else:
                log.warning("skip %s: snapshot failed (after retry)", etype)
                audit_event(cfg, event, etype, "live", "snapshot_failed")
                _observe(observe, event, etype, False)
            continue
        try:
            description = ""
            s = None
            if score is not None:
                # Local scorer is the arbiter; Groq no longer decides anything.
                s = score(image)
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
            if etype == "motion":
                if empty:
                    if defer_motion:
                        log.info("defer %s: live empty, SD follow-up queued", etype)
                        defer(event, etype, False)
                        audit_event(cfg, event, etype, "live", "defer", score=s,
                                    threshold=cfg.scorer.threshold if score is not None else None,
                                    reason="below_threshold" if score is not None else "empty")
                        _observe(observe, event, etype, True)
                        continue
                    if score is not None:
                        # Keep the score in the trace: threshold calibration reads this.
                        log.info("drop %s: score %.2f below threshold %.2f",
                                 etype, s, cfg.scorer.threshold)
                        audit_event(cfg, event, etype, "live", "drop", score=s,
                                    threshold=cfg.scorer.threshold, reason="below_threshold")
                    else:
                        log.info("drop %s: Groq reports empty scene", etype)
                        audit_event(cfg, event, etype, "live", "drop", reason="empty")
                    _observe(observe, event, etype, False)
                    continue
            elif empty and defer is not None:
                # Camera confirmed a person but the frame shows nothing — hand it to
                # the SD follow-up (live_sent=False) instead of pinging a blank photo.
                # Still record the alert so the per-type cooldown sees this person.
                log.info("defer %s: live empty, SD follow-up queued (no live send)", etype)
                if on_alert is not None:
                    on_alert(etype)
                defer(event, etype, False)
                audit_event(cfg, event, etype, "live", "defer", score=s,
                            threshold=cfg.scorer.threshold if score is not None else None,
                            reason="below_threshold" if score is not None else "empty")
                _observe(observe, event, etype, True)
                continue
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
            caption = notify.build_caption(
                TYPE_EMOJI.get(etype, "👁"), time_str(event),
                description=description or None, detail=label or None,
            )
            ok = notify.send_photo(telegram_token, telegram_chat, image, caption)
            log.info("alert %s sent (faces=%r, desc=%r)", etype, label, description)
            audit_event(cfg, event, etype, "live", "send", score=s,
                        threshold=cfg.scorer.threshold if score is not None else None,
                        telegram=ok)
            if on_alert is not None:
                on_alert(etype)
            _observe(observe, event, etype, True)
        finally:
            _safe_unlink(image)
    return watermark
