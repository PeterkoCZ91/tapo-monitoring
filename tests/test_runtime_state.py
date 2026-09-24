import json
import logging

from tapo_monitor import daemon, runtime_state


def _state_with_work(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")
    state = daemon.MonitorState()
    state.pending_hub.append({
        "camera": "a", "start_time": 990.0, "image": str(frame), "caption": "c",
        "score": 0.7, "attempts": 1, "queued_at": 995.0, "due_at": 1055.0,
        "audit_extra": {"clip_s": 12.0}})
    state.pending_sd.append({
        "camera": "a", "etype": "person", "event": {"start_time": 990, "end_time": 998},
        "span": 36, "full_span": 48, "due_at": 1100.0, "live_sent": False})
    state.last_alert[("a", "confirmed")] = 1000.0
    state.last_event_start[("a", "confirmed")] = 990.0
    return state


def test_round_trip_restores_queues_and_cooldowns(tmp_path):
    path = tmp_path / "runtime.json"
    before = _state_with_work(tmp_path)
    assert runtime_state.save(path, runtime_state.snapshot(before), now=1000)
    after = daemon.MonitorState()
    counts = runtime_state.load(path, after, now=1010)
    assert counts == {"pending_hub": 1, "pending_sd": 1, "cooldowns": 2}
    assert after.pending_hub == before.pending_hub
    assert after.pending_sd == before.pending_sd
    assert after.last_alert == before.last_alert
    assert after.last_event_start == before.last_event_start


def test_restart_inside_the_cooldown_does_not_send_the_same_person_twice(tmp_path):
    path = tmp_path / "runtime.json"
    first = daemon.MonitorState()
    _, on_alert = daemon.alert_gate(first, "a", cooldown=120, now=1000)
    on_alert("person", {"start_time": 995})
    runtime_state.save(path, runtime_state.snapshot(first), now=1000)

    restarted = daemon.MonitorState()
    runtime_state.load(path, restarted, now=1030)
    can_alert, _ = daemon.alert_gate(restarted, "a", cooldown=120, now=1030)
    assert can_alert("person", {"start_time": 1001}) is False


def test_hub_retry_whose_frame_is_gone_is_dropped_with_a_log(tmp_path, caplog):
    path = tmp_path / "runtime.json"
    before = _state_with_work(tmp_path)
    runtime_state.save(path, runtime_state.snapshot(before), now=1000)
    (tmp_path / "frame.jpg").unlink()
    after = daemon.MonitorState()
    with caplog.at_level(logging.WARNING):
        counts = runtime_state.load(path, after, now=1010,
                                    logger=logging.getLogger("t"))
    assert counts["pending_hub"] == 0 and after.pending_hub == []
    assert "frame is gone" in caplog.text


def test_stale_file_is_discarded_whole(tmp_path):
    path = tmp_path / "runtime.json"
    runtime_state.save(path, runtime_state.snapshot(_state_with_work(tmp_path)), now=1000)
    after = daemon.MonitorState()
    counts = runtime_state.load(path, after, now=1000 + runtime_state.MAX_AGE + 1)
    assert counts == {"pending_hub": 0, "pending_sd": 0, "cooldowns": 0}
    assert after.pending_hub == [] and after.last_alert == {}


def test_corrupt_or_foreign_file_starts_empty(tmp_path):
    path = tmp_path / "runtime.json"
    for content in ("{not json", json.dumps({"version": 99, "saved_at": 1000}),
                    json.dumps({"version": 1, "saved_at": 1000,
                                "last_alert": [["a", "confirmed", "soon"]]})):
        path.write_text(content)
        after = daemon.MonitorState()
        assert runtime_state.load(path, after, now=1000)["cooldowns"] == 0
        assert after.last_alert == {}


def test_missing_file_is_a_fresh_start(tmp_path):
    after = daemon.MonitorState()
    assert runtime_state.load(tmp_path / "absent.json", after, now=1)["pending_sd"] == 0


def test_an_unserializable_entry_is_left_out_not_the_whole_file(tmp_path):
    state = _state_with_work(tmp_path)
    state.pending_sd.append({"camera": "b", "event": {"odd": object()}})
    data = runtime_state.snapshot(state)
    assert [e["camera"] for e in data["pending_sd"]] == ["a"]
    assert runtime_state.save(tmp_path / "runtime.json", data, now=1000)


def test_save_if_changed_writes_only_on_change(tmp_path, monkeypatch):
    state = _state_with_work(tmp_path)
    state.runtime_path = str(tmp_path / "runtime.json")
    writes = []
    real_save = runtime_state.save
    monkeypatch.setattr(runtime_state, "save",
                        lambda *a, **k: writes.append(1) or real_save(*a, **k))
    assert runtime_state.save_if_changed(state, now=1000) is True
    assert runtime_state.save_if_changed(state, now=1004) is False
    state.pending_sd.clear()
    assert runtime_state.save_if_changed(state, now=1008) is True
    assert len(writes) == 2


def test_loaded_state_is_not_rewritten_until_it_changes(tmp_path):
    path = tmp_path / "runtime.json"
    runtime_state.save(path, runtime_state.snapshot(_state_with_work(tmp_path)), now=1000)
    after = daemon.MonitorState()
    after.runtime_path = str(path)
    runtime_state.load(path, after, now=1010)
    assert runtime_state.save_if_changed(after, now=1010) is False


def test_file_is_private(tmp_path):
    path = tmp_path / "runtime.json"
    runtime_state.save(path, runtime_state.snapshot(daemon.MonitorState()), now=1)
    assert path.stat().st_mode & 0o777 == 0o600


def test_default_path_sits_beside_the_health_state(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert runtime_state.default_path(env) == str(tmp_path / "tapo-monitor" / "runtime.json")


def test_loop_step_persists_runtime_state_when_it_changes(tmp_path):
    app = None                                 # every pass below is stubbed
    state = daemon.MonitorState()
    state.runtime_path = str(tmp_path / "runtime.json")

    def queue_work(app_, cams, st, **kw):
        st.pending_sd.append({"camera": "a", "etype": "person",
                              "event": {"start_time": 1}, "due_at": 5})

    noop = lambda *a, **k: None  # noqa: E731
    daemon.loop_step(app, {}, state, now=1000, secrets={}, last_control=1000,
                     control_interval=60, monitor=queue_work, hubpoll=noop, sample=noop,
                     drain=noop, guard=noop, digest=noop, is_night=lambda: True)
    saved = json.loads((tmp_path / "runtime.json").read_text())
    assert saved["pending_sd"][0]["camera"] == "a"
