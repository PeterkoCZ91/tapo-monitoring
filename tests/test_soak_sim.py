"""tools/soak_sim.py: a short accelerated soak passes, and a planted leak makes it fail."""

import gc
import importlib.util
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("soak_sim", ROOT / "tools" / "soak_sim.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

FAST = ["--days", "1", "--tick", "60", "--cameras", "2", "--samples", "40"]


def run_json(capsys, *extra):
    code = soak.main([*FAST, "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_one_simulated_day_runs_clean_and_exercises_the_features(capsys):
    code, out = run_json(capsys, "--features", "all", "--seed", "4")
    assert code == 0, out["findings"]
    assert out["findings"] == [] and out["exceptions"] == 0
    assert out["ticks"] == 1440 and out["alerts_sent"] > 0
    assert out["features"] == sorted(soak.FEATURES)
    assert out["metrics"]["threads_max"] <= 32


def test_same_seed_gives_the_same_story(capsys):
    _, a = run_json(capsys, "--seed", "9", "--features", "none")
    _, b = run_json(capsys, "--seed", "9", "--features", "none")
    assert (a["alerts_sent"], a["scorer_requests"], a["audit_actions"]) == (
        b["alerts_sent"], b["scorer_requests"], b["audit_actions"])


def test_a_planted_memory_leak_fails_the_run(capsys):
    code, out = run_json(capsys, "--inject", "leak", "--inject-rate", "100000",
                         "--max-rss-growth-mb", "10", "--features", "none")
    assert code == 1
    assert any(f.startswith("RSS grew") for f in out["findings"])


def test_memory_measurement_collects_unreachable_cycles_but_keeps_live_objects(tmp_path, monkeypatch):
    class Allocation:
        def __init__(self):
            self.cycle = self
            self.payload = bytearray(100_000)

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        garbage = Allocation()
        garbage_ref = weakref.ref(garbage)
        del garbage
        live = Allocation()
        live_ref = weakref.ref(live)

        def measured_rss():
            assert garbage_ref() is None
            assert live_ref() is live
            return 1.0

        monkeypatch.setattr(soak, "rss_mb", measured_rss)
        simulation = SimpleNamespace(state_files=[], tmp=tmp_path, count_files=lambda: {})
        assert soak.Soak.measure(simulation)["rss_mb"] == 1.0
    finally:
        if was_enabled:
            gc.enable()


def test_a_growing_state_file_fails_the_run(capsys):
    code, out = run_json(capsys, "--inject", "statefile", "--inject-rate", "4096",
                         "--features", "none", "--restarts-per-day", "0")
    assert code == 1
    assert any("runtime.json reached" in f for f in out["findings"])


def test_piling_up_threads_fail_the_run(capsys):
    code, out = run_json(capsys, "--inject", "threads", "--max-threads", "20",
                         "--features", "none")
    assert code == 1
    assert any("threads (limit 20)" in f for f in out["findings"])


def test_unknown_feature_is_a_usage_error(capsys):
    assert soak.main(["--features", "zones,bogus"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_evaluate_applies_every_limit():
    args = soak.make_parser().parse_args([])
    sample = {"rss_mb": 50.0, "threads": 5, "files": {n: 1024 for n in soak.STATE_FILES},
              "dirs": {"sent": 0, "review": 0}, "ledger": 0,
              "counts": {n: 0 for n in soak.ARCHIVES}}
    summary = {"days": 1, "samples": [dict(sample) for _ in range(20)],
               "tick_seconds": {"max": 9.0, "p99": 1, "mean": 0.1}, "exceptions": 2,
               "exception_samples": ["boom"], "shutdown_ok": False}
    findings, _ = soak.evaluate(summary, args)
    assert len(findings) == 3
    assert any("slowest tick" in f for f in findings)
    assert any("exception" in f for f in findings)
    assert any("shutdown" in f for f in findings)


# -- wrapper mode: daemon.tick, stall watchdog, status endpoint view ------------------

def test_wrapper_mode_goes_through_daemon_tick_and_the_stall_watchdog(capsys):
    code, out = run_json(capsys, "--wrapper", "--features", "none", "--seed", "4")
    assert code == 0, out["findings"]
    assert out["wrapper"] is True
    stall = out["stall"]
    assert stall["episodes"] >= 1 and stall["ticks"] > 0
    assert stall["alerts"] >= 1 and stall["recoveries"] >= 1
    assert out["status"]["snapshots"] > 100 and out["status"]["errors"] == 0
    assert out["exceptions"] == 0          # the injected tick errors are not findings


def test_without_the_wrapper_switch_the_loop_step_is_called_directly(capsys):
    _, out = run_json(capsys, "--features", "none", "--seed", "4")
    assert out["wrapper"] is False and out["stall"]["episodes"] == 0


def test_a_dead_stall_watchdog_fails_the_wrapper_run(capsys, monkeypatch):
    monkeypatch.setattr(soak.daemon, "stall_watchdog", lambda *a, **k: None)
    code, out = run_json(capsys, "--wrapper", "--features", "none", "--seed", "4")
    assert code == 1
    assert any("stall watchdog" in f for f in out["findings"])


def test_a_status_view_that_raises_fails_the_wrapper_run(capsys, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("view")
    monkeypatch.setattr(soak.statusd, "served_snapshot", boom)
    code, out = run_json(capsys, "--wrapper", "--features", "none")
    assert code == 1
    assert any("status snapshot" in f for f in out["findings"])


# -- file counts, retention and the leak injection -------------------------------------

def test_a_run_reports_file_counts_and_exercises_every_archive(capsys):
    _, out = run_json(capsys, "--features", "all", "--seed", "4", "--tick", "20")
    counts = out["metrics"]["files_final"]
    assert set(counts) == {"sent", "review", "panlimit", "tmp"}
    assert counts["sent"] > 0 and counts["panlimit"] > 0
    assert out["metrics"]["files_growth_per_day"].keys() == counts.keys()
    # every hub clip dir is removed by the pipeline; the harness counts only dirs that no
    # running job, uncollected result or retry-queued frame owns, so any left is a leak
    assert out["metrics"]["files_growth_per_day"]["tmp"] == 0 and counts["tmp"] == 0


def test_retention_deletes_files_whose_mtime_follows_the_fake_clock(capsys):
    # The sent log keeps two days; the harness stamps files with the simulated time, so
    # the daily maintenance (which compares mtime with the fake `now`) really prunes.
    code, out = run_json(capsys, "--days", "4", "--features", "none", "--seed", "4")
    assert code == 0, out["findings"]
    assert out["metrics"]["files_pruned"]["sent"] > 0


def test_a_planted_file_leak_fails_the_run(capsys):
    code, out = run_json(capsys, "--inject", "filesleak", "--features", "none",
                         "--max-files-growth-per-day", "200")
    assert code == 1
    assert any("files/day" in f and "tmp" in f for f in out["findings"])


def test_evaluate_applies_the_file_growth_limit():
    args = soak.make_parser().parse_args(["--max-files-growth-per-day", "10"])
    samples = []
    for i in range(20):
        samples.append({"rss_mb": 50.0, "threads": 5, "files": {n: 1 for n in soak.STATE_FILES},
                        "dirs": {"sent": 0, "review": 0}, "ledger": 0,
                        "counts": {"sent": 0, "review": 0, "panlimit": i * 100, "tmp": 0}})
    summary = {"days": 1, "samples": samples, "tick_seconds": {"max": 0, "p99": 0, "mean": 0},
               "exceptions": 0, "exception_samples": [], "shutdown_ok": True,
               "seen": {}, "oldest_age_days": {}}
    findings, _ = soak.evaluate(summary, args)
    assert any("panlimit" in f and "files/day" in f for f in findings)
