import json
import logging
import sqlite3
import stat

import pytest

from tapo_monitor import ledger


def _observation(identifier, source, event_at, event_type="person"):
    adapter = "getevents" if source == "camera" else "local_scorer"
    return ledger.Observation(
        id=identifier,
        camera="front",
        source=source,
        adapter=adapter,
        event_type=event_type,
        event_at=event_at,
        observed_at=event_at,
        confidence=None,
        metadata={},
    )


def test_default_path_honors_override_and_xdg():
    assert ledger.default_ledger_path(
        {"TAPO_LEDGER_FILE": "/tmp/custom.sqlite3"}
    ) == "/tmp/custom.sqlite3"
    assert ledger.default_ledger_path(
        {"XDG_STATE_HOME": "/tmp/state"}
    ) == "/tmp/state/tapo-monitor/events.sqlite3"
    assert ledger.default_ledger_path({}, home="/tmp/home") == (
        "/tmp/home/.local/state/tapo-monitor/events.sqlite3"
    )


def test_schema_is_initialized_atomically_and_file_is_private(tmp_path):
    path = tmp_path / "nested" / "events.sqlite3"
    ledger.EventLedger(path)

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT version FROM ledger_schema WHERE singleton = 1"
        ).fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(observations)")
        }

    assert version == ledger.SCHEMA_VERSION
    assert {"camera", "source", "adapter", "event_type", "event_at", "fingerprint"} <= columns
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_schema_version_rolls_back_initialization(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE ledger_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO ledger_schema VALUES (1, 999)")

    with pytest.raises(ValueError, match="unsupported event ledger schema"):
        ledger.EventLedger(path)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "observations" not in tables


def test_observations_persist_and_duplicates_return_same_id(tmp_path):
    path = tmp_path / "events.sqlite3"
    events = ledger.EventLedger(path)
    first = events.record_camera_event(
        camera="front",
        event_type="Person",
        event_at=100.0,
        observed_at=101.0,
        dedupe_key="opaque-event-1",
    )
    duplicate = events.record_camera_event(
        camera="front",
        event_type="person",
        event_at=999.0,
        observed_at=1000.0,
        dedupe_key="opaque-event-1",
    )
    shadow = events.record_shadow_event(
        camera="front",
        event_type="person",
        event_at=101.0,
        confidence=0.8,
    )

    reopened = ledger.EventLedger(path)
    stored = reopened.observations(camera="front", start=0, end=200)

    assert first == duplicate
    assert shadow != first
    assert len(stored) == 2
    assert stored[0].event_type == "person"
    assert stored[1].confidence == 0.8


def test_metadata_is_allowlisted_and_never_keeps_media_or_credentials(tmp_path):
    path = tmp_path / "events.sqlite3"
    events = ledger.EventLedger(path)
    events.record_shadow_event(
        camera="front",
        adapter="recorder",
        event_type="person",
        event_at=100,
        metadata={
            "score": 0.9,
            "bbox": [1, 2, 3, 4],
            "zone": "gate",
            "password": "do-not-store",
            "image": b"raw-media",
            "path": "/private/clip.mp4",
            "reason": "token=do-not-store",
            "nested": {"api_key": "do-not-store"},
        },
        dedupe_key="api-secret-that-must-only-be-hashed",
    )

    with sqlite3.connect(path) as connection:
        metadata_json, fingerprint = connection.execute(
            "SELECT metadata_json, fingerprint FROM observations"
        ).fetchone()

    assert json.loads(metadata_json) == {
        "bbox": [1, 2, 3, 4],
        "score": 0.9,
        "zone": "gate",
    }
    database_bytes = path.read_bytes()
    assert b"do-not-store" not in database_bytes
    assert b"raw-media" not in database_bytes
    assert b"api-secret" not in database_bytes
    assert len(fingerprint) == 64


def test_matching_is_deterministic_max_cardinality_and_never_reuses_events():
    # Nearest-first would pair camera 4 -> shadow 5 and lose camera 6.  Ordered
    # matching consumes 4 -> 0, leaving 6 -> 5, so both observations are audited.
    cameras = [_observation(2, "camera", 6), _observation(1, "camera", 4)]
    shadows = [_observation(4, "shadow", 5), _observation(3, "shadow", 0)]

    pairs = ledger.match_observations(cameras, shadows, window_seconds=4)

    assert [(camera.id, shadow.id) for camera, shadow in pairs] == [(1, 3), (2, 4)]
    assert len({camera.id for camera, _ in pairs}) == len(pairs)
    assert len({shadow.id for _, shadow in pairs}) == len(pairs)


def test_matching_does_not_combine_different_event_types():
    camera = _observation(1, "camera", 100, "motion")
    shadow = _observation(2, "shadow", 100, "person")

    assert ledger.match_observations([camera], [shadow], 5) == []


def test_correlation_report_counts_camera_and_shadow_gaps(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    for timestamp in (100, 110, 130):
        events.record_camera_event(camera="front", event_type="person", event_at=timestamp)
    for timestamp in (101, 111, 150, 170):
        events.record_shadow_event(camera="front", event_type="person", event_at=timestamp)

    report = events.correlation_report(
        camera="front", start=90, end=180, window_seconds=2, event_type="person"
    )

    assert report["camera_events"] == 3
    assert report["shadow_events"] == 4
    assert report["matched"] == 2
    assert report["camera_only"] == 1
    assert report["shadow_only"] == 2
    assert report["precision_like"] == pytest.approx(2 / 3)
    assert report["recall_like"] == pytest.approx(1 / 2)
    assert report["f1_like"] == pytest.approx(4 / 7)


def test_retention_cleanup_deletes_only_expired_observations(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    for timestamp in (10, 20, 30):
        events.record_camera_event(camera="front", event_type="person", event_at=timestamp)

    assert events.delete_before(20) == 1
    assert events.cleanup(5, now=30) == 1
    assert [event.event_at for event in events.observations(
        camera="front", start=0, end=100
    )] == [30]


def test_rejects_unsupported_sources_and_invalid_confidence(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")

    with pytest.raises(ValueError):
        events.record(
            camera="front",
            source="camera",
            adapter="recorder",
            event_type="person",
            event_at=1,
        )
    with pytest.raises(ValueError):
        events.record_shadow_event(
            camera="front", event_type="person", event_at=1, confidence=1.5
        )


def test_audit_records_camera_observation_and_pipeline_decision(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")

    events.record_audit({
        "camera": "front", "path": "getevents", "action": "detect",
        "etype": "person", "start": 100,
    }, observed_at=101)
    events.record_audit({
        "camera": "front", "path": "live", "action": "send",
        "etype": "person", "start": 100, "score": 0.9,
        "threshold": 0.4, "telegram": True,
    }, observed_at=102)

    assert len(events.observations(camera="front", start=0, end=200)) == 1
    with sqlite3.connect(events.path) as connection:
        decision = connection.execute(
            "SELECT action, score, threshold, telegram FROM decisions"
        ).fetchone()
    assert decision == ("send", 0.9, 0.4, 1)


def test_audit_logging_handler_is_best_effort(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    handler = ledger.AuditLedgerHandler(events)
    record = logging.LogRecord(
        "tapo_monitor.monitor", logging.INFO, __file__, 1,
        "audit camera=front path=getevents action=detect etype=person start=100",
        (), None,
    )

    handler.handle(record)
    handler.flush()

    assert len(events.observations(camera="front", start=0, end=200)) == 1


def test_camera_events_between_filters_by_camera_and_window(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    events.record_camera_event(camera="front", event_type="motion", event_at=100.0)
    events.record_camera_event(camera="front", event_type="person", event_at=500.0)
    events.record_shadow_event(camera="front", event_type="motion", event_at=100.0)
    events.record_camera_event(camera="yard", event_type="motion", event_at=110.0)

    assert events.camera_events_between("front", 50.0, 200.0) == [100.0]
    assert events.camera_events_between("front", 0.0, 1000.0) == [100.0, 500.0]
    assert events.camera_events_between("front", 600.0, 700.0) == []
