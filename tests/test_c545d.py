"""Tapo C545D (dual lens) support, pinned against real (sanitized) captures.

The fixture ``tests/fixtures/c545d.json`` holds getEvents entries and getter answers read
from one C545D. Two things differ from every earlier model and are what this file pins:
events report per lens under ``chn_events`` instead of a top-level ``events_1``, and a
person is ``alarm_type`` 6 / bit 5 (value 32) — the pair a C560WS uses for its PIR.
"""

import copy
import json
import logging
import os
from pathlib import Path

import pytest

from tapo_monitor import capabilities, daemon, detection, monitor, motion, sampler, twin
from tapo_monitor import config as cfg_mod

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "c545d.json").read_text())
EVENTS = FIXTURE["events"]
GETTERS = FIXTURE["getters"]
FIRST_POLL, MOTION_1, MOTION_2, PERSON = (copy.deepcopy(e) for e in EVENTS)


def c545d_camera(**overrides):
    base = {"name": "front", "host": "192.0.2.20", "event_profile": "c545d",
            "enrich": {"groq": False}}
    base.update(overrides)
    return base


# ── normalization ────────────────────────────────────────────────────────────

def test_normalize_or_s_the_channel_masks_and_lists_the_lenses():
    ev = detection.normalize_event(PERSON)
    assert ev["events_1"] == 34
    assert ev["channels"] == [1, 2]
    assert ev["start_time"] == PERSON["start_time"]
    assert ev["chn_events"] == PERSON["chn_events"]          # kept for the record
    assert "events_1" not in PERSON                           # the input is not mutated


def test_normalize_single_lens_motion():
    ev = detection.normalize_event(MOTION_2)
    assert (ev["events_1"], ev["channels"]) == (2, [1])


def test_normalize_leaves_a_top_level_event_untouched():
    # Every existing model: the very same object comes back, so nothing downstream moves.
    ev = {"start_time": 100, "events_1": 524288, "alarm_type": 2}
    assert detection.normalize_event(ev) is ev


def test_normalize_top_level_events_1_wins_over_the_channels():
    ev = detection.normalize_event({**PERSON, "events_1": 524288})
    assert ev["events_1"] == 524288
    assert ev["channels"] == [1, 2]


def test_normalize_falls_back_to_the_earliest_channel_start():
    raw = {k: v for k, v in PERSON.items() if k != "start_time"}
    assert detection.normalize_event(raw)["start_time"] == 1790330876


def test_normalize_is_idempotent_and_stamps_a_non_default_profile():
    once = detection.normalize_event(PERSON, "c545d")
    twice = detection.normalize_event(once, "c545d")
    assert once == twice
    assert once["event_profile"] == "c545d"
    # A default-profile camera gets no stamp even when the shape is multi-lens.
    assert "event_profile" not in detection.normalize_event(PERSON)


def test_normalize_ignores_junk_channel_entries():
    ev = detection.normalize_event({"start_time": 5, "chn_events": {
        "x": {"events_1": 2}, "2": "bad", "1": {"events_1": "nope"}}})
    assert ev["channels"] == [1]
    assert ev["events_1"] == 0


# ── model-specific bit meaning ──────────────────────────────────────────────

def test_c545d_person_pair_decodes_as_person_not_pir():
    ev = detection.normalize_event(PERSON, "c545d")
    flags = detection.event_flags(ev)
    assert flags["person"] is True
    assert flags["pir"] is False
    assert flags["unknown_bits"] == []


def test_the_same_pair_on_the_default_profile_is_still_pir():
    ev = detection.normalize_event(PERSON)                    # default profile
    flags = detection.event_flags(ev)
    assert (flags["pir"], flags["person"]) == (True, False)
    assert detection.classify_getevent(None, events_1=34, alarm_type=6) == "motion"


@pytest.mark.parametrize("events_1, alarm_type, expected", [
    (34, 6, "person"),       # observed: person walking by
    (2, 2, "motion"),        # observed: plain motion, wide lens only
    (32, 2, "person"),       # bit 5 alone is enough on this model
    (2, 6, "person"),        # so is the alarm class
    (524288, 2, "person"),   # the generic AI-person bit keeps working
])
def test_c545d_classification(events_1, alarm_type, expected):
    assert detection.classify_getevent(None, events_1=events_1, profile="c545d",
                                       alarm_type=alarm_type) == expected


def test_unknown_profile_name_is_the_default():
    assert detection.event_profile("nope") is detection.DEFAULT_PROFILE
    assert detection.event_profile(None) is detection.DEFAULT_PROFILE


# ── collect_detections / audit ──────────────────────────────────────────────

def test_collect_detections_classifies_the_captured_events():
    alertable, watermark = monitor.collect_detections(
        [MOTION_1, MOTION_2, PERSON], 0, True, profile="c545d")
    assert [etype for _ev, etype in alertable] == ["motion", "motion", "person"]
    assert watermark == PERSON["start_time"]
    person_ev = alertable[-1][0]
    assert person_ev["channels"] == [1, 2] and person_ev["event_profile"] == "c545d"


def test_a_re_poll_that_moves_the_start_one_second_back_does_not_re_alert():
    # Captured: the same event came back with start_time 1 s earlier in a later window.
    _a, watermark = monitor.collect_detections([FIRST_POLL], 0, True, profile="c545d")
    again, watermark2 = monitor.collect_detections([MOTION_1], watermark, True,
                                                   profile="c545d")
    assert again == [] and watermark2 == watermark


def test_collect_detections_without_a_profile_sees_the_motion_it_used_to_miss():
    # Before normalization a C545D event had no events_1 at all; now even the default
    # profile reads the mask (and calls bit 5 PIR, as on its own models).
    alertable, _ = monitor.collect_detections([PERSON], 0, True)
    assert alertable[0][1] == "motion"
    assert detection.event_flags(alertable[0][0])["pir"] is True


def test_event_seen_gets_every_fresh_event():
    seen = []
    monitor.collect_detections([MOTION_1, PERSON], 0, True, profile="c545d",
                               event_seen=seen.append)
    assert [e["start_time"] for e in seen] == [MOTION_1["start_time"], PERSON["start_time"]]


def test_event_log_line_names_the_lenses(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.monitor")
    monitor.collect_detections([PERSON], 0, True, profile="c545d")
    assert "channels=1,2 profile=c545d -> person" in caplog.text


def test_event_log_line_of_a_single_lens_camera_is_unchanged(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.monitor")
    monitor.collect_detections([{"start_time": 1, "events_1": 2, "alarm_type": 2}], 0)
    assert "alarm_type=2 faces=0 -> motion" in caplog.text


def test_audit_line_carries_the_channels(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.monitor")
    cam = cfg_mod.load_camera_config(c545d_camera())
    ev = detection.normalize_event(PERSON, "c545d")
    monitor.audit_event(cam, ev, "person", "getevents", "detect")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("audit "))
    assert "channels=1,2" in line
    assert f"incident=front-{PERSON['start_time']}" in line


def test_audit_line_of_a_single_lens_event_has_no_channels(caplog):
    caplog.set_level(logging.INFO, logger="tapo_monitor.monitor")
    cam = cfg_mod.load_camera_config({"name": "a", "host": "192.0.2.21"})
    monitor.audit_event(cam, {"start_time": 5, "events_1": 2}, "motion", "getevents", "detect")
    assert "channels=" not in caplog.text


# ── sampler PIR flag ────────────────────────────────────────────────────────

def test_sampler_does_not_call_a_c545d_person_pir_backed():
    assert sampler._is_pir_backed(detection.normalize_event(PERSON, "c545d")) is False
    assert sampler._is_pir_backed({"start_time": 1, "events_1": 34}) is True


# ── config ──────────────────────────────────────────────────────────────────

def test_config_defaults_keep_the_default_profile():
    cam = cfg_mod.load_camera_config({"name": "a", "host": "192.0.2.21"})
    assert cam.event_profile == "default"
    assert cam.lens_pick_stream is None


def test_config_accepts_the_c545d_profile_case_insensitively():
    assert cfg_mod.load_camera_config(c545d_camera(event_profile="C545D")).event_profile == "c545d"


def test_config_rejects_an_unknown_profile():
    with pytest.raises(cfg_mod.ConfigError, match="event_profile"):
        cfg_mod.load_camera_config(c545d_camera(event_profile="c999"))


def test_lens_pick_needs_a_pan_tilt_profile_and_a_scorer():
    with pytest.raises(cfg_mod.ConfigError, match="pan/tilt"):
        cfg_mod.load_camera_config({"name": "a", "host": "192.0.2.21",
                                    "lens_pick_stream": "stream7",
                                    "scorer": {"url": "http://127.0.0.1:1/score"}})
    with pytest.raises(cfg_mod.ConfigError, match="scorer.url"):
        cfg_mod.load_camera_config(c545d_camera(lens_pick_stream="stream7"))
    with pytest.raises(cfg_mod.ConfigError, match="differ"):
        cfg_mod.load_camera_config(c545d_camera(
            rtsp_stream="stream7", lens_pick_stream="stream7",
            scorer={"url": "http://127.0.0.1:1/score"}))
    cam = cfg_mod.load_camera_config(c545d_camera(
        rtsp_stream="stream2", lens_pick_stream="stream7",
        scorer={"url": "http://127.0.0.1:1/score"}))
    assert cam.lens_pick_stream == "stream7"


# ── motion arbiter: firmware lens linkage ───────────────────────────────────

@pytest.mark.parametrize("requester", [motion.SCHEDULE, motion.GUARD])
def test_linkage_refuses_both_motor_paths_whatever_auto_track_says(requester):
    move = motion.decide(requester, linkage=True, autotrack_on=False,
                         out_of_bounds_for=999, hold_grace=0)
    assert move == motion.Decision(False, motion.LINKAGE)


def test_privacy_still_outranks_linkage():
    assert motion.decide(motion.SCHEDULE, privacy_on=True, linkage=True).reason == motion.PRIVACY


def test_linkage_off_leaves_the_decision_as_before():
    assert motion.decide(motion.SCHEDULE) == motion.ALLOW


def test_linkage_due_window():
    assert daemon.linkage_due(None, 100, 180) is False
    assert daemon.linkage_due(100, 279, 180) is True
    assert daemon.linkage_due(100, 280, 180) is False
    assert daemon.linkage_due(100, 150, 0) is False


def test_only_a_pan_tilt_lens_event_arms_the_linkage():
    app = cfg_mod.load_config_from_dict({"cameras": [
        c545d_camera(), {"name": "b", "host": "192.0.2.22"}]})
    state = daemon.MonitorState()
    front, other = app.cameras
    daemon.note_lens_event(state, front, detection.normalize_event(MOTION_2, "c545d"))
    assert state.lens_linkage_at == {}                         # wide lens only
    daemon.note_lens_event(state, front, detection.normalize_event(PERSON, "c545d"))
    assert state.lens_linkage_at == {"front": PERSON["end_time"]}
    daemon.note_lens_event(state, other, detection.normalize_event(PERSON))
    assert "b" not in state.lens_linkage_at                    # default profile: never
    assert daemon.cameras_under_linkage(app, state, PERSON["end_time"] + 179) == {"front"}
    assert daemon.cameras_under_linkage(app, state, PERSON["end_time"] + 180) == set()


def test_an_older_segment_does_not_shorten_the_hold():
    app = cfg_mod.load_config_from_dict({"cameras": [c545d_camera()]})
    state = daemon.MonitorState()
    front = app.cameras[0]
    daemon.note_lens_event(state, front, detection.normalize_event(PERSON, "c545d"))
    older = {**PERSON, "start_time": 10, "end_time": 20}
    daemon.note_lens_event(state, front, detection.normalize_event(older, "c545d"))
    assert state.lens_linkage_at["front"] == PERSON["end_time"]


# ── digital twin ────────────────────────────────────────────────────────────

class C545DClient:
    """Answers from the fixture; records every call and its chn_id."""

    def __init__(self, sd_card="getSDCard"):
        self.calls = []
        self._sd = sd_card

    def __getattr__(self, name):
        if not name.startswith("get"):
            raise AttributeError(name)

        def call(chn_id=None):
            self.calls.append((name, chn_id))
            if name == "getSDCard":
                return copy.deepcopy(GETTERS[self._sd])
            if chn_id is not None:
                return copy.deepcopy(GETTERS[f"{name}_chn12"])
            if name in GETTERS:
                return copy.deepcopy(GETTERS[name])
            raise Exception("-40101 not supported")
        return call


def test_single_lens_snapshot_sends_no_dual_lens_or_per_channel_probe():
    client = C545DClient()
    snap = capabilities.collect_snapshot(client)
    assert "dual_cam" not in snap["groups"] and "detection_chn" not in snap["groups"]
    assert all(chn is None for _name, chn in client.calls)
    assert not {n for n, _ in client.calls} & {"getAllChnInfo", "getDualCamLinkage"}


def test_dual_lens_snapshot_reads_both_lenses_and_the_linkage():
    client = C545DClient()
    snap = capabilities.collect_snapshot(client, channels=(1, 2))
    groups = snap["groups"]
    assert groups["dual_cam"]["linkage"]["state"] == "available"
    person = groups["detection_chn"]["person"]["value"]
    assert person["2"]["enabled"] == "on"
    assert ("getPersonDetection", [1, 2]) in client.calls
    # The lens layout keeps the aliases but not the per-lens device IDs.
    chn = groups["dual_cam"]["channels"]["value"]["system"]["chn_info"]
    assert [c["chn_alias"] for c in chn] == ["Fixed Lens", "PT Lens"]
    assert all(c["chn_dev_id"] == capabilities.REDACTED for c in chn)
    json.dumps(snap, allow_nan=False)


def _plan():
    return daemon.plan_camera(cfg_mod.load_camera_config(c545d_camera(role="static")),
                              True, False)


def test_twin_drift_paths_for_a_dual_lens_camera():
    snap = capabilities.collect_snapshot(C545DClient(), channels=(1, 2))
    evaluation = twin.evaluate_snapshot("front", _plan(), snap)
    actual = evaluation["actual"]
    assert actual["dual_cam.linkage.enabled"] is True
    assert actual["detection.person.chn1.enabled"] is True
    assert actual["detection.person.chn2.enabled"] is True
    paths = {r["path"] for r in twin.alertable_results(evaluation)}
    assert not any(p.startswith(("dual_cam.", "detection.person.chn")) for p in paths)


def test_twin_reports_linkage_switched_off_and_a_lens_without_person_detection():
    snap = capabilities.collect_snapshot(C545DClient(), channels=(1, 2))
    snap["groups"]["dual_cam"]["linkage"]["value"] = {
        "dual_cam_linkage": {"linkage_state": {"enabled": "off", "linkage_type": 0}}}
    snap["groups"]["detection_chn"]["person"]["value"]["2"]["enabled"] = "off"
    evaluation = twin.evaluate_snapshot("front", _plan(), snap)
    paths = {r["path"]: r["severity"] for r in twin.alertable_results(evaluation)}
    assert paths["dual_cam.linkage.enabled"] == "warning"
    assert paths["detection.person.chn2.enabled"] == "critical"


def test_twin_of_a_single_lens_snapshot_has_no_dual_lens_paths():
    snap = capabilities.collect_snapshot(C545DClient())
    evaluation = twin.evaluate_snapshot("front", _plan(), snap)
    assert not any(p.startswith(("dual_cam.", "detection.person.chn"))
                   for p in evaluation["desired"])


def test_counterfeit_sd_card_is_a_storage_warning():
    snap = capabilities.collect_snapshot(C545DClient(sd_card="sd_card_dilatant"))
    assert capabilities.storage_warnings(snap) == ("counterfeit or defective SD card",)
    # status says "normal": the detect_status alone degrades storage.
    assert capabilities.derive_health(snap)["storage"] == "degraded"


def test_a_missing_sd_card_is_degraded_but_not_counterfeit():
    snap = capabilities.collect_snapshot(C545DClient())
    assert capabilities.storage_warnings(snap) == ()
    assert capabilities.derive_health(snap)["storage"] == "degraded"   # offline


def test_the_daemon_probes_a_c545d_per_lens_and_records_the_sd_warning(caplog):
    app = cfg_mod.load_config_from_dict({
        "observability": {"digital_twin": True},
        "cameras": [c545d_camera(), {"name": "b", "host": "192.0.2.22"}]})
    state = daemon.MonitorState()
    seen = {}

    def probe(cam, **kw):
        seen[cam] = kw
        return capabilities.collect_snapshot(C545DClient(sd_card="sd_card_dilatant"), **kw)

    caplog.set_level(logging.WARNING, logger="tapo_monitor.daemon")
    daemon.process_digital_twin(app, {"front": "cam-front", "b": "cam-b"}, state, now=1,
                                secrets={"telegram_token": "", "telegram_chat": ""},
                                probe=probe)
    assert seen == {"cam-front": {"channels": (1, 2)}, "cam-b": {}}
    assert state.twin_fleet["front"]["warnings"] == ["counterfeit or defective SD card"]
    assert "counterfeit or defective SD card" in caplog.text


# ── lens pick ───────────────────────────────────────────────────────────────

def _frame(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8x")
    return str(path)


def _scorer(scores, boxes=None):
    def score(image):
        return scores[os.path.basename(image)]
    score.boxes = {}
    for name, box in (boxes or {}).items():
        score.boxes[name] = box
    return score


def _pick_cfg():
    return cfg_mod.load_camera_config(c545d_camera(
        rtsp_stream="stream2", lens_pick_stream="stream7",
        scorer={"url": "http://127.0.0.1:1/score"}))


def test_lens_pick_takes_the_larger_subject_and_removes_the_other(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    score = _scorer({"wide.jpg": 0.9, "pt.jpg": 0.7})
    score.boxes = {wide: [0, 0, 10, 20], pt: [0, 0, 40, 80]}
    image, s = daemon.pick_lens_frame(_pick_cfg(), wide, pt, score,
                                      blur_score=lambda f, box=None: 1.0)
    assert (image, s) == (pt, 0.7)
    assert not os.path.exists(wide)


def test_lens_pick_without_boxes_takes_the_higher_score(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, s = daemon.pick_lens_frame(_pick_cfg(), wide, pt,
                                      _scorer({"wide.jpg": 0.9, "pt.jpg": 0.7}))
    assert (image, s) == (wide, 0.9)
    assert not os.path.exists(pt)


def test_lens_pick_below_threshold_keeps_the_best_evidence(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, s = daemon.pick_lens_frame(_pick_cfg(), wide, pt,
                                      _scorer({"wide.jpg": 0.1, "pt.jpg": 0.3}))
    assert (image, s) == (pt, 0.3)


def test_lens_pick_scorer_failure_keeps_the_first_lens(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, s = daemon.pick_lens_frame(_pick_cfg(), wide, pt,
                                      _scorer({"wide.jpg": 0.9, "pt.jpg": None}))
    assert (image, s) == (wide, None)
    assert not os.path.exists(pt)


def test_lens_pick_with_one_failed_grab_uses_the_other(tmp_path):
    pt = _frame(tmp_path, "pt.jpg")
    assert daemon.pick_lens_frame(_pick_cfg(), None, pt, _scorer({})) == (pt, None)


class _Cam:
    def __init__(self, batch):
        self.batch = batch

    def getEvents(self):
        batch, self.batch = self.batch, []
        return batch


def _run_pick_pass(monkeypatch, tmp_path, event, **camera):
    app = cfg_mod.load_config_from_dict({"cameras": [c545d_camera(**camera)]})
    scored = []

    def score_for(_cfg):
        def score(image):
            scored.append(os.path.basename(image))
            return {"wide.jpg": 0.9, "pt.jpg": 0.8}[os.path.basename(image)]
        score.boxes = {}
        return score

    sent, grabs = [], []
    monkeypatch.setattr(daemon, "score_for", score_for)
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda token, chat, image, *a, **k: sent.append(
                            os.path.basename(image)) or True)

    def snapshot_for(_cfg):
        return lambda _c, _e: grabs.append("wide") or _frame(tmp_path, "wide.jpg")

    def lens_snapshot_for(_cfg):
        return lambda _c, _e: grabs.append("pt") or _frame(tmp_path, "pt.jpg")

    state = daemon.MonitorState()
    daemon.run_monitor_pass(app, {"front": _Cam([event])}, state, now=PERSON["start_time"],
                            secrets={"telegram_token": "t", "telegram_chat": "c",
                                     "groq_key": ""},
                            snapshot_for=snapshot_for, time_str=lambda _e: "t",
                            lens_snapshot_for=lens_snapshot_for)
    return state, grabs, scored, sent


def test_run_monitor_pass_picks_between_lenses_for_a_pan_tilt_event(monkeypatch, tmp_path):
    state, grabs, scored, sent = _run_pick_pass(
        monkeypatch, tmp_path, copy.deepcopy(PERSON), rtsp_stream="stream2",
        lens_pick_stream="stream7", scorer={"url": "http://127.0.0.1:1/score"})
    assert grabs == ["wide", "pt"]
    assert scored == ["wide.jpg", "pt.jpg"]          # the kept frame is not scored twice
    assert sent == ["wide.jpg"]
    assert state.lens_linkage_at["front"] == PERSON["end_time"]


def test_run_monitor_pass_grabs_one_lens_for_a_wide_lens_event(monkeypatch, tmp_path):
    _state, grabs, _scored, _sent = _run_pick_pass(
        monkeypatch, tmp_path, copy.deepcopy(MOTION_2), rtsp_stream="stream2",
        lens_pick_stream="stream7", scorer={"url": "http://127.0.0.1:1/score"})
    assert grabs == ["wide"]


def test_run_monitor_pass_without_lens_pick_grabs_one_lens(monkeypatch, tmp_path):
    state, grabs, _scored, sent = _run_pick_pass(
        monkeypatch, tmp_path, copy.deepcopy(PERSON),
        scorer={"url": "http://127.0.0.1:1/score"})
    assert grabs == ["wide"] and sent == ["wide.jpg"]
    assert state.lens_linkage_at["front"] == PERSON["end_time"]
