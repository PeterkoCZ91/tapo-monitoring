"""tools/soak_sim.py: a short accelerated soak passes, and a planted leak makes it fail."""

import importlib.util
import json
from pathlib import Path

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
              "dirs": {"sent": 0, "review": 0}, "ledger": 0}
    summary = {"days": 1, "samples": [dict(sample) for _ in range(20)],
               "tick_seconds": {"max": 9.0, "p99": 1, "mean": 0.1}, "exceptions": 2,
               "exception_samples": ["boom"], "shutdown_ok": False}
    findings, _ = soak.evaluate(summary, args)
    assert len(findings) == 3
    assert any("slowest tick" in f for f in findings)
    assert any("exception" in f for f in findings)
    assert any("shutdown" in f for f in findings)
