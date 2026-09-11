"""Exercise the workstation table and its real remote probe without network access."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def fleet_probe(tmp_path):
    root = tmp_path / "monitor"
    root.mkdir()
    (root / "tapo_monitor").symlink_to(REPO / "tapo_monitor", target_is_directory=True)
    (root / "cameras.yaml").write_text(
        "cameras:\n  - name: front\n    host: 192.0.2.50\n"
    )
    state = tmp_path / "health.json"
    records = tmp_path / "records" / "192.0.2.50"
    records.mkdir(parents=True)
    segment = records / "zaznam_20260911T000000.mkv"
    segment.write_bytes(b"video")
    envfile = tmp_path / "monitor.env"
    envfile.write_text(f"TAPO_HEALTH_STATE_FILE={state}\nRECORDING_ROOT={records.parent}\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in {
        "ssh": 'exec bash -c "${@: -1}"',
        "systemctl": f"""case "$*" in
*EnvironmentFiles*)
    printf '%s\\n' LoadState=loaded ActiveState=active SubState=running NRestarts=0 ActiveEnterTimestampMonotonic=0 'EnvironmentFiles={envfile}' ;;
*LastTriggerUSec*) printf '%s\\n' LoadState=not-found ;;
*) printf '%s\\n' success ;;
esac""",
        "df": "printf 'Filesystem Blocks Used Available Capacity Mounted\\nroot 100 1 99 1%% /\\n'",
    }.items():
        executable = bins / name
        executable.write_text("#!/usr/bin/env bash\n" + body + "\n")
        executable.chmod(0o755)

    def run(*, offline=False, stale=False, missing=False, retired=False,
            battery=False, event_failure=False):
        payload = {"online_since": {"front": time.time() - 300}}
        if offline:
            payload["fail_since"] = {"front": time.time() - 172800}
        if retired:
            payload["fail_since"] = {"retired": time.time() - 172800}
        if battery:
            (root / "cameras.yaml").write_text(
                "cameras:\n  - name: front\n    host: 192.0.2.50\n"
                "    detection:\n      sources: [hubpoll]\n"
                "    hub_host: 192.0.2.60\n    go2rtc_src: front\n"
            )
            payload = {}
        if event_failure:
            payload["event_fail_since"] = {"front": time.time() - 600}
        state.write_text(json.dumps({"version": 1, "state": payload}))
        if missing:
            state.unlink()
        if stale:
            os.utime(segment, (time.time() - 172800,) * 2)
        env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}",
                   TAPO_FLEET_HOSTS="test-host", TAPO_FLEET_ROOT=str(root),
                   TAPO_FLEET_PYTHON=sys.executable)
        return subprocess.run(["bash", str(REPO / "tools/fleet_status.sh"), "--no-scorer"],
                              env=env, text=True, capture_output=True, timeout=20)

    return run


def test_offline_camera_is_a_fleet_finding(fleet_probe):
    result = fleet_probe(offline=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "front: offline" in result.stdout


def test_stale_recording_is_a_fleet_finding(fleet_probe):
    result = fleet_probe(stale=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "front: recording stale_output" in result.stdout


def test_missing_camera_state_cannot_report_healthy(fleet_probe):
    result = fleet_probe(missing=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "health state unavailable" in result.stdout


def test_removed_camera_does_not_poison_active_fleet(fleet_probe):
    result = fleet_probe(retired=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "findings: none" in result.stdout
    assert "retired" not in result.stdout


@pytest.mark.parametrize("missing", [False, True])
def test_sleeping_battery_camera_does_not_require_wired_health(fleet_probe, missing):
    result = fleet_probe(battery=True, missing=missing)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "battery/hub (may sleep)" in result.stdout
    assert "availability unknown" not in result.stdout


def test_battery_event_failure_remains_a_finding(fleet_probe):
    result = fleet_probe(battery=True, event_failure=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "front: event API unavailable" in result.stdout
