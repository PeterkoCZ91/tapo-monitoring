import json
import types

import pytest

from tapo_monitor import cli, health, ledger, twin


def _state(**overrides):
    values = {name: {} for name in health.PERSISTED_FIELDS}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_status_prints_observed_uptime_and_outages(tmp_path, capsys, monkeypatch):
    path = tmp_path / "health.json"
    state = _state(
        online_since={"front": 100, "back": 300},
        fail_since={"back": 900},
        last_outage_duration={"front": 60},
        reconnect_count={"front": 2, "back": 1},
        total_observed_online={"back": 600},
        total_observed_offline={"front": 100},
    )
    assert health.save_state(str(path), state)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)

    assert cli.main(["status", str(path)]) == 0

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert next(line for line in lines if line.startswith("front")).split() == [
        "front", "online", "15m", "90.00%", "1m", "2"]
    assert next(line for line in lines if line.startswith("back")).split() == [
        "back", "offline", "1m", "40s", "85.71%", "-", "1"]
    assert output.count("reconnects") == 1


def test_status_missing_state_returns_failure(tmp_path, capsys):
    path = tmp_path / "missing.json"

    assert cli.main(["status", str(path)]) == 1
    assert str(path) in capsys.readouterr().err


def test_health_status_rows_are_sorted_and_clamp_clock_skew():
    state = _state(
        online_since={"z": 200, "a": 100},
        fail_since={"z": 150},
        last_observed_uptime={"z": 50},
    )

    rows = health.status_rows(state, now=120)

    assert [row["camera"] for row in rows] == ["a", "z"]
    assert rows[0]["current_for"] == 20
    assert rows[1]["state"] == "offline"
    assert rows[1]["current_for"] == 0


def test_twin_status_prints_layered_health_and_drift(tmp_path, capsys):
    path = tmp_path / "twin.json"
    cameras = {
        "front": {
            "health": {
                "status": "degraded",
                "layers": {
                    "network": "ok", "api": "ok", "events": "degraded",
                    "rtsp": "ok", "storage": "unknown",
                },
            },
            "drift": {"counts": {"drift": 1, "unknown": 2}},
        }
    }
    assert twin.save_state(str(path), cameras)

    assert cli.main(["twin-status", str(path)]) == 0

    row = next(line for line in capsys.readouterr().out.splitlines()
               if line.startswith("front"))
    assert row.split() == [
        "front", "degraded", "ok", "ok", "degraded", "ok", "unknown", "1", "2"
    ]


def test_twin_status_json_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "twin.json"
    assert twin.save_state(str(path), {"front": {"health": {}, "drift": {}}})

    assert cli.main(["twin-status", str(path), "--json"]) == 0

    assert "front" in json.loads(capsys.readouterr().out)["cameras"]


def test_shadow_record_and_report_commands(tmp_path, capsys):
    path = tmp_path / "events.sqlite3"
    events = ledger.EventLedger(path)
    events.record_camera_event(camera="front", event_type="person", event_at=100)

    assert cli.main([
        "shadow-record", "front", "person", "101", "--confidence", "0.9",
        "--ledger", str(path),
    ]) == 0
    capsys.readouterr()
    assert cli.main([
        "shadow-report", "front", "--end", "110", "--hours", "1",
        "--window", "2", "--ledger", str(path), "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["matched"] == 1
    assert report["camera_only"] == 0
    assert report["shadow_only"] == 0


def test_shadow_report_rejects_non_positive_window(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "shadow-report", "front", "--window", "-1",
            "--ledger", str(tmp_path / "events.sqlite3"),
        ])

    assert exc.value.code == 2
    assert "--window" in capsys.readouterr().err


@pytest.mark.parametrize("window", ["nan", "inf", "-inf"])
def test_shadow_report_rejects_non_finite_window(tmp_path, capsys, window):
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "shadow-report", "front", "--window", window,
            "--ledger", str(tmp_path / "events.sqlite3"),
        ])

    assert exc.value.code == 2
    assert "--window" in capsys.readouterr().err


def _probe_config(tmp_path):
    path = tmp_path / "cameras.yaml"
    path.write_text(
        "cameras:\n"
        "  - name: front\n"
        "    host: 192.0.2.50\n"
    )
    return path


def test_probe_reports_layered_health_for_one_camera(tmp_path, capsys, monkeypatch):
    # The probe opens its own authenticated session, so it must be explicit about that
    # and must never be reachable by accident from the daemon path.
    monkeypatch.setattr(cli, "_probe_camera",
                        lambda cfg, night: {"health": {"status": "degraded",
                                                       "layers": {"network": "ok",
                                                                  "api": "down"}},
                                            "drift": {"counts": {"drift": 1, "unknown": 2}}})
    rc = cli.main(["probe", str(_probe_config(tmp_path))])
    out = capsys.readouterr()
    assert rc == 0
    assert "front" in out.out and "degraded" in out.out
    assert "authenticated session" in out.err      # the operator is told what it costs


def test_probe_json_is_machine_readable(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_probe_camera",
                        lambda cfg, night: {"health": {"status": "ok", "layers": {}},
                                            "drift": {"counts": {}}})
    rc = cli.main(["probe", str(_probe_config(tmp_path)), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cameras"]["front"]["health"]["status"] == "ok"


def test_probe_can_select_a_single_camera(tmp_path, capsys, monkeypatch):
    path = tmp_path / "cameras.yaml"
    path.write_text(
        "cameras:\n"
        "  - name: front\n    host: 192.0.2.50\n"
        "  - name: yard\n    host: 192.0.2.51\n"
    )
    seen = []
    monkeypatch.setattr(cli, "_probe_camera",
                        lambda cfg, night: seen.append(cfg.name) or
                        {"health": {"status": "ok", "layers": {}}, "drift": {"counts": {}}})
    assert cli.main(["probe", str(path), "--camera", "yard"]) == 0
    assert seen == ["yard"]


def test_probe_rejects_an_unknown_camera(tmp_path, capsys):
    assert cli.main(["probe", str(_probe_config(tmp_path)), "--camera", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_main_dispatches_shadow_scan(monkeypatch):
    called = {}
    monkeypatch.setattr("tapo_monitor.shadowscan.main",
                        lambda argv: called.update(argv=argv) or 0)
    assert cli.main(["shadow-scan", "cameras.yaml", "--date", "2026-08-12"]) == 0
    assert called["argv"] == ["cameras.yaml", "--date", "2026-08-12"]


def test_version_prints_release_and_package_fingerprint(capsys):
    from tapo_monitor import __version__

    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == f"tapo-monitor {__version__}"
    fingerprint = [line for line in out if line.startswith("package ")][0].split()[1]
    assert len(fingerprint) == 12                     # short sha256 over the module set
    assert cli.package_fingerprint() == fingerprint   # stable for identical trees


def test_package_fingerprint_changes_with_module_contents(tmp_path):
    first = tmp_path / "pkg_a"
    second = tmp_path / "pkg_b"
    for path, body in ((first, "x = 1\n"), (second, "x = 2\n")):
        path.mkdir()
        (path / "daemon.py").write_text(body, encoding="utf-8")
        (path / "notes.txt").write_text("ignored\n", encoding="utf-8")

    assert cli.package_fingerprint(str(first)) != cli.package_fingerprint(str(second))
    assert cli.package_fingerprint(str(first)) == cli.package_fingerprint(str(first))


def test_selfcheck_reports_every_gate_and_succeeds(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text(
        "cameras:\n"
        "  - name: front\n"
        "    host: 203.0.113.10\n"
        "    user_env: CAM_USER\n"
        "    password_env: CAM_PASS\n",
        encoding="utf-8")
    monkeypatch.setenv("CAM_USER", "operator")
    monkeypatch.setenv("CAM_PASS", "secret")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli.main(["selfcheck", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "modules: ok" in out
    assert "config: ok (1 camera(s))" in out
    assert "credentials: ok" in out
    assert "ffmpeg: ok" in out
    assert "secret" not in out                        # never echo a credential


def test_selfcheck_fails_on_missing_camera_credentials(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text(
        "cameras:\n"
        "  - name: front\n"
        "    host: 203.0.113.10\n"
        "    user_env: CAM_USER\n"
        "    password_env: CAM_PASS\n",
        encoding="utf-8")
    monkeypatch.delenv("CAM_USER", raising=False)
    monkeypatch.delenv("CAM_PASS", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli.main(["selfcheck", str(config_path)]) == 1
    out = capsys.readouterr().out
    assert "credentials: FAILED" in out
    assert "front: CAM_USER, CAM_PASS" in out         # names, never values


def test_selfcheck_fails_on_a_broken_config(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: front\n", encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli.main(["selfcheck", str(config_path)]) == 1
    assert "config: FAILED" in capsys.readouterr().out


def test_selfcheck_fails_when_ffmpeg_is_missing(tmp_path, capsys, monkeypatch):
    # The exact 2026-08-16 failure: the daemon ran for two days without ffmpeg on PATH.
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: front\n    host: 203.0.113.10\n",
                           encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli.main(["selfcheck", str(config_path)]) == 1
    assert "ffmpeg: FAILED" in capsys.readouterr().out


def test_selfcheck_skips_a_camera_that_names_no_credential_env(tmp_path, capsys, monkeypatch):
    # A battery camera read through a hub has no login of its own — nothing to assert.
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: gate\n    host: 203.0.113.10\n",
                           encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli.main(["selfcheck", str(config_path)]) == 0
    assert "credentials: ok" in capsys.readouterr().out
