"""Who may move a lens right now — one decision shared by every motor path.

Two paths send motor commands and neither used to know about the other: the control pass
recalls the day/night preset through pytapo, and the pan-limit guard sends ONVIF
``GotoPreset`` on its own, faster cadence. Each had grown its own exceptions — the recall
skipped a parked lens and a held subject, the guard skipped neither — so the guard could
yank a lens off the subject a dwell was holding it on, and hammered a lens parked by
privacy mode with moves it could only refuse.

The order, highest first:

1. privacy — a parked lens answers every motor call with MOTOR_BUSY; nobody moves it;
2. linkage — the firmware of a dual-lens camera (C545D ``dualCamLinkage``) is turning
   its pan/tilt lens after a subject the fixed lens saw; neither our recall nor the
   guard moves it until the event on that lens is ``linkage_hold`` seconds old. Like
   auto-track, but owned by the firmware and independent of our auto-track switch;
3. hold — auto-track has the lens on a subject (``track_hold``, only while tracking);
4. pan-limit guard — overrides the hold once the lens has been out of bounds for
   ``hold_grace`` seconds, so a dwell never licenses staring into a wall for long;
5. scheduled preset recall.

Pure: callers supply the facts, this module only decides. It refuses; it never adds a
move that neither path would have sent.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEDULE = "schedule"     # control-pass preset recall
GUARD = "pan_limit"       # ONVIF soft pan/tilt limit

PRIVACY = "privacy"
LINKAGE = "linkage"
HOLD = "hold"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None   # why a move was refused; None when allowed


ALLOW = Decision(True)


def decide(requester, *, privacy_on=False, hold=False, autotrack_on=False,
           out_of_bounds_for=0.0, hold_grace=0.0, linkage=False) -> Decision:
    """May ``requester`` move the lens now? Pure.

    ``hold`` counts only while ``autotrack_on``: with tracking off the scheduled recall is
    the single thing that repairs a nudged aim, so a hold leaking into the day must not
    stop it. ``out_of_bounds_for`` is how long the guard has continuously seen the lens
    outside its span; it matters only to the guard, and only against a hold.

    ``linkage`` (the firmware is moving a dual-lens camera's pan/tilt lens) refuses both
    requesters whatever our auto-track switch says: it is the firmware's move, and it is
    bounded by the caller's hold window, so it cannot leak into the day the way an
    auto-track hold could.
    """
    if privacy_on:
        return Decision(False, PRIVACY)
    if linkage:
        return Decision(False, LINKAGE)
    if hold and autotrack_on:
        if requester == GUARD and out_of_bounds_for >= hold_grace:
            return ALLOW
        return Decision(False, HOLD)
    return ALLOW


def count_refusal(counters, camera, requester, reason):
    """Add one refused move to ``counters[camera]["<requester>:<reason>"]``. Mutates."""
    bucket = counters.setdefault(camera, {})
    key = f"{requester}:{reason}"
    bucket[key] = bucket.get(key, 0) + 1
