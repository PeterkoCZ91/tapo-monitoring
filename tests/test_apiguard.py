"""The camera API deny-list: denied methods never leave the client."""

import sys
import types

import pytest

from tapo_monitor import apiguard, camera, capabilities, hubclient


class FakeTapo:
    """Mimics pytapo: every public call funnels into ``performRequest``."""

    def __init__(self, *args):
        self.sent = []

    def performRequest(self, requestData, loginRetryCount=0):
        self.sent.append(requestData)
        return {"error_code": 0, "result": {"responses": [
            {"method": r.get("method"), "result": {"ok": True}, "error_code": 0}
            for r in requestData.get("params", {}).get("requests", [])
        ]}}

    def executeFunction(self, method, params):
        envelope = {"method": "multipleRequest",
                    "params": {"requests": [{"method": method, "params": params}]}}
        return self.performRequest(envelope)["result"]["responses"][0]["result"]

    def getBasicInfo(self):
        return self.executeFunction("getDeviceInfo", {"device_info": {"name": ["basic_info"]}})

    def getInfLampCapability(self):  # a wrapper a future pytapo might add
        return self.executeFunction("getInfLampCapability", {})

    def getSomethingHarmless(self):  # a wrapper that reaches a denied method internally
        return self.executeFunction("checkDetectEventState", {})


@pytest.fixture
def guarded():
    return apiguard.guard_client(FakeTapo())


@pytest.mark.parametrize("method", sorted(apiguard.DENIED_METHODS))
def test_direct_call_is_refused_and_not_sent(guarded, method, caplog):
    with pytest.raises(apiguard.DeniedMethodError) as info:
        guarded.executeFunction(method, {})
    assert info.value.method == method
    assert guarded.sent == []
    assert method in caplog.text


def test_allowed_calls_pass_through(guarded):
    assert guarded.getBasicInfo() == {"ok": True}
    assert len(guarded.sent) == 1


def test_denied_method_inside_a_batch_blocks_the_whole_batch(guarded):
    batch = {"method": "multipleRequest", "params": {"requests": [
        {"method": "getDeviceInfo", "params": {}},
        {"method": "checkDetectEventState", "params": {}},
    ]}}
    with pytest.raises(apiguard.DeniedMethodError):
        guarded.performRequest(batch)
    with pytest.raises(apiguard.DeniedMethodError):
        guarded.executeFunction("multipleRequest", batch["params"])
    assert guarded.sent == []


def test_denied_method_inside_a_child_envelope_is_found():
    request = {"method": "multipleRequest", "params": {"requests": [{
        "method": "controlChild",
        "params": {"childControl": {"device_id": "x", "request_data": {
            "method": "multipleRequest",
            "params": {"requests": [{"method": "getInfLampCapability", "params": {}}]}}}},
    }]}}
    with pytest.raises(apiguard.DeniedMethodError):
        apiguard.check_request(request)


def test_explicit_allow_lets_one_method_through():
    client = apiguard.guard_client(FakeTapo(), allow=["getInfLampCapability"])
    assert client.getInfLampCapability() == {"ok": True}
    with pytest.raises(apiguard.DeniedMethodError):
        client.executeFunction("checkDetectEventState", {})


def test_guard_is_idempotent(guarded):
    wrapped = guarded.performRequest
    assert apiguard.guard_client(guarded).performRequest is wrapped


def test_tapo_factory_returns_a_guarded_client(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytapo", types.SimpleNamespace(Tapo=FakeTapo))
    client = camera.tapo_factory("cam.invalid", "u", "p")()
    with pytest.raises(apiguard.DeniedMethodError):
        client.executeFunction("checkDetectEventState", {})
    assert client.sent == []


def test_probe_skips_a_denied_method_without_calling_it():
    class Client:
        called = False

        def getInfLampCapability(self):
            Client.called = True

    assert capabilities._probe(Client(), "getInfLampCapability") == {
        "state": "unknown", "reason": "denied_method"}
    assert Client.called is False


def test_probe_reports_a_getter_refused_by_the_guard_as_skipped(guarded):
    assert capabilities._probe(guarded, "getSomethingHarmless") == {
        "state": "unknown", "reason": "denied_method"}
    assert guarded.sent == []


def test_no_probe_table_lists_a_denied_method():
    tables = (capabilities._SAFE_PROBES, capabilities._DUAL_LENS_PROBES,
              capabilities._PER_CHANNEL_PROBES)
    names = {row[2] for table in tables for row in table}
    assert not names & set(apiguard.DENIED_METHODS)


def test_hub_envelope_refuses_a_denied_method():
    with pytest.raises(apiguard.DeniedMethodError):
        hubclient.wrap("checkDetectEventState", {})
    assert hubclient.wrap("getDeviceInfo", {})["method"] == "multipleRequest"
