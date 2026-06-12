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

import time as _time

SMARTTRACK_KEYS = {
    "people": "people_enabled",
    "vehicle": "vehicle_enabled",
    "pet": "pet_enabled",
    "baby": "baby_enabled",
}


def decide_tracking(role, night, rain_active, strategy):
    """Return (autotrack_on, rain_parked) for one camera tick.

    - static cameras never track;
    - tracking cameras track only at night;
    - at night with the ``disable_tracking`` weather strategy, rain parks the camera
      (tracking off) instead.
    """
    if role == "static" or not night:
        return (False, False)
    if rain_active and strategy == "disable_tracking":
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


def set_autotrack(cam, enabled):
    """Set the auto-track master switch, trying known method shapes. Returns bool."""
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


def verify_autotrack(cam, expected):
    """Read the auto-track state back and compare to expected."""
    try:
        actual = cam.getAutoTrackTarget().get("enabled", "").lower() == "on"
        return actual == expected
    except Exception:
        return False


def ensure_autotrack(cam, enabled, sleep=None):
    """Assert auto-track LAST and verify; one retry. Returns True on success.

    ``sleep`` is injectable so tests run without real delays (resolved at call time).
    """
    if sleep is None:
        sleep = _time.sleep
    if not set_autotrack(cam, enabled):
        return False
    sleep(1)
    if verify_autotrack(cam, enabled):
        return True
    sleep(3)
    set_autotrack(cam, enabled)
    sleep(2)
    return verify_autotrack(cam, enabled)
