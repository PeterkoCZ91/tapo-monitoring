"""Event-window sampling: group event bursts, schedule follow-up grabs. Pure logic.

A Tapo camera merges a whole passage into one long event (tens of seconds to minutes)
but the inline pipeline only ever saw its first seconds — a subject appearing mid-event
was structurally invisible (live miss 2026-07-07: person +172 s into a PIR event). A
*group* tracks one burst of activity per camera; while it is open and no alert has gone
out, the daemon keeps grabbing frames on a fixed schedule so a late subject is still
seen. All functions here are pure; the daemon owns the I/O.
"""


PIR_BIT = 32

# A single marginal bare-motion frame is held until this many frames in the
# [threshold, motion_send_threshold) band accumulate within the group's window.
MOTION_CONFIRM_FRAMES = 2


def _is_pir_backed(event):
    """True when a getEvents event carries the camera's hardware-PIR flag."""
    try:
        return bool(int(event.get("events_1", 0)) & PIR_BIT)
    except (AttributeError, TypeError, ValueError):
        return False


def ensure_group(groups, name, event, etype, now, scfg):
    """Return the camera's open group, creating a fresh (unsent) one if none is open.

    Mutates ``groups``. A group is "open" while the last event is within ``group_gap``;
    past that the burst is over and a new group starts. Shared by :func:`observe_event`
    and the corroboration wiring so the group exists as soon as the first frame scores.
    """
    g = groups.get(name)
    if g is None or (now - g["last_event_at"]) > scfg.group_gap:
        started = event.get("start_time") or now
        g = {
            "camera": name,
            "etype": etype,
            "event": event,
            "started": started,
            "last_event_at": now,
            "frames": 0,
            "next_due": started + scfg.interval,
            "sent": False,
            "delivered": False,
            "pir_backed": _is_pir_backed(event),
        }
        groups[name] = g
    return g


def observe_event(groups, name, event, etype, sent, now, scfg, delivered=False):
    """Fold one alertable event into the camera's group (create/extend). Mutates groups.

    ``sent`` means an alert already went out (or was handed to the SD follow-up) for
    this event — a sent group stops sampling but keeps absorbing the rest of the burst
    so trailing events don't spawn a fresh group. ``delivered`` is the narrower fact: a
    photo of this burst actually reached Telegram. Both are sticky for the group's life;
    a queued follow-up or a refused send never sets ``delivered``.
    """
    g = ensure_group(groups, name, event, etype, now, scfg)
    g["last_event_at"] = now
    g["sent"] = g["sent"] or bool(sent)
    g["delivered"] = g.get("delivered", False) or bool(delivered)
    g["pir_backed"] = g.get("pir_backed", False) or _is_pir_backed(event)
    if etype != "motion":
        g["etype"] = etype        # camera-confirmed detection outranks bare motion
        g["event"] = event
        # A camera-confirmed detection overrules a low-score early exit: the burst
        # is live again, so resume sampling for the rest of the window.
        g.pop("early_exit", None)
        g.pop("low_streak", None)


def due(group, now, scfg):
    """True when a follow-up grab should happen now. Pure."""
    return (not group["sent"]
            and not group.get("early_exit")
            and group["frames"] < scfg.max_frames
            and now >= group["next_due"])


def note_score(group, score, scfg):
    """Fold one scored frame into the group's low-score streak. Mutates the group.

    Returns True exactly when this call closes the group early: a *motion-only*
    group whose last ``scfg.low_score_exit`` consecutive frames all scored below
    ``scfg.low_score`` is judged empty (rustling foliage, light, insects) and stops
    sampling. Person/PIR-confirmed groups never exit early — the whole point of the
    window is a subject appearing mid-event, so only bare motion may give up.
    """
    if score >= scfg.low_score:
        group.pop("low_streak", None)
        return False
    streak = group.get("low_streak", 0) + 1
    group["low_streak"] = streak
    if (scfg.low_score_exit > 0 and group["etype"] == "motion" and not group.get("pir_backed")
            and streak >= scfg.low_score_exit and not group.get("early_exit")):
        group["early_exit"] = True
        return True
    return False


def corroborate_motion(group, score, confirm, send_now):
    """Decide a non-PIR bare-motion frame; mutate the group's candidate counter.

    An empty IR scene hallucinates "person" on one frame and not the next, while a
    real subject persists — so a single marginal frame is held for corroboration.
      score >= send_now            -> "send"  (a clear single frame; no wait)
      confirm <= score < send_now  -> candidate; "send" once MOTION_CONFIRM_FRAMES reached
      score < confirm              -> "drop"  (does not reset the candidate count)

    A held frame also stashes its score as ``last_hold_score`` on the group, so an
    expiring hold can be audited with the score it was holding.
    """
    if score >= send_now:
        return "send"
    if score >= confirm:
        group["last_hold_score"] = score
        group["motion_candidates"] = group.get("motion_candidates", 0) + 1
        return "send" if group["motion_candidates"] >= MOTION_CONFIRM_FRAMES else "hold"
    return "drop"


def record_grab(group, now, scfg):
    """Account one grab attempt and schedule the next. Mutates the group."""
    group["frames"] += 1
    group["next_due"] = now + scfg.interval


def expired(group, now, scfg):
    """True when the group is finished and should be dropped. Pure.

    An unsent group must survive its whole sampling window (started +
    interval*max_frames) even with no further events — that window is the entire
    point. A sent or exhausted group only lingers group_gap past the last event so
    the tail of the burst still folds in instead of spawning a new group.
    """
    activity_end = group["last_event_at"] + scfg.group_gap
    if group["sent"] or group.get("early_exit") or group["frames"] >= scfg.max_frames:
        return now > activity_end
    sample_end = group["started"] + scfg.interval * scfg.max_frames
    # Unsent groups survive the sample window plus group_gap to fold in trailing events
    return now > (sample_end + scfg.group_gap)
