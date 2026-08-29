import json
import stat
import types

from tapo_monitor import drift, twin


def _plan(**overrides):
    values = {"motion_sensitivity": 60, "autotrack_on": True}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _snapshot(person="on", vehicle="off", motion="60", auto="on", privacy="off"):
    def available(value):
        return {"state": "available", "value": value}

    return {"schema_version": 1, "groups": {
        "privacy": {"lens_mask": available({"enabled": privacy})},
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


def test_privacy_mode_is_critical_drift():
    # Privacy mode parks the lens: the camera records nothing, detects nothing and
    # answers every motor call with MOTOR_BUSY. Two cameras sat like that for nine
    # hours on 2026-08-29 and nothing in the system could say so, because nothing
    # read the setting. Somebody switching it on is legitimate — being unable to
    # tell that they did is not.
    evaluation = twin.evaluate_snapshot("camera-a", _plan(), _snapshot(privacy="on"))

    results = {item["path"]: item for item in twin.alertable_results(evaluation)}
    assert results["privacy.enabled"]["severity"] == "critical"
    assert len(twin.alertable_keys(evaluation)) == 1


def test_privacy_mode_off_is_not_drift():
    evaluation = twin.evaluate_snapshot("camera-a", _plan(), _snapshot(privacy="off"))

    assert evaluation["drift"]["clean"] is True


def test_a_camera_that_cannot_report_privacy_mode_is_not_drift():
    # Older firmware without getPrivacyMode must not be reported as switched off.
    snapshot = _snapshot()
    snapshot["groups"]["privacy"]["lens_mask"] = {
        "state": "unknown", "reason": "missing_method"}

    evaluation = twin.evaluate_snapshot("camera-a", _plan(), snapshot)
    statuses = {item["path"]: item["status"] for item in evaluation["drift"]["results"]}

    assert statuses["privacy.enabled"] == "unsupported"
    assert twin.alertable_keys(evaluation) == set()


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


def test_transitions_records_a_health_status_change():
    previous = {"health": {"status": "ok"}, "drift": {"results": []}}
    current = {"health": {"status": "degraded"}, "drift": {"results": []}}
    changes = twin.transitions(previous, current, at=100.0)
    assert changes == ({"at": 100.0, "kind": "health", "from": "ok", "to": "degraded"},)


def test_transitions_is_silent_when_nothing_changed():
    entry = {"health": {"status": "ok"}, "drift": {"results": []}}
    assert twin.transitions(entry, entry, at=1.0) == ()


def test_transitions_records_drift_opening_and_clearing():
    drifted = {"health": {"status": "ok"}, "drift": {"results": [
        {"key": "drift:abc", "path": "tracking.auto.enabled", "alertable": True}]}}
    clean = {"health": {"status": "ok"}, "drift": {"results": [
        {"key": "drift:abc", "path": "tracking.auto.enabled", "alertable": False}]}}
    opened = twin.transitions(clean, drifted, at=5.0)
    assert opened == ({"at": 5.0, "kind": "drift", "event": "opened",
                       "path": "tracking.auto.enabled"},)
    cleared = twin.transitions(drifted, clean, at=6.0)
    assert cleared == ({"at": 6.0, "kind": "drift", "event": "cleared",
                        "path": "tracking.auto.enabled"},)


def test_transitions_treats_a_first_observation_as_no_history():
    current = {"health": {"status": "degraded"}, "drift": {"results": []}}
    assert twin.transitions(None, current, at=1.0) == ()


def test_extend_history_keeps_the_newest_entries_within_the_limit():
    history = [{"at": float(i), "kind": "health", "from": "ok", "to": "degraded"}
               for i in range(twin.HISTORY_LIMIT)]
    extended = twin.extend_history(history, [{"at": 999.0, "kind": "health",
                                              "from": "degraded", "to": "ok"}])
    assert len(extended) == twin.HISTORY_LIMIT
    assert extended[-1]["at"] == 999.0
    assert extended[0]["at"] == 1.0          # the oldest entry was dropped


def test_fleet_entry_carries_and_extends_the_previous_history():
    previous = {"health": {"status": "ok"}, "drift": {"results": []},
                "history": [{"at": 1.0, "kind": "health", "from": "unknown", "to": "ok"}]}
    entry = twin.fleet_entry(
        captured_at=2.0, snapshot={}, health={"status": "degraded"},
        evaluation={"desired": {}, "actual": {}, "drift": {"results": []}},
        previous=previous,
    )
    assert [item["at"] for item in entry["history"]] == [1.0, 2.0]
    assert entry["history"][-1] == {"at": 2.0, "kind": "health",
                                    "from": "ok", "to": "degraded"}
