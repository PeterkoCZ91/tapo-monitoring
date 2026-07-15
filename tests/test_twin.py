import json
import stat
import types

from tapo_monitor import drift, twin


def _plan(**overrides):
    values = {"motion_sensitivity": 60, "autotrack_on": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _snapshot(person="on", vehicle="off", motion="60", auto="on"):
    def available(value):
        return {"state": "available", "value": value}

    return {"schema_version": 1, "groups": {
        "detection": {
            "person": available({"enabled": person}),
            "vehicle": available({"enabled": vehicle}),
            "motion": available({"digital_sensitivity": motion}),
        },
        "track": {"auto_target": available({"enabled": auto})},
    }}


def test_evaluate_snapshot_matches_controlled_state():
    evaluation = twin.evaluate_snapshot("camera-a", _plan(), _snapshot())

    assert evaluation["drift"]["clean"] is True
    assert twin.alertable_keys(evaluation) == set()


def test_evaluate_snapshot_reports_critical_and_warning_drift():
    evaluation = twin.evaluate_snapshot(
        "camera-a", _plan(), _snapshot(person="off", vehicle="on", auto="off"))

    results = {item["path"]: item for item in twin.alertable_results(evaluation)}
    assert results["detection.person.enabled"]["severity"] == "critical"
    assert results["detection.vehicle.enabled"]["severity"] == "warning"
    assert results["tracking.auto.enabled"]["severity"] == "critical"
    assert len(twin.alertable_keys(evaluation)) == 3


def test_unknown_and_missing_probes_do_not_create_drift():
    snapshot = _snapshot()
    snapshot["groups"]["detection"]["person"] = {
        "state": "unknown", "reason": "empty_response"}
    snapshot["groups"]["track"]["auto_target"] = {
        "state": "unknown", "reason": "missing_method"}

    evaluation = twin.evaluate_snapshot("camera-a", _plan(), snapshot)
    statuses = {item["path"]: item["status"] for item in evaluation["drift"]["results"]}

    assert statuses["detection.person.enabled"] == "unknown"
    assert statuses["tracking.auto.enabled"] == "unsupported"
    assert twin.alertable_keys(evaluation) == set()


def test_state_round_trip_is_private_and_redacted(tmp_path):
    path = tmp_path / "state" / "twin.json"
    cameras = {"camera-a": {
        "captured_at": 100,
        "snapshot": {"device_id": "private", "status": "ok"},
        "health": drift.aggregate_health({"network": "ok"}).to_dict(),
    }}

    assert twin.save_state(str(path), cameras) is True
    restored = twin.load_state(str(path))

    assert restored["camera-a"]["snapshot"]["device_id"] == "<redacted>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    json.dumps(restored, allow_nan=False)


def test_default_state_path_honors_override_and_xdg():
    assert twin.default_state_path({"TAPO_TWIN_STATE_FILE": "/tmp/twin.json"}) == (
        "/tmp/twin.json")
    assert twin.default_state_path({"XDG_STATE_HOME": "/tmp/state"}) == (
        "/tmp/state/tapo-monitor/twin.json")
