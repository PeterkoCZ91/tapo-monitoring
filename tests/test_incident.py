import json
import logging

import pytest

from tapo_monitor import config, incident, ledger, monitor, sentlog


def test_incident_id_is_camera_and_event_start():
    assert incident.incident_id("yard", {"start_time": 1710003600.7}) == "yard-1710003600"


def test_incident_id_is_none_without_a_usable_start():
    assert incident.incident_id("yard", {}) is None
    assert incident.incident_id("yard", {"start_time": "soon"}) is None
    assert incident.incident_id("yard", {"start_time": 0}) is None


def test_parse_round_trips_camera_names_with_dashes():
    assert incident.parse("back-yard-1710003600") == ("back-yard", 1710003600)


@pytest.mark.parametrize("bad", ["", "yard", "yard-", "-17", "yard-12x"])
def test_parse_rejects_malformed_ids(bad):
    with pytest.raises(ValueError):
        incident.parse(bad)


def test_every_audit_line_carries_the_incident(caplog):
    cfg = config.load_config_from_dict(
        {"cameras": [{"name": "yard", "host": "192.0.2.10"}]}).cameras[0]
    with caplog.at_level(logging.INFO, logger="tapo_monitor.monitor"):
        monitor.audit_event(cfg, {"start_time": 1710003600}, "person", "live", "send")
        monitor.audit_event(cfg, {}, "person", "live", "send")
    first, second = (r.getMessage() for r in caplog.records)
    assert "incident=yard-1710003600" in first
    assert "incident=" not in second


def test_sent_log_index_records_the_incident(tmp_path):
    sentlog.archive_sent(str(tmp_path), b"jpeg", "cap", now=1710003610.0,
                         camera="yard", incident="yard-1710003600")
    record = json.loads((tmp_path / sentlog.INDEX_NAME).read_text())
    assert record["incident"] == "yard-1710003600"


def _chain_fixture(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    start = 1710003600.0
    events.record_camera_event(camera="yard", event_type="person", event_at=start,
                               observed_at=start + 4)
    events.record_decision(camera="yard", event_type="person", event_at=start,
                           path="live", action="send", observed_at=start + 6,
                           score=0.71, threshold=0.4, telegram=True)
    events.record_decision(camera="yard", event_type="person", event_at=start + 50,
                           path="live", action="send", observed_at=start + 55)
    sent = tmp_path / "sent"
    sentlog.archive_sent(str(sent), b"jpeg", "cap", now=start + 6, camera="yard",
                         incident="yard-1710003600")
    sentlog.archive_sent(str(sent), b"jpeg", "other", now=start + 55, camera="yard",
                         incident="yard-1710003650")
    return events, sent


def test_chain_collects_only_this_incident(tmp_path):
    events, sent = _chain_fixture(tmp_path)
    found = incident.chain("yard-1710003600", events, str(sent))
    assert [o["event_type"] for o in found["observations"]] == ["person"]
    assert [(d["path"], d["action"], d["telegram"]) for d in found["decisions"]] == [
        ("live", "send", True)]
    assert [s["caption"] for s in found["sent"]] == ["cap"]


def test_chain_without_a_sent_log_is_still_answered(tmp_path):
    events, _ = _chain_fixture(tmp_path)
    found = incident.chain("yard-1710003600", events, None)
    assert found["sent"] == [] and len(found["decisions"]) == 1


def test_cli_prints_the_chain(tmp_path, capsys):
    events, sent = _chain_fixture(tmp_path)
    code = incident.main(["yard-1710003600", "--ledger", events.path,
                          "--sent-log", str(sent)])
    out = capsys.readouterr().out
    assert code == 0
    assert "live send" in out and "+6.0s" in out and "cap" in out


def test_cli_json_and_bad_id(tmp_path, capsys):
    events, sent = _chain_fixture(tmp_path)
    assert incident.main(["yard-1710003600", "--ledger", events.path, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["incident"] == "yard-1710003600"
    assert incident.main(["nonsense", "--ledger", events.path]) == 2


def test_cli_dispatches_the_incident_command(tmp_path, capsys):
    from tapo_monitor import cli
    events, _ = _chain_fixture(tmp_path)
    assert cli.main(["incident", "yard-1710003600", "--ledger", events.path]) == 0
    assert "incident yard-1710003600" in capsys.readouterr().out
