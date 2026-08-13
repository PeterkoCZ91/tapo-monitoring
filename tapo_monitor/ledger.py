"""Durable, privacy-minimal observations for the shadow detection auditor.

The ledger deliberately stores facts, not media: event timestamps, detector source,
an optional confidence, and a very small metadata allow-list.  Camera detections can
then be compared with local scorer/recorder observations without retaining snapshots,
stream URLs, credentials, or opaque camera responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import audit

SCHEMA_VERSION = 1

_ADAPTER_SOURCE = {
    "getevents": "camera",
    "local_scorer": "shadow",
    "recorder": "shadow",
}
_SAFE_METADATA_KEYS = frozenset({
    "bbox",
    "confirmed",
    "decision",
    "duration_ms",
    "events_1",
    "frame_height",
    "frame_width",
    "label",
    "model",
    "model_version",
    "reason",
    "score",
    "threshold",
    "track_id",
    "zone",
})
_SENSITIVE_VALUE = re.compile(
    r"(?i)(authorization|bearer|token|secret|passw(?:or)?d|api[_-]?key|://|-----begin)"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.: -]{0,127}$")

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS ledger_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    camera TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('camera', 'shadow')),
    adapter TEXT NOT NULL CHECK (adapter IN ('getevents', 'local_scorer', 'recorder')),
    event_type TEXT NOT NULL,
    event_at REAL NOT NULL,
    observed_at REAL NOT NULL,
    confidence REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL UNIQUE,
    CHECK (
        (source = 'camera' AND adapter = 'getevents') OR
        (source = 'shadow' AND adapter IN ('local_scorer', 'recorder'))
    ),
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);
CREATE INDEX IF NOT EXISTS observations_camera_time
    ON observations(camera, event_at, id);
CREATE INDEX IF NOT EXISTS observations_source_time
    ON observations(source, event_at, id);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    camera TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_at REAL NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    observed_at REAL NOT NULL,
    score REAL,
    threshold REAL,
    telegram INTEGER,
    reason TEXT,
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS decisions_camera_time
    ON decisions(camera, event_at, id);
"""


@dataclass(frozen=True)
class Observation:
    """One media-free detection fact stored by the auditor."""

    id: int
    camera: str
    source: str
    adapter: str
    event_type: str
    event_at: float
    observed_at: float
    confidence: float | None
    metadata: dict


def default_ledger_path(env=None, home=None) -> str:
    """Return the SQLite path, honoring TAPO_LEDGER_FILE and XDG state layout."""
    env = os.environ if env is None else env
    override = env.get("TAPO_LEDGER_FILE")
    if override:
        return os.path.expanduser(override)
    state_home = env.get("XDG_STATE_HOME")
    if not state_home:
        home = home or env.get("HOME") or os.path.expanduser("~")
        state_home = os.path.join(home, ".local", "state")
    return os.path.join(state_home, "tapo-monitor", "events.sqlite3")


def _safe_number(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _safe_metadata_value(value):
    number = _safe_number(value)
    if number is not None:
        return number
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 160 or _SENSITIVE_VALUE.search(value):
            return None
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 16:
        clean = [_safe_number(item) for item in value]
        if all(item is not None for item in clean):
            return clean
    return None


def sanitize_metadata(metadata: Mapping | None) -> dict:
    """Return a small safe subset of metadata; unknown or risky values are dropped."""
    if not isinstance(metadata, Mapping):
        return {}
    clean = {}
    for key in sorted(_SAFE_METADATA_KEYS.intersection(metadata)):
        value = _safe_metadata_value(metadata[key])
        if value is not None:
            clean[key] = value
    return clean


def _finite_timestamp(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite timestamp")
    value = float(value)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _safe_identifier(value, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a short local identifier")
    return value


def _fingerprint(camera, source, adapter, event_type, event_at, dedupe_key) -> str:
    if dedupe_key is None:
        identity = [camera, source, adapter, event_type, event_at]
    else:
        # Opaque upstream identifiers are useful for idempotency but never persisted.
        key_hash = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()
        identity = [camera, source, adapter, event_type, key_hash]
    packed = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _row_to_observation(row) -> Observation:
    return Observation(
        id=row["id"],
        camera=row["camera"],
        source=row["source"],
        adapter=row["adapter"],
        event_type=row["event_type"],
        event_at=row["event_at"],
        observed_at=row["observed_at"],
        confidence=row["confidence"],
        metadata=json.loads(row["metadata_json"]),
    )


class EventLedger:
    """Small SQLite repository for normalized detection observations."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path or default_ledger_path())))
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            row = connection.execute(
                "SELECT version FROM ledger_schema WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO ledger_schema(singleton, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] != SCHEMA_VERSION:
                raise ValueError(f"unsupported event ledger schema version {row['version']}")
        os.chmod(self.path, 0o600)

    def record(
        self,
        *,
        camera: str,
        source: str,
        adapter: str,
        event_type: str,
        event_at: float,
        observed_at: float | None = None,
        confidence: float | None = None,
        metadata: Mapping | None = None,
        dedupe_key=None,
    ) -> int:
        """Insert one normalized observation and return its stable database id.

        Repeating an observation returns the existing id. ``dedupe_key`` is hashed
        before use and is never stored in its original form.
        """
        camera = _safe_identifier(camera, "camera")
        source = _safe_identifier(source, "source")
        adapter = _safe_identifier(adapter, "adapter")
        event_type = _safe_identifier(event_type.lower(), "event_type")
        if _ADAPTER_SOURCE.get(adapter) != source:
            raise ValueError("unsupported source/adapter combination")
        event_at = _finite_timestamp(event_at, "event_at")
        observed_at = _finite_timestamp(
            time.time() if observed_at is None else observed_at, "observed_at"
        )
        if confidence is not None:
            confidence = _finite_timestamp(confidence, "confidence")
            if confidence > 1:
                raise ValueError("confidence must be between 0 and 1")
        metadata_json = json.dumps(
            sanitize_metadata(metadata), sort_keys=True, separators=(",", ":")
        )
        fingerprint = _fingerprint(
            camera, source, adapter, event_type, event_at, dedupe_key
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO observations(
                    camera, source, adapter, event_type, event_at, observed_at,
                    confidence, metadata_json, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    camera,
                    source,
                    adapter,
                    event_type,
                    event_at,
                    observed_at,
                    confidence,
                    metadata_json,
                    fingerprint,
                ),
            )
            row = connection.execute(
                "SELECT id FROM observations WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return int(row["id"])

    def record_camera_event(self, **observation) -> int:
        """Record an observation returned by the camera ``getEvents`` API."""
        return self.record(source="camera", adapter="getevents", **observation)

    def record_shadow_event(self, *, adapter: str = "local_scorer", **observation) -> int:
        """Record a local scorer or recorder observation."""
        return self.record(source="shadow", adapter=adapter, **observation)

    def record_decision(
        self,
        *,
        camera: str,
        event_type: str,
        event_at: float,
        path: str,
        action: str,
        observed_at: float | None = None,
        score: float | None = None,
        threshold: float | None = None,
        telegram: bool | None = None,
        reason: str | None = None,
    ) -> int:
        """Persist one media-free pipeline decision idempotently."""
        camera = _safe_identifier(camera, "camera")
        event_type = _safe_identifier(event_type.lower(), "event_type")
        path = _safe_identifier(path.lower(), "path")
        action = _safe_identifier(action.lower(), "action")
        event_at = _finite_timestamp(event_at, "event_at")
        observed_at = _finite_timestamp(
            time.time() if observed_at is None else observed_at, "observed_at")
        score = _optional_confidence(score, "score")
        threshold = _optional_confidence(threshold, "threshold")
        reason = _safe_metadata_value(reason)
        identity = [camera, event_type, event_at, path, action, score, threshold, telegram, reason]
        fingerprint = hashlib.sha256(json.dumps(
            identity, ensure_ascii=True, separators=(",", ":")
        ).encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO decisions(
                    camera, event_type, event_at, path, action, observed_at,
                    score, threshold, telegram, reason, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (camera, event_type, event_at, path, action, observed_at, score,
                 threshold, None if telegram is None else int(bool(telegram)), reason,
                 fingerprint),
            )
            row = connection.execute(
                "SELECT id FROM decisions WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return int(row["id"])

    def record_audit(self, record: Mapping, *, observed_at: float | None = None):
        """Fold one parsed structured audit record into observations/decisions."""
        if not isinstance(record, Mapping):
            return None
        camera = record.get("camera")
        event_type = record.get("etype")
        event_at = record.get("start")
        path = str(record.get("path") or "").lower()
        action = str(record.get("action") or "").lower()
        if not camera or not event_type or not isinstance(event_at, (int, float)):
            return None
        if action == "detect" and path == "getevents":
            return self.record_camera_event(
                camera=str(camera), event_type=str(event_type), event_at=event_at,
                observed_at=observed_at, metadata=record,
            )
        if action == "detect" and path == "shadow":
            adapter = str(record.get("adapter") or "local_scorer")
            return self.record_shadow_event(
                camera=str(camera), adapter=adapter, event_type=str(event_type),
                event_at=event_at, observed_at=observed_at,
                confidence=record.get("score"), metadata=record,
            )
        if path and action:
            return self.record_decision(
                camera=str(camera), event_type=str(event_type), event_at=event_at,
                path=path, action=action, observed_at=observed_at,
                score=record.get("score"), threshold=record.get("threshold"),
                telegram=record.get("telegram"), reason=record.get("reason"),
            )
        return None

    def observations(
        self,
        *,
        camera: str,
        start: float,
        end: float,
        source: str | None = None,
        event_type: str | None = None,
    ) -> list[Observation]:
        """Return observations in deterministic event-time order."""
        camera = _safe_identifier(camera, "camera")
        start = _finite_timestamp(start, "start")
        end = _finite_timestamp(end, "end")
        if end < start:
            raise ValueError("end must not precede start")
        clauses = ["camera = ?", "event_at >= ?", "event_at <= ?"]
        params: list = [camera, start, end]
        if source is not None:
            if source not in ("camera", "shadow"):
                raise ValueError("source must be camera or shadow")
            clauses.append("source = ?")
            params.append(source)
        if event_type is not None:
            event_type = _safe_identifier(event_type.lower(), "event_type")
            clauses.append("event_type = ?")
            params.append(event_type)
        query = "SELECT * FROM observations WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_observation(row) for row in rows]

    def camera_events_between(self, camera: str, start: float, end: float) -> list[float]:
        """Ascending event_at timestamps of camera-source observations for one camera."""
        events = self.observations(camera=camera, start=start, end=end, source="camera")
        return [event.event_at for event in events]

    def correlation_report(
        self,
        *,
        camera: str,
        start: float,
        end: float,
        window_seconds: float = 3.0,
        event_type: str | None = None,
    ) -> dict:
        """Compare camera events to local shadow evidence over one time range.

        ``precision_like`` treats shadow observations as the reference for camera
        alerts. ``recall_like`` measures how many shadow observations the camera saw.
        They are audit hints, not ground-truth ML metrics.
        """
        events = self.observations(
            camera=camera, start=start, end=end, event_type=event_type
        )
        camera_events = [event for event in events if event.source == "camera"]
        shadow_events = [event for event in events if event.source == "shadow"]
        pairs = match_observations(camera_events, shadow_events, window_seconds)
        matched = len(pairs)
        camera_count = len(camera_events)
        shadow_count = len(shadow_events)
        precision = matched / camera_count if camera_count else None
        recall = matched / shadow_count if shadow_count else None
        f1 = None
        if precision is not None and recall is not None and precision + recall:
            f1 = 2 * precision * recall / (precision + recall)
        return {
            "camera": camera,
            "start": float(start),
            "end": float(end),
            "window_seconds": float(window_seconds),
            "event_type": event_type.lower() if event_type else None,
            "camera_events": camera_count,
            "shadow_events": shadow_count,
            "matched": matched,
            "camera_only": camera_count - matched,
            "shadow_only": shadow_count - matched,
            "precision_like": precision,
            "recall_like": recall,
            "f1_like": f1,
        }

    def delete_before(self, cutoff: float) -> int:
        """Delete observations older than cutoff and return the number removed."""
        cutoff = _finite_timestamp(cutoff, "cutoff")
        with self._connect() as connection:
            observations = connection.execute(
                "DELETE FROM observations WHERE event_at < ?", (cutoff,)
            )
            decisions = connection.execute(
                "DELETE FROM decisions WHERE event_at < ?", (cutoff,)
            )
        return observations.rowcount + decisions.rowcount

    def cleanup(self, retention_seconds: float, *, now: float | None = None) -> int:
        """Apply an age-based retention policy and return the number removed."""
        retention_seconds = _finite_timestamp(retention_seconds, "retention_seconds")
        now = _finite_timestamp(time.time() if now is None else now, "now")
        return self.delete_before(max(0.0, now - retention_seconds))


def match_observations(
    camera_events: Iterable[Observation],
    shadow_events: Iterable[Observation],
    window_seconds: float,
) -> list[tuple[Observation, Observation]]:
    """Deterministically pair same-type events once within a symmetric time window.

    For each event type the earliest compatible pair is consumed.  On ordered
    interval data this maximizes match cardinality while guaranteeing that neither
    camera nor shadow evidence is reused.
    """
    window_seconds = _finite_timestamp(window_seconds, "window_seconds")
    camera_by_type = defaultdict(list)
    shadow_by_type = defaultdict(list)
    for event in camera_events:
        camera_by_type[event.event_type].append(event)
    for event in shadow_events:
        shadow_by_type[event.event_type].append(event)

    pairs = []
    for event_type in sorted(camera_by_type.keys() & shadow_by_type.keys()):
        cameras = sorted(camera_by_type[event_type], key=lambda item: (item.event_at, item.id))
        shadows = sorted(shadow_by_type[event_type], key=lambda item: (item.event_at, item.id))
        camera_index = shadow_index = 0
        while camera_index < len(cameras) and shadow_index < len(shadows):
            camera = cameras[camera_index]
            shadow = shadows[shadow_index]
            if camera.event_at < shadow.event_at - window_seconds:
                camera_index += 1
            elif shadow.event_at < camera.event_at - window_seconds:
                shadow_index += 1
            else:
                pairs.append((camera, shadow))
                camera_index += 1
                shadow_index += 1
    return sorted(pairs, key=lambda pair: (pair[0].event_at, pair[0].id, pair[1].id))


class AuditLedgerHandler(logging.Handler):
    """Mirror audit lines through a bounded background queue.

    SQLite writes never run on the live alert thread. If the queue is saturated, the
    observation is dropped rather than delaying camera polling or Telegram delivery.
    """

    def __init__(self, event_ledger, *, queue_size=1024):
        super().__init__(level=logging.INFO)
        self.event_ledger = event_ledger
        self.dropped = 0
        self.errors = 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._worker = threading.Thread(
            target=self._run,
            name="tapo-audit-ledger",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record):
        try:
            parsed = audit.parse_audit_line(record.getMessage())
            if parsed:
                self._queue.put_nowait((parsed, record.created))
        except queue.Full:
            self.dropped += 1
        except Exception:
            # Observability must never interrupt the alert pipeline.
            self.handleError(record)

    def _run(self):
        while True:
            parsed, observed_at = self._queue.get()
            try:
                self.event_ledger.record_audit(parsed, observed_at=observed_at)
            except Exception:  # noqa: BLE001 - worker is deliberately best effort
                self.errors += 1
            finally:
                self._queue.task_done()

    def flush(self):
        """Wait for queued writes; intended for tests and orderly maintenance only."""
        self._queue.join()


def _optional_confidence(value, name):
    if value is None:
        return None
    value = _finite_timestamp(value, name)
    if value > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value
