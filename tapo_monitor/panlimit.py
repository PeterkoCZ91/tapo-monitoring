"""Soft pan-limit: keep a PTZ camera within the span of its presets.

The C560WS auto-track physically follows a target anywhere — the local Tapo API exposes
no angular limit and no motor position, so it can swing past the last useful preset into
a wall and sit there until ``back_time`` returns it. ONVIF *does* expose position
(``GetStatus`` -> ``Position.PanTilt.x``) and each preset's position (``GetPresets`` ->
``PTZPosition``), so we can read the current pan, and when it drifts outside the
[leftmost preset, rightmost preset] span, recall the camera to that bounding preset.

The pure decision (:func:`limit_target`, :func:`bounds_from_presets`) is separated from
the ONVIF I/O so it can be tested without a camera.
"""

import logging

log = logging.getLogger(__name__)


def bounds_from_presets(presets):
    """``(x_min, min_token, x_max, max_token)`` from ``[(token, pan_x), ...]``, or None.

    The camera's own presets define the allowed pan span: the leftmost and rightmost are
    the bounds. None when fewer than two presets carry a position. Pure.
    """
    pts = [(float(x), str(tok)) for tok, x in presets if x is not None]
    if len(pts) < 2:
        return None
    lo = min(pts, key=lambda p: p[0])
    hi = max(pts, key=lambda p: p[0])
    return (lo[0], lo[1], hi[0], hi[1])


def limit_target(x, bounds, margin=0.01):
    """Preset token to recall to when pan ``x`` is outside the bounds, else None. Pure.

    ``bounds`` is ``(x_min, min_token, x_max, max_token)``. A ``margin`` of slack keeps
    normal tracking near a bound from ping-ponging.
    """
    if bounds is None or x is None:
        return None
    x_min, min_token, x_max, max_token = bounds
    if x > x_max + margin:
        return max_token
    if x < x_min - margin:
        return min_token
    return None


# ── ONVIF I/O (thin; not unit-tested) ────────────────────────────────────────

def build_ptz(host, port, user, password):  # pragma: no cover - network I/O
    """Return ``(ptz_service, profile_token)`` for a camera's ONVIF PTZ, or raise."""
    from onvif import ONVIFCamera

    cam = ONVIFCamera(host, int(port), user, password)
    profile_token = cam.create_media_service().GetProfiles()[0].token
    return cam.create_ptz_service(), profile_token


def read_pan_x(ptz, profile_token):  # pragma: no cover - network I/O
    """Current pan ``x`` from ONVIF GetStatus, or None."""
    status = ptz.GetStatus({"ProfileToken": profile_token})
    pos = getattr(status, "Position", None)
    pantilt = getattr(pos, "PanTilt", None) if pos else None
    return float(pantilt.x) if pantilt is not None and pantilt.x is not None else None


def read_preset_bounds(ptz, profile_token):  # pragma: no cover - network I/O
    """Read presets and derive the pan bounds tuple (see :func:`bounds_from_presets`)."""
    presets = ptz.GetPresets({"ProfileToken": profile_token})
    pairs = []
    for p in presets or []:
        pos = getattr(p, "PTZPosition", None)
        pantilt = getattr(pos, "PanTilt", None) if pos else None
        if pantilt is not None and pantilt.x is not None:
            pairs.append((getattr(p, "token", None), pantilt.x))
    return bounds_from_presets(pairs)


def goto_preset(ptz, profile_token, preset_token):  # pragma: no cover - network I/O
    """Recall the camera to a preset via ONVIF."""
    ptz.GotoPreset({"ProfileToken": profile_token, "PresetToken": str(preset_token)})
