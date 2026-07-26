import json
import stat
import types

import pytest

from tapo_monitor import health


def _state(**overrides):
    values = {name: {} for name in health.PERSISTED_FIELDS}
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_default_state_path_honors_override_and_xdg():
    assert health.default_state_path(
        {"TAPO_HEALTH_STATE_FILE": "/tmp/custom-health.json"}) == "/tmp/custom-health.json"
    assert health.default_state_path(
        {"XDG_STATE_HOME": "/tmp/state"}) == "/tmp/state/tapo-monitor/health.json"
    assert health.default_state_path({}, home="/tmp/home") == (
        "/tmp/home/.local/state/tapo-monitor/health.json")


def test_health_state_round_trip_is_secret_free_and_private(tmp_path):
    path = tmp_path / "nested" / "health.json"
    original = _state(
        last_seen={"front": 99.0},
        online_since={"front": 100.0},
        fail_since={"back": 200.0},
        outage_alerted={"back": True},
        reconnect_count={"front": 3},
    )

    assert health.save_state(str(path), original) is True
    restored = _state()
    assert health.load_state(str(path), restored) is True

    assert health.snapshot(restored) == health.snapshot(original)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert set(payload) == {"version", "state"}


def test_invalid_health_state_leaves_existing_state_unchanged(tmp_path):
    path = tmp_path / "health.json"
    path.write_text('{"version":1,"state":{"online_since":[]}}')
    state = _state(online_since={"front": 123})

    assert health.load_state(str(path), state) is False
    assert state.online_since == {"front": 123}


def test_restore_rejects_future_schema_without_partial_mutation():
    state = _state(online_since={"front": 123})
    with pytest.raises(ValueError):
        health.restore(state, {"version": 2, "state": {"online_since": {"front": 999}}})
    assert state.online_since == {"front": 123}


def test_snapshot_excludes_high_frequency_and_secret_fields():
    state = _state()
    state.last_success = {"front": 100}
    state.telegram_token = "secret"

    data = health.snapshot(state)

    assert "last_success" not in data
    assert "telegram_token" not in data
