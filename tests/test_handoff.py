import pytest

from tapo_monitor.handoff import HandoffManager


def test_handoff_expires_and_returns_restore_lease():
    manager = HandoffManager()
    lease = manager.begin(group="yard", source_camera="a", target_camera="b",
                          preset="handoff", now=100, duration=30,
                          previous_policy="day")
    assert manager.active("yard", 129) == lease
    assert manager.active("yard", 130) is None
    assert manager.expire(130) == [lease]
    assert manager.expire(131) == []


def test_new_handoff_replaces_group_lease():
    manager = HandoffManager()
    manager.begin(group="yard", source_camera="a", target_camera="b",
                  preset="one", now=100, duration=30)
    current = manager.begin(group="yard", source_camera="b", target_camera="a",
                            preset="two", now=110, duration=10)
    assert manager.active("yard", 115) == current
    assert manager.expire(140) == [current]


@pytest.mark.parametrize("kwargs", [
    {"source_camera": "a", "target_camera": "a"},
    {"source_camera": "", "target_camera": "b"},
])
def test_handoff_rejects_invalid_camera_pair(kwargs):
    manager = HandoffManager()
    base = {"group": "yard", "preset": "handoff", "now": 0, "duration": 1,
            "source_camera": "a", "target_camera": "b"}
    base.update(kwargs)
    with pytest.raises(ValueError):
        manager.begin(**base)
