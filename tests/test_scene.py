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



def test_scene_event_reports_measured_direction_and_delta():
    coordinator = SceneCoordinator()
    coordinator.record_delivery("overlap-group", "source", "person", {"start_time": 100}, 101)
    coordinator.record_delivery("overlap-group", "destination", "person", {"start_time": 106}, 107)

    event = coordinator.scene_event(
        "overlap-group", 104, window=10, camera_order=("source", "destination")
    )

    assert event.lead_camera == "source"
    assert event.follow_camera == "destination"
    assert event.delta_seconds == 6
    assert event.direction == "forward"
    assert event.cameras == ("source", "destination")


def test_scene_event_does_not_guess_direction_without_measured_order():
    coordinator = SceneCoordinator()
    coordinator.record_delivery("g", "camera-a", "person", {"start_time": 100}, 100)
    coordinator.record_delivery("g", "camera-b", "person", {"start_time": 101}, 101)

    assert coordinator.scene_event("g", 100, window=2).direction is None


def test_scene_event_reverse_direction_is_explicit():
    coordinator = SceneCoordinator()
    coordinator.record_delivery("g", "destination", "person", {"start_time": 100}, 100)
    coordinator.record_delivery("g", "source", "person", {"start_time": 103}, 103)
    event = coordinator.scene_event("g", 101, window=5, camera_order=("source", "destination"))
    assert event.direction == "reverse"



def test_choose_best_frame_uses_score_then_capture_time():
    early = {"camera": "source", "frame": "a", "score": 0.8, "captured_at": 100}
    late = {"camera": "destination", "frame": "b", "score": 0.8, "captured_at": 101}
    assert __import__("tapo_monitor.scene", fromlist=["choose_best_frame"]).choose_best_frame(
        [late, early, {"score": "nan"}]
    ) is early


def test_estimate_clock_offset_uses_median_and_rejects_empty():
    from tapo_monitor.scene import estimate_clock_offset
    assert estimate_clock_offset([(100, 102), (200, 203), (300, 302)]) == 2
    assert estimate_clock_offset([("bad", 1), (float("nan"), 2)]) is None

def test_offline_scene_pipeline_round_trip(tmp_path):
    from tapo_monitor.ledger import EventLedger
    from tapo_monitor.scene import choose_best_frame, estimate_clock_offset

    offset = estimate_clock_offset([(1000.0, 1003.0), (1010.0, 1013.0)])
    coordinator = SceneCoordinator()
    coordinator.record_delivery("yard", "source", "person", {"start_time": 1000}, 1000)
    coordinator.record_delivery(
        "yard", "destination", "person", {"start_time": 1006 - offset}, 1006
    )
    event = coordinator.scene_event(
        "yard", 1003, window=10, camera_order=("source", "destination")
    )
    assert event is not None
    best = choose_best_frame([
        {"camera": "source", "frame": "a", "score": 0.6, "captured_at": 1000},
        {"camera": "destination", "frame": "b", "score": 0.9, "captured_at": 1006},
    ])
    assert best["frame"] == "b"

    ledger = EventLedger(tmp_path / "events.sqlite3")
    assert ledger.record_scene_event(
        group=event.group, event_at=event.event_at, lead_camera=event.lead_camera,
        follow_camera=event.follow_camera, delta_seconds=event.delta_seconds,
        direction=event.direction,
    ) == 1
    assert ledger.record_scene_event(
        group=event.group, event_at=event.event_at, lead_camera=event.lead_camera,
        follow_camera=event.follow_camera, delta_seconds=event.delta_seconds,
        direction=event.direction,
    ) == 1
    assert len(ledger.scene_events(start=0, end=2000, group="yard")) == 1
