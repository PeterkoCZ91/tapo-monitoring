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


def _rendered(record, name="a"):
    return soak.render_host(name, soak.build_report({name: record})["hosts"][name])


def test_first_alert_is_also_split_by_the_path_that_delivered_it():
    fa = probe("host-a")["first_alert"]
    # The event at start=1000 was delivered live twice; only its earliest send counts.
    assert fa["by_path"] == {
        "live": {"n": 2, "median_s": 65.0, "p90_s": pytest.approx(93.0), "untimed": 0},
        "sampler": {"n": 1, "median_s": 70.0, "p90_s": 70.0, "untimed": 0},
        "sd": {"n": 1, "median_s": 190.0, "p90_s": 190.0, "untimed": 0},
    }
    # The summary and its per-camera split keep their meaning and their keys.
    assert fa["n"] == 4 and fa["median_s"] == 85
    assert set(fa["by_camera"]) == {"cam-a", "cam-b"}


def _audit(start, path, age, delivered="true", etype="person", reason=None, camera="cam-a"):
    age_field = "" if age is None else f" event_age_s={age}"
    reason_field = f" reason={reason}" if reason else ""
    return (f"INFO audit camera={camera} path={path} action=send etype={etype} start={start}"
            f"{age_field}{reason_field} telegram={delivered}")


def _first_alert(lines):
    raw = (FIXTURES / "host-a.raw").read_text().splitlines()
    kept = [line for line in raw if "audit " not in line]
    at = next(i for i, line in enumerate(raw) if "audit " in line)
    return soak.analyze("\n".join(kept[:at] + lines + kept[at:]) + "\n", 24)["first_alert"]


def test_first_alert_path_is_the_earliest_delivery_not_the_first_logged():
    """A card send logged first but older than a later live send must not claim the event."""
    fa = _first_alert([_audit(1000, "sd", 150), _audit(1000, "live", 20)])
    assert set(fa["by_path"]) == {"live"}
    assert fa["by_path"]["live"]["median_s"] == 20


def test_first_alert_ignores_a_failed_send_when_choosing_the_path():
    fa = _first_alert([_audit(1000, "live", 20, "false"), _audit(1000, "sd", 150)])
    assert set(fa["by_path"]) == {"sd"}


def test_first_alert_by_path_is_empty_without_deliveries():
    assert _first_alert([])["by_path"] == {}


def test_an_incident_counts_once_across_event_types():
    """A vehicle and a person alert of one camera event are one incident: its first
    delivery is the earlier of the two, and the later one is a follow-up, not a first."""
    fa = _first_alert([_audit(1000, "live", 20, etype="person"),
                       _audit("1000.0", "sd", 150, etype="vehicle")])
    assert fa["n"] == 1
    assert set(fa["by_path"]) == {"live"}


def test_the_incident_field_groups_sends_when_present():
    fa = _first_alert([
        _audit(1000, "sd", 150) + " incident=cam-a-1000",
        _audit("1000.4", "live", 20, etype="vehicle") + " incident=cam-a-1000",
    ])
    assert fa["n"] == 1 and fa["by_path"]["live"]["median_s"] == 20


def test_the_same_start_on_two_cameras_is_two_incidents():
    fa = _first_alert([_audit(1000, "live", 20), _audit(1000, "sd", 150, camera="cam-b")])
    assert fa["n"] == 2 and set(fa["by_path"]) == {"live", "sd"}


@pytest.mark.parametrize("path, reason, expected", [
    ("hubpoll", "retry", "hubpoll_retry"),
    ("sampler", "hold_rescue_recall", "hold_rescue"),
    ("sampler", "hold_expiry_send", "hold_expiry"),
    ("sampler", None, "sampler"),
    ("hubpoll", None, "hubpoll"),
    ("sd", "scorer_unavailable", "sd"),
])
def test_sends_that_wait_by_design_get_a_path_of_their_own(path, reason, expected):
    """Named as in the sent log (sentlog.SEND_PATHS): a hub retry or a hold that expired
    is slow on purpose and must not drag the plain path's median."""
    fa = _first_alert([_audit(1000, path, 90, reason=reason)])
    assert set(fa["by_path"]) == {expected}


def test_a_delivery_without_an_event_age_is_untimed_not_dropped():
    """An older package logs no event_age_s: the incident still counts for its path, with
    no time, instead of vanishing or turning into a zero."""
    fa = _first_alert([_audit(1000, "live", None), _audit(2000, "live", 30),
                       _audit(3000, "sd", None), _audit(3000, "live", None)])
    assert fa["n"] == 1
    assert fa["untimed"] == 2
    # Without any age the path of the first logged delivery stands for the incident.
    assert fa["by_path"]["live"] == {"n": 1, "median_s": 30.0, "p90_s": 30.0, "untimed": 1}
    assert fa["by_path"]["sd"] == {"n": 0, "median_s": None, "p90_s": None, "untimed": 1}


def test_a_timed_delivery_wins_over_an_untimed_one_of_the_same_incident():
    fa = _first_alert([_audit(1000, "sd", None), _audit(1000, "live", 25)])
    assert fa["untimed"] == 0
    assert set(fa["by_path"]) == {"live"}


def _per_path_block(lines):
    at = lines.index("   first delivery per path (event start to the first delivered alert "
                     "of an incident)")
    block = []
    for line in lines[at + 1:]:
        if not line.startswith("              "):
            break
        block.append(line.strip())
    return block


def test_terminal_report_shows_the_split_in_a_fixed_order():
    lines = _rendered(probe("host-a")).splitlines()
    assert _per_path_block(lines) == [
        "live          n=2    median 65 s   p90 93 s",
        "sampler       n=1    median 70 s   p90 70 s",
        "sd            n=1    median 190 s  p90 190 s",
    ]


def test_terminal_report_puts_unknown_paths_after_the_known_ones_and_shows_untimed():
    rec = probe("host-a")
    rec["first_alert"]["by_path"] = {
        "zz_new": {"n": 1, "median_s": 5.0, "p90_s": 5.0, "untimed": 0},
        "hold_expiry": {"n": 0, "median_s": None, "p90_s": None, "untimed": 3},
        "live": {"n": 1, "median_s": 9.0, "p90_s": 9.0, "untimed": 0},
    }
    assert _per_path_block(_rendered(rec).splitlines()) == [
        "live          n=1    median 9 s    p90 9 s",
        "hold_expiry   n=0    median n/a    p90 n/a   (+3 without event_age_s)",
        "zz_new        n=1    median 5 s    p90 5 s",
    ]


def test_terminal_report_says_so_when_nothing_was_delivered():
    rec = probe("host-a")
    rec["first_alert"].update(n=0, median_s=None, p90_s=None, by_path={})
    assert _per_path_block(_rendered(rec).splitlines()) == ["none delivered"]


def test_terminal_report_accepts_a_record_without_the_split():
    """A report saved by an older version has no by_path; rendering it must not fail."""
    rec = probe("host-a")
    del rec["first_alert"]["by_path"]
    assert _per_path_block(_rendered(rec).splitlines()) == ["n/a (record has no per-path split)"]


def test_a_baseline_saved_before_the_split_still_compares(tmp_path, capsys):
    base = tmp_path / "base.json"
    args = [f"a={FIXTURES / 'host-a.raw'}", "--baseline", str(base)]
    assert soak.main(args) == 0
    saved = json.loads(base.read_text())
    saved["hosts"]["a"]["first_alert"].pop("by_path")
    base.write_text(json.dumps(saved))
    capsys.readouterr()
    assert soak.main(args) == 0
    assert "vs baseline" not in capsys.readouterr().out


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


@pytest.mark.parametrize("message", [
    "PTZ stop guard Stop failed",
    "PTZ stop guard Stop failed name=cam error=TimeoutError",
    "PTZ stop guard could not verify tracking OFF",
    "PTZ stop guard episode cam: beyond_limit=True first_stop_ok=+0.10s off=verified+0.20s",
    "PTZ stop guard episode cam: beyond_limit=False first_stop_ok=none off=verified+0.20s",
    "PTZ stop guard episode cam: beyond_limit=False first_stop_ok=+0.10s off=unverified",
    "rotation stop cam: shutdown tracking OFF unconfirmed",
])
def test_stopguard_problems_fail_soak_even_at_warning_level(message):
    text = (FIXTURES / "host-a.raw").read_text().replace("@@journal", "@@journal\nWARNING " + message)
    rec = soak.analyze(text, 24)
    assert rec["stopguard_problems"] == 1
    assert soak.host_status(rec) == "ATTENTION"
    assert any("Stopguard problem" in f for f in rec["findings"])
    assert "Stopguard problem lines" in " ".join(soak.compare(probe("host-a"), rec))


def test_safe_stopguard_episode_is_not_a_soak_problem():
    text = (FIXTURES / "host-a.raw").read_text().replace("@@journal", "@@journal\nINFO "
        "PTZ stop guard episode cam: beyond_limit=False first_stop_ok=+0.10s off=verified+0.20s")
    rec = soak.analyze(text, 24)
    assert rec["stopguard_problems"] == 0
    assert rec["findings"] == []
    assert probe("host-denied")["stopguard_problems"] is None


def test_stopguard_episode_counts_all_retries_when_warning_is_deduplicated():
    text = (FIXTURES / "host-a.raw").read_text().replace(
        "@@journal",
        "@@journal\nWARNING PTZ stop guard Stop failed name=cam error=ONVIFError\n"
        "INFO PTZ stop guard episode cam: reason=pose_unavailable duration=73.20s "
        "beyond_limit=False reads_failed=13 stops=13 stops_failed=8 "
        "first_stop_ok=+1.98s off=verified+1.33s off_failed=0",
    )
    rec = soak.analyze(text, 24)

    assert rec["stopguard_problems"] == 1  # one deduplicated warning line
    assert rec["stopguard_episodes"] == 1
    assert rec["stopguard_reads_failed"] == 13
    assert rec["stopguard_stop_attempts"] == 13
    assert rec["stopguard_stop_failures"] == 8
    assert rec["stopguard_off_failures"] == 0
    assert any("8 Stop RPC failure(s)" in f for f in rec["findings"])
    assert any("Stopguard failed Stop RPCs" in d
               for d in soak.compare(probe("host-a"), rec))
    rec["deviations"] = []
    assert "8/13 Stop failures/attempts" in soak.render_host("host-a", rec)

def test_stopguard_report_distinguishes_observed_crossing_after_off_from_rpc_failures():
    journal = (
        "WARNING PTZ stop guard episode cam: reason=boundary duration=63s "
        "beyond_limit=True reads_failed=5 stops=7 stops_failed=4 "
        "first_stop_ok=+1.92s off=verified+1.46s off_failed=0 peak_at=+59.56s\n"
        "INFO PTZ stop guard episode cam: reason=boundary duration=32s "
        "beyond_limit=False reads_failed=0 stops=3 stops_failed=0 "
        "first_stop_ok=+0.12s off=verified+0.80s off_failed=0 peak_at=+0.18s"
    )
    text = (FIXTURES / "host-a.raw").read_text().replace("@@journal", "@@journal\n" + journal)
    rec = soak.analyze(text, 24)
    assert rec["stopguard_limit_crossings"] == 1
    assert rec["stopguard_crossings_after_off"] == 1
    assert rec["stopguard_peak_after_off"] == 1
    assert rec["stopguard_peak_timing_unknown"] == 0
    rec["deviations"] = []
    assert "1 observed limit crossing" in soak.render_host("host-a", rec)
    assert "1 crossing with peak after OFF" in soak.render_host("host-a", rec)
    assert "1 peak after verified OFF" in soak.render_host("host-a", rec)
    assert rec["stopguard_stop_failures"] == 4  # distinct from physical crossings

def test_stopguard_missing_timing_is_unknown_not_zero():
    text = (FIXTURES / "host-a.raw").read_text().replace(
        "@@journal", "@@journal\nINFO PTZ stop guard episode cam: "
        "beyond_limit=False reads_failed=0 stops=3 stops_failed=0 off_failed=0")
    rec = soak.analyze(text, 24)
    assert rec["stopguard_peak_after_off"] == 0
    assert rec["stopguard_peak_timing_unknown"] == 1
    assert probe("host-denied")["stopguard_limit_crossings"] is None
