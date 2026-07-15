"""Pure configuration drift and layered camera-health evaluation.

This module deliberately has no camera, network, or persistence dependencies.  A
caller supplies a desired configuration and an already collected actual snapshot;
the returned value can be rendered by a CLI, sent to Telegram, or persisted as JSON.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import Enum
from typing import Any


class _ObservationMarker(Enum):
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


# Explicit markers avoid treating a legitimate ``None`` camera value as missing.
UNKNOWN = _ObservationMarker.UNKNOWN
UNSUPPORTED = _ObservationMarker.UNSUPPORTED
_MISSING = object()

DRIFT_STATUSES = frozenset({"match", "drift", "unknown", "unsupported"})
SEVERITIES = frozenset({"info", "warning", "critical"})
HEALTH_LAYERS = ("network", "api", "events", "rtsp", "storage")
HEALTH_STATUSES = frozenset({"ok", "degraded", "down", "unknown"})


@dataclass(frozen=True)
class DriftResult:
    """Comparison result for one desired path.

    ``key`` only depends on the caller-provided scope and path, so it remains stable
    while actual values and alert severity change.  ``alertable`` is true only for a
    real mismatch; unknown and unsupported observations never cause false alarms.
    """

    key: str
    path: str
    status: str
    severity: str
    expected: Any
    actual: Any
    reason: str | None = None

    @property
    def alertable(self) -> bool:
        return self.status == "drift" and self.severity != "info"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "key": self.key,
            "path": self.path,
            "status": self.status,
            "severity": self.severity,
            "expected": _json_value(self.expected),
            "actual": _json_value(self.actual),
            "reason": self.reason,
            "alertable": self.alertable,
        }


@dataclass(frozen=True)
class DriftReport:
    """A complete, deterministic desired-vs-actual comparison."""

    scope: str
    results: tuple[DriftResult, ...]

    @property
    def drifts(self) -> tuple[DriftResult, ...]:
        return tuple(result for result in self.results if result.status == "drift")

    @property
    def alertable(self) -> tuple[DriftResult, ...]:
        return tuple(result for result in self.results if result.alertable)

    @property
    def clean(self) -> bool:
        return not self.drifts

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report suitable for a CLI or notification."""
        counts = {status: 0 for status in sorted(DRIFT_STATUSES)}
        for result in self.results:
            counts[result.status] += 1
        return {
            "scope": self.scope,
            "clean": self.clean,
            "counts": counts,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class LayeredHealth:
    """Aggregate health plus the normalized state of every camera layer."""

    status: str
    layers: dict[str, str]
    causes: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "layers": dict(self.layers),
            "causes": list(self.causes),
            "reason": self.reason,
        }


def normalize_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten nested mappings to deterministic dotted paths.

    A key already containing dots is treated as an explicit path.  Collisions are
    rejected rather than silently overwriting a desired value.
    """
    if not isinstance(values, Mapping):
        raise TypeError("configuration must be a mapping")
    flattened: dict[str, Any] = {}

    def visit(mapping: Mapping[str, Any], prefix: str = "") -> None:
        for raw_key, value in mapping.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError("configuration keys must be non-empty strings")
            path = f"{prefix}.{raw_key}" if prefix else raw_key
            if isinstance(value, Mapping):
                visit(value, path)
                continue
            if path in flattened:
                raise ValueError(f"duplicate configuration path: {path}")
            flattened[path] = value

    visit(values)
    return dict(sorted(flattened.items()))


def evaluate_drift(
    desired: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    supported: Mapping[str, Any] | None = None,
    severities: Mapping[str, str] | None = None,
    scope: str = "camera",
) -> DriftReport:
    """Compare desired configuration with an actual state snapshot.

    Desired scalar values use equality.  Desired sets/frozensets use unordered set
    equality and accept an actual set, frozenset, list, or tuple.  Actual values may
    be nested or keyed by dotted path.  ``supported[path] is False`` and the
    :data:`UNSUPPORTED` marker produce a non-alerting unsupported result.  Missing
    paths and the :data:`UNKNOWN` marker similarly produce a non-alerting unknown
    result.
    """
    if not isinstance(actual, Mapping):
        raise TypeError("actual snapshot must be a mapping")
    if supported is not None and not isinstance(supported, Mapping):
        raise TypeError("supported capabilities must be a mapping")
    if severities is not None and not isinstance(severities, Mapping):
        raise TypeError("severities must be a mapping")
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty string")

    normalized_desired = normalize_mapping(desired)
    normalized_support = normalize_mapping(supported or {})
    normalized_severity = normalize_mapping(severities or {})
    results = []
    for path, expected in normalized_desired.items():
        severity = normalized_severity.get(path, "warning")
        if severity not in SEVERITIES:
            raise ValueError(f"invalid severity for {path}: {severity!r}")

        observed = _path_value(actual, path)
        support = normalized_support.get(path, True)
        if support is False or observed is UNSUPPORTED:
            status = "unsupported"
            actual_value = None
            reason = "capability is not supported"
        elif observed is _MISSING or observed is UNKNOWN or support is UNKNOWN:
            status = "unknown"
            actual_value = None
            reason = "actual value is not known"
        else:
            actual_value = observed
            try:
                matches = _matches(expected, observed)
            except TypeError:
                matches = False
            status = "match" if matches else "drift"
            reason = None if matches else "actual value differs from desired value"

        results.append(
            DriftResult(
                key=_drift_key(scope, path),
                path=path,
                status=status,
                severity=severity,
                expected=expected,
                actual=actual_value,
                reason=reason,
            )
        )
    return DriftReport(scope=scope, results=tuple(results))


def aggregate_health(layers: Mapping[str, str]) -> LayeredHealth:
    """Aggregate network, API, events, RTSP, and storage health.

    Precedence models service dependencies rather than merely taking the worst enum:

    1. network down makes the camera down;
    2. network unknown makes the aggregate unknown unless another known problem exists;
    3. API and RTSP both down make the reachable camera operationally down;
    4. any other down or degraded layer makes it degraded;
    5. otherwise any unknown layer makes it unknown; all-known-good is ok.

    Missing layers normalize to ``unknown``.  Extra layer names are rejected so a
    typo cannot silently hide an observation.
    """
    if not isinstance(layers, Mapping):
        raise TypeError("health layers must be a mapping")
    extras = set(layers) - set(HEALTH_LAYERS)
    if extras:
        raise ValueError(f"unknown health layers: {', '.join(sorted(extras))}")
    normalized = {name: layers.get(name, "unknown") for name in HEALTH_LAYERS}
    for name, status in normalized.items():
        if status not in HEALTH_STATUSES:
            raise ValueError(f"invalid health status for {name}: {status!r}")

    if normalized["network"] == "down":
        return _health("down", normalized, ("network",), "camera network is down")

    known_problems = tuple(
        name for name in HEALTH_LAYERS if normalized[name] in {"down", "degraded"}
    )
    if normalized["api"] == "down" and normalized["rtsp"] == "down":
        return _health(
            "down", normalized, ("api", "rtsp"), "both camera control and media are down"
        )
    if known_problems:
        return _health("degraded", normalized, known_problems, "one or more layers are impaired")

    unknown = tuple(name for name in HEALTH_LAYERS if normalized[name] == "unknown")
    if unknown:
        return _health("unknown", normalized, unknown, "one or more layers have not been observed")
    return _health("ok", normalized, (), "all layers are healthy")


def _path_value(values: Mapping[str, Any], path: str) -> Any:
    # Explicit dotted keys take precedence over traversing nested dictionaries.
    if path in values:
        return values[path]
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Set) and not isinstance(expected, (str, bytes)):
        if not isinstance(actual, (Set, list, tuple)) or isinstance(actual, (str, bytes)):
            return False
        return set(expected) == set(actual)
    return expected == actual


def _drift_key(scope: str, path: str) -> str:
    digest = hashlib.sha256(f"{scope}\0{path}".encode()).hexdigest()[:20]
    return f"drift:{digest}"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Set) and not isinstance(value, (str, bytes)):
        converted = [_json_value(item) for item in value]
        return sorted(converted, key=lambda item: repr(item))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _health(status: str, layers: dict[str, str], causes: tuple[str, ...], reason: str) -> LayeredHealth:
    return LayeredHealth(status=status, layers=dict(layers), causes=causes, reason=reason)
