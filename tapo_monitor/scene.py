"""Small, in-memory correlation gate for overlapping camera views."""

from dataclasses import dataclass


@dataclass(frozen=True)
class _Delivery:
    camera: str
    event_type: str
    event_at: float
    observed_at: float


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
