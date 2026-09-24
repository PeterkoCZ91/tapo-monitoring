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
    assert summary["front"] == {"events": 2, "would_alert": 1, "suppressed": {"cooldown": 1},
                                "delivered": {}}
    assert summary["back"] == {"events": 1, "would_alert": 1, "suppressed": {}, "delivered": {}}


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


# ── recorded non-live deliveries ──────────────────────────────────────────────

def _delivery(camera, event_type, at, observed, path="sd"):
    return replay.ReplayEvent(camera=camera, event_type=event_type, event_at=at,
                              observed_at=observed, recorded="send", path=path)


def test_sd_delivery_arms_the_cooldown_a_later_live_event_sees():
    app = _app(_cam("front"), cooldown=120)
    # The live person was deferred in production; its SD follow-up went out at T0+90.
    # A new person at T0+150 is outside the wall-clock cooldown of the live event but
    # inside the one the SD delivery armed.
    events = [_delivery("front", "person", T0 + 10, T0 + 90),
              _ev("front", "person", T0 + 150, observed=T0 + 152)]
    decisions = replay.replay(app, events, is_night=_night(True))
    assert [(d.event.path, d.outcome, d.reason) for d in decisions] == [
        ("sd", "delivered", None), ("live", "suppressed", "cooldown")]
    # Without the recorded delivery the same live event would alert: the over-report.
    alone = replay.replay(app, events[1:], is_night=_night(True))
    assert alone[0].outcome == "would_alert"


def test_hubpoll_delivery_arms_by_event_start_too():
    app = _app(_cam("front"), cooldown=120)
    # Wall clock has moved on (observed 300 s later), but the camera event start of the
    # live event is within the cooldown of the hub clip's: the same passage.
    events = [_delivery("front", "person", T0, T0 + 5, path="hubpoll"),
              _ev("front", "person", T0 + 60, observed=T0 + 300)]
    decisions = replay.replay(app, events, is_night=_night(True))
    assert decisions[1].reason == "cooldown"


def test_sampler_delivery_records_its_scene_delivery_for_the_group():
    group = {"group": "yard", "scene_window": 15}
    app = _app(_cam("front", coordinator=group),
               _cam("back", host="192.0.2.11", coordinator=group))
    events = [_delivery("front", "motion", T0, T0 + 20, path="sampler"),
              _ev("back", "motion", T0 + 5, observed=T0 + 21),
              _ev("front", "motion", T0 + 400, observed=T0 + 60)]
    decisions = replay.replay(app, events, is_night=_night(True))
    assert [(d.event.camera, d.outcome, d.reason) for d in decisions] == [
        ("front", "delivered", None),
        ("back", "suppressed", "scene_duplicate"),
        ("front", "suppressed", "cooldown"),   # the sampler arms the wall-clock cooldown
    ]


def test_a_muted_delivery_arms_nothing_and_is_labelled():
    app = _app(_cam("front", night_only=True))
    events = [_delivery("front", "person", T0, T0 + 10),
              _ev("front", "person", T0 + 30, observed=T0 + 40)]
    decisions = replay.replay(app, events, is_night=lambda ts: ts > T0 + 20)
    assert [(d.outcome, d.reason) for d in decisions] == [
        ("suppressed", "night_only"), ("would_alert", None)]
    summary = replay.summarize(decisions)["front"]
    assert summary["events"] == 1 and summary["suppressed"] == {"sd:night_only": 1}


def test_deliveries_are_summarized_by_path_not_as_events():
    app = _app(_cam("front"))
    decisions = replay.replay(app, [_delivery("front", "person", T0, T0 + 60),
                                    _delivery("front", "motion", T0 + 500, T0 + 900,
                                              path="sampler")],
                              is_night=_night(True))
    assert replay.summarize(decisions)["front"] == {
        "events": 0, "would_alert": 0, "suppressed": {},
        "delivered": {"sampler": 1, "sd": 1}}


# ── threshold what-if ─────────────────────────────────────────────────────────

def _scored(camera, event_type, at, action, score, reason=None, observed=None):
    return replay.ReplayEvent(camera=camera, event_type=event_type, event_at=at,
                              observed_at=at + 3 if observed is None else observed,
                              recorded=action, score=score, recorded_reason=reason)


def _scorer_cam(name="front", threshold=0.5, **extra):
    return _cam(name, scorer={"url": "http://192.0.2.50:8766/score", "threshold": threshold},
                **extra)


def test_a_motion_send_under_a_raised_threshold_is_suppressed_and_arms_nothing():
    events = [_scored("front", "motion", T0, "send", 0.55),
              _scored("front", "motion", T0 + 30, "send", 0.9)]
    base = replay.replay(_app(_scorer_cam(threshold=0.5)), events, is_night=_night(True))
    raised = replay.replay(_app(_scorer_cam(threshold=0.6)), events, is_night=_night(True))
    assert [(d.outcome, d.reason) for d in base] == [
        ("would_alert", None), ("suppressed", "cooldown")]
    assert [(d.outcome, d.reason) for d in raised] == [
        ("suppressed", "threshold"), ("would_alert", None)]


def test_a_below_threshold_drop_is_suppressed_and_alerts_once_the_threshold_drops():
    events = [_scored("front", "motion", T0, "drop", 0.45, reason="below_threshold")]
    base = replay.replay(_app(_scorer_cam(threshold=0.5)), events, is_night=_night(True))
    lowered = replay.replay(_app(_scorer_cam(threshold=0.4)), events, is_night=_night(True))
    assert (base[0].outcome, base[0].reason) == ("suppressed", "threshold")
    assert (lowered[0].outcome, lowered[0].reason) == ("would_alert", None)


def test_a_person_under_the_threshold_defers_to_sd_and_still_arms_the_cooldown():
    events = [_scored("front", "person", T0, "send", 0.55),
              _scored("front", "person", T0 + 30, "send", 0.9)]
    app = _app(_scorer_cam(threshold=0.6, sd_snapshot=True))
    decisions = replay.replay(app, events, is_night=_night(True))
    assert [(d.outcome, d.reason) for d in decisions] == [
        ("suppressed", "threshold_defer"), ("suppressed", "cooldown")]
    # Without an SD path the live path's always-send safety net still sends it.
    net = replay.replay(_app(_scorer_cam(threshold=0.6)), events, is_night=_night(True))
    assert net[0].outcome == "would_alert"


def test_threshold_leaves_unscored_unrelated_and_scorerless_events_alone():
    events = [_ev("front", "motion", T0),                                   # no score
              _scored("front", "motion", T0 + 300, "hold", 0.2,
                      reason="awaiting_corroboration"),                   # not a threshold call
              _scored("front", "tamper", T0 + 310, "send", 0.0),           # never scorer-gated
              _scored("front", "person", T0 + 600, "drop", 0.1,
                      reason="burst_already_sent")]                       # another reason
    decisions = replay.replay(_app(_scorer_cam(threshold=0.9)), events, is_night=_night(True))
    assert [d.outcome for d in decisions] == ["would_alert"] * 4
    # A config without a scorer does not threshold anything.
    scored = [_scored("front", "motion", T0, "send", 0.1)]
    assert replay.replay(_app(_cam("front")), scored,
                         is_night=_night(True))[0].outcome == "would_alert"


def _night_cam(threshold=0.5, night_threshold=0.3, **extra):
    return _cam("front", scorer={"url": "http://192.0.2.50:8766/score",
                                 "threshold": threshold, "night_threshold": night_threshold},
                **extra)


def test_night_threshold_applies_to_events_handled_during_the_cameras_night():
    events = [_scored("front", "motion", T0, "drop", 0.4, reason="below_threshold")]
    night = replay.replay(_app(_night_cam()), events, is_night=_night(True))
    day = replay.replay(_app(_night_cam()), events, is_night=_night(False))
    assert (night[0].outcome, night[0].reason, night[0].night) == ("would_alert", None, True)
    assert (day[0].outcome, day[0].reason, day[0].night) == ("suppressed", "threshold", False)
    # The camera's schedule decides, as for its IR plan: always_night by day too.
    always = replay.replay(_app(_night_cam(schedule="always_night")), events,
                           is_night=_night(False))
    assert always[0].outcome == "would_alert"


def test_night_threshold_follows_the_time_the_daemon_handled_the_event():
    # Started in daylight, handled after dusk: the live tick that scored it was a night
    # tick, so replay asks about observed_at, never event_at.
    events = [_scored("front", "motion", T0, "send", 0.4, observed=T0 + 60)]
    dusk = replay.replay(_app(_night_cam()), events, is_night=lambda ts: ts >= T0 + 30)
    assert (dusk[0].outcome, dusk[0].night) == ("would_alert", True)


def test_compare_moves_only_night_events_for_a_night_threshold_change():
    events = [_scored("front", "motion", T0, "drop", 0.4, reason="below_threshold"),
              _scored("front", "motion", T0 + 600, "drop", 0.4, reason="below_threshold")]
    is_night = lambda ts: ts >= T0 + 300  # noqa: E731 - day, then night
    base = replay.replay(_app(_scorer_cam(threshold=0.5)), events, is_night=is_night)
    other = replay.replay(_app(_night_cam()), events, is_night=is_night)
    differences = replay.compare(base, other)
    assert [(b.event.event_at, b.reason, o.outcome) for b, o in differences] == [
        (T0 + 600, "threshold", "would_alert")]


def test_scene_reach_counts_alerts_the_gate_removed():
    group = {"group": "yard", "scene_window": 15}
    app = _app(_cam("front", coordinator=group),
               _cam("back", host="192.0.2.11", coordinator=group))
    events = [_ev("front", "person", T0), _ev("back", "person", T0 + 5),
              _ev("back", "person", T0 + 400)]
    reach = replay.scene_reach(app, events, is_night=_night(True))
    assert reach == {"back": {"without_gate": 2, "with_gate": 1, "removed": 1},
                     "front": {"without_gate": 1, "with_gate": 1, "removed": 0}}


# ── ledger input: non-live rows and scores ────────────────────────────────────

def _ledger_with_paths(tmp_path):
    path = tmp_path / "paths.sqlite3"
    events = ledger.EventLedger(path)
    events.record_camera_event(camera="front", event_type="motion", event_at=T0,
                               observed_at=T0 + 2)
    events.record_camera_event(camera="front", event_type="person", event_at=T0 + 150,
                               observed_at=T0 + 152)
    events.record_decision(camera="front", event_type="motion", event_at=T0, path="live",
                           action="drop", observed_at=T0 + 3, score=0.45, threshold=0.5,
                           reason="below_threshold")
    events.record_decision(camera="front", event_type="person", event_at=T0 + 10, path="sd",
                           action="send", observed_at=T0 + 90, score=0.8, threshold=0.5,
                           telegram=True)
    # Not deliveries: a failed send, an SD drop and a sampler cooldown.
    events.record_decision(camera="front", event_type="person", event_at=T0 + 11, path="sd",
                           action="send", observed_at=T0 + 95, telegram=False)
    events.record_decision(camera="front", event_type="motion", event_at=T0 + 12, path="sd",
                           action="drop", observed_at=T0 + 96, score=0.1)
    events.record_decision(camera="front", event_type="motion", event_at=T0 + 13,
                           path="sampler", action="cooldown", observed_at=T0 + 97)
    return path


def test_load_events_adds_recorded_deliveries_and_live_scores(tmp_path):
    events = replay.load_events(_ledger_with_paths(tmp_path), T0 - 1, T0 + 200)
    assert [(e.path, e.event_type, e.event_at, e.observed_at, e.recorded, e.score,
             e.recorded_reason) for e in events] == [
        ("live", "motion", T0, T0 + 2, "drop", 0.45, "below_threshold"),
        ("live", "person", T0 + 150, T0 + 152, None, None, None),
        ("sd", "person", T0 + 10, T0 + 90, "send", 0.8, None),
    ]


def test_read_camera_window_still_defaults_to_live_decisions(tmp_path):
    path = _ledger_with_paths(tmp_path)
    _, decisions = ledger.read_camera_window(path, start=T0 - 1, end=T0 + 200)
    assert [d["path"] for d in decisions] == ["live"]
    _, none = ledger.read_camera_window(path, start=T0 - 1, end=T0 + 200, decision_paths=())
    assert none == []


def _scorer_config(tmp_path, name, threshold):
    path = tmp_path / name
    path.write_text(
        "alerts:\n  cooldown: 120\n"
        "cameras:\n"
        "  - name: front\n    host: 192.0.2.10\n"
        f"    scorer:\n      url: http://192.0.2.50:8766/score\n      threshold: {threshold}\n"
    )
    return path


def test_cli_labels_deliveries_and_compares_a_threshold(tmp_path, capsys, always_night):
    rc = cli.main(["replay", str(_scorer_config(tmp_path, "a.yaml", 0.5)),
                   "--ledger", str(_ledger_with_paths(tmp_path)), "--start", str(T0 - 1),
                   "--end", str(T0 + 200),
                   "--compare", str(_scorer_config(tmp_path, "b.yaml", 0.4))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "delivered[sd]" in out
    assert "suppressed(threshold)" in out and "score=0.45" in out
    assert "suppressed(cooldown)" in out   # the person after the SD delivery
    assert "recorded non-live deliveries: sd=1" in out
    assert "differences vs" in out and "suppressed(threshold) -> would_alert" in out


def test_cli_compares_a_night_threshold(tmp_path, capsys, always_night):
    other = tmp_path / "night.yaml"
    other.write_text(_scorer_config(tmp_path, "day.yaml", 0.5).read_text()
                     + "      night_threshold: 0.4\n")
    rc = cli.main(["replay", str(tmp_path / "day.yaml"),
                   "--ledger", str(_ledger_with_paths(tmp_path)), "--start", str(T0 - 1),
                   "--end", str(T0 + 200), "--json", "--compare", str(other)])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    [difference] = report["compare"]["differences"]
    assert (difference["base"]["reason"], difference["other"]["outcome"]) == (
        "threshold", "would_alert")


def test_cli_summary_only_and_scene_reach(tmp_path, capsys, always_night):
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                   "--start", str(T0 - 1), "--end", str(T0 + 100), "--summary-only",
                   "--scene-reach"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would_alert" not in out.split("back:")[0]   # no per-event lines
    assert "front: 2 events, 1 would alert" in out
    assert "total: 3 events" in out
    assert "scene gate reach" in out and "total removed: 0" in out
    other = _config(tmp_path, "other.yaml", cooldown=10)
    rc = cli.main(["replay", str(_config(tmp_path)), "--ledger", str(_ledger(tmp_path)),
                   "--start", str(T0 - 1), "--end", str(T0 + 100), "--summary-only",
                   "--scene-reach", "--json", "--compare", str(other)])
    report = json.loads(capsys.readouterr().out)
    assert "decisions" not in report
    assert report["compare"] == {"config": str(other), "count": 1}
    assert report["scene_reach"]["front"] == {"without_gate": 1, "with_gate": 1, "removed": 0}
