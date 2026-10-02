"""The standalone watcher must distinguish loss, broken probes, and missing history."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "network_watch", Path(__file__).parents[1] / "tools/network_watch.py")
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)


def test_partial_loss_duplicate_and_missing_reply():
    result = watch.parse_ping("""64 bytes: icmp_seq=1 ttl=64 time=2.0 ms
64 bytes: icmp_seq=2 ttl=64 time=8.0 ms
64 bytes: icmp_seq=2 ttl=64 time=9.0 ms (DUP!)
64 bytes: icmp_seq=4 ttl=64 time=4.0 ms
5 packets transmitted, 3 received, +1 duplicates, 40% packet loss
""")
    assert result["sent"] == 5 and result["received"] == 3
    assert result["rtt_ms"] == [2, 8, 4]
    assert result["jitter_ms"] == [6]  # no pair spanning the missing sequence 3
    assert result["error"] is None


def test_total_loss_is_a_valid_measurement():
    metrics = watch.parse_ping("5 packets transmitted, 0 received, 100% packet loss")
    assert metrics["sent"] == 5 and metrics["received"] == 0
    assert metrics["error"] is None
    record = watch.probe({"name": "camera", "host": "example.invalid"},
                         run=lambda *a, **kw: subprocess.CompletedProcess(a, 1,
                             "5 packets transmitted, 0 received, 100% packet loss"))
    assert record["error"] is None


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("ping", 10)])
def test_probe_failure_is_unknown_not_packet_loss(failure):
    def run(*args, **kwargs):
        raise failure
    record = watch.probe({"name": "camera", "host": "example.invalid"}, run=run)
    assert record["sent"] is None and record["received"] is None
    assert record["error"]


def record(at, *, name="camera", host="example.invalid", sent=5, received=5, rtts=None):
    return {"at": at, "name": name, "host": host, "sent": sent, "received": received,
            "rtt_ms": rtts or [], "jitter_ms": [], "error": None}


def test_summary_window_weighting_total_loss_errors_and_address_changes(tmp_path):
    now = 1780000000
    rows = [record(now - 90000), record(now - 200, sent=2, received=1, rtts=[10]),
            record(now - 100, received=0), record(now - 50, rtts=[2, 4, 6, 8, 10]),
            {**record(now - 20), "sent": None, "received": None, "error": "OSError"},
            record(now - 10, host="other.invalid", rtts=[1, 1, 1, 1, 1])]
    watch.append_records(tmp_path, rows, now=now)
    with next(tmp_path.glob("*.jsonl")).open("a") as file:
        file.write('{"partial":\n')
    result = watch.summary(tmp_path, 24, now=now)
    assert len(result["targets"]) == 2
    row = result["targets"][0]
    assert row["sent"] == 12 and row["received"] == 6
    assert row["loss_pct"] == 50
    assert row["probe_errors"] == 1
    assert row["p95_ms"] == 10
    assert row["avg_ms"] == pytest.approx(40 / 6)
    assert row["age_s"] == 20
    assert result["skipped_records"] == 1


def test_retention_and_private_modes(tmp_path):
    now = 1780000000
    old = tmp_path / "2000-01-01.jsonl"
    old.write_text("old")
    unrelated = tmp_path / "operator-notes.jsonl"
    unrelated.write_text("keep")
    watch.append_records(tmp_path, [record(now)], now=now)
    assert not old.exists() and unrelated.exists()
    assert next(p for p in tmp_path.glob("*.jsonl") if p != unrelated).stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "latest.json").read_text())["targets"][0]["at"] == now


def test_gateway_duplicates_and_bad_targets():
    routes = [{"gateway": "192.0.2.1", "dev": "wlan0"},
              {"gateway": "192.0.2.1", "dev": "eth0"}]
    targets = watch.gateway_targets(run=lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(routes)))
    assert targets == [{"name": "router:wlan0", "host": "192.0.2.1"}]
    with pytest.raises(ValueError):
        watch.validate_targets([{"name": "camera", "host": "-f"}])
    with pytest.raises(ValueError):
        watch.validate_targets([{"name": "camera", "host": "example.invalid"}] * 2)


def test_missing_history_and_corrupt_sample_are_visible(tmp_path):
    assert watch.main(["summary", "--state-dir", str(tmp_path), "--json"]) == 1
    now = 1780000000
    path = tmp_path / "2026-05-28.jsonl"
    path.write_text(json.dumps({**record(now), "rtt_ms": [float("inf")]}) + "\n")
    assert watch.summary(tmp_path, 24, now=now)["skipped_records"] == 1


def test_fleet_preserves_success_when_another_host_is_unavailable(tmp_path, monkeypatch, capsys):
    inventory = tmp_path / "fleet.json"
    inventory.write_text(json.dumps({"hosts": [
        {"name": "site-a", "command": ["ssh", "-o", "BatchMode=yes", "site-a"]},
        {"name": "site-b", "command": ["ssh", "site-b"]},
    ]}))
    commands = []
    row = {"name": "hub", "received": 5, "sent": 5, "loss_pct": 0,
           "avg_ms": 2, "p95_ms": 3, "max_ms": 3, "jitter_ms": 1,
           "age_s": 20, "probe_errors": 0}

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 30
        if command[1] == "site-b":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0, json.dumps({"targets": [row]}))

    monkeypatch.setattr(watch.subprocess, "run", run)
    assert watch.main(["fleet", "--inventory", str(inventory), "--hours", "48"]) == 1
    output = capsys.readouterr().out
    assert "site-a / hub" in output
    assert "site-b: measurements unavailable (TimeoutExpired)" in output
    assert commands[0][0:5] == ["ssh", "-o", "BatchMode=yes", "site-a", watch.REMOTE_SUMMARY + "48.0"]


def test_fleet_empty_remote_history_is_not_a_successful_measurement(tmp_path, monkeypatch, capsys):
    inventory = tmp_path / "fleet.json"
    inventory.write_text(json.dumps({"hosts": [{"name": "site-a", "command": ["ssh", "site-a"]}]}))
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, json.dumps({"targets": []})))
    assert watch.main(["fleet", "--inventory", str(inventory), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["hosts"][0]["targets"] == []


@pytest.mark.parametrize("hours", ["0", "-1", "721", "nan", "inf"])
def test_cli_rejects_an_invalid_summary_window(hours):
    with pytest.raises(SystemExit) as failure:
        watch.main(["summary", "--hours", hours])
    assert failure.value.code == 2


def test_gateway_discovery_failure_still_measures_configured_camera(tmp_path, monkeypatch, capsys):
    config = tmp_path / "targets.json"
    config.write_text(json.dumps({"targets": [{"name": "camera", "host": "example.invalid"}]}))
    now = 1780000000
    monkeypatch.setattr(watch.time, "time", lambda: now)

    def missing_gateway():
        raise ValueError("no default gateway found")

    monkeypatch.setattr(watch, "gateway_targets", missing_gateway)
    measured = []

    def probe(target):
        measured.append(target)
        return record(now, name=target["name"], host=target["host"], rtts=[1] * 5)

    monkeypatch.setattr(watch, "probe", probe)
    assert watch.sample(config, tmp_path / "state") == 1
    assert measured == [{"name": "camera", "host": "example.invalid"}]
    targets = watch.summary(tmp_path / "state", 24, now=now)["targets"]
    assert targets[0]["received"] == 5 and targets[0]["loss_pct"] == 0
    assert targets[1]["latest_error"] == "gateway_discovery_ValueError"
    assert targets[1]["loss_pct"] is None
    assert "gateway_discovery_ValueError" in capsys.readouterr().err


def test_invalid_calendar_filename_does_not_break_retention_or_summary(tmp_path):
    invalid = tmp_path / "2026-99-99.jsonl"
    invalid.write_text("operator file")
    now = 1780000000
    watch.append_records(tmp_path, [record(now)], now=now)
    assert invalid.read_text() == "operator file"
    assert len(watch.summary(tmp_path, 24, now=now)["targets"]) == 1


def test_history_size_cap_removes_oldest_day_first(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "MAX_HISTORY_BYTES", 200)
    older = tmp_path / "2026-05-27.jsonl"
    newest = tmp_path / "2026-05-28.jsonl"
    older.write_bytes(b"x" * 180)
    newest.write_bytes(b"y" * 80)
    watch.prune_history(tmp_path, date=watch.dt.date(2026, 5, 28))
    assert not older.exists()
    assert newest.read_bytes() == b"y" * 80


def test_history_size_cap_keeps_complete_newest_records_from_oversized_day(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "MAX_HISTORY_BYTES", 300)
    path = tmp_path / "2026-05-28.jsonl"
    records = [{"sequence": number, "padding": "x" * 50} for number in range(12)]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    watch.prune_history(tmp_path, date=watch.dt.date(2026, 5, 28))
    kept = [json.loads(line) for line in path.read_text().splitlines()]
    assert 0 < path.stat().st_size <= watch.MAX_HISTORY_BYTES
    assert kept and kept == records[-len(kept):]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".history-*"))


def test_summary_distinguishes_latest_attempt_from_latest_measurement(tmp_path):
    now = 1780000000
    latest_error = {**record(now - 10), "sent": None, "received": None, "error": "OSError"}
    # Input need not be sorted; a recent failed invocation must not hide the age
    # of the last completed burst, including a valid burst with complete loss.
    watch.append_records(tmp_path, [latest_error, record(now - 100, received=0),
                                    record(now - 200)], now=now)
    [row] = watch.summary(tmp_path, 24, now=now)["targets"]
    assert row["age_s"] == 10
    assert row["last_measurement_at"] == now - 100
    assert row["measurement_age_s"] == 100
    assert row["latest_error"] == "OSError"
    assert row["loss_pct"] == 50
