"""Alert work in flight, persisted so a restart does not lose it.

Every deploy restarts the daemon, and until this existed a restart dropped three things
held only in memory: the hub retry queue (the hub cursor has already moved past those
clips, so the queue is the only copy of the alert), the pending SD follow-ups, and the
alert cooldowns — the last meaning a restart in the middle of a passage could send the
same person twice.

The file is small, secret-free and written atomically beside ``health.json``. It is a
bridge across a restart, not an archive: a file older than ``MAX_AGE`` describes work
whose own TTLs have long expired and is discarded whole.
"""

from __future__ import annotations

import json
import math
import os
import tempfile

from . import health

SCHEMA_VERSION = 1
# Hub retries and SD follow-ups both expire after ten minutes; an hour-old file can
# only replay stale work.
MAX_AGE = 3600


def default_path(env=None, home=None):
    """``runtime.json`` beside the health state file (same env/XDG resolution)."""
    return os.path.join(os.path.dirname(health.default_state_path(env, home)),
                        "runtime.json")


def _cooldowns(mapping):
    """``{(camera, key): ts}`` as JSON rows. Pure."""
    return sorted([camera, key, ts] for (camera, key), ts in mapping.items())


def _serializable(entries, what, logger):
    """The entries that survive a JSON round trip; the rest are logged and left out."""
    out = []
    for entry in entries:
        try:
            json.dumps(entry)
        except (TypeError, ValueError) as exc:
            if logger is not None:
                logger.warning("runtime state: %s entry for %s not saved: %s",
                               what, entry.get("camera") if isinstance(entry, dict) else "?",
                               exc)
            continue
        out.append(entry)
    return out


def snapshot(state, logger=None):
    """The persisted subset of a MonitorState, JSON-ready. ``saved_at`` is added on save."""
    return {
        "pending_hub": _serializable(state.pending_hub, "hub retry", logger),
        "pending_sd": _serializable(state.pending_sd, "SD follow-up", logger),
        "last_alert": _cooldowns(state.last_alert),
        "last_event_start": _cooldowns(state.last_event_start),
        "privacy_announced": {str(k): bool(v)
                              for k, v in (getattr(state, "privacy_announced", None)
                                           or {}).items()},
    }


def save(path, data, now, logger=None):
    """Atomically write ``data`` (a :func:`snapshot`) with mode 0600. Returns success."""
    temp = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".runtime-", dir=directory, text=True)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": SCHEMA_VERSION, "saved_at": now, **data}, fh,
                      sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        temp = None
        return True
    except (OSError, TypeError, ValueError) as exc:
        if logger is not None:
            logger.warning("runtime state save failed: %s", exc)
        return False
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


def save_if_changed(state, now, logger=None):
    """Persist when the snapshot differs from the last one written. Returns True if written.

    Called every tick; the comparison keeps a quiet night from rewriting the file every
    few seconds.
    """
    if not state.runtime_path:
        return False
    data = snapshot(state, logger)
    if data == state.runtime_saved:
        return False
    if save(state.runtime_path, data, now, logger):
        state.runtime_saved = data
        return True
    return False


def _restore_cooldowns(rows, target):
    for row in rows:
        camera, key, ts = row
        if (not isinstance(camera, str) or not isinstance(key, str)
                or isinstance(ts, bool) or not isinstance(ts, (int, float))
                or not math.isfinite(ts)):
            raise ValueError("bad cooldown row")
        target[(camera, key)] = float(ts)


def load(path, state, now, logger=None, max_age=MAX_AGE):
    """Restore queues and cooldowns into ``state``. Returns what was restored, as counts.

    Never raises: an absent, stale, corrupt or foreign file leaves the state empty, as a
    fresh start always did. A queued hub alert whose frame file is gone cannot be sent
    and is dropped with a log line rather than kept as a retry that can only fail.
    """
    empty = {"pending_hub": 0, "pending_sd": 0, "cooldowns": 0}
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            raise ValueError("unsupported runtime state file")
        saved_at = payload.get("saved_at")
        if isinstance(saved_at, bool) or not isinstance(saved_at, (int, float)):
            raise ValueError("runtime state has no saved_at")
        if now - saved_at > max_age:
            if logger is not None:
                logger.info("runtime state is %ds old; discarded", int(now - saved_at))
            return empty
        hub = payload.get("pending_hub") or []
        sd = payload.get("pending_sd") or []
        if not isinstance(hub, list) or not isinstance(sd, list):
            raise ValueError("bad runtime queues")
        last_alert, last_event_start = {}, {}
        _restore_cooldowns(payload.get("last_alert") or [], last_alert)
        _restore_cooldowns(payload.get("last_event_start") or [], last_event_start)
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if logger is not None:
            logger.warning("runtime state load failed: %s", exc)
        return empty
    kept_hub = []
    for entry in hub:
        if not isinstance(entry, dict) or not os.path.exists(str(entry.get("image", ""))):
            if logger is not None:
                logger.warning("runtime state: hub retry for %s dropped, its frame is gone",
                               entry.get("camera") if isinstance(entry, dict) else "?")
            continue
        kept_hub.append(entry)
    kept_sd = [entry for entry in sd if isinstance(entry, dict)]
    state.pending_hub.extend(kept_hub)
    state.pending_sd.extend(kept_sd)
    state.last_alert.update(last_alert)
    state.last_event_start.update(last_event_start)
    announced = payload.get("privacy_announced") or {}
    if isinstance(announced, dict) and hasattr(state, "privacy_announced"):
        state.privacy_announced.update(
            {str(k): v for k, v in announced.items() if isinstance(v, bool)})
    state.runtime_saved = snapshot(state, logger)
    return {"pending_hub": len(kept_hub), "pending_sd": len(kept_sd),
            "cooldowns": len(last_alert) + len(last_event_start)}
