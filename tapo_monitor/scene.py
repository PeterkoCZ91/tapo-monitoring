"""Small, in-memory correlation gate for overlapping camera views."""

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class _Delivery:
    camera: str
    event_type: str
    event_at: float
    observed_at: float


@dataclass(frozen=True)
class SceneEvent:
    """A read-only summary of deliveries from an overlapping camera group."""

    group: str
    event_at: float
    lead_camera: str
    follow_camera: str
    delta_seconds: float
    direction: str | None
    cameras: tuple[str, ...]


def _event_time(event):
    try:
        return float(event.get("start_time"))
    except (AttributeError, TypeError, ValueError):
        return None


def choose_best_frame(candidates):
    """Choose the highest-scoring frame with deterministic tie breaking.

    Candidates are mappings with ``score`` and optional ``captured_at``, ``camera`` and
    ``frame`` fields. Invalid scores are ignored; ties prefer the earliest capture and
    then stable camera/frame names. The original mapping is returned unchanged.
    """
    valid = []
    for candidate in candidates or ():
        try:
            score = float(candidate.get("score"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        captured = candidate.get("captured_at", float("inf"))
        try:
            captured = float(captured)
        except (TypeError, ValueError):
            captured = float("inf")
        valid.append((score, captured, str(candidate.get("camera", "")),
                      str(candidate.get("frame", "")), candidate))
    if not valid:
        return None
    return min(valid, key=lambda item: (-item[0], item[1], item[2], item[3]))[-1]


def estimate_clock_offset(pairs):
    """Return the median ``other - reference`` offset from timestamp pairs.

    Invalid or non-finite pairs are ignored. ``None`` means there is no usable evidence;
    the estimator never invents zero as a measurement.
    """
    offsets = []
    for reference, other in pairs or ():
        try:
            reference, other = float(reference), float(other)
        except (TypeError, ValueError):
            continue
        if math.isfinite(reference) and math.isfinite(other):
            offsets.append(other - reference)
    return statistics.median(offsets) if offsets else None


class SceneCoordinator:
    """Deduplicate equivalent detections from cameras in one configured group."""

    def __init__(self):
        self._deliveries: dict[str, list[_Delivery]] = {}
        self._candidates: dict[str, list[dict]] = {}
        self._clock_readings: dict[str, list[tuple[float, float]]] = {}

    @staticmethod
    def _active(deliveries, event_at, window):
        return [item for item in deliveries if abs(item.event_at - event_at) <= window]

    def allows(self, group, camera, event_type, event, now, *, window=15):
        """Return whether this detection may proceed to snapshot and notification."""
        if not group:
            return True
        event_at = _event_time(event)
        if event_at is None:
            return True
        active = self._active(self._deliveries.get(group, []), event_at, window)
        if event_type == "motion":
            return not any(item.camera != camera for item in active)
        return not any(
            item.camera != camera and item.event_type == event_type
            for item in active
        )

    def record_delivery(self, group, camera, event_type, event, now, *, window=15):
        """Commit one delivered detection as the group watermark."""
        if not group:
            return
        event_at = _event_time(event)
        if event_at is None:
            return
        deliveries = self._deliveries.setdefault(group, [])
        deliveries[:] = [
            item for item in deliveries
            if abs(item.event_at - event_at) <= max(window, 1) * 2
        ]
        deliveries.append(_Delivery(camera, event_type, event_at, float(now)))
        if len(deliveries) > 32:
            del deliveries[:-32]

    def scene_event(self, group, event_at, *, window=15, camera_order=()):
        """Summarize an observed multi-camera scene without changing alert behavior.

        ``camera_order`` is an explicit, operator-measured sequence from first view to
        next view. An absent or incomplete order leaves ``direction`` unknown; geometry
        is never inferred from camera names or event timing alone.
        """
        try:
            event_at = float(event_at)
            window = float(window)
        except (TypeError, ValueError):
            return None
        if window < 0:
            return None
        active = sorted(self._active(self._deliveries.get(group, []), event_at, window),
                        key=lambda item: (item.event_at, item.observed_at, item.camera))
        cameras = tuple(dict.fromkeys(item.camera for item in active))
        if len(cameras) < 2:
            return None
        lead, follow = cameras[0], cameras[-1]
        order = tuple(camera_order)
        direction = None
        if lead in order and follow in order:
            lead_index, follow_index = order.index(lead), order.index(follow)
            if lead_index != follow_index:
                direction = "forward" if lead_index < follow_index else "reverse"
        return SceneEvent(group, event_at, lead, follow,
                          active[-1].event_at - active[0].event_at,
                          direction, cameras)

    def record_candidate(self, group, camera, frame_path, score, *, captured_at=None, window=15):
        """Record an alert candidate frame for cross-camera selection.

        Candidates within the correlation window are tracked per group so the best
        available frame across cameras can be identified.
        """
        if not group or not frame_path:
            return
        try:
            score = float(score)
        except (TypeError, ValueError):
            return
        candidates = self._candidates.setdefault(group, [])
        now_ts = float(captured_at if captured_at is not None else 0.0)
        candidates[:] = [
            item for item in candidates
            if abs(item.get("captured_at", now_ts) - now_ts) <= max(window, 1) * 2
        ]
        candidates.append({
            "camera": str(camera),
            "frame": str(frame_path),
            "score": score,
            "captured_at": now_ts,
        })
        if len(candidates) > 32:
            del candidates[:-32]

    def best_candidate(self, group, event_at, *, window=15):
        """Return the highest-scoring candidate frame across the group, or None."""
        if not group or group not in self._candidates:
            return None
        try:
            event_at = float(event_at)
        except (TypeError, ValueError):
            return None
        candidates = self._candidates[group]
        active = [
            item for item in candidates
            if abs(item.get("captured_at", event_at) - event_at) <= window
        ]
        return choose_best_frame(active)

    def record_clock_reading(self, camera, host_time, camera_time):
        """Record an observed (host_time, camera_time) pair for clock offset estimation."""
        if not camera:
            return
        try:
            h, c = float(host_time), float(camera_time)
        except (TypeError, ValueError):
            return
        readings = self._clock_readings.setdefault(camera, [])
        readings.append((h, c))
        if len(readings) > 32:
            del readings[:-32]

    def clock_offset(self, camera):
        """Return the estimated clock offset (camera - host) in seconds, or None."""
        readings = self._clock_readings.get(camera)
        return estimate_clock_offset(readings) if readings else None


# Phase 5 read-only scene summary is exposed through SceneCoordinator.scene_event.
