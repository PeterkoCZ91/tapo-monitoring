"""Observe modes of the opt-in alert features (zones, loitering, vehicles).

``observe`` computes the feature and audits what it would have done, without sending or
filtering anything. The default ``enforce`` is covered by the feature's own tests.
"""
import io
import json

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, monitor, scorer
from tests import test_daemon as td
from tests import test_monitor as tm
from tests.test_scorer import RIGHT, _zoned

# ── config ───────────────────────────────────────────────────────────────────


def test_observe_modes_default_to_enforce_and_parse():
    app = cfg.load_config_from_dict({"cameras": [{"name": "a", "host": "203.0.113.10",
                                                  "scorer": {"url": "http://x"}}]})
    c = app.cameras[0]
    assert (c.scorer.alert_zones_mode, c.scorer.vehicle_mode, c.sampler.loiter_mode) == (
        "enforce", "enforce", "enforce")
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "a", "host": "203.0.113.10",
        "sampler": {"enabled": True, "max_frames": 6, "loiter_frames": 3,
                    "loiter_mode": "observe"},
        "scorer": {"url": "http://x", "alert_vehicles": True, "vehicle_mode": "observe",
                   "alert_zones": [[[0.5, 0], [1, 0], [1, 1]]],
                   "alert_zones_mode": "observe"}}]})
    c = app.cameras[0]
    assert (c.scorer.alert_zones_mode, c.scorer.vehicle_mode, c.sampler.loiter_mode) == (
        "observe", "observe", "observe")


@pytest.mark.parametrize("key,section", [("alert_zones_mode", "scorer"),
                                         ("vehicle_mode", "scorer"),
                                         ("loiter_mode", "sampler")])
def test_observe_modes_reject_unknown_values(key, section):
    camera = {"name": "a", "host": "203.0.113.10", "scorer": {"url": "http://x"},
              "sampler": {"enabled": True}}
    camera[section][key] = "dry-run"
    with pytest.raises(cfg.ConfigError, match=key):
        cfg.load_config_from_dict({"cameras": [camera]})


@pytest.mark.parametrize("scorer_cfg,sampler_cfg,match", [
    ({"alert_zones_mode": "observe"}, {}, "alert_zones_mode"),
    ({"vehicle_mode": "observe"}, {}, "vehicle_mode"),
    ({}, {"loiter_mode": "observe"}, "loiter_mode")])
def test_observe_mode_without_its_feature_is_refused(scorer_cfg, sampler_cfg, match):
    with pytest.raises(cfg.ConfigError, match=match):
        cfg.load_config_from_dict({"cameras": [{
            "name": "a", "host": "203.0.113.10",
            "sampler": {"enabled": True, **sampler_cfg},
            "scorer": {"url": "http://x", **scorer_cfg}}]})


# ── zones ────────────────────────────────────────────────────────────────────

def _serve(monkeypatch, body):
    monkeypatch.setattr(scorer.urllib.request, "urlopen",
                        lambda req, timeout=10: io.BytesIO(json.dumps(body).encode()))


def test_score_image_observe_keeps_the_people_and_reports_what_a_zone_would_drop(
        monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    _serve(monkeypatch, _zoned([0.9], [[10, 100, 60, 300]]))
    out = scorer.score_image("http://x/score", str(img), alert_zones=[RIGHT],
                             alert_zones_mode="observe")
    assert out["person"] == 0.9 and out["person_scores"] == [0.9]
    assert out["alert_zone_observe"] == {"inside": 0, "outside": 1, "person": 0.0}
    s = scorer.subject_score(out, threshold=0.4)
    assert s == 0.9 and s.zone_observe == (0, 1)


def test_score_image_enforce_is_unchanged(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    _serve(monkeypatch, _zoned([0.9], [[10, 100, 60, 300]]))
    out = scorer.score_image("http://x/score", str(img), alert_zones=[RIGHT])
    assert out["person"] == 0.0 and "alert_zone_observe" not in out


def test_observe_is_silent_when_the_zone_would_keep_the_person(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    _serve(monkeypatch, _zoned([0.9], [[700, 100, 800, 300]]))
    out = scorer.score_image("http://x/score", str(img), alert_zones=[RIGHT],
                             alert_zones_mode="observe")
    assert "alert_zone_observe" not in out
    assert scorer.subject_score(out, threshold=0.4).zone_observe is None


def test_observe_leaves_vehicles_unfiltered(monkeypatch, tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"jpg")
    _serve(monkeypatch, {**_zoned([0.9], [[700, 100, 800, 300]]), "vehicle": 0.9,
                         "vehicle_scores": [0.9], "vehicle_boxes": [[10, 100, 60, 300]]})
    out = scorer.score_image("http://x/score", str(img), alert_zones=[RIGHT],
                             alert_zones_mode="observe")
    assert out["vehicle"] == 0.9


def test_a_score_below_threshold_anyway_is_not_a_zone_drop():
    result = {"person": 0.2, "animal": 0.0,
              "alert_zone_observe": {"inside": 0, "outside": 1, "person": 0.0}}
    assert scorer.subject_score(result, threshold=0.4).zone_observe is None


def _audit_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("audit ")]


def _zone_score():
    return scorer.subject_score(
        {"person": 0.9, "animal": 0.0,
         "alert_zone_observe": {"inside": 0, "outside": 2, "person": 0.0}}, threshold=0.4)


def test_audit_event_adds_a_zone_line_for_a_send_the_zone_would_have_dropped(caplog):
    camera = tm._vehicle_cfg()
    with caplog.at_level("INFO"):
        monitor.audit_event(camera, {"start_time": 100}, "person", "live", "send",
                            score=_zone_score(), threshold=0.4)
    lines = _audit_lines(caplog)
    assert any("action=send " in m for m in lines)
    zone = [m for m in lines if "action=would_drop_outside_alert_zone" in m]
    assert len(zone) == 1
    assert "persons_inside=0" in zone[0] and "persons_outside=2" in zone[0]
    assert "203.0.113" not in zone[0] and "127.0.0.1" not in zone[0]


def test_audit_event_has_no_zone_line_for_other_actions_or_plain_scores(caplog):
    camera = tm._vehicle_cfg()
    plain = scorer.subject_score({"person": 0.9, "animal": 0.0}, threshold=0.4)
    with caplog.at_level("INFO"):
        monitor.audit_event(camera, {"start_time": 100}, "person", "live", "drop",
                            score=_zone_score())
        monitor.audit_event(camera, {"start_time": 100}, "person", "live", "send", score=plain)
    assert not any("would_drop_outside_alert_zone" in m for m in _audit_lines(caplog))


# ── vehicles ─────────────────────────────────────────────────────────────────

def _observe_vehicle_cfg():
    camera = tm._vehicle_cfg()
    camera.scorer.vehicle_mode = "observe"
    return camera


def test_live_vehicle_observe_audits_would_send_and_sends_nothing(monkeypatch):
    sent, armed, audits = tm._run_live_vehicle(
        monkeypatch, _observe_vehicle_cfg(), {"person": 0.05, "animal": 0.0, "vehicle": 0.8})
    assert "vehicle" not in [etype for _cap, etype in sent]
    assert "vehicle" not in armed                       # no cooldown armed
    assert ("vehicle", "would_send") in audits
    assert ("vehicle", "send") not in audits


def test_sampler_vehicle_observe_audits_would_send_without_arming_the_cooldown(
        monkeypatch, caplog):
    sent = []
    app = td._vehicle_app()
    app.cameras[0].scorer.vehicle_mode = "observe"
    state = daemon.MonitorState()
    state.groups["a"] = td._group()
    with caplog.at_level("INFO"):
        td._run_vehicle_sampler(app, state, sent, monkeypatch,
                                {"person": 0.0, "animal": 0.0, "vehicle": 0.8})
    assert sent == []
    assert ("a", "vehicle") not in state.last_alert
    would = [m for m in _audit_lines(caplog) if "action=would_send" in m]
    assert len(would) == 1 and "etype=vehicle" in would[0] and "path=sampler" in would[0]
    assert not any("action=send " in m for m in _audit_lines(caplog))


def test_sd_followup_vehicle_observe_audits_would_send_once(monkeypatch, caplog):
    sent = []
    app, state, fetch = td._vehicle_sd(["/tmp/sdf_1000_1_00.jpg"])
    app.cameras[0].scorer.vehicle_mode = "observe"
    td._vehicle_scores(monkeypatch, {"/tmp/sdf_1000_1_00.jpg": td.CAR})
    with caplog.at_level("INFO"):
        td._run_vehicle_sd(app, state, fetch, sent, monkeypatch)
    assert sent == []
    assert ("a", "vehicle") not in state.last_alert
    would = [m for m in _audit_lines(caplog) if "action=would_send" in m]
    assert len(would) == 1 and "etype=vehicle" in would[0] and "path=sd" in would[0]


# ── loitering ────────────────────────────────────────────────────────────────

def _run_observe_loiter(monkeypatch, caplog, scores, **kw):
    real_app = td._loiter_app

    def observe_app(**k):
        app = real_app(**k)
        app.cameras[0].sampler.loiter_mode = "observe"
        return app
    monkeypatch.setattr(td, "_loiter_app", observe_app)
    with caplog.at_level("INFO"):
        return td._run_loiter_ticks(monkeypatch, scores, **kw)


def test_loiter_observe_audits_would_loiter_once_and_sends_nothing(monkeypatch, caplog):
    captions, state = _run_observe_loiter(monkeypatch, caplog, [0.9] * 6)
    assert captions == []
    assert state.groups["a"]["loiter_sent"] is True
    would = [m for m in _audit_lines(caplog) if "action=would_loiter" in m]
    assert len(would) == 1
    assert "minutes=2" in would[0] and "path=sampler" in would[0]
    assert not any("action=loiter " in m for m in _audit_lines(caplog))


def test_loiter_observe_stays_quiet_when_the_person_leaves(monkeypatch, caplog):
    captions, _ = _run_observe_loiter(monkeypatch, caplog, [0.9, 0.9, 0.0, 0.9, 0.9, 0.0])
    assert captions == []
    assert not any("would_loiter" in m for m in _audit_lines(caplog))
