"""tools/soak_report.py: the arithmetic behind the soak report, on synthetic probes."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "soak"

spec = importlib.util.spec_from_file_location("soak_report", ROOT / "tools" / "soak_report.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


def probe(name, hours=24):
    return soak.analyze((FIXTURES / f"{name}.raw").read_text(), hours)


def clone(record):
    return json.loads(json.dumps(record))


def test_percentile_and_median():
    assert soak.median([]) is None
    assert soak.median([7]) == 7
    assert soak.median([1, 2, 3, 4]) == 2.5
    assert soak.percentile([10, 20, 30, 40, 50], 0.9) == pytest.approx(46.0)
    assert soak.percentile([5, 1, 3], 0.0) == 1


def test_pct_change_without_base_is_none():
    assert soak.pct_change(0, 5) is None
    assert soak.pct_change(10, 13) == pytest.approx(0.3)


def test_healthy_host_numbers():
    rec = probe("host-a")
    assert rec["findings"] == []
    assert rec["unit"]["ok"] is True
    assert rec["package"] == {"fingerprint": "abc123def456", "version": "0.8.1",
                              "release": "20261001T060000Z-abc123def456"}
    assert rec["starts"] == 1
    assert rec["errors"] == 0
    assert rec["alerts"]["sent_ok"] == 5
    assert rec["alerts"]["sent_failed"] == 1
    assert rec["alerts"]["by_camera"]["cam-a"]["sd"]["ok"] == 1
    # Four events; the second send of the first event (45 s) is not the first alert.
    assert rec["first_alert"]["n"] == 4
    assert rec["first_alert"]["median_s"] == 85
    assert rec["first_alert"]["p90_s"] == pytest.approx(163)
    assert rec["gates"] == {"cam-a": {"drop": 1, "hold": 1}, "cam-b": {"cooldown": 1, "drop": 1}}
    assert rec["would"] == {"cam-a": {"would_send": 2}, "cam-b": {"would_loiter": 1}}
    assert rec["host_switch"] == 1
    assert rec["hub_errors"] == 2
    assert rec["outbox_lines"] == 1 and rec["outbox_problems"] == 0
    assert rec["resources"]["memory_bytes"] == 128 * 1048576
    assert rec["resources"]["cpu_pct"] == pytest.approx(720 / 10200.5 * 100, rel=1e-3)


def test_errors_are_counted_from_content_and_grouped():
    rec = probe("host-bad")
    assert rec["errors"] == 4
    assert rec["failures"] == 1
    assert rec["error_signatures"][0] == {"count": 2, "text": "ERROR camera N failed"}
    assert not rec["unit"]["ok"]
    assert "auto-restart" in rec["unit"]["verdict"]
    # accounting off: a UINT64_MAX / "[not set]" counter is unknown, not a huge number
    assert rec["resources"]["memory_bytes"] is None
    assert rec["resources"]["cpu_s"] is None


def test_missing_unit_cannot_pass():
    """`systemctl show` on a missing unit says Result=success; LoadState is the real key."""
    rec = probe("host-missing")
    assert rec["unit"]["ok"] is False
    assert "not-found" in rec["unit"]["verdict"]
    assert rec["errors"] is None  # unknown, not 0
    assert any("no lines" in f for f in rec["findings"])


@pytest.mark.parametrize("name, fragment", [
    ("host-truncated", "truncated"),
    ("host-denied", "unreadable"),
])
def test_unreliable_probes_are_findings_not_zeroes(name, fragment):
    rec = probe(name)
    assert any(fragment in f for f in rec["findings"])
    assert rec["errors"] is None


def test_silent_probe_is_unreachable():
    rec = probe("host-silent")
    assert rec["reachable"] is False
    assert soak.host_status(rec) == "UNREACHABLE"


def test_compare_flags_volume_and_new_errors():
    base = probe("host-a")
    cur = clone(base)
    assert soak.compare(base, cur) == []
    cur["alerts"]["sent_ok"] = 12
    cur["errors"] = 3
    cur["package"]["fingerprint"] = "fff"
    deviations = soak.compare(base, cur)
    assert any(d.startswith("alert volume +140%") for d in deviations)
    assert any("Traceback/ERROR/CRITICAL lines: 0 in 24 h -> 3 in 24 h" in d for d in deviations)
    assert any("package changed abc123def456 -> fff" in d for d in deviations)


def test_compare_ignores_small_absolute_alert_swings():
    base = probe("host-a")
    cur = clone(base)
    cur["alerts"]["sent_ok"] = 8  # +60 % but only 3 alerts
    assert soak.compare(base, cur) == []


def test_compare_uses_rates_across_windows():
    base = probe("host-a", hours=24)
    cur = clone(base)
    cur["since_hours"] = 12
    cur["alerts"]["sent_ok"] = 20  # 20 in 12 h vs 5 in 24 h
    assert any("alert volume" in d for d in soak.compare(base, cur))


def test_baseline_roundtrip(tmp_path, capsys):
    base = tmp_path / "base.json"
    args = [f"a={FIXTURES / 'host-a.raw'}", "--baseline", str(base)]
    assert soak.main(args) == 0
    assert "saved this run" in capsys.readouterr().out
    saved = json.loads(base.read_text())
    assert saved["version"] == 1 and "a" in saved["hosts"]
    assert soak.main(args) == 0  # compares, no deviations
    assert "vs baseline" not in capsys.readouterr().out
    # a different host name is reported as absent from the baseline
    assert soak.main([f"b={FIXTURES / 'host-a.raw'}", "--baseline", str(base)]) == 1
    assert "host not in the baseline" in capsys.readouterr().out


def test_json_output_and_exit_code(capsys):
    rc = soak.main([f"bad={FIXTURES / 'host-bad.raw'}", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["hosts"]["bad"]["errors"] == 4


def test_foreign_baseline_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"version": 99}')
    with pytest.raises(SystemExit):
        soak.main([f"a={FIXTURES / 'host-a.raw'}", "--baseline", str(path)])
