"""Durable, secret-free camera health state.

Only transition data needed to preserve observed uptime and outage notification
de-duplication is stored. High-frequency detection state stays in memory.
"""

from __future__ import annotations

import json
import os
import tempfile

SCHEMA_VERSION = 1
PERSISTED_FIELDS = (
    "last_seen",
    "online_since",
    "fail_since",
    "outage_alerted",
    "last_outage_duration",
    "last_observed_uptime",
    "reconnect_count",
    "recovery_pending",
    "total_observed_online",
    "total_observed_offline",
    "event_fail_since",
    "event_alerted",
    "event_restart_attempted",
)


def default_state_path(env=None, home=None):
    """Return the health state path, honoring an explicit env override and XDG."""
    env = os.environ if env is None else env
    override = env.get("TAPO_HEALTH_STATE_FILE")
    if override:
        return os.path.expanduser(override)
    state_home = env.get("XDG_STATE_HOME")
    if not state_home:
        home = home or env.get("HOME") or os.path.expanduser("~")
        state_home = os.path.join(home, ".local", "state")
    return os.path.join(state_home, "tapo-monitor", "health.json")


def snapshot(state):
    """Return the persisted subset of a MonitorState as plain dictionaries."""
    return {name: dict(getattr(state, name, {}) or {}) for name in PERSISTED_FIELDS}


def status_rows(state, now):
    """Return per-camera health summaries suitable for a CLI or status endpoint."""
    data = snapshot(state)
    cameras = sorted({camera for values in data.values() for camera in values})
    rows = []
    for camera in cameras:
        fail_since = data["fail_since"].get(camera)
        online_since = data["online_since"].get(camera)
        if fail_since is not None:
            current_state = "offline"
            current_for = max(0, now - fail_since)
        else:
            current_state = "online"
            current_for = None if online_since is None else max(0, now - online_since)
        rows.append({
            "camera": camera,
            "state": current_state,
            "current_for": current_for,
            "last_outage": data["last_outage_duration"].get(camera),
            "last_uptime": data["last_observed_uptime"].get(camera),
            "reconnects": data["reconnect_count"].get(camera, 0),
            "availability": _availability(data, camera, current_state, current_for),
        })
    return rows


def _availability(data, camera, current_state, current_for):
    online = data["total_observed_online"].get(camera, 0)
    offline = data["total_observed_offline"].get(camera, 0)
    if current_for is not None:
        if current_state == "online":
            online += current_for
        else:
            offline += current_for
    observed = online + offline
    return None if observed <= 0 else 100 * online / observed


def _validated(payload):
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing health state version")
    raw = payload.get("state")
    if not isinstance(raw, dict):
        raise ValueError("health state must be an object")
    clean = {}
    for field_name in PERSISTED_FIELDS:
        values = raw.get(field_name, {})
        if not isinstance(values, dict):
            raise ValueError(f"health field {field_name!r} must be an object")
        field = {}
        for camera, value in values.items():
            if not isinstance(camera, str):
                raise ValueError("health camera names must be strings")
            if field_name in ("outage_alerted", "event_alerted", "event_restart_attempted"):
                if not isinstance(value, bool):
                    raise ValueError("outage_alerted values must be booleans")
            elif field_name == "reconnect_count":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("reconnect_count values must be non-negative integers")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"health field {field_name!r} must contain non-negative numbers")
            field[camera] = value
        clean[field_name] = field
    return clean


def restore(state, payload):
    """Validate and atomically replace the persisted subset on an existing state object."""
    clean = _validated(payload)
    for field_name, values in clean.items():
        setattr(state, field_name, values)
    return state


def load_state(path, state, logger=None):
    """Load health data into state; missing or invalid files leave it unchanged."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        restore(state, payload)
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if logger is not None:
            logger.warning("health state load failed: %s", exc)
        return False


def save_state(path, state, logger=None):
    """Atomically save health state with mode 0600. Returns success."""
    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".health-", dir=directory, text=True)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"version": SCHEMA_VERSION, "state": snapshot(state)},
                fh,
                sort_keys=True,
                separators=(",", ":"),
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        if logger is not None:
            logger.warning("health state save failed: %s", exc)
        return False
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
