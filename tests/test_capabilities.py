import json

from tapo_monitor import capabilities


class PartialCamera:
    def __init__(self):
        self.calls = []

    def getBasicInfo(self):
        self.calls.append("basic")
        return {"model": "C560WS", "device_id": "private-device", "fw_ver": "1.2.3"}

    def getSDCard(self):
        self.calls.append("storage")
        return {"status": "normal", "total_space": 64_000, "free_space": 1_000}

    def getMotionDetection(self):
        self.calls.append("motion")
        return {}  # Valid wrapper, but feature/config unavailable on this firmware.

    def getModuleSpec(self):
        raise AssertionError("unsafe raw-transport wrapper must never be called")


def test_collect_snapshot_supports_partial_camera_without_raw_transport():
    camera = PartialCamera()
    twin = capabilities.collect_snapshot(camera)

    assert twin["schema_version"] == 1
    assert twin["groups"]["basic"]["info"]["state"] == "available"
    assert twin["groups"]["storage"]["sd_card"]["state"] == "available"
    assert twin["groups"]["detection"]["motion"] == {
        "state": "unknown", "reason": "empty_response"
    }
    assert twin["groups"]["detection"]["person"] == {
        "state": "unknown", "reason": "missing_method"
    }
    assert twin["groups"]["module"]["spec"] == {
        "state": "unknown", "reason": "raw_transport_disabled"
    }
    assert camera.calls == ["basic", "storage", "motion"]
    json.dumps(twin, allow_nan=False)


class BrokenCamera:
    def getBasicInfo(self):
        raise RuntimeError("token=secret user@example.test at 192.0.2.1")

    def getVideoCapability(self):
        return {"codec": "h265"}


def test_probe_exceptions_are_isolated_and_messages_are_not_exposed():
    twin = capabilities.collect_snapshot(BrokenCamera())

    assert twin["groups"]["basic"]["info"] == {
        "state": "error", "error_type": "RuntimeError"
    }
    assert twin["groups"]["video"]["capability"] == {
        "state": "available", "value": {"codec": "h265"}
    }
    rendered = json.dumps(twin)
    assert "secret" not in rendered
    assert "example.test" not in rendered
    assert "192.0.2.1" not in rendered


def test_redaction_is_recursive_and_json_safe():
    source = {
        "model": "C260",
        "mac": "aa:bb:cc:dd:ee:ff",
        "nested": {
            "face_id": 123456789,
            "device_id": "abc",
            "owner_email": "owner@example.test",
            "endpoint": "rtsp://user:pass@192.0.2.8/stream1",
            "release_url": "https://updates.example.test/fw.bin?token=sensitive",
            "status": "normal",
        },
        "values": (1, float("nan"), b"secret bytes"),
    }

    clean = capabilities.redact(source)

    assert clean["mac"] == capabilities.REDACTED
    assert clean["nested"]["face_id"] == capabilities.REDACTED
    assert clean["nested"]["device_id"] == capabilities.REDACTED
    assert clean["nested"]["owner_email"] == capabilities.REDACTED
    assert clean["nested"]["endpoint"] == capabilities.REDACTED
    assert clean["nested"]["release_url"] == "https://updates.example.test/fw.bin"
    assert clean["nested"]["status"] == "normal"
    assert clean["values"] == [1, None, "<binary>"]
    json.dumps(clean, allow_nan=False)


def test_derive_health_keeps_layers_independent():
    twin = capabilities.collect_snapshot(PartialCamera())

    assert capabilities.derive_health(twin, network=True, events=False, rtsp=None) == {
        "network": "ok",
        "api": "ok",
        "events": "degraded",
        "rtsp": "unknown",
        "storage": "ok",
    }


def test_derive_health_distinguishes_hard_transport_failure_from_stale_events():
    twin = capabilities.collect_snapshot(PartialCamera())

    health = capabilities.derive_health(twin, network=False, events=False, rtsp=False)

    assert health["network"] == "down"
    assert health["rtsp"] == "down"
    assert health["events"] == "degraded"


def test_derive_health_marks_storage_error_without_poisoning_api():
    twin = {
        "schema_version": 1,
        "groups": {
            "basic": {"info": {"state": "available", "value": {"model": "camera"}}},
            "storage": {"sd_card": {"state": "available", "value": {"status": "fault"}}},
        },
    }

    health = capabilities.derive_health(twin)
    assert health["api"] == "ok"
    assert health["storage"] == "degraded"
    assert health["network"] == health["events"] == health["rtsp"] == "unknown"
