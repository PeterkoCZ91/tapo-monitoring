"""Event-window sampling: group event bursts, schedule follow-up grabs. Pure logic.

A Tapo camera merges a whole passage into one long event (tens of seconds to minutes)
but the inline pipeline only ever saw its first seconds — a subject appearing mid-event
was structurally invisible (live miss 2026-07-07: person +172 s into a PIR event). A
*group* tracks one burst of activity per camera; while it is open and no alert has gone
out, the daemon keeps grabbing frames on a fixed schedule so a late subject is still
seen. All functions here are pure; the daemon owns the I/O.
"""


def observe_event(groups, name, event, etype, sent, now, scfg):
    """Fold one alertable event into the camera's group (create/extend). Mutates groups.

    ``sent`` means an alert already went out (or was handed to the SD follow-up) for
    this event — a sent group stops sampling but keeps absorbing the rest of the burst
    so trailing events don't spawn a fresh group.
    """
    started = event.get("start_time") or now
    g = groups.get(name)
    if g is None or (now - g["last_event_at"]) > scfg.group_gap:
        groups[name] = {
            "camera": name,
            "etype": etype,
            "event": event,
            "started": started,
            "last_event_at": now,
            "frames": 0,
            "next_due": started + scfg.interval,
            "sent": bool(sent),
        }
        return
    g["last_event_at"] = now
    g["sent"] = g["sent"] or bool(sent)
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
    if (scfg.low_score_exit > 0 and group["etype"] == "motion"
            and streak >= scfg.low_score_exit and not group.get("early_exit")):
        group["early_exit"] = True
        return True
    return False


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
