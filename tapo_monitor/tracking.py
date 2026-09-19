"""Auto-tracking and SmartTrack control.

Two parts:

* Pure decisions — given the camera role, night/rain state and weather strategy, what
  should tracking and motion sensitivity be. Trivially testable.
* Camera I/O — apply those decisions in the firmware-safe order.

**Ordering hazard (validated on C560WS):** ``setSmartTrackConfig`` silently clears the
auto-track master switch. Therefore SmartTrack/preset/sensitivity are applied first and
``setAutoTrackTarget`` is always asserted **last** (and verified). ``ensure_autotrack``
encapsulates this so callers can't get the order wrong.
"""

import logging
import time as _time

log = logging.getLogger(__name__)

SMARTTRACK_KEYS = {
    "people": "people_enabled",
    "vehicle": "vehicle_enabled",
    "pet": "pet_enabled",
    "baby": "baby_enabled",
}


def decide_tracking(role, night, rain_active, strategy, storm_park=False):
    """Return (autotrack_on, rain_parked) for one camera tick.

    - static cameras never track;
    - tracking cameras track only at night;
    - rain parks the camera (tracking off) when either the ``disable_tracking`` weather
      strategy is set or ``storm_park`` is on. ``storm_park`` is independent of the
      sensitivity strategy, so a ``lower_sensitivity`` camera can both lower motion
      sensitivity *and* stop swinging after raindrops/branches in the rain.
    """
    if role == "static" or not night:
        return (False, False)
    if rain_active and (strategy == "disable_tracking" or storm_park):
        return (False, True)
    return (True, False)


def decide_motion_sensitivity(rain_active, normal, rain, strategy="lower_sensitivity"):
    """Lower motion sensitivity while raining under the ``lower_sensitivity`` strategy."""
    if strategy == "lower_sensitivity" and rain_active:
        return rain
    return normal


def smarttrack_payload(kinds):
    """Build a setSmartTrackConfig payload enabling only the given SmartTrack kinds."""
    info = {field: "off" for field in SMARTTRACK_KEYS.values()}
    for kind in kinds:
        if kind in SMARTTRACK_KEYS:
            info[SMARTTRACK_KEYS[kind]] = "on"
    return {"smart_track": {"smart_track_info": info}}


def apply_smarttrack(cam, kinds):
    """Enable only the requested SmartTrack kinds (people/vehicle/pet/baby)."""
    cam.executeFunction("setSmartTrackConfig", smarttrack_payload(kinds))


def set_autotrack(cam, enabled, back_time=None):
    """Set the auto-track master switch, trying known method shapes. Returns bool.

    ``back_time`` is the firmware's own return timer: how many seconds the camera keeps
    looking where auto-track took it before swinging back to where the track started
    (30 s out of the box on the C560WS). It rides along in the SAME setTargetTrackConfig
    request as the master switch, and deliberately so — see the module docstring: nothing
    may run between ``apply_smarttrack`` and the auto-track assert, and a second call to
    write the dwell would sit in exactly that gap. A camera that refuses the combined
    payload still gets tracking asserted by the ordinary path below.
    """
    if back_time is not None:
        try:
            cam.executeFunction("setTargetTrackConfig", {"target_track": {
                "target_track_info": {"enabled": "on" if enabled else "off",
                                      "back_time": str(int(back_time))}}})
            return True
        except Exception as exc:
            log.warning("camera refused the combined auto-track/back_time call (%s); "
                        "falling back to the plain switch", exc)
    if hasattr(cam, "setAutoTrackTarget"):
        try:
            cam.setAutoTrackTarget(enabled)
            return True
        except Exception:
            pass
    value = "on" if enabled else "off"
    try:
        cam.executeFunction("setAutoTrackTarget", {"auto_track_target": {"enabled": value}})
        return True
    except Exception:
        return False


def verify_autotrack(cam, expected, back_time=None):
    """Read the auto-track state back and compare to expected.

    A ``back_time`` that did not land is reported but does not fail the check: tracking
    being on is what the night depends on, a longer dwell is a comfort on top of it, and
    conflating the two would turn a refused comfort into "auto-track not confirmed". It
    must still be said out loud — a camera quietly keeping its 30 s looks exactly like one
    that took 180, and in that state the whole dwell is worth nothing.
    """
    try:
        info = cam.getAutoTrackTarget()
        actual = info.get("enabled", "").lower() == "on"
        if back_time is not None and str(info.get("back_time")) != str(int(back_time)):
            log.warning("camera did not take back_time=%s (reads %s): auto-track will "
                        "still pull the lens home early", back_time, info.get("back_time"))
        return actual == expected
    except Exception:
        return False


def ensure_autotrack(cam, enabled, sleep=None, back_time=None):
    """Assert auto-track LAST and verify; one retry. Returns True on success.

    ``sleep`` is injectable so tests run without real delays (resolved at call time).
    ``back_time`` travels with the assert; see :func:`set_autotrack`.
    """
    if sleep is None:
        sleep = _time.sleep
    if not set_autotrack(cam, enabled, back_time=back_time):
        return False
    sleep(1)
    if verify_autotrack(cam, enabled, back_time=back_time):
        return True
    sleep(3)
    set_autotrack(cam, enabled, back_time=back_time)
    sleep(2)
    return verify_autotrack(cam, enabled, back_time=back_time)
