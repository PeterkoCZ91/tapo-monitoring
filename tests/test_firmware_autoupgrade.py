"""Firmware auto-upgrade: read into the twin, shown to the operator, opt-in drift.

A camera that updates its own firmware overnight can come back speaking a different
login protocol. The monitor only reads the setting (``getFirmwareAutoUpgradeConfig``);
nothing here may ever write it.
"""

import json
import types

import pytest

from tapo_monitor import capabilities, cli, reviewdigest, twin
from tapo_monitor import config as cfg

# The shape a real C545D answered with (discovery capture, 2026-09-25).
AUTO_ON = {"auto_upgrade": {"common": {"enabled": "on", "time": "03:00",
                                       "random_range": "120"}}}
AUTO_OFF = {"auto_upgrade": {"common": {"enabled": "off", "time": "03:00",
                                        "random_range": "120"}}}
FW_OLD = "1.2.2 Build 260601 Rel.10001n"
FW_NEW = "1.2.3 Build 260805 Rel.20715n"


def _available(value):
    return {"state": "available", "value": value}


def _snapshot(auto=AUTO_ON, version=FW_NEW):
    groups = {"basic": {}, "firmware": {}}
    if version is not None:
        groups["basic"]["info"] = _available(
            {"device_info": {"basic_info": {"device_model": "C260", "sw_version": version}}})
    if auto is not None:
        groups["firmware"]["auto_upgrade"] = _available(auto)
    return {"schema_version": 1, "groups": groups}


def _plan(**overrides):
    values = {"motion_sensitivity": 60, "autotrack_on": False}
    values.update(overrides)
    return types.SimpleNamespace(**values)


# --- probe -----------------------------------------------------------------------

def test_auto_upgrade_is_a_safe_read_only_probe():
    assert ("firmware", "auto_upgrade", "getFirmwareAutoUpgradeConfig") in \
        capabilities._SAFE_PROBES
    assert not any(row[2].startswith("set") for row in capabilities._SAFE_PROBES)


def test_snapshot_reads_auto_upgrade_and_never_writes_it():
    calls = []

    class Client:
        def getFirmwareAutoUpgradeConfig(self):
            calls.append("get")
            return AUTO_ON

        def setFirmwareAutoUpgradeConfig(self, *a, **k):
            raise AssertionError("the monitor must never write auto-upgrade")

    snapshot = capabilities.collect_snapshot(Client())
    assert snapshot["groups"]["firmware"]["auto_upgrade"] == _available(AUTO_ON)
    assert calls == ["get"]


# --- parsing ---------------------------------------------------------------------

def test_firmware_info_reads_version_and_auto_upgrade_window():
    assert twin.firmware_info(_snapshot()) == {
        "version": FW_NEW, "auto_upgrade": True, "auto_upgrade_time": "03:00"}
    assert twin.firmware_info(_snapshot(auto=AUTO_OFF))["auto_upgrade"] is False


@pytest.mark.parametrize("reading", [
    {"state": "unknown", "reason": "missing_method"},
    {"state": "error", "error_type": "Exception", "error_code": -40101},
    _available({"auto_upgrade": {"common": {"time": "03:00"}}}),
    _available("garbage"),
])
def test_firmware_info_unreadable_auto_upgrade_is_unknown(reading):
    snapshot = _snapshot()
    snapshot["groups"]["firmware"]["auto_upgrade"] = reading
    info = twin.firmware_info(snapshot)
    assert info["auto_upgrade"] is None
    assert info["version"] == FW_NEW


def test_firmware_info_survives_an_empty_snapshot():
    assert twin.firmware_info({}) == {
        "version": None, "auto_upgrade": None, "auto_upgrade_time": None}


@pytest.mark.parametrize("info,text", [
    ({"version": FW_NEW, "auto_upgrade": True, "auto_upgrade_time": "03:00"},
     f"{FW_NEW}, auto-upgrade: on (03:00)"),
    ({"version": FW_NEW, "auto_upgrade": False, "auto_upgrade_time": "03:00"},
     f"{FW_NEW}, auto-upgrade: off"),
    ({"version": None, "auto_upgrade": None, "auto_upgrade_time": None},
     "unknown, auto-upgrade: unknown"),
])
def test_firmware_text(info, text):
    assert twin.firmware_text(info) == text


# --- opt-in drift ----------------------------------------------------------------

def test_no_desired_auto_upgrade_means_no_check():
    evaluation = twin.evaluate_snapshot("cam", _plan(), _snapshot())
    assert "firmware.auto_upgrade.enabled" not in evaluation["desired"]


def test_desired_auto_upgrade_off_reports_warning_drift_when_on():
    evaluation = twin.evaluate_snapshot(
        "cam", _plan(firmware_auto_upgrade=False), _snapshot(auto=AUTO_ON))
    results = {item["path"]: item for item in twin.alertable_results(evaluation)}
    item = results["firmware.auto_upgrade.enabled"]
    assert item["severity"] == "warning"
    assert item["expected"] is False and item["actual"] is True


def test_desired_auto_upgrade_matching_is_clean():
    evaluation = twin.evaluate_snapshot(
        "cam", _plan(firmware_auto_upgrade=False), _snapshot(auto=AUTO_OFF))
    statuses = {r["path"]: r["status"] for r in evaluation["drift"]["results"]}
    assert statuses["firmware.auto_upgrade.enabled"] == "match"


def test_desired_auto_upgrade_unreadable_is_not_an_alert():
    snapshot = _snapshot(auto=None)
    snapshot["groups"]["firmware"]["auto_upgrade"] = {"state": "unknown",
                                                      "reason": "missing_method"}
    evaluation = twin.evaluate_snapshot("cam", _plan(firmware_auto_upgrade=True), snapshot)
    statuses = {r["path"]: r["status"] for r in evaluation["drift"]["results"]}
    assert statuses["firmware.auto_upgrade.enabled"] == "unsupported"
    assert twin.alertable_keys(evaluation) == set()


def test_config_parses_firmware_auto_upgrade():
    app = cfg.load_config_from_dict({"cameras": [
        {"name": "front", "host": "192.0.2.50", "firmware_auto_upgrade": False}]})
    assert app.cameras[0].firmware_auto_upgrade is False
    app = cfg.load_config_from_dict({"cameras": [{"name": "front", "host": "192.0.2.50"}]})
    assert app.cameras[0].firmware_auto_upgrade is None


def test_config_rejects_non_bool_firmware_auto_upgrade():
    with pytest.raises(cfg.ConfigError, match="'firmware_auto_upgrade' must be a boolean"):
        cfg.load_config_from_dict({"cameras": [
            {"name": "front", "host": "192.0.2.50", "firmware_auto_upgrade": "off"}]})


def test_plan_carries_the_desired_auto_upgrade():
    from tapo_monitor import daemon
    app = cfg.load_config_from_dict({"cameras": [
        {"name": "front", "host": "192.0.2.50", "firmware_auto_upgrade": True}]})
    assert daemon.plan_camera(app.cameras[0], False, False).firmware_auto_upgrade is True


# --- fleet entry and version change -----------------------------------------------

def _entry(snapshot, previous=None, at=2.0):
    return twin.fleet_entry(
        captured_at=at, snapshot=snapshot, health={"status": "ok"},
        evaluation={"desired": {}, "actual": {}, "drift": {"results": []}},
        previous=previous)


def test_fleet_entry_records_firmware_info():
    assert _entry(_snapshot())["firmware"] == {
        "version": FW_NEW, "auto_upgrade": True, "auto_upgrade_time": "03:00"}


def test_firmware_version_change_is_a_history_transition():
    before = _entry(_snapshot(version=FW_OLD), at=1.0)
    after = _entry(_snapshot(version=FW_NEW), previous=before, at=2.0)
    assert after["history"] == [
        {"at": 2.0, "kind": "firmware", "from": FW_OLD, "to": FW_NEW}]
    # It is a detail, not a drift: nothing becomes alertable.
    assert twin.alertable_keys(after) == set()


def test_firmware_change_is_read_from_an_older_entry_without_the_firmware_block():
    before = _entry(_snapshot(version=FW_OLD), at=1.0)
    del before["firmware"]
    after = _entry(_snapshot(version=FW_NEW), previous=before, at=2.0)
    assert after["history"][-1]["kind"] == "firmware"


@pytest.mark.parametrize("old,new", [(FW_NEW, FW_NEW), (None, FW_NEW), (FW_OLD, None)])
def test_unchanged_or_unreadable_version_is_no_transition(old, new):
    before = _entry(_snapshot(version=old), at=1.0)
    after = _entry(_snapshot(version=new), previous=before, at=2.0)
    assert after["history"] == []


# --- CLI ---------------------------------------------------------------------------

def test_twin_status_shows_firmware_and_auto_upgrade(tmp_path, capsys):
    path = tmp_path / "twin.json"
    before = _entry(_snapshot(version=FW_OLD), at=1_800_000_000.0)
    front = _entry(_snapshot(version=FW_NEW), previous=before, at=1_800_000_100.0)
    yard = _entry(_snapshot(auto=None, version=None))
    assert twin.save_state(str(path), {"front": front, "yard": yard})

    assert cli.main(["twin-status", str(path)]) == 0

    out = capsys.readouterr().out
    assert "auto-upgrade" in out
    front_line = [line for line in out.splitlines()
                  if line.startswith("front") and FW_NEW in line]
    assert len(front_line) == 1
    assert "on (03:00)" in front_line[0]
    assert f"was {FW_OLD.split()[0]}" in front_line[0]
    assert ["yard", "unknown", "unknown"] in [line.split() for line in out.splitlines()]


def test_twin_status_json_carries_the_firmware_block(tmp_path, capsys):
    path = tmp_path / "twin.json"
    assert twin.save_state(str(path), {"front": _entry(_snapshot())})
    assert cli.main(["twin-status", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cameras"]["front"]["firmware"]["auto_upgrade"] is True


# --- daily digest ------------------------------------------------------------------

def test_digest_snapshot_carries_firmware_and_a_recent_change():
    from tapo_monitor import daemon
    app = cfg.load_config_from_dict({"cameras": [{"name": "front", "host": "192.0.2.50"}]})
    state = daemon.MonitorState()
    before = _entry(_snapshot(version=FW_OLD), at=900.0)
    state.twin_fleet["front"] = _entry(_snapshot(version=FW_NEW), previous=before, at=1000.0)

    snap = daemon.fleet_health_snapshot(app, state, now=1100, fetch_metrics=lambda u: {})

    firmware = snap["cameras"]["front"]["firmware"]
    assert firmware["version"] == FW_NEW
    assert firmware["auto_upgrade"] is True
    assert firmware["changed"] == {"from": FW_OLD, "to": FW_NEW, "age_s": 100.0}


def test_digest_snapshot_forgets_a_firmware_change_older_than_a_day():
    from tapo_monitor import daemon
    app = cfg.load_config_from_dict({"cameras": [{"name": "front", "host": "192.0.2.50"}]})
    state = daemon.MonitorState()
    before = _entry(_snapshot(version=FW_OLD), at=900.0)
    state.twin_fleet["front"] = _entry(_snapshot(version=FW_NEW), previous=before, at=1000.0)

    snap = daemon.fleet_health_snapshot(app, state, now=1000 + 90_000,
                                        fetch_metrics=lambda u: {})
    assert "changed" not in snap["cameras"]["front"]["firmware"]


def test_digest_lines_show_firmware_as_detail_not_a_failure():
    lines = reviewdigest.fleet_lines({
        "cameras": {
            "front": {"reachable": True, "events": True,
                      "firmware": {"version": FW_NEW, "auto_upgrade": True,
                                   "auto_upgrade_time": "03:00",
                                   "changed": {"from": FW_OLD, "to": FW_NEW,
                                               "age_s": 3600.0}}},
            "yard": {"reachable": True, "events": True,
                     "firmware": {"version": FW_OLD, "auto_upgrade": False,
                                  "auto_upgrade_time": "03:00"}},
        },
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
    })
    text = "\n".join(lines)
    assert text.startswith("\U0001f49a Fleet OK")
    assert f"firmware: front {FW_NEW}, auto-upgrade: on (03:00)" in text
    assert f"yard {FW_OLD}, auto-upgrade: off" in text
    assert f"firmware changed: front {FW_OLD} → {FW_NEW}" in text


def test_digest_lines_without_firmware_say_nothing_about_it():
    lines = reviewdigest.fleet_lines({
        "cameras": {"front": {"reachable": True, "events": True}},
        "tick": {"ok": True}, "scorer": None, "recorder": None, "repairs": {},
    })
    assert "firmware" not in "\n".join(lines)
