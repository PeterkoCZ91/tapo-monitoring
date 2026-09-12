"""Small, in-memory correlation gate for overlapping camera views."""

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


class SceneCoordinator:
    """Deduplicate equivalent detections from cameras in one configured group."""

    def __init__(self):
        self._deliveries: dict[str, list[_Delivery]] = {}

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


# Phase 5 read-only scene summary is exposed through SceneCoordinator.scene_event.
