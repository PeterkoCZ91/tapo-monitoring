import pytest

from tapo_monitor import config, monitor


def _cfg():
    return config.load_config_from_dict({
        "cameras": [{"name": "a", "host": "203.0.113.10", "enrich": {"groq": False}}]
    }).cameras[0]


def _person_event(start):
    return {"start_time": start, "events_1": 524290, "alarm_type": 2}


class _Cam:
    def __init__(self, *starts):
        self.starts = starts

    def getEvents(self):
        return [_person_event(s) for s in self.starts]


def _run(cam, **kw):
    return monitor.run_monitor(
        cam, _cfg(), 0, now=1000, groq_key="", telegram_token="", telegram_chat="",
        snapshot=kw.pop("snapshot", lambda c, e: "/tmp/x.jpg"),
        time_str=kw.pop("time_str", lambda e: "T"), **kw)


def test_callback_typeerror_in_body_is_not_retried():
    calls = []

    def on_alert(etype, event=None):
        calls.append(etype)
        raise TypeError("boom inside body")

    with pytest.raises(TypeError):
        monitor._on_alert(on_alert, "person", {})
    assert calls == ["person"]


def test_callbacks_use_the_shape_their_signature_accepts():
    seen = []
    monitor._on_alert(lambda etype: seen.append(("on_alert", etype)), "person", {})
    assert monitor._can_alert(lambda etype: seen.append(("can", etype)) or True, "p", {})
    monitor._observe(lambda e, t, s: seen.append(("obs", s)), {}, "p", True, True)
    monitor._health_observe(lambda ok: seen.append(("health", ok)), True, "err")
    assert monitor._defer(lambda e, t, ls: seen.append("defer"), {}, "p", False,
                          keep_sampling=True)
    assert seen == [("on_alert", "person"), ("can", "p"), ("obs", True),
                    ("health", True), "defer"]


def test_callbacks_get_the_richest_signature_exactly_once():
    calls = []
    ev = {"start_time": 1}

    def on_alert(etype, event=None, extra=None):
        calls.append(("on_alert", etype, event, extra))

    def can(etype, event=None):
        calls.append(("can", etype, event))
        return True

    def obs(event, etype, sent, delivered=False):
        calls.append(("obs", event, etype, sent, delivered))

    def health_obs(ok, error=None):
        calls.append(("health", ok, error))

    def defer(event, etype, live_sent, keep_sampling=False):
        calls.append(("defer", event, etype, live_sent, keep_sampling))
        return True

    monitor._on_alert(on_alert, "person", ev)
    assert monitor._can_alert(can, "person", ev) is True
    monitor._observe(obs, ev, "person", True, True)
    monitor._health_observe(health_obs, 1, "err")
    assert monitor._defer(defer, ev, "person", False, keep_sampling=True) is True
    assert calls == [("on_alert", "person", ev, None),
                     ("can", "person", ev),
                     ("obs", ev, "person", True, True),
                     ("health", True, "err"),
                     ("defer", ev, "person", False, True)]


def test_narrow_signatures_get_trimmed_args_exactly_once():
    calls = []
    ev = {"start_time": 1}
    monitor._health_observe(lambda ok: calls.append(("health", ok)), 1, "err")
    monitor._observe(lambda e, t, s: calls.append(("obs", e, t, s)), ev, "p", True, True)
    assert monitor._defer(lambda e, t, ls: calls.append(("defer", e, t, ls)) or True,
                          ev, "p", False, keep_sampling=True)
    assert monitor._can_alert(lambda t: calls.append(("can", t)) or True, "p", ev)
    assert calls == [("health", True), ("obs", ev, "p", True), ("defer", ev, "p", False),
                     ("can", "p")]


def test_defer_typeerror_in_body_is_not_retried():
    calls = []

    def defer(event, etype, live_sent, keep_sampling=False):
        calls.append(keep_sampling)
        raise TypeError("boom")

    with pytest.raises(TypeError):
        monitor._defer(defer, {}, "person", False, keep_sampling=True)
    assert calls == [True]


def test_whitelamp_typeerror_in_body_is_not_resent(monkeypatch):
    calls = []

    def lamp(cam, force_time=None):
        calls.append(force_time)
        raise TypeError("boom")

    cfg = _cfg()
    cfg.light_trigger = config.load_config_from_dict({"cameras": [{
        "name": "a", "host": "203.0.113.10",
        "light_trigger": {"enabled": True, "types": ["person"]}}]}).cameras[0].light_trigger
    cfg.whitelamp_force_time = 30
    monkeypatch.setattr(monitor.camera, "whitelamp_seen", lambda *a, **k: None)
    monitor.run_monitor(
        _Cam(100), cfg, 0, now=1000, groq_key="", telegram_token="", telegram_chat="",
        snapshot=lambda c, e: None, time_str=lambda e: "T", trigger_whitelamp=lamp)
    assert calls == [30]


def test_failing_event_stops_poll_and_is_retried_then_skipped():
    monitor._EVENT_FAILURES.clear()
    sent = []
    health = []

    def time_str(event):
        if event["start_time"] == 200:
            raise ValueError("secret-token-123")
        return "T"

    def poll():
        return _run(_Cam(100, 200, 300), time_str=time_str,
                    poll_observe=lambda ok, error=None: health.append(ok),
                    send_alert=lambda image, caption, score, **_: sent.append(caption) or True)

    # Events 100 sent; 200 fails: later 300 untouched, watermark just before 200.
    assert poll() == 199
    assert len(sent) == 1
    assert health == [True]                    # an event failure is not an unreachable API
    assert poll() == 199                       # retried (second failure)
    wm = _run(_Cam(200, 300), time_str=time_str,
              send_alert=lambda image, caption, score, **_: sent.append(caption) or True)
    assert wm == 300                           # third failure: skipped, 300 processed
    assert len(sent) == 3                      # 100 (fake cam replays it), 100, 300


def test_event_error_log_has_class_only(caplog):
    monitor._EVENT_FAILURES.clear()
    caplog.set_level("INFO")

    def time_str(event):
        raise ValueError("secret-token-123")

    wm = _run(_Cam(100), time_str=time_str, send_alert=lambda *a, **k: True)
    assert wm == 99
    assert "ValueError" in caplog.text
    assert "secret-token-123" not in caplog.text


def test_shape_cache_does_not_hold_callbacks():
    import gc
    import weakref

    class Cb:
        def __call__(self, a, b):
            pass

    cb = Cb()
    ref = weakref.ref(cb)
    monitor._call_compat(cb, 1, 2)
    del cb
    gc.collect()
    assert ref() is None


def test_keyboard_interrupt_still_propagates():
    def time_str(event):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(_Cam(100), time_str=time_str, send_alert=lambda *a, **k: True)
