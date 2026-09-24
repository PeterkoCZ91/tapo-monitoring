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

def test_a_lens_parked_by_privacy_gets_no_motor_calls_once_the_twin_saw_it(
        monkeypatch, tmp_path):
    # Privacy mode parks the lens (here outside the pan span) and answers every motor call
    # with MOTOR_BUSY. Until the twin has read the switch, both paths still try; from the
    # probe that reads it on, neither sends a thing. When it goes off again, the guard
    # restores the aim on the tick the twin reads it and the schedule follows.
    sc = Scenario(monkeypatch, tmp_path, [tracking_camera()],
                  observability={"digital_twin": True, "probe_interval": 60})
    cam = sc.cams["a"]
    sc.run(90)                                   # 0..85
    cam.privacy = True                           # someone flips it in the app
    cam.pan_x = 0.95
    sc.run(210)                                  # 90..295
    cam.privacy = False
    sc.run(90)                                   # 300..385

    stale = [a for t, a in sc.timeline if at(90) <= t < at(120)]
    assert stale == [("goto", "a", "3")] * 3     # 90, 100, 110: the twin has not looked
    assert sc.when(("recall", "a", "2")) == [at(0), at(60), at(120), at(120), at(360)]
    parked = [a for t, a in sc.timeline if at(120) < t < at(300)
              and a[0] in ("recall", "goto")]
    assert parked == []                          # twin saw it at 120: hands off
    assert sc.state.motion_refusals["a"]["schedule:privacy"] == 3     # 180, 240, 300
    assert collapse(sc.motor(since=at(300))) == [
        ("goto", "a", "3"),                      # 300: twin reads "off", guard fixes aim
        ("recall", "a", "2"),                    # 360: first control pass that knows
    ]
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
