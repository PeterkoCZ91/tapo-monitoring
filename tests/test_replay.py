"""`tapo-monitor replay`: a recorded ledger window pushed through the production gates."""

import json
import os
from datetime import datetime

import pytest

from tapo_monitor import cli, daemon, ledger, replay
from tapo_monitor import config as cfg

T0 = 1_790_000_000.0  # arbitrary fixed epoch


def _app(*cameras, cooldown=120):
    return cfg.load_config_from_dict({"alerts": {"cooldown": cooldown}, "cameras": list(cameras)})


def _cam(name, host="192.0.2.10", **extra):
    return {"name": name, "host": host, **extra}


def _ev(camera, event_type, at, observed=None):
    return replay.ReplayEvent(camera=camera, event_type=event_type, event_at=at,
                              observed_at=at + 3 if observed is None else observed)


def _outcomes(decisions):
    return [(d.event.camera, d.event.event_type, d.outcome, d.reason) for d in decisions]


def _night(value):
    return lambda _ts: value


# ── gates ─────────────────────────────────────────────────────────────────────

def test_cooldown_suppresses_a_repeat_person_on_the_same_camera():
    app = _app(_cam("front"))
    decisions = replay.replay(app, [_ev("front", "person", T0), _ev("front", "person", T0 + 30),
                                    _ev("front", "person", T0 + 300)], is_night=_night(True))
    assert _outcomes(decisions) == [
        ("front", "person", "would_alert", None),
        ("front", "person", "suppressed", "cooldown"),
        ("front", "person", "would_alert", None),
    ]


def test_person_suppresses_motion_but_motion_never_eats_a_person():
    app = _app(_cam("front"))
    decisions = replay.replay(app, [_ev("front", "motion", T0), _ev("front", "person", T0 + 10),
                                    _ev("front", "motion", T0 + 20)], is_night=_night(True))
    assert [(d.outcome, d.reason) for d in decisions] == [
        ("would_alert", None), ("would_alert", None), ("suppressed", "cooldown"),
    ]


def test_cooldowns_are_per_camera():
    app = _app(_cam("front"), _cam("back", host="192.0.2.11"))
    decisions = replay.replay(app, [_ev("front", "person", T0), _ev("back", "person", T0 + 5)],
                              is_night=_night(True))
    assert [d.outcome for d in decisions] == ["would_alert", "would_alert"]


def test_events_are_replayed_in_observation_order():
    app = _app(_cam("front"))
    late = _ev("front", "person", T0 + 30, observed=T0 + 40)
    early = _ev("front", "person", T0, observed=T0 + 2)
    decisions = replay.replay(app, [late, early], is_night=_night(True))
    assert [d.event for d in decisions] == [early, late]
    assert decisions[1].reason == "cooldown"


def test_night_only_camera_is_muted_during_the_day():
    app = _app(_cam("front", night_only=True))
    events = [_ev("front", "person", T0), _ev("front", "person", T0 + 600)]
    decisions = replay.replay(app, events, is_night=lambda ts: ts > T0 + 300)
    assert [(d.outcome, d.reason, d.night) for d in decisions] == [
        ("suppressed", "night_only", False), ("would_alert", None, True),
    ]


def test_quiet_hours_mute_inside_the_clock_window():
    app = _app(_cam("front", quiet_hours="00:30-04:30"))
    inside = datetime(2026, 1, 1, 2, 0).timestamp()
    outside = datetime(2026, 1, 1, 12, 0).timestamp()
    decisions = replay.replay(app, [_ev("front", "person", inside),
                                    _ev("front", "person", outside)], is_night=_night(False))
    assert [(d.outcome, d.reason) for d in decisions] == [
        ("suppressed", "quiet_hours"), ("would_alert", None),
    ]


def test_scene_group_suppresses_the_second_camera_of_one_passage():
    group = {"group": "yard", "scene_window": 15}
    app = _app(_cam("front", coordinator=group), _cam("back", host="192.0.2.11", coordinator=group),
               _cam("side", host="192.0.2.12"))
    decisions = replay.replay(app, [_ev("front", "person", T0), _ev("back", "person", T0 + 5),
                                    _ev("side", "person", T0 + 6), _ev("back", "person", T0 + 60)],
                              is_night=_night(True))
    assert _outcomes(decisions) == [
        ("front", "person", "would_alert", None),
        ("back", "person", "suppressed", "scene_duplicate"),
        ("side", "person", "would_alert", None),
        ("back", "person", "would_alert", None),
    ]


def test_unknown_camera_and_disabled_source_are_reported_not_dropped():
    app = _app(_cam("front", detection={"sources": ["onvif"]}))
    decisions = replay.replay(app, [_ev("front", "person", T0), _ev("gone", "person", T0 + 1)],
                              is_night=_night(True))
    assert [(d.outcome, d.reason) for d in decisions] == [
        ("suppressed", "source_disabled"), ("suppressed", "unknown_camera"),
    ]


def test_replay_goes_through_the_daemon_gate_functions(monkeypatch):
    # The whole point is running production code, not a copy: patching the daemon's
    # mute gate must change what replay reports.
    monkeypatch.setattr(daemon, "camera_muted", lambda cfg, night, now: True)
    app = _app(_cam("front"))
    decisions = replay.replay(app, [_ev("front", "person", T0)], is_night=_night(True))
    assert decisions[0].outcome == "suppressed"


def test_summary_counts_outcomes_per_camera():
    app = _app(_cam("front"), _cam("back", host="192.0.2.11"))
    decisions = replay.replay(app, [_ev("front", "person", T0), _ev("front", "person", T0 + 5),
                                    _ev("back", "motion", T0 + 9)], is_night=_night(True))
    summary = replay.summarize(decisions)
    assert summary["front"] == {"events": 2, "would_alert": 1, "suppressed": {"cooldown": 1}}
    assert summary["back"] == {"events": 1, "would_alert": 1, "suppressed": {}}


def test_compare_lists_only_events_whose_outcome_differs():
    events = [_ev("front", "person", T0), _ev("front", "person", T0 + 30),
              _ev("front", "person", T0 + 300)]
    base = replay.replay(_app(_cam("front"), cooldown=120), events, is_night=_night(True))
    other = replay.replay(_app(_cam("front"), cooldown=10), events, is_night=_night(True))
    diff = replay.compare(base, other)
    assert len(diff) == 1
    left, right = diff[0]
    assert left.event == right.event == events[1]
    assert (left.reason, right.outcome) == ("cooldown", "would_alert")


# ── ledger input ──────────────────────────────────────────────────────────────

def _ledger(tmp_path):
    path = tmp_path / "events.sqlite3"
    events = ledger.EventLedger(path)
    events.record_camera_event(camera="front", event_type="person", event_at=T0,
                               observed_at=T0 + 2)
    events.record_camera_event(camera="front", event_type="person", event_at=T0 + 30,
                               observed_at=T0 + 33)
    events.record_camera_event(camera="back", event_type="motion", event_at=T0 + 50,
                               observed_at=T0 + 52)
    events.record_camera_event(camera="front", event_type="person", event_at=T0 + 9000,
                               observed_at=T0 + 9002)
    # Shadow evidence is not a camera event: the daemon never gated it.
    events.record_shadow_event(camera="front", event_type="person", event_at=T0 + 1,
                               confidence=0.9)
    events.record_decision(camera="front", event_type="person", event_at=T0 + 30,
                           path="live", action="cooldown")
    events.record_decision(camera="front", event_type="person", event_at=T0,
                           path="live", action="send", telegram=True)
    return path


def test_load_events_reads_camera_events_in_window_with_recorded_outcome(tmp_path):
    path = _ledger(tmp_path)
    events = replay.load_events(path, T0 - 1, T0 + 100)
    assert [(e.camera, e.event_type, e.event_at, e.observed_at, e.recorded) for e in events] == [
        ("front", "person", T0, T0 + 2, "send"),
        ("front", "person", T0 + 30, T0 + 33, "cooldown"),
        ("back", "motion", T0 + 50, T0 + 52, None),
    ]
    assert [e.camera for e in replay.load_events(path, T0 - 1, T0 + 100,
                                                 cameras=["back"])] == ["back"]


def test_load_events_is_read_only_and_never_creates_a_ledger(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    with pytest.raises(FileNotFoundError):
        replay.load_events(missing, 0, 1)
    assert not missing.exists()
    path = _ledger(tmp_path)
    before = os.stat(path).st_mtime_ns
    replay.load_events(path, 0, T0 * 2)
    assert os.stat(path).st_mtime_ns == before


# ── CLI ───────────────────────────────────────────────────────────────────────

def _config(tmp_path, name="cameras.yaml", cooldown=120):
    path = tmp_path / name
    path.write_text(
        f"alerts:\n  cooldown: {cooldown}\n"
        "cameras:\n"
        "  - name: front\n    host: 192.0.2.10\n"
        "  - name: back\n    host: 192.0.2.11\n"
    )
    return path


@pytest.fixture
def always_night(monkeypatch):
    monkeypatch.setattr(replay.scheduling, "is_night", lambda now=None, location=None: True)


def test_cli_prints_decisions_and_summary(tmp_path, capsys, always_night):
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                   "--start", str(T0 - 1), "--end", str(T0 + 100)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would_alert" in out and "cooldown" in out
    assert "front: 2 events, 1 would alert" in out


def test_cli_json_with_compare(tmp_path, capsys, always_night):
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                   "--start", str(T0 - 1), "--end", str(T0 + 100), "--json",
                   "--compare", str(_config(tmp_path, "other.yaml", cooldown=10))])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [d["outcome"] for d in report["decisions"]] == [
        "would_alert", "suppressed", "would_alert"]
    assert report["decisions"][1]["recorded"] == "cooldown"
    assert report["summary"]["front"]["suppressed"] == {"cooldown": 1}
    assert len(report["compare"]["differences"]) == 1
    difference = report["compare"]["differences"][0]
    assert difference["camera"] == "front" and difference["event_at"] == T0 + 30
    assert (difference["base"]["reason"], difference["other"]["outcome"]) == (
        "cooldown", "would_alert")


def test_cli_accepts_iso_times(tmp_path, capsys, always_night):
    start = datetime.fromtimestamp(T0 - 60).isoformat(timespec="seconds")
    end = datetime.fromtimestamp(T0 + 100).isoformat(timespec="seconds")
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                   "--start", start, "--end", end, "--json"])
    assert rc == 0
    assert len(json.loads(capsys.readouterr().out)["decisions"]) == 3


def test_cli_reports_a_missing_ledger(tmp_path, capsys):
    missing = tmp_path / "missing.sqlite3"
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(missing)])
    assert rc == 1
    assert "ledger" in capsys.readouterr().err
    assert not missing.exists()


def test_cli_rejects_an_inverted_window(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                  "--start", str(T0 + 10), "--end", str(T0)])
    assert exc.value.code == 2


def test_default_night_asks_the_daemon_scheduler_once_per_minute_and_warns_once(
        monkeypatch, capsys):
    import sys
    calls = []

    def fake_is_night(now=None, location=None):
        calls.append(now)
        print("[scheduling] astral failed", file=sys.stderr)
        return now.hour >= 22

    monkeypatch.setattr(replay.scheduling, "is_night", fake_is_night)
    monkeypatch.delenv("NIGHT_TZ", raising=False)  # keep the timestamps in host-local time
    night = replay.default_is_night(_app(_cam("front")))
    late = datetime(2026, 1, 1, 23, 0, 5).timestamp()
    assert night(late) is True and night(late + 10) is True
    assert night(datetime(2026, 1, 1, 12, 0).timestamp()) is False
    assert len(calls) == 2
    assert capsys.readouterr().err.count("astral failed") == 1
