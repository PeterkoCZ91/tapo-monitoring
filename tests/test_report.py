import json

import pytest

from tapo_monitor import apiguard, capabilities, report

NOW = 1_790_400_000
HOST = "192.0.2.44"


def _pytapo_error(code):
    # The shape pytapo raises: the message text plus the camera's JSON answer.
    return Exception(f"Error: Method not supported, Response: "
                     f'{{"error_code": {code}, "result": {{}}}}')


class FakeCamera:
    """Answers like a single-lens C560WS, with identifiers sprinkled everywhere."""

    def __init__(self, events=None):
        self.calls = []
        self.events = events if events is not None else [
            {"start_time": NOW - 300, "end_time": NOW - 280, "alarm_type": 2,
             "events_1": 524290, "face_id": "f00dfacecafe1234", "startRelative": 300,
             "endRelative": 280},
            {"start_time": NOW - 100, "end_time": NOW - 90, "alarm_type": 8,
             "events_1": 130},
        ]

    def __getattr__(self, name):
        if name.startswith("get"):
            def missing(**kwargs):
                self.calls.append(name)
                raise _pytapo_error(-40210)
            return missing
        raise AttributeError(name)

    def getBasicInfo(self):
        self.calls.append("getBasicInfo")
        return {"device_info": {"basic_info": {
            "device_model": "C560WS", "hw_version": "1.0",
            "sw_version": "1.2.3 Build 250101 Rel.1234n", "device_type": "SMART.IPCAMERA",
            "device_info": "C560WS 1.0 IPC", "device_alias": "Front gate",
            "dev_id": "0123456789ABCDEF0123456789ABCDEF01234567",
            "mac": "AA-BB-CC-DD-EE-FF", "hw_id": "ABCDEF0123456789", "oem_id": "FEDCBA98",
            "ssid": "HomeNetwork", "latitude": 501234, "longitude": 141234,
            "timezone": "UTC+01:00", "zone_id": "Europe/Somewhere",
            "features": "3", "barcode": "ZZ-1", "ip": HOST, "owner": "someone",
        }}}

    def getTimeCorrection(self):
        self.calls.append("getTimeCorrection")
        return 3600

    def getMotionDetection(self, **kwargs):
        self.calls.append("getMotionDetection")
        return {"enabled": "on", "sensitivity": "high", "digital_sensitivity": "80",
                "note": "owner@example.com"}

    def getSDCard(self):
        self.calls.append("getSDCard")
        return {"harddisk_manage": {"hd_info": [{"hd_info_1": {
            "status": "normal", "detect_status": "normal", "total_space": "59.4GB",
            "record_start_time": "1790000000", "disk_name": "1"}}]}}

    def getOsd(self):
        self.calls.append("getOsd")
        return {"OSD": {"label_info_1": {"enabled": "on", "text": "Back yard"}}}

    def getAllChnInfo(self):
        self.calls.append("getAllChnInfo")
        raise _pytapo_error(-40106)

    def getEvents(self, startTime=None, endTime=None):
        self.calls.append("getEvents")
        return [dict(e) for e in self.events]

    # Anything that writes must never be reached by a report.
    def setMotionDetection(self, *a, **k):
        raise AssertionError("report must not call setters")


def _build(camera=None, **kwargs):
    return report.build_report(camera or FakeCamera(), now=NOW, **kwargs)


def test_report_keeps_model_and_settings_and_withholds_identity():
    built = _build()
    text = report.render(built, literals=(HOST, "camuser"))
    data = json.loads(text)

    assert data["schema"] == report.SCHEMA
    assert data["camera"]["model"] == "C560WS"
    assert data["camera"]["fw_version"] == "1.2.3 Build 250101 Rel.1234n"
    assert data["camera"]["lens_channels"] == []
    assert data["values"]["detection"]["motion"]["sensitivity"] == "high"
    for secret in ("Front gate", "HomeNetwork", "AA-BB-CC-DD-EE-FF", "0123456789ABCDEF",
                   "FEDCBA98", "Europe/Somewhere", "UTC+01:00", "501234", "141234",
                   "owner@example.com", "Back yard", "1790000000", "someone",
                   "f00dfacecafe1234", HOST):
        assert secret not in text, secret
    info = "values.basic.info.device_info.basic_info"
    for key in ("device_alias", "dev_id", "mac", "ssid", "latitude", "timezone", "zone_id",
                "features", "ip", "owner"):
        assert f"{info}.{key}" in data["redacted_keys"]
    assert "values.detection.motion.note" in data["redacted_keys"]
    assert "values.basic.clock_correction" in data["redacted_keys"]
    assert "events.recent[].face_id" in data["redacted_keys"]
    assert "review" in data["note"].lower() and "nothing is uploaded" in data["note"]


def test_getters_record_state_and_camera_error_code():
    built = _build()
    rows = {row["method"]: row for row in built["getters"] if row["method"]}
    assert rows["getMotionDetection"]["state"] == "available"
    assert rows["getPetDetection"] == {
        "method": "getPetDetection", "group": "detection", "name": "pet",
        "state": "error", "error_type": "Exception", "error_code": -40210}
    # Single lens: no per-lens reads at all.
    assert not any(row["group"] == "detection_chn" for row in built["getters"])


def test_events_are_relative_and_raw_bits_survive():
    events = _build()["events"]
    assert events["total"] == 2
    first, second = events["recent"]
    assert first == {"age_s": 300, "duration_s": 20, "alarm_type": 2, "events_1": 524290}
    assert second["events_1"] == 130 and second["alarm_type"] == 8


def test_dual_lens_camera_gets_per_lens_reads():
    class DualLens(FakeCamera):
        def getBasicInfo(self):
            self.calls.append("getBasicInfo")
            return {"device_info": {"basic_info": {"device_model": "C545D"}}}

        def getAllChnInfo(self):
            self.calls.append("getAllChnInfo")
            return {"system": {"chn_info": [
                {"chn_id": "1", "chn_alias": "Fixed Lens", "chn_dev_id": "abc"},
                {"chn_id": "2", "chn_alias": "PT Lens", "chn_dev_id": "def"}]}}

        def getPersonDetection(self, chn_id=None):
            self.calls.append(("getPersonDetection", chn_id))
            if chn_id:
                return {str(c): {"enabled": "on", "sensitivity": "60"} for c in chn_id}
            return {"enabled": "on", "sensitivity": "60"}

    camera = DualLens(events=[{"start_time": NOW - 50, "end_time": NOW - 10,
                               "alarm_type": 6, "chn_events": {
                                   "2": {"events_1": 34, "event_start_time": NOW - 50},
                                   "1": {"events_1": 34, "event_start_time": NOW - 48}}}])
    built = _build(camera)
    assert built["camera"]["lens_channels"] == [1, 2]
    assert built["camera"]["event_profile"] == "c545d"
    assert ("getPersonDetection", [1, 2]) in camera.calls
    assert built["values"]["detection_chn"]["person"]["2"]["enabled"] == "on"
    assert "Fixed Lens" not in report.render(built)
    lens = built["events"]["recent"][0]["chn_events"]
    assert lens == {"2": {"events_1": 34, "start_offset_s": 0},
                    "1": {"events_1": 34, "start_offset_s": 2}}


def test_only_read_only_getters_are_sent_and_denied_methods_never():
    camera = FakeCamera()
    _build(camera)
    names = [c if isinstance(c, str) else c[0] for c in camera.calls]
    assert names and all(n.startswith("get") for n in names)
    assert not set(names) & set(apiguard.DENIED_METHODS)
    assert "getModuleSpec" not in names and "getMotorCapability" not in names


@pytest.mark.parametrize("leak,kind", [
    ("192.0.2.7", "IPv4 address"),
    ("aa:bb:cc:dd:ee:ff", "MAC address"),
    ("a1b2c3d4e5f6a7b8", "long hex identifier"),
    ("someone@example.org", "e-mail address"),
])
def test_self_check_refuses_to_render_a_leak(leak, kind):
    built = _build()
    built["camera"]["model"] = leak   # simulate an anonymizer bug
    with pytest.raises(report.SelfCheckError) as err:
        report.render(built)
    assert kind in err.value.findings
    assert leak not in str(err.value)


def test_self_check_refuses_typed_host_name():
    built = _build()
    built["camera"]["device_info"] = "cam-garage.lan"
    with pytest.raises(report.SelfCheckError):
        report.render(built, literals=("cam-garage.lan", "user"))


def test_allowed_key_with_identifier_like_value_is_still_withheld():
    anon = report.Anonymizer()
    clean = anon.clean({"status": "192.0.2.9", "enabled": True, "sensitivity": 60,
                        "wifi": {"enabled": "on"}}, "x")
    assert clean == {"status": "<redacted>", "enabled": True, "sensitivity": 60,
                     "wifi": "<redacted>"}
    assert anon.redacted == {"x.status", "x.wifi"}


def test_watch_records_new_and_changed_events_only():
    clock_now = {"t": NOW}

    def clock():
        return clock_now["t"]

    def sleep(seconds):
        clock_now["t"] += seconds

    polls = [
        [],
        [{"start_time": NOW + 3, "end_time": NOW + 8, "alarm_type": 2, "events_1": 2}],
        [{"start_time": NOW + 3, "end_time": NOW + 8, "alarm_type": 2, "events_1": 2}],
        [{"start_time": NOW + 3, "end_time": NOW + 14, "alarm_type": 2, "events_1": 2}],
    ]

    class Watched:
        calls = 0

        def getEvents(self, startTime=None, endTime=None):
            assert endTime - startTime <= report.WATCH_WINDOW + 60
            answer = polls[min(Watched.calls, len(polls) - 1)]
            Watched.calls += 1
            return [dict(e) for e in answer]

    result = report.watch_events(Watched(), 20, interval=5, clock=clock, sleep=sleep)
    assert result["polls"] == Watched.calls == 5
    assert [e["duration_s"] for e in result["events"]] == [5, 11]
    assert [e["seen_at_s"] for e in result["events"]] == [5, 15]
    assert result["stopped_by"] == "time"


def test_watch_interval_has_a_floor_and_stops_on_repeated_errors():
    now = {"t": NOW}
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    class Locked:
        def getEvents(self, startTime=None, endTime=None):
            raise _pytapo_error(-40214)

    result = report.watch_events(Locked(), 600, interval=0.1, clock=lambda: now["t"],
                                 sleep=sleep)
    assert set(slept) == {report.WATCH_MIN_INTERVAL}
    assert result["stopped_by"] == "errors"
    assert len(result["errors"]) == report.WATCH_MAX_ERRORS
    assert result["errors"][0]["error_code"] == -40214


def test_error_code_parsing():
    assert capabilities.error_code(_pytapo_error(-40106)) == -40106
    assert capabilities.error_code(Exception("Error: -40214")) == -40214
    assert capabilities.error_code(Exception("Failed to get correct camera time.")) is None
    assert capabilities.error_code(Exception("version 1.2.3-45678")) is None


def test_rtsp_probe_keeps_codec_and_geometry_only():
    seen = []

    class Done:
        returncode = 0
        stdout = json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264",
                                          "width": 2560, "height": 1440,
                                          "tags": {"title": "secret"}}]})

    def run(argv, **kwargs):
        seen.append(argv)
        return Done()

    result = report.probe_rtsp("192.0.2.5", "u", "p", run=run, which=lambda _n: "/x")
    assert result["stream1"]["tracks"] == [{"codec_type": "video", "codec_name": "h264",
                                            "width": 2560, "height": 1440}]
    assert "secret" not in json.dumps(result) and "192.0.2.5" not in json.dumps(result)
    assert report.probe_rtsp("h", "u", "p", which=lambda _n: None)["state"] == "skipped"


def test_summarize_lists_getters_unknown_bits_and_profile(tmp_path, capsys):
    built = _build()
    built["watch"] = {"events": [{"alarm_type": 9, "events_1": 256 | 8}]}
    path = tmp_path / "r.json"
    path.write_text(report.render(built))
    assert report.main(["--summarize", str(path)]) == 0
    out = capsys.readouterr().out
    assert "model C560WS" in out
    assert "getMotionDetection" in out
    assert "getPetDetection (-40210)" in out
    assert "none matches" in out
    # 130 = bits 1 + 7, 264 = bits 3 + 8 (8 is linecrossing in the default table).
    assert "unknown events_1 bits: 3 (value 8), 7 (value 128)" in out


def test_summarize_matches_event_profile():
    built = _build()
    built["camera"]["model"] = "C545D"
    built["events"]["recent"] = [{"alarm_type": 6, "chn_events": {"1": {"events_1": 34}}}]
    lines = report.summarize(built)
    assert "event profile: c545d" in lines
    assert "unknown events_1 bits: none" in lines


def test_summarize_rejects_other_files():
    with pytest.raises(ValueError):
        report.summarize({"schema_version": 1})


def _cli(tmp_path, camera, *extra, confirm=lambda: True):
    out = tmp_path / "report.json"
    made = []

    def factory_for(host, user, password):
        made.append((host, user, password))
        return lambda: camera

    def connect(factory, retries):
        assert retries == 1
        return factory(), None

    code = report.main(["--host", HOST, "--out", str(out), *extra],
                       factory_for=factory_for, connect=connect,
                       credentials=lambda: ("camuser", "hunter22"), confirm=confirm,
                       clock=lambda: NOW, sleep=lambda _s: None,
                       rtsp_probe=lambda *a: {"state": "skipped"})
    return code, out, made


def test_cli_writes_report_with_one_session(tmp_path, capsys):
    code, out, made = _cli(tmp_path, FakeCamera())
    assert code == 0
    assert made == [(HOST, "camuser", "hunter22")]
    text = out.read_text()
    assert "hunter22" not in text and "camuser" not in text and HOST not in text
    err = capsys.readouterr().err
    assert "OWN authenticated session" in err and "-40214" in err
    assert "nothing is uploaded" in err


def test_cli_aborts_without_confirmation(tmp_path):
    code, out, made = _cli(tmp_path, FakeCamera(), confirm=lambda: False)
    assert code == 1 and not out.exists() and made == []


def test_cli_refuses_to_write_when_self_check_hits(tmp_path, monkeypatch, capsys):
    original = report.build_report

    def leaky(*args, **kwargs):
        built = original(*args, **kwargs)
        built["camera"]["model"] = "aa:bb:cc:dd:ee:ff"
        return built

    monkeypatch.setattr(report, "build_report", leaky)
    code, out, _made = _cli(tmp_path, FakeCamera(), "--yes")
    assert code == 3 and not out.exists()
    assert "aa:bb" not in capsys.readouterr().err


def test_cli_dispatches_report(monkeypatch):
    from tapo_monitor import cli

    seen = []
    monkeypatch.setattr(report, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["report", "--summarize", "x.json"]) == 0
    assert seen == [["--summarize", "x.json"]]


def test_credentials_come_from_env_or_prompt_never_argv():
    assert report._credentials(env={"TAPO_USER": "a", "TAPO_PASSWORD": "b"},
                               prompt=lambda _p: pytest.fail("prompted"),
                               ask=lambda _p: pytest.fail("asked")) == ("a", "b")
    assert report._credentials(env={}, prompt=lambda _p: "pw",
                               ask=lambda _p: "me ") == ("me", "pw")
