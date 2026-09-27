"""Contract over every accepted camera report in ``tests/fixtures/cameras/``.

Each ``*.json`` there is a ``tapo-monitor report`` file somebody shared (see
docs/camera-reports.md). Adding one makes it a regression test: it must keep the known
schema, pass the leak self-check, be exactly what the current sanitizer writes (so a
narrowed allow-list or a new leak shows up as a diff), decode through the real event
normalizer and classifier, and summarize offline.
"""

import json
import re
from pathlib import Path

import pytest

from tapo_monitor import detection, report

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "cameras").glob("*.json"))

TOP_KEYS = {"schema", "tool", "camera", "getters", "values", "events", "watch", "rtsp",
            "note", "redacted_keys"}
CAMERA_KEYS = {"model", "hw_version", "fw_version", "device_type", "device_info",
               "lens_channels", "event_profile"}
GETTER_KEYS = set(report._GETTER_ROW_KEYS)
STATES = {"available", "error", "unknown"}
EVENT_KEYS = {"age_s", "duration_s", "alarm_type", "events_1", "event_type", "chn_events",
              "seen_at_s"}
CLASSES = {*detection.EVENT_TYPES, ""}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _entries(data):
    events = data.get("events") or {}
    watch = data.get("watch") or {}
    return list(events.get("recent") or []) + list(watch.get("events") or [])


def _raw_event(entry, now=1_800_000_000):
    """A getEvents entry rebuilt from a report row, in the camera's own shape."""
    start = now - (entry.get("age_s") or 0)
    raw = {"start_time": start, "end_time": start + (entry.get("duration_s") or 0),
           "alarm_type": entry.get("alarm_type")}
    if entry.get("event_type") is not None:
        raw["event_type"] = entry["event_type"]
    if "events_1" in entry:
        raw["events_1"] = entry["events_1"]
    if "chn_events" in entry:
        raw["chn_events"] = {
            ch: {"events_1": lens.get("events_1"),
                 "event_start_time": start + lens.get("start_offset_s", 0)}
            for ch, lens in entry["chn_events"].items()}
    return raw


def test_there_is_at_least_one_accepted_report():
    assert FIXTURES


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_keeps_the_known_schema(path):
    data = _load(path)
    assert data["schema"] == report.SCHEMA
    assert set(data) == TOP_KEYS
    assert set(data["camera"]) == CAMERA_KEYS
    assert data["camera"]["model"], "a report without a model cannot be accepted"
    assert all(isinstance(c, int) for c in data["camera"]["lens_channels"])
    assert data["note"] == report.REVIEW_NOTE
    assert data["redacted_keys"] == sorted(set(data["redacted_keys"]))
    available = set()
    for row in data["getters"]:
        assert set(row) <= GETTER_KEYS and {"method", "group", "name", "state"} <= set(row)
        assert row["state"] in STATES
        if row["state"] == "available":
            available.add((row["group"], row["name"]))
        if row["state"] == "error" and "error_code" in row:
            assert isinstance(row["error_code"], int)
    for group, names in data["values"].items():
        for name in names:
            assert (group, name) in available, f"values.{group}.{name} has no getter row"
    for entry in _entries(data):
        assert set(entry) <= EVENT_KEYS


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_file_is_named_model_and_firmware(path):
    cam = _load(path)["camera"]
    fw = str(cam["fw_version"] or "").split()[0]
    stem = path.stem
    assert re.fullmatch(r"[a-z0-9]+-[0-9][0-9a-z.]*(?:-[a-z0-9]+)?", stem), stem
    assert stem.startswith(f"{cam['model'].lower()}-{fw}"), stem


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_passes_the_leak_self_check(path):
    text = path.read_text(encoding="utf-8")
    assert report.self_check(text) == []
    report.render(_load(path))      # raises SelfCheckError on a hit


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_is_what_the_current_sanitizer_writes(path):
    data = _load(path)
    assert report.resanitize(data) == data


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_events_go_through_the_real_normalizer_and_classifier(path):
    data = _load(path)
    profile = detection.event_profile(data["camera"]["event_profile"])
    for entry in _entries(data):
        raw = _raw_event(entry)
        event = detection.normalize_event(raw, profile)
        masks = [lens.get("events_1") or 0 for lens in (entry.get("chn_events") or {}).values()]
        if "events_1" in entry:
            assert event["events_1"] == entry["events_1"]
        elif masks:
            expected = 0
            for mask in masks:
                expected |= mask
            assert event["events_1"] == expected
            assert detection.event_channels(event) == sorted(int(c) for c in
                                                             entry["chn_events"])
        flags = detection.event_flags(event)
        assert flags["raw"] == (event.get("events_1") or 0)
        kind = detection.classify_getevent(
            event.get("event_type"), events_1=event.get("events_1"),
            profile=profile, alarm_type=event.get("alarm_type"))
        assert kind in CLASSES


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_summarizes_offline(path, capsys):
    assert report.main(["--summarize", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"model {_load(path)['camera']['model']}" in out


# What the accepted reports pin beyond the shared contract.

def _fixture(name):
    return _load(Path(__file__).parent / "fixtures" / "cameras" / name)


def _classes(data):
    profile = detection.event_profile(data["camera"]["event_profile"])
    out = []
    for entry in _entries(data):
        event = detection.normalize_event(_raw_event(entry), profile)
        out.append(detection.classify_getevent(
            None, events_1=event.get("events_1"), profile=profile,
            alarm_type=event.get("alarm_type")))
    return out


def test_c545d_report_decodes_a_person_on_both_lenses():
    data = _fixture("c545d-1.1.7.json")
    assert data["camera"]["event_profile"] == "c545d"
    assert data["camera"]["lens_channels"] == [1, 2]
    assert _classes(data).count("person") == 1
    assert "unknown events_1 bits: none" in report.summarize(data)


def test_c260_report_uses_the_default_table():
    data = _fixture("c260-1.2.3.json")
    assert data["camera"]["event_profile"] is None
    assert data["camera"]["lens_channels"] == []
    assert _classes(data) == ["motion"]
    assert "unknown events_1 bits: none" in report.summarize(data)
