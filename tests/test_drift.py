import json

import pytest

from tapo_monitor.drift import (
    UNKNOWN,
    UNSUPPORTED,
    aggregate_health,
    evaluate_drift,
    normalize_mapping,
)


def test_normalize_mapping_flattens_nested_and_sorts_paths():
    normalized = normalize_mapping({"video": {"quality": "3M"}, "alarm": True})

    assert normalized == {"alarm": True, "video.quality": "3M"}
    assert list(normalized) == ["alarm", "video.quality"]


def test_evaluate_drift_supports_nested_and_explicit_dotted_actual_paths():
    report = evaluate_drift(
        {"video.quality": "3M", "detection": {"person": True}},
        {"video": {"quality": "2M"}, "detection.person": True},
        scope="front",
    )

    assert [(item.path, item.status) for item in report.results] == [
        ("detection.person", "match"),
        ("video.quality", "drift"),
    ]
    assert report.clean is False
    assert [item.path for item in report.alertable] == ["video.quality"]


def test_expected_sets_are_compared_without_order_and_serialize_as_lists():
    report = evaluate_drift(
        {"tracking.categories": {"person", "vehicle"}},
        {"tracking": {"categories": ["vehicle", "person"]}},
    )

    assert report.clean is True
    assert report.results[0].status == "match"
    payload = report.to_dict()
    assert payload["results"][0]["expected"] == ["person", "vehicle"]
    json.dumps(payload)


def test_missing_unknown_and_unsupported_values_never_raise_false_alerts():
    report = evaluate_drift(
        {
            "missing": True,
            "unknown": True,
            "marked_unsupported": True,
            "capability_unsupported": True,
        },
        {"unknown": UNKNOWN, "marked_unsupported": UNSUPPORTED},
        supported={"capability_unsupported": False},
        severities={"missing": "critical", "marked_unsupported": "critical"},
    )

    assert {item.path: item.status for item in report.results} == {
        "capability_unsupported": "unsupported",
        "marked_unsupported": "unsupported",
        "missing": "unknown",
        "unknown": "unknown",
    }
    assert report.clean is True
    assert report.drifts == ()
    assert report.alertable == ()


def test_explicit_none_is_a_real_observation_not_unknown():
    report = evaluate_drift({"privacy.mode": False}, {"privacy": {"mode": None}})

    assert report.results[0].status == "drift"
    assert report.results[0].actual is None


def test_info_drift_is_reported_but_not_alertable():
    report = evaluate_drift(
        {"firmware.channel": "stable"},
        {"firmware.channel": "beta"},
        severities={"firmware.channel": "info"},
    )

    assert len(report.drifts) == 1
    assert report.alertable == ()


def test_drift_key_is_stable_across_values_and_severity_but_scoped():
    first = evaluate_drift({"alarm": True}, {"alarm": False}, scope="one")
    changed = evaluate_drift(
        {"alarm": True}, {"alarm": None}, severities={"alarm": "critical"}, scope="one"
    )
    other_camera = evaluate_drift({"alarm": True}, {"alarm": False}, scope="two")

    assert first.results[0].key == changed.results[0].key
    assert first.results[0].key != other_camera.results[0].key


def test_report_dictionary_has_counts_and_is_json_serializable():
    report = evaluate_drift(
        {"a": 1, "b": 2, "c": 3, "d": 4},
        {"a": 1, "b": 9, "c": UNKNOWN, "d": UNSUPPORTED},
        severities={"b": "critical"},
        scope="test-camera",
    )

    payload = report.to_dict()
    assert payload["counts"] == {"drift": 1, "match": 1, "unknown": 1, "unsupported": 1}
    assert payload["results"][1]["severity"] == "critical"
    assert payload["results"][1]["alertable"] is True
    json.dumps(payload)


@pytest.mark.parametrize(
    ("layers", "expected", "causes"),
    [
        ({"network": "down"}, "down", ("network",)),
        ({"network": "ok", "api": "down", "rtsp": "down"}, "down", ("api", "rtsp")),
        ({"network": "ok", "api": "down", "rtsp": "ok"}, "degraded", ("api",)),
        ({"network": "ok", "storage": "down"}, "degraded", ("storage",)),
        ({"network": "ok", "events": "degraded"}, "degraded", ("events",)),
        ({"network": "ok"}, "unknown", ("api", "events", "rtsp", "storage")),
        (
            {name: "ok" for name in ("network", "api", "events", "rtsp", "storage")},
            "ok",
            (),
        ),
    ],
)
def test_layered_health_precedence(layers, expected, causes):
    health = aggregate_health(layers)

    assert health.status == expected
    assert health.causes == causes
    json.dumps(health.to_dict())


def test_known_problem_takes_precedence_over_unknown_network():
    health = aggregate_health({"network": "unknown", "storage": "down"})

    assert health.status == "degraded"
    assert health.causes == ("storage",)


def test_health_rejects_unknown_layer_and_invalid_status():
    with pytest.raises(ValueError, match="unknown health layers"):
        aggregate_health({"motor": "down"})
    with pytest.raises(ValueError, match="invalid health status"):
        aggregate_health({"network": "offline"})


def test_invalid_drift_inputs_fail_loudly():
    with pytest.raises(ValueError, match="severity"):
        evaluate_drift({"alarm": True}, {"alarm": False}, severities={"alarm": "urgent"})
    with pytest.raises(ValueError, match="scope"):
        evaluate_drift({"alarm": True}, {"alarm": False}, scope="")
    with pytest.raises(ValueError, match="keys"):
        normalize_mapping({"": True})
