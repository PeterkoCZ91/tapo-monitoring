"""Bounded, side-effect-free PTZ handoff lease state."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HandoffLease:
    """A temporary preset lease with the policy to restore when it expires."""

    group: str
    source_camera: str
    target_camera: str
    preset: str
    started_at: float
    expires_at: float
    previous_policy: str | None

    def expired(self, now: float) -> bool:
        return _time(now, "now") >= self.expires_at


class HandoffManager:
    """Track at most one bounded handoff per camera group."""

    def __init__(self):
        self._leases: dict[str, HandoffLease] = {}

    def begin(self, *, group: str, source_camera: str, target_camera: str,
              preset: str, now: float, duration: float,
              previous_policy: str | None = None) -> HandoffLease:
        group = _name(group, "group")
        source_camera = _name(source_camera, "source_camera")
        target_camera = _name(target_camera, "target_camera")
        preset = _name(preset, "preset")
        if source_camera == target_camera:
            raise ValueError("source_camera and target_camera must differ")
        now = _time(now, "now")
        duration = _time(duration, "duration")
        if duration <= 0:
            raise ValueError("duration must be positive")
        lease = HandoffLease(group, source_camera, target_camera, preset, now,
                             now + duration, previous_policy)
        self._leases[group] = lease
        return lease

    def active(self, group: str, now: float) -> HandoffLease | None:
        group = _name(group, "group")
        lease = self._leases.get(group)
        if lease is None or lease.expired(now):
            return None
        return lease

    def expire(self, now: float) -> list[HandoffLease]:
        """Remove expired leases and return them for policy restoration."""
        now = _time(now, "now")
        expired = [lease for lease in self._leases.values() if lease.expired(now)]
        for lease in expired:
            self._leases.pop(lease.group, None)
        return sorted(expired, key=lambda lease: (lease.expires_at, lease.group))


def _time(value, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be finite") from None
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _name(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
