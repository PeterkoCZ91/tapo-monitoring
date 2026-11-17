"""A lit white lamp nothing asked for: log it, optionally send one notice per episode."""

import logging
from types import SimpleNamespace

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, ledger, twin

IR = "inf_night_vision"
SMART = "md_night_vision"
SECRETS = {"telegram_token": "t", "telegram_chat": "c"}


def _entry(lamp, rest=0, mode=IR):
    groups = {"light": {
        "whitelamp_status": {"state": "available",
                             "value": {"status": lamp, "rest_time": rest}}}}
    if mode is not None:
        groups["light"]["night_vision_mode"] = {"state": "available", "value": {
            "image": {"switch": {"night_vision_mode": mode}}}}
    return {"snapshot": {"groups": groups}}


# -- pure decision ----------------------------------------------------------------------

def test_dark_lamp_is_never_an_anomaly():
    assert twin.lamp_without_cause(_entry(0)["snapshot"], expected_mode=IR,
                                   detected_recently=False) is None


def test_lit_in_ir_mode_is_always_an_anomaly_even_after_a_detection():
    snap = _entry(1, 40)["snapshot"]
    assert twin.lamp_without_cause(snap, expected_mode=IR,
                                   detected_recently=True) == "lit_in_ir_mode"


def test_lit_in_smart_mode_needs_a_detection():
    snap = _entry(1, 40, mode=SMART)["snapshot"]
    assert twin.lamp_without_cause(snap, expected_mode=SMART,
                                   detected_recently=True) is None
    assert twin.lamp_without_cause(snap, expected_mode=SMART,
                                   detected_recently=False) == "no_detection"
    # No ledger to ask: unknown, never a claim.
    assert twin.lamp_without_cause(snap, expected_mode=SMART,
                                   detected_recently=None) is None


def test_explicit_exception_makes_no_claim():
    snap = _entry(1, 40)["snapshot"]
    assert twin.lamp_without_cause(snap, expected_mode=None,
                                   detected_recently=False) is None


def test_unreadable_lamp_is_no_claim():
    snap = {"groups": {"light": {"whitelamp_status": {"state": "unknown"}}}}
    assert twin.lamp_without_cause(snap, expected_mode=IR, detected_recently=False) is None


# -- daemon wiring ----------------------------------------------------------------------

def _cam(**extra):
    base = {"name": "front", "host": "203.0.113.10"}
    base.update(extra)
    return cfg.load_config_from_dict({"cameras": [base]}).cameras[0]


def _state(tmp_path, detection_at=None):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    if detection_at is not None:
        events.record_camera_event(camera="front", event_type="person", event_at=detection_at)
    return SimpleNamespace(event_ledger=events, twin_lamp_episode={})


def _check(state, cam, entry, now, plan_expect=IR):
    plan = SimpleNamespace(night_vision_expect=plan_expect)
    return daemon._check_lamp_without_cause(state, cam, plan, entry, now)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "lamp without cause" in r.getMessage()]


def test_one_greppable_line_per_episode_and_a_ledger_row(tmp_path, caplog):
    cam, state = _cam(), _state(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert _check(state, cam, _entry(1, 40), 1000.0) == "lit_in_ir_mode"
        _check(state, cam, _entry(1, 0), 1900.0)          # same episode: no second line
    lines = _lines(caplog)
    assert len(lines) == 1
    for part in ("camera=front", f"night_vision_mode={IR}", "status=1", "rest_time=40",
                 "reason=lit_in_ir_mode", "detection_in_window=no"):
        assert part in lines[0]
    rows = state.event_ledger.state_changes(camera="front", start=0, end=2000,
                                            key="lamp_without_cause")
    assert [(r["value"], r["metadata"]) for r in rows] == [
        ("lit_in_ir_mode", {"rest_time": 40})]


def test_a_new_episode_after_the_lamp_goes_dark_is_logged_again(tmp_path, caplog):
    cam, state = _cam(), _state(tmp_path)
    with caplog.at_level(logging.WARNING):
        _check(state, cam, _entry(1, 40), 1000.0)
        _check(state, cam, _entry(0), 1900.0)
        _check(state, cam, _entry(1, 40), 2800.0)
    assert len(_lines(caplog)) == 2


def test_never_sends_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.notify, "send_operational",
                        lambda *a: pytest.fail("a lamp episode must not send a notice"))
    _check(_state(tmp_path), _cam(), _entry(1, 40), 1000.0)


def test_a_recent_detection_is_a_cause_in_smart_mode(tmp_path, caplog):
    cam = _cam(light_trigger={"window": "00:30-04:30", "mode": "firmware"})
    state = _state(tmp_path, detection_at=950.0)
    with caplog.at_level(logging.WARNING):
        assert _check(state, cam, _entry(1, 40, mode=SMART), 1000.0,
                      plan_expect=SMART) is None
    assert _lines(caplog) == []


def test_old_detection_is_no_cause(tmp_path, caplog):
    state = _state(tmp_path, detection_at=1.0)
    with caplog.at_level(logging.WARNING):
        assert _check(state, _cam(), _entry(1, 40, mode=SMART), 5000.0,
                      plan_expect=SMART) == "no_detection"
    assert "detection_in_window=no" in _lines(caplog)[0]


def test_no_ledger_or_no_expectation_or_no_plan_is_silent(tmp_path, caplog):
    cam = _cam()
    state = _state(tmp_path)
    state.event_ledger = None
    with caplog.at_level(logging.WARNING):
        assert _check(state, cam, _entry(1, 40, mode=SMART), 1000.0, plan_expect=SMART) is None
        assert _check(_state(tmp_path), cam, _entry(1, 40), 1000.0, plan_expect=None) is None
        assert daemon._check_lamp_without_cause(
            _state(tmp_path), cam, None, _entry(1, 40), 1000.0) is None
    assert _lines(caplog) == []


def test_unread_lamp_is_no_claim(tmp_path, caplog):
    entry = {"snapshot": {"groups": {"light": {"whitelamp_status": {"state": "unknown"}}}}}
    with caplog.at_level(logging.WARNING):
        assert _check(_state(tmp_path), _cam(), entry, 1000.0) is None
    assert _lines(caplog) == []


def test_ledger_failure_never_raises(tmp_path):
    class Broken:
        def observations(self, **k):
            raise OSError("disk")

    state = SimpleNamespace(event_ledger=Broken(), twin_lamp_episode={})
    assert _check(state, _cam(), _entry(1, 40), 1000.0) is None
