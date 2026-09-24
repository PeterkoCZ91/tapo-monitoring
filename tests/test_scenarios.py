"""Multi-tick stories through the real ``daemon.loop_step`` (see ``tests/scenario.py``).

Each test states the world tick by tick and asserts on the ORDER of what the daemon did —
which motor command, which delivery, which transition — not just on the final state.
Ticks are 5 s apart and the control pass runs every 60 s unless a story says otherwise;
times in comments are seconds since the scenario started.
"""


from tapo_monitor import twin
from tests.scenario import (
    START,
    Scenario,
    camera_dict,
    collapse,
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
