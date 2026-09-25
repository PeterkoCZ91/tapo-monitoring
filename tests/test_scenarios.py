"""Multi-tick stories through the real ``daemon.loop_step`` (see ``tests/scenario.py``).

Each test states the world tick by tick and asserts on the ORDER of what the daemon did —
which motor command, which delivery, which transition — not just on the final state.
Ticks are 5 s apart and the control pass runs every 60 s unless a story says otherwise;
times in comments are seconds since the scenario started.
"""


import functools
import logging
import os
import threading
import time

from tapo_monitor import daemon, sdworker, twin
from tests.scenario import (
    START,
    Scenario,
    camera_dict,
    collapse,
    dual_lens,
    motion,
    pan_limit,
    person,
)

HOST_A = "192.0.2.10"
OUT_OF_SPAN = 0.63          # past the right-most preset (0.61) plus margin


def at(seconds):
    return START + seconds


def tracking_camera(**overrides):
    """A night-tracking camera with a 3-minute dwell, a night preset and the pan guard."""
    base = dict(tracking={"track_hold": 180, "back_time": 180,
                          "day_preset": "2", "night_preset": "2"},
                pan_limit=pan_limit())
    base.update(overrides)
    return camera_dict("a", HOST_A, **base)


# ── dwell vs pan guard ────────────────────────────────────────────────────────

def test_hold_keeps_the_subject_until_the_guard_grace_runs_out(monkeypatch, tmp_path):
    # Auto-track follows a person past the right-hand preset. The dwell holds the
    # scheduled recall; the guard waits pan_limit.hold_grace (20 s) of continuous
    # out-of-bounds, then pulls the lens back to the nearest bound. Once nobody has been
    # seen for track_hold (180 s) the night preset comes back on the next control pass.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()])
    cam = sc.cams["a"]
    sc.run(30)                                   # 0..25: quiet night, lens home
    cam.push(person(at(30)))
    cam.pan_x = OUT_OF_SPAN                      # auto-track swings after the subject
    sc.run(250)                                  # 30..275

    assert collapse(sc.actions("recall", "goto", "send", "autotrack")) == [
        ("recall", "a", "2"),                    # 0: first control pass
        ("autotrack", "a", True),                # asserted last, after the recall
        ("send", "a"),                           # 30: the person
        ("goto", "a", "3"),                      # 50: guard, after the grace
        ("recall", "a", "2"),                    # 240: hold over, night preset again
    ]
    assert sc.when(("goto", "a", "3")) == [at(50)]          # 30 + hold_grace
    assert sc.when(("recall", "a", "2")) == [at(0), at(240)]
    refusals = sc.state.motion_refusals["a"]
    assert refusals["schedule:hold"] == 3                   # control passes 60, 120, 180
    assert refusals["pan_limit:hold"] == 2                  # guard polls 30, 40


def test_a_subject_that_never_leaves_still_gets_the_preset_back(monkeypatch, tmp_path):
    # A through-location fires events all evening. Every event re-arms the dwell, but one
    # unbroken hold is capped at track_hold: the recall is the only thing on this fleet
    # that corrects tilt, so it must get through once per stretch.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  alerts={"cooldown": 1})
    cam = sc.cams["a"]
    sc.run(30)
    for _ in range(16):                          # 30..505: an event every 30 s
        cam.push(person(sc.clock.now))
        sc.run(30)

    assert sc.when(("recall", "a", "2")) == [at(0), at(240), at(480)]
    assert len(sc.actions("send")) == 16         # the dwell never costs an alert


# ── privacy mode ─────────────────────────────────────────────────────────────

def test_a_lens_parked_by_privacy_gets_no_motor_calls_once_a_control_pass_saw_it(
        monkeypatch, tmp_path):
    # Privacy mode parks the lens (here outside the pan span) and answers every motor call
    # with MOTOR_BUSY. Between control passes the guard still tries — and its refusals are
    # counted, not treated as ONVIF failures; from the control pass that reads the switch
    # on, neither path sends a thing. When it goes off again, the first control pass
    # restores the aim, and the twin keeps reporting the state on its own probes.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  observability={"digital_twin": True, "probe_interval": 60})
    cam = sc.cams["a"]
    sc.run(90)                                   # 0..85
    cam.privacy = True                           # someone flips it in the app
    cam.pan_x = 0.95
    sc.run(210)                                  # 90..295
    assert sc.state.twin_fleet["a"]["actual"]["privacy.enabled"] is True
    cam.privacy = False
    sc.run(90)                                   # 300..385

    stale = [a for t, a in sc.timeline if at(90) <= t < at(120)]
    assert stale == [("goto", "a", "3")] * 3     # 90, 100, 110: no control pass yet
    assert sc.when(("recall", "a", "2")) == [at(0), at(60), at(300), at(360)]
    parked = [a for t, a in sc.timeline if at(120) <= t < at(300)
              and a[0] in ("recall", "goto")]
    assert parked == []                          # control pass read it at 120: hands off
    assert sc.state.motion_refusals["a"]["schedule:privacy"] == 3     # 120, 180, 240
    assert collapse(sc.motor(since=at(300))) == [
        ("recall", "a", "2"),                    # 300: the control pass reads "off"
    ]
    assert sc.state.twin_fleet["a"]["actual"]["privacy.enabled"] is False
    assert cam.pan_x == 0.50


def test_firmware_without_a_privacy_read_never_blocks_the_recall(monkeypatch, tmp_path):
    # Older firmware has no getPrivacyMode and refuses every auto-track call. An unread
    # privacy state must never be taken for "parked" — a camera nobody could read still
    # needs its aim repaired — and a refused auto-track must not stop the loop.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  observability={"digital_twin": True, "probe_interval": 60})
    cam = sc.cams["a"]
    cam.getPrivacyMode = None                    # firmware without the call
    cam.refuse_autotrack = True
    sc.run(180)

    assert sc.when(("recall", "a", "2")) == [at(0), at(60), at(120)]
    assert sc.actions("autotrack") == []
    assert sc.state.twin_fleet["a"]["actual"]["privacy.enabled"] == "unsupported"
    assert twin.cameras_in_privacy(sc.state.twin_fleet) == set()
    assert "a" not in sc.state.motion_refusals


def test_the_control_pass_reads_privacy_itself_on_the_pass_it_goes_on(monkeypatch, tmp_path):
    # The twin probes every 15 minutes (the default); privacy goes on just before a
    # control pass. That pass reads the switch on its own connected client, so it sends
    # neither the recall nor its retry, and the guard in the same tick keeps its hands off.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  observability={"digital_twin": True})
    cam = sc.cams["a"]
    sc.run(60)                                   # 0..55: twin read "off" at 0
    cam.privacy = True
    cam.pan_x = 0.95                             # parked outside the span
    sc.run(130)                                  # 60..185

    assert sc.when(("recall", "a", "2")) == [at(0)]
    assert sc.motor(since=at(60)) == []
    assert sc.state.motion_refusals["a"]["schedule:privacy"] == 3     # 60, 120, 180
    assert sc.state.twin_fleet["a"]["actual"]["privacy.enabled"] is False  # not probed yet


def test_aim_comes_back_on_the_first_control_pass_after_privacy_goes_off(
        monkeypatch, tmp_path):
    # Privacy on at 60, off at 190. The twin would not look again until 900; the control
    # pass at 240 reads "off" itself and restores the aim, and the guard (same tick, after
    # the recall) finds the lens back inside its span.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  observability={"digital_twin": True})
    cam = sc.cams["a"]
    sc.run(60)
    cam.privacy = True
    cam.pan_x = 0.95
    sc.run(130)                                  # 60..185
    cam.privacy = False
    sc.run(60)                                   # 190..245

    assert sc.motor(since=at(190)) == [("recall", "a", "2")]
    assert sc.when(("recall", "a", "2")) == [at(0), at(240)]
    assert cam.pan_x == 0.50


def test_a_failed_privacy_read_falls_back_to_the_twin(monkeypatch, tmp_path):
    # The quick read failing must never be taken for "parked": with the twin saying off,
    # the recall still goes out every pass.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  observability={"digital_twin": True})
    cam = sc.cams["a"]

    def broken():
        raise Exception("-40401 read refused")
    cam.getPrivacyMode = broken
    sc.run(180)

    assert sc.when(("recall", "a", "2")) == [at(0), at(60), at(120)]
    assert "a" not in sc.state.motion_refusals


def test_a_guard_goto_refused_by_a_parked_lens_keeps_the_onvif_client(monkeypatch, tmp_path):
    # Privacy goes on between control passes, so the guard still tries its GotoPreset and
    # the parked lens answers MOTOR_BUSY. That is a refusal, not a transport failure: it is
    # counted as one and the ONVIF client is kept, not rebuilt every poll. From the next
    # control pass on the guard does not even try; after privacy goes off it restores the
    # aim on the next control pass.
    builds = []
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  observability={"digital_twin": True})
    build = sc._build_ptz

    def counting_build(*a, **k):
        builds.append(sc.clock.now)
        return build(*a, **k)
    monkeypatch.setattr("tapo_monitor.daemon.panlimit.build_ptz", counting_build)
    cam = sc.cams["a"]
    sc.run(90)                                   # 0..85
    cam.privacy = True
    cam.pan_x = 0.95
    sc.run(90)                                   # 90..175
    cam.privacy = False
    sc.run(20)                                   # 180..195

    assert builds == [at(0)]
    assert sc.when(("goto", "a", "3")) == [at(90), at(100), at(110)]
    # 90..110 refused by the lens, 120..170 skipped because the control pass read it
    assert sc.state.motion_refusals["a"]["pan_limit:privacy"] == 3 + 6
    assert sc.motor(since=at(180)) == [("recall", "a", "2")]
    assert cam.pan_x == 0.50


# ── connectivity ─────────────────────────────────────────────────────────────

def test_camera_dropping_right_after_an_alert_does_not_stop_the_loop(monkeypatch, tmp_path):
    # The camera vanishes seconds after a person alert: getEvents, ONVIF, ping and login
    # all fail. Every pass keeps running (loop_step is called directly, so any exception
    # would fail this test), one outage alert goes out, one recovery notice, and the first
    # control pass after it returns re-aims the lens and detection resumes.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  alerts={"outage_threshold": 120, "event_failure_threshold": 600,
                          "event_restart_threshold": 900})
    cam = sc.cams["a"]
    sc.run(10)
    cam.push(person(at(10)))
    sc.tick(advance=5)                           # 10: alert delivered
    cam.online = False                           # 15: gone
    sc.run(385)                                  # 15..395
    cam.online = True
    sc.run(40)                                   # 400..435
    cam.push(person(at(440)))
    sc.run(20)

    sends_and_texts = sc.actions("send", "text")
    assert [a[0] for a in sends_and_texts] == ["send", "text", "text", "send"]
    assert "unreachable" in sends_and_texts[1][1]            # 180: 120 s after 60
    assert "back online" in sends_and_texts[2][1]            # 420: first pass it answers
    assert sc.when(sends_and_texts[1]) == [at(180)]
    assert sc.when(("recall", "a", "2"))[-1] == at(420)      # re-aimed on reconnect
    assert sc.state.network_reachable["a"] is True
    assert sc.state.events_reachable["a"] is True


def test_rtsp_down_while_the_api_answers(monkeypatch, tmp_path):
    # Events keep arriving but every frame grab fails. With no SD path the person cannot
    # be shown; what must not happen is an outage alert (the camera is up) or the failed
    # grab eating the cooldown, which would silence the next person too.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})])
    cam = sc.cams["a"]
    cam.rtsp_ok = False
    sc.run(10)
    cam.push(person(at(10)))
    sc.tick(advance=5)
    assert sc.state.rtsp_reachable["a"] is False
    assert sc.state.events_reachable["a"] is True
    cam.rtsp_ok = True
    cam.push(person(at(25)))                     # same passage, 15 s later
    sc.run(200)

    assert sc.actions("send", "send_failed", "text") == [("send", "a")]
    assert sc.when(("send", "a")) == [at(15)]
    assert sc.state.rtsp_reachable["a"] is True


# ── delivery ─────────────────────────────────────────────────────────────────

def test_duplicate_person_events_of_one_passage_send_once(monkeypatch, tmp_path):
    # The camera reports one passage as several person events seconds apart, spread over
    # two polls, and re-delivers one it already reported. One photo; a new passage after
    # the cooldown is a new alert.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  alerts={"cooldown": 120})
    cam = sc.cams["a"]
    sc.run(10)
    cam.push(person(at(8)), person(at(10)))
    sc.tick(advance=5)                           # 10
    cam.push(person(at(10)), person(at(14)))     # a repeat and one more
    sc.run(185)                                  # 15..195
    cam.push(person(at(200)))
    sc.run(10)                                   # 200: a new passage

    assert sc.when(("send", "a")) == [at(10), at(200)]


def test_telegram_down_does_not_consume_the_cooldown(monkeypatch, tmp_path):
    # A refused delivery must not arm the cooldown: the next person of the same passage,
    # seconds later, is the one that gets through once Telegram answers again.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  alerts={"cooldown": 120})
    cam = sc.cams["a"]
    sc.run(10)
    sc.notifier.down = True
    cam.push(person(at(10)))
    sc.tick(advance=5)                           # 10: refused
    assert sc.state.last_alert == {}
    sc.notifier.down = False
    sc.run(10)                                   # 15, 20: quiet
    cam.push(person(at(25)))
    sc.run(60)                                   # 25: delivered

    assert sc.actions("send", "send_failed") == [("send_failed", "a"), ("send", "a")]
    assert sc.when(("send", "a")) == [at(25)]
    assert sc.state.last_alert[("a", "confirmed")] == at(25)


def test_restart_inside_the_cooldown_does_not_resend(monkeypatch, tmp_path):
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})],
                  alerts={"cooldown": 120})
    cam = sc.cams["a"]
    sc.run(10)
    cam.push(person(at(10)))
    sc.run(20)                                   # 10: delivered
    sc.restart()                                 # 30: a deploy restarts the daemon
    cam.push(person(at(30)))                     # same passage, 20 s after the alert
    sc.run(30)

    assert sc.when(("send", "a")) == [at(10)]


def test_a_score_between_the_night_and_day_thresholds_alerts_only_at_night(
        monkeypatch, tmp_path):
    # scorer.night_threshold: an IR scene scoring 0.4 is a person at night (night value
    # 0.3) but not by day (0.5). The same bare-motion frame is dropped by day and sent
    # once the night starts; the ledger-facing audit records the threshold applied.
    scorer = {"url": "http://scorer.invalid/score", "threshold": 0.5,
              "night_threshold": 0.3}
    sc = Scenario(monkeypatch, tmp_path, [camera_dict("a", HOST_A, scorer=scorer)],
                  alerts={"cooldown": 60})
    monkeypatch.setattr("tapo_monitor.daemon.scorer.score_image",
                        lambda *a, **k: {"person": 0.4, "animal": 0.0})
    thresholds = []
    monkeypatch.setattr("tapo_monitor.monitor.audit_event",
                        lambda cfg, event, etype, path, action, threshold=None, **k:
                        thresholds.append((path, action, threshold)))
    cam = sc.cams["a"]
    sc.night = False
    sc.run(10)
    cam.push(motion(at(10)))
    sc.run(90)                                   # 10: day, dropped
    assert sc.actions("send") == []
    sc.night = True
    cam.push(motion(at(100)))
    sc.run(30)                                   # 100: night, sent

    assert sc.when(("send", "a")) == [at(100)]
    assert [t for t in thresholds if t[0] == "live"] == [("live", "drop", 0.5),
                                                          ("live", "send", 0.3)]


# ── weather ──────────────────────────────────────────────────────────────────

def test_rain_parks_a_tracking_camera_on_its_day_preset_despite_a_hold(
        monkeypatch, tmp_path):
    # disable_tracking: rain at night turns auto-track off and parks the lens on the day
    # preset. A dwell that was holding a subject must not keep it off its park — the hold
    # only counts while the plan tracks. When the rain stops, tracking resumes.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(
        pan_limit={"enabled": False},
        tracking={"track_hold": 180, "back_time": 180, "day_preset": "2",
                  "night_preset": "1"},
        weather={"strategy": "disable_tracking"})])
    cam = sc.cams["a"]
    sc.run(30)
    cam.push(person(at(30)))
    cam.pan_x = 0.55                             # following the subject
    sc.run(20)                                   # 30..45
    sc.raining = True                            # 50
    sc.run(180)                                  # 50..225
    sc.raining = False
    sc.run(30)                                   # 230..255

    assert collapse(sc.actions("recall", "send")) == [
        ("recall", "a", "1"),                    # 0: night preset
        ("send", "a"),                           # 30
        ("recall", "a", "2"),                    # 60: rain -> park, hold or not
        ("recall", "a", "1"),                    # 240: dry again
    ]
    assert sc.actions("autotrack") == [
        ("autotrack", "a", True), ("autotrack", "a", False), ("autotrack", "a", True)]
    assert sc.when(("autotrack", "a", False)) == [at(60)]
    assert sc.when(("recall", "a", "2")) == [at(60), at(120), at(180)]
    assert "schedule:hold" not in sc.state.motion_refusals.get("a", {})
    assert sc.state.desired_plans["a"].rain_parked is False


# ── one outage, one notice ───────────────────────────────────────────────────

def _texts(sc):
    return [(t - START, a[1]) for t, a in sc.timeline if a[0] == "text"]


def _record_reboots(monkeypatch, sc, cam):
    def reboot():
        sc._record(("reboot", cam.name))
        cam._require_online()
    monkeypatch.setattr(cam, "reboot", reboot)


def test_offline_camera_with_default_thresholds_sends_one_notice_each_way(
        monkeypatch, tmp_path):
    # A camera drops off the network for 20 minutes with the default thresholds (outage
    # 900 s, event API 300 s, event restart 900 s). Without a client getEvents cannot
    # succeed, so the event-API watchdog used to fire too: "event API unavailable" at
    # 360 s (before the outage alert itself), a reboot of the camera the moment it came
    # back, and "event API restored after 0s". One outage is one 🔴 and one 🟢.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})])
    cam = sc.cams["a"]
    _record_reboots(monkeypatch, sc, cam)
    sc.run(30)
    cam.online = False
    sc.run(1200)                                 # 30..1225
    cam.online = True
    sc.run(600)                                  # 1230..1825: events healthy again

    assert _texts(sc) == [
        (960.0, "🔴 camera 'a' unreachable after 1m observed uptime"),
        (1260.0, "🟢 camera 'a' back online after 20m outage"),
    ]
    assert sc.actions("reboot") == []
    assert not sc.state.event_alerted.get("a")
    assert "a" not in sc.state.event_fail_since
    assert sc.state.events_reachable["a"] is True


def test_event_api_failure_on_a_reachable_camera_still_alerts(monkeypatch, tmp_path):
    # The camera answers ping and login but getEvents fails: that is the event watchdog's
    # own job and the network stand-down must not hide it. Alert at the threshold, one
    # restart at the restart threshold, restored with the real duration once it answers.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})])
    cam = sc.cams["a"]
    _record_reboots(monkeypatch, sc, cam)
    sc.run(30)
    cam.events_error = TimeoutError("getEvents timed out")
    sc.run(1000)                                 # 30..1025
    cam.events_error = None
    sc.run(200)

    texts = _texts(sc)
    assert [m.split(" for camera")[0] for _, m in texts] == [
        "event API unavailable", "event API restart requested", "event API restored"]
    assert texts[0][0] == 360.0                  # first watchdog pass >= 300 s after 30
    assert "TimeoutError" in texts[0][1]
    assert sc.when(("reboot", "a")) == [at(960)]
    assert texts[2][0] == 1080.0
    assert texts[2][1].endswith("after 17m 30s")  # 30 -> 1080, not "after 0s"
    assert not any("unreachable" in m for _, m in texts)


def test_event_api_still_broken_after_an_outage_alerts_only_after_its_threshold(
        monkeypatch, tmp_path):
    # The camera comes back on the network but its event endpoint stays broken. The event
    # clock starts when the camera is reachable again, so the event alert comes a full
    # event_failure_threshold after the return, not the instant it answers ping.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera(pan_limit={"enabled": False})])
    cam = sc.cams["a"]
    _record_reboots(monkeypatch, sc, cam)
    sc.run(30)
    cam.online = False
    cam.events_error = TimeoutError("getEvents timed out")
    sc.run(1200)                                 # 30..1225
    cam.online = True
    sc.run(400)                                  # 1230..1625

    texts = _texts(sc)
    assert [t for t, _ in texts] == [960.0, 1260.0, 1560.0]
    assert "unreachable" in texts[0][1]
    assert "back online" in texts[1][1]
    assert texts[2][1].startswith("event API unavailable for camera a after 5m")
    assert sc.actions("reboot") == []            # restart clock restarted too


# ── corroboration hold expiry ────────────────────────────────────────────────

def _held_camera(policy, pan=None, coordinator=None, **sampler):
    """Bare motion scoring 0.45 is held (threshold 0.3, send line 0.6); the sampler takes
    two follow-up frames 10 s apart (20, 30), after which the exhausted group closes
    group_gap (20 s) past its last event — on the tick at 35."""
    extra = {} if pan is None else {"pan_limit": pan}
    if coordinator is not None:
        extra["coordinator"] = coordinator
    return camera_dict("a", HOST_A,
                       scorer={"url": "http://scorer.invalid/score", "threshold": 0.3,
                               "motion_send_threshold": 0.6},
                       sampler={"enabled": True, "interval": 10, "max_frames": 2,
                                "group_gap": 20, "hold_expiry": policy, **sampler},
                       **extra)


def _hold_story(monkeypatch, tmp_path, policy, *, review_log=True, pan=None, swing=False,
                coordinator=None, **sampler):
    """The live frame of a motion at 10 scores 0.45 and is held; every follow-up frame
    scores 0.1, so no corroboration ever comes. ``swing`` puts the lens past its span as
    the motion arrives. Returns the scenario, run up to 15, and its audit list of
    ``(path, action, reason, expiry)``."""
    sc = Scenario(monkeypatch, tmp_path, [_held_camera(policy, pan=pan, coordinator=coordinator, **sampler)],
                  alerts={"cooldown": 120})
    if review_log:
        monkeypatch.setenv("TAPO_REVIEW_LOG_DIR", str(tmp_path / "review"))
    calls = []

    def score(*_a, **_k):
        calls.append(1)
        return {"person": 0.45 if len(calls) == 1 else 0.1, "animal": 0.0}

    monkeypatch.setattr("tapo_monitor.daemon.scorer.score_image", score)
    audits = []
    monkeypatch.setattr(
        "tapo_monitor.monitor.audit_event",
        lambda cfg, event, etype, path, action, reason=None, extra=None, **k:
        audits.append((path, action, reason, (extra or {}).get("expiry"))))
    sc.run(10)                                   # 0..5: quiet
    sc.cams["a"].push(motion(at(10)))
    if swing:
        sc.cams["a"].pan_x = OUT_OF_SPAN
    sc.tick(advance=5)                           # 10: live frame held
    return sc, audits


def _expiry_audits(audits):
    """What the expiry did: its sends and would-sends, and the hold_expired drop."""
    return [a for a in audits
            if (a[0] == "sampler" and a[1] in ("send", "would_send")) or a[2] == "hold_expired"]


def test_an_expired_hold_is_dropped_as_before_with_the_policy_off(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "off")
    assert ("live", "hold", "awaiting_corroboration", None) in audits
    sc.run(60)                                   # 15..70: low frames at 20 and 30, expiry at 35

    assert sc.actions("send") == []
    assert "a" not in sc.state.groups
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", None)]


def test_observe_audits_the_send_it_would_make_and_sends_nothing(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "observe")
    sc.run(60)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [
        ("sampler", "would_send", "hold_expiry_observe", None),
        ("sampler", "drop", "hold_expired", None),       # still counted as expired
    ]
    assert sc.state.last_alert.get(("a", "motion")) is None   # nothing armed


def test_send_delivers_the_held_frame_once_when_the_hold_expires(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send")
    sc.run(120)                                  # 15..130: well past the expiry

    assert sc.when(("send", "a")) == [at(35)]    # exactly once, at the expiry tick
    assert sc.notifier.send_paths == ["hold_expiry"]         # named so in the sent log
    assert _expiry_audits(audits) == [("sampler", "send", "hold_expiry_send", None)]
    assert sc.state.last_alert[("a", "motion")] == at(35)     # arms the motion cooldown


def test_send_respects_a_cooldown_armed_by_another_path(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send")
    sc.run(20)                                   # 15..30: both follow-up frames drop
    # An alert outside this group (an SD follow-up, say) armed the motion cooldown.
    sc.state.last_alert[("a", "motion")] = sc.clock.now
    sc.run(40)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", "cooldown")]


def test_send_leaves_the_scene_to_an_overlapping_camera_that_already_alerted(
        monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send",
                             coordinator={"group": "yard", "scene_window": 15})
    # A second camera of the group delivered this passage while the hold waited.
    sc.state.scene_coordinator.record_delivery("yard", "b", "motion", {"start_time": at(12)},
                                               at(15), window=15)
    sc.run(60)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", "scene_duplicate")]


def test_below_the_floor_the_held_frame_stays_dropped(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send", hold_expiry_min_score=0.5)
    sc.run(60)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", "below_floor")]


def test_an_archived_frame_that_is_gone_is_not_sent(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send")
    held = sc.state.groups["a"]["hold_path"]
    os.unlink(held)                              # review-log retention got there first
    sc.run(60)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", "archive_missing")]


def test_without_the_review_log_the_policy_has_nothing_to_send(monkeypatch, tmp_path):
    sc, audits = _hold_story(monkeypatch, tmp_path, "send", review_log=False)
    assert "hold_path" not in sc.state.groups["a"]
    sc.run(60)

    assert sc.actions("send") == []
    assert _expiry_audits(audits) == [("sampler", "drop", "hold_expired", "no_archive")]
    # Said once, at startup, rather than per expiry.
    warning = daemon.hold_expiry_archive_warning(sc.app, env={})
    assert warning and "TAPO_REVIEW_LOG_DIR" in warning
    assert daemon.hold_expiry_archive_warning(
        sc.app, env={"TAPO_REVIEW_LOG_DIR": str(tmp_path)}) is None


def test_a_pan_limit_recall_rescue_still_wins_over_the_policy(monkeypatch, tmp_path):
    # Auto-track swung past the span as the subject appeared; the guard pulls the lens
    # back in the hold's own tick, so the corroborating frame can never come. The rescue
    # sends the held frame — once, under its own reason — before the policy is asked.
    sc, audits = _hold_story(monkeypatch, tmp_path, "send", pan=pan_limit(), swing=True)
    assert sc.when(("goto", "a", "3")) == [at(10)]
    sc.run(60)

    assert sc.when(("send", "a")) == [at(35)]
    assert _expiry_audits(audits) == [("sampler", "send", "hold_rescue_recall", None)]


# ── SD follow-ups off the loop ───────────────────────────────────────────────

HOST_B = "192.0.2.11"


def _sd_story(monkeypatch, tmp_path):
    """Camera "a" reads its card for follow-ups, "b" alerts live only. The live grab of "a"
    fails, so its person is queued for the card, whose read blocks until the story
    releases it, like a slow download."""
    sc = Scenario(monkeypatch, tmp_path,
                  [camera_dict("a", HOST_A, sd_snapshot=True), camera_dict("b", HOST_B)],
                  alerts={"cooldown": 120})
    sc.cams["a"].rtsp_ok = False
    real_job_dir = sdworker.job_dir     # a read abandoned by the restart stays in tmp_path
    monkeypatch.setattr(sdworker, "job_dir", lambda tmp=None: real_job_dir(tmp or str(tmp_path)))
    reading, release = threading.Event(), threading.Event()
    reads = []

    def fetch(cfg_, start_time, span=None, out_dir=None, **_):
        reads.append((cfg_.name, start_time, out_dir))
        reading.set()
        assert release.wait(10), "the story never released the card read"
        path = os.path.join(out_dir, "sd.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8card")
        return [path]

    sc._collaborators["drain"] = functools.partial(
        daemon.process_pending_sd, snapshot_for=sc._snapshot_for,
        time_str=lambda _e: "scenario", fetch_frames=fetch)
    sc._collaborators["sd_worker"] = sdworker.SdWorker()
    return sc, reading, release, reads


def _until_read_is_back(worker):
    deadline = time.monotonic() + 5
    while worker._done.empty():
        assert time.monotonic() < deadline, "the read never came back"
        time.sleep(0.005)


def test_cameras_keep_being_polled_while_a_card_is_read(monkeypatch, tmp_path):
    # A card read blocks for minutes. The loop hands it to the worker and keeps going: a
    # person on the other camera during the read is alerted at once, and the follow-up
    # goes out on the first tick after the read is back.
    sc, reading, release, reads = _sd_story(monkeypatch, tmp_path)
    worker = sc._collaborators["sd_worker"]
    sc.run(10)
    sc.cams["a"].push(person(at(10)))
    sc.tick(advance=5)                           # 10: no live frame, follow-up queued
    due = sc.state.pending_sd[0]["due_at"]
    while sc.clock.now < due:
        sc.tick(advance=5)
    sc.tick(advance=5)                           # the first tick at or past due submits
    assert reading.wait(5)                       # the read started and is blocking
    [(camera, start, out_dir)] = reads
    assert (camera, start) == ("a", at(10))
    read_started = sc.clock.now

    sc.cams["b"].push(person(read_started))
    sc.run(20)                                   # four ticks while the card is read
    assert sc.when(("send", "b")) == [read_started]
    assert worker.busy("a") and len(sc.state.pending_sd) == 1
    assert len(reads) == 1                       # never submitted twice

    release.set()
    _until_read_is_back(worker)
    sent_at = sc.tick(advance=5)
    assert sc.actions("send") == [("send", "b"), ("send", "a")]
    assert sc.when(("send", "a")) == [sent_at]
    assert sc.notifier.send_paths[-1] == "sd"
    assert sc.state.pending_sd == [] and not os.path.exists(out_dir)
    worker.shutdown(wait=True)


def test_a_restart_during_a_card_read_reads_the_window_again(monkeypatch, tmp_path):
    sc, reading, release, reads = _sd_story(monkeypatch, tmp_path)
    sc.run(10)
    sc.cams["a"].push(person(at(10)))
    sc.tick(advance=5)
    while not reading.wait(0.05):
        sc.tick(advance=5)
    sc._collaborators["sd_worker"].shutdown(wait=False)   # a stop does not wait for it
    sc.restart()                                 # the queue comes back from runtime.json
    assert len(sc.state.pending_sd) == 1
    sc._collaborators["sd_worker"] = restarted = sdworker.SdWorker()
    release.set()                                # the old read ends; nobody collects it
    sc.tick(advance=5)
    _until_read_is_back(restarted)
    sc.tick(advance=5)

    assert [r[1] for r in reads] == [at(10), at(10)]
    assert sc.actions("send") == [("send", "a")]
    assert sc.state.pending_sd == []
    restarted.shutdown(wait=True)


# ── dual-lens C545D ──────────────────────────────────────────────────────────
# Shapes as captured on a C545D: a person is alarm_type 6 with events_1 34 on both lenses
# (no AI-person bit), plain motion is alarm_type 2 with events_1 2 on the wide lens only.
# The scorer is stubbed low (0.1) so the two paths differ visibly: a camera-confirmed
# person still goes out (no SD path: always-send safety net), bare motion is dropped.

def _low_scorer(monkeypatch):
    def score_for(_cfg):
        def score(_image):
            return 0.1
        score.boxes = {}
        return score
    monkeypatch.setattr(daemon, "score_for", score_for)


def _c545d(**overrides):
    base = dict(event_profile="c545d", role="static",
                tracking={"day_preset": None, "night_preset": None},
                scorer={"url": "http://127.0.0.1:9/score"})
    base.update(overrides)
    return camera_dict("front", HOST_A, **base)


def _audit(caplog, etype):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("audit ") and f"etype={etype}" in r.getMessage()]


def test_a_c545d_person_alerts_on_the_person_path(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    sc = Scenario(monkeypatch, tmp_path, [_c545d()])
    _low_scorer(monkeypatch)
    sc.run(10)
    sc.cams["front"].push(dual_lens(at(10), 6, {2: 34, 1: 34}))
    sc.run(20)

    assert sc.actions("send") == [("send", "front")]
    detect = [line for line in _audit(caplog, "person") if "action=detect" in line]
    assert len(detect) == 1 and "channels=1,2" in detect[0]


def test_c545d_plain_motion_on_the_wide_lens_takes_the_motion_path(
        monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    sc = Scenario(monkeypatch, tmp_path, [_c545d()])
    _low_scorer(monkeypatch)
    sc.run(10)
    sc.cams["front"].push(dual_lens(at(10), 2, {1: 2}))
    sc.run(20)

    assert sc.actions("send") == []
    drops = [line for line in _audit(caplog, "motion") if "action=drop" in line]
    assert len(drops) == 1 and "channels=1" in drops[0]
    assert _audit(caplog, "person") == []
    assert "front" not in sc.state.lens_linkage_at          # the pan/tilt lens never fired


def test_on_a_c560ws_the_same_pair_is_still_pir_motion(monkeypatch, tmp_path, caplog):
    # The unchanged default: alarm_type 6 / bit 32 is the C560WS PIR, bare motion that
    # the scorer must confirm, and a single-lens event carries no channel list.
    caplog.set_level(logging.INFO)
    sc = Scenario(monkeypatch, tmp_path, [camera_dict(
        "a", HOST_A, role="static", tracking={"day_preset": None, "night_preset": None},
        scorer={"url": "http://127.0.0.1:9/score"})])
    _low_scorer(monkeypatch)
    sc.run(10)
    sc.cams["a"].push({"start_time": at(10), "events_1": 34, "alarm_type": 6})
    sc.run(20)

    assert sc.actions("send") == []
    drops = [line for line in _audit(caplog, "motion") if "action=drop" in line]
    assert len(drops) == 1 and "channels=" not in drops[0]
    assert "pir=1 person=0" in caplog.text
    assert sc.state.lens_linkage_at == {}


def test_the_firmware_lens_linkage_holds_recall_and_guard(monkeypatch, tmp_path):
    # A person seen by both lenses: the C545D firmware turns the pan/tilt lens after them
    # (dual-cam linkage), here past the preset span. Neither the scheduled recall nor the
    # pan guard may pull it back until 180 s after the event's end, auto-track off or not;
    # then they move it as usual.
    sc = Scenario(monkeypatch, tmp_path, [_c545d(
        role="tracking", tracking={"day_preset": "2", "night_preset": "2"},
        pan_limit=pan_limit(), scorer={})])
    cam = sc.cams["front"]
    sc.run(30)                                   # 0..25: lens home
    cam.push(dual_lens(at(30), 6, {1: 34, 2: 34}, duration=20))    # ends at 50
    cam.pan_x = OUT_OF_SPAN                      # the firmware swings the PT lens
    sc.run(250)                                  # 30..275

    moves = [t for t, a in sc.timeline if a[0] in ("recall", "goto") and t >= at(30)]
    assert moves, "the lens must come back once the linkage lets go"
    assert min(moves) >= at(50 + 180)            # nothing moved it inside the hold
    assert sc.actions("send") == [("send", "front")]
    refusals = sc.state.motion_refusals["front"]
    assert refusals["schedule:linkage"] >= 1
    assert refusals["pan_limit:linkage"] >= 1
