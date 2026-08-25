from tapo_monitor.scene import SceneCoordinator


def test_unconfigured_group_is_passthrough():
    coordinator = SceneCoordinator()

    assert coordinator.allows(None, "camera-a", "person", {"start_time": 100}, 100)


def test_delivered_event_suppresses_same_type_on_other_camera():
    coordinator = SceneCoordinator()
    event = {"start_time": 100}

    assert coordinator.allows("overlap-group", "camera-a", "person", event, 100, window=15)
    coordinator.record_delivery("overlap-group", "camera-a", "person", event, 100, window=15)

    assert not coordinator.allows(
        "overlap-group", "camera-b", "person", {"start_time": 108}, 108, window=15
    )


def test_motion_is_upgraded_by_a_confirmed_event():
    coordinator = SceneCoordinator()
    coordinator.record_delivery(
        "overlap-group", "camera-a", "motion", {"start_time": 100}, 100, window=15
    )

    assert coordinator.allows(
        "overlap-group", "camera-b", "person", {"start_time": 105}, 105, window=15
    )
    coordinator.record_delivery(
        "overlap-group", "camera-b", "person", {"start_time": 105}, 105, window=15
    )
    assert not coordinator.allows(
        "overlap-group", "camera-a", "motion", {"start_time": 110}, 110, window=15
    )


def test_different_event_type_is_not_suppressed():
    coordinator = SceneCoordinator()
    coordinator.record_delivery(
        "overlap-group", "camera-a", "person", {"start_time": 100}, 100, window=15
    )

    assert coordinator.allows(
        "overlap-group", "camera-b", "vehicle", {"start_time": 105}, 105, window=15
    )


def test_outside_window_and_same_camera_are_allowed():
    coordinator = SceneCoordinator()
    coordinator.record_delivery(
        "overlap-group", "camera-a", "person", {"start_time": 100}, 100, window=15
    )

    assert coordinator.allows(
        "overlap-group", "camera-b", "person", {"start_time": 116}, 116, window=15
    )
    assert coordinator.allows(
        "overlap-group", "camera-a", "person", {"start_time": 108}, 108, window=15
    )


def test_missing_event_time_is_passthrough():
    coordinator = SceneCoordinator()
    coordinator.record_delivery(
        "overlap-group", "camera-a", "person", {"start_time": 100}, 100, window=15
    )

    assert coordinator.allows("overlap-group", "camera-b", "person", {}, 108, window=15)
