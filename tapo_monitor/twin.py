"""Camera Digital Twin orchestration and private local persistence.

The low-level camera reads live in :mod:`tapo_monitor.capabilities`; the comparison
algorithm lives in :mod:`tapo_monitor.drift`.  This module translates the few states the
daemon actively controls into a stable desired/actual contract and persists the latest
redacted fleet view for offline CLI inspection.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping

from . import capabilities, drift

SCHEMA_VERSION = 1
# Bounded so the always-rewritten state file cannot grow without limit when a camera flaps.
HISTORY_LIMIT = 50


def default_state_path(env=None, home=None):
    """Return the twin-state path, honoring TAPO_TWIN_STATE_FILE and XDG."""
    env = os.environ if env is None else env
    override = env.get("TAPO_TWIN_STATE_FILE")
    if override:
        return os.path.expanduser(override)
    state_home = env.get("XDG_STATE_HOME")
    if not state_home:
        home = home or env.get("HOME") or os.path.expanduser("~")
        state_home = os.path.join(home, ".local", "state")
    return os.path.join(state_home, "tapo-monitor", "twin.json")


def evaluate_snapshot(camera_name, plan, snapshot):
    """Return desired/actual/drift dictionaries for one camera snapshot."""
    desired = {
        # A camera in privacy mode is not monitoring anything. It is a legitimate thing
        # for someone to switch on, which is exactly why it has to be reported: the
        # drift report fires on the transition, so it says so once when it goes on and
        # once when it comes back, instead of every control pass.
        "privacy.enabled": False,
        "detection.person.enabled": True,
        "detection.vehicle.enabled": False,
        "detection.motion.sensitivity": int(plan.motion_sensitivity),
        "tracking.auto.enabled": bool(plan.autotrack_on),
    }
    actual = {
        "privacy.enabled": _enabled(_probe_value(snapshot, "privacy", "lens_mask")),
        "detection.person.enabled": _enabled(_probe_value(snapshot, "detection", "person")),
        "detection.vehicle.enabled": _enabled(
            _probe_value(snapshot, "detection", "vehicle")
        ),
        "detection.motion.sensitivity": _sensitivity(
            _probe_value(snapshot, "detection", "motion")
        ),
        "tracking.auto.enabled": _enabled(_probe_value(snapshot, "track", "auto_target")),
    }
    severities = {
        "privacy.enabled": "critical",
        "detection.person.enabled": "critical",
        "detection.vehicle.enabled": "warning",
        "detection.motion.sensitivity": "warning",
        "tracking.auto.enabled": "critical",
    }
    report = drift.evaluate_drift(
        desired, actual, severities=severities, scope=str(camera_name)
    )
    return {"desired": desired, "actual": _json_actual(actual), "drift": report.to_dict()}


def cameras_in_privacy(fleet):
    """Names whose last snapshot read privacy mode as ON. Pure.

    Only a value the probe actually returned counts: `unknown` is also what an
    unreachable camera looks like, and a camera nobody could read still needs its aim
    repaired. Callers use this to skip motor calls a parked lens can only refuse.
    """
    names = set()
    if not isinstance(fleet, Mapping):
        return names
    for name, entry in fleet.items():
        if not isinstance(entry, Mapping):
            continue
        actual = entry.get("actual")
        if isinstance(actual, Mapping) and actual.get("privacy.enabled") is True:
            names.add(str(name))
    return names


def _alertable_paths(entry):
    results = (entry or {}).get("drift", {}).get("results", [])
    return {str(item.get("path", "")) for item in results
            if isinstance(item, Mapping) and item.get("alertable")}


def transitions(previous, current, *, at):
    """Changes between two consecutive observations of one camera. Pure.

    A snapshot answers "what is wrong now"; only a transition answers "since when", which
    is what an operator reconstructing an incident actually needs. Reported changes are
    the aggregate health status and each drift path opening or clearing.

    The first observation of a camera has nothing to compare against, so it yields no
    transitions rather than a synthetic "appeared" entry.
    """
    if not previous:
        return ()
    changes = []
    was = str((previous.get("health") or {}).get("status", "unknown"))
    now_status = str((current.get("health") or {}).get("status", "unknown"))
    if was != now_status:
        changes.append({"at": float(at), "kind": "health", "from": was, "to": now_status})
    before, after = _alertable_paths(previous), _alertable_paths(current)
    for path in sorted(after - before):
        changes.append({"at": float(at), "kind": "drift", "event": "opened", "path": path})
    for path in sorted(before - after):
        changes.append({"at": float(at), "kind": "drift", "event": "cleared", "path": path})
    return tuple(changes)


def extend_history(history, new_items, *, limit=HISTORY_LIMIT):
    """Append transitions to a bounded history, dropping the oldest first. Pure.

    The bound is what makes this safe to keep in the same always-rewritten state file:
    the entry cannot grow without limit no matter how long a camera flaps.
    """
    combined = [item for item in (history or []) if isinstance(item, Mapping)]
    combined.extend(new_items)
    return combined[-max(1, int(limit)):] if combined else []


def fleet_entry(*, captured_at, snapshot, health, evaluation, previous=None):
    """Build one fully JSON-safe persisted fleet entry.

    ``previous`` is that camera's last entry; passing it carries the bounded transition
    history forward and appends whatever changed since. Omitting it keeps the entry a pure
    snapshot, which is what a one-shot probe wants.
    """
    entry = {
        "captured_at": float(captured_at),
        "snapshot": snapshot,
        "health": health.to_dict() if hasattr(health, "to_dict") else health,
        **evaluation,
    }
    if previous is not None:
        entry["history"] = extend_history(
            previous.get("history"), transitions(previous, entry, at=captured_at)
        )
    return capabilities.redact(entry)


def save_state(path, cameras, logger=None):
    """Atomically persist a redacted fleet twin with mode 0600."""
    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".twin-", dir=directory, text=True)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"version": SCHEMA_VERSION, "cameras": capabilities.redact(cameras)},
                fh,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = None
        return True
    except (OSError, TypeError, ValueError) as exc:
        if logger is not None:
            logger.warning("digital twin state save failed: %s", type(exc).__name__)
        return False
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def load_state(path, logger=None):
    """Load and minimally validate a fleet twin; return an empty fleet on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, Mapping) or payload.get("version") != SCHEMA_VERSION:
            raise ValueError("unsupported digital twin state version")
        cameras = payload.get("cameras")
        if not isinstance(cameras, Mapping):
            raise ValueError("digital twin cameras must be an object")
        return {str(name): value for name, value in cameras.items() if isinstance(value, Mapping)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if logger is not None:
            logger.warning("digital twin state load failed: %s", type(exc).__name__)
        return {}


def alertable_keys(evaluation):
    """Return stable keys for currently alertable drift results."""
    results = evaluation.get("drift", {}).get("results", [])
    return {str(item["key"]) for item in results
            if isinstance(item, Mapping) and item.get("alertable") and item.get("key")}


def alertable_results(evaluation):
    """Return alertable drift result dictionaries in deterministic path order."""
    results = evaluation.get("drift", {}).get("results", [])
    return sorted(
        (item for item in results if isinstance(item, Mapping) and item.get("alertable")),
        key=lambda item: str(item.get("path", "")),
    )


def _probe_value(snapshot, group, name):
    try:
        result = snapshot["groups"][group][name]
    except (KeyError, TypeError):
        return drift.UNKNOWN
    if not isinstance(result, Mapping):
        return drift.UNKNOWN
    if result.get("state") != "available":
        return drift.UNSUPPORTED if result.get("reason") == "missing_method" else drift.UNKNOWN
    return result.get("value", drift.UNKNOWN)


def _enabled(value):
    if value in (drift.UNKNOWN, drift.UNSUPPORTED):
        return value
    if not isinstance(value, Mapping):
        return drift.UNKNOWN
    raw = value.get("enabled", drift.UNKNOWN)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if raw.lower() in {"on", "true", "1", "enabled"}:
            return True
        if raw.lower() in {"off", "false", "0", "disabled"}:
            return False
    return drift.UNKNOWN


def _sensitivity(value):
    if value in (drift.UNKNOWN, drift.UNSUPPORTED):
        return value
    if not isinstance(value, Mapping):
        return drift.UNKNOWN
    raw = value.get("digital_sensitivity", value.get("sensitivity", drift.UNKNOWN))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return drift.UNKNOWN


def _json_actual(actual):
    return {
        path: (
            value.value
            if value is drift.UNKNOWN or value is drift.UNSUPPORTED
            else value
        )
        for path, value in actual.items()
    }
