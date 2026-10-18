import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tapo_monitor.sessionguard import (
    SessionRetiredError,
    native_tracking_off,
    protect_client,
    retire_client,
)


class Client:
    def __init__(self, lock=True):
        if lock:
            self.asyncHandler = SimpleNamespace(_lock=threading.RLock())
        self.requests = []
        self.enabled = "on"

    def performRequest(self, request):
        self.requests.append(request)

    def setAutoTrackTarget(self, enabled):
        self.performRequest(
            {
                "method": "setTargetTrackConfig",
                "params": {"target_track": {"target_track_info": {"enabled": "on" if enabled else "off"}}},
            }
        )
        self.enabled = "on" if enabled else "off"

    def getAutoTrackTarget(self):
        self.performRequest({"method": "getTargetTrackConfig"})
        return {"enabled": self.enabled}


@pytest.mark.parametrize("lock", [True, False])
def test_stale_nested_enable_is_rewritten_without_changing_caller(lock):
    client = Client(lock)
    allowed = [True]
    protect_client(client, lambda: allowed[0])
    request = {
        "method": "multipleRequest",
        "params": {
            "requests": [
                {
                    "method": "setTargetTrackConfig",
                    "params": {"target_track": {"target_track_info": {"enabled": "on"}}},
                },
                {
                    "method": "multipleRequest",
                    "params": {
                        "requests": [
                            {
                                "method": "setAutoTrackTarget",
                                "params": {"auto_track_target": {"enabled": "on"}},
                            }
                        ]
                    },
                },
                {"method": "setOther", "params": {"enabled": "on"}},
            ]
        },
    }
    before = deepcopy(request)
    allowed[0] = False
    client.performRequest(request)
    sent = client.requests[0]["params"]["requests"]
    assert sent[0]["params"]["target_track"]["target_track_info"]["enabled"] == "off"
    assert sent[1]["params"]["requests"][0]["params"]["auto_track_target"]["enabled"] == "off"
    assert sent[2]["params"]["enabled"] == "on"
    assert request == before


def test_existing_request_restriction_survives_and_rebinding_is_idempotent():
    client = Client()
    original = client.performRequest

    def restricted(request):
        if request["method"] == "denied":
            raise ValueError("Denied")
        return original(request)

    client.performRequest = restricted
    protect_client(client, lambda: True)
    wrapper = client.performRequest
    protect_client(client, lambda: False)
    assert client.performRequest is wrapper
    with pytest.raises(ValueError, match="Denied"):
        client.performRequest({"method": "denied"})


def test_authentication_and_send_share_transport_lock():
    client = Client()
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    events = []

    def compound(request):
        # Match pytapo: separately acquired transport lock for auth and send.
        with client.asyncHandler._lock:
            events.append((request, "auth"))
        if request == "first":
            entered.set()
            assert release.wait(3)
        with client.asyncHandler._lock:
            events.append((request, "send"))

    client.performRequest = compound
    protect_client(client, lambda: True)

    def second():
        second_started.set()
        client.performRequest("second")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.performRequest, "first")
        assert entered.wait(3)
        # The guard must retain the SAME transport lock between auth and send.
        acquired = client.asyncHandler._lock.acquire(blocking=False)
        if acquired:
            client.asyncHandler._lock.release()
        assert acquired is False
        other = pool.submit(second)
        assert second_started.wait(3)
        release.set()
        first.result(3)
        other.result(3)
    assert events == [("first", "auth"), ("first", "send"), ("second", "auth"), ("second", "send")]


def test_retirement_waits_for_verification_and_blocks_stale_callbacks():
    client = Client()
    entered = threading.Event()
    release = threading.Event()
    retiring = threading.Event()
    events = []
    original_get = client.getAutoTrackTarget

    def verify():
        events.append("verify")
        entered.set()
        assert release.wait(3)
        return original_get()

    client.getAutoTrackTarget = verify
    protect_client(client, lambda: False)

    def close():
        events.append("close")
        assert native_tracking_off(client) is False

    def retire():
        retiring.set()
        retire_client(client, close)

    with ThreadPoolExecutor(2) as pool:
        operation = pool.submit(native_tracking_off, client)
        assert entered.wait(3)
        retirement = pool.submit(retire)
        assert retiring.wait(3)
        release.set()
        assert operation.result(3) is True
        retirement.result(3)
    assert events == ["verify", "close"]
    count = len(client.requests)
    assert native_tracking_off(client) is False
    with pytest.raises(SessionRetiredError):
        client.performRequest({"method": "getTargetTrackConfig"})
    protect_client(client, lambda: True)
    retire_client(client, close)
    assert len(client.requests) == count
    assert events.count("close") == 1


def test_tracking_off_requires_confirmed_readback():
    client = Client()
    protect_client(client, lambda: False)
    client.getAutoTrackTarget = lambda: {"enabled": "on"}
    assert native_tracking_off(client) is False


class AsyncClient(Client):
    def __init__(self, authenticate):
        super().__init__()
        self.authenticate = authenticate
        self.transport = SimpleNamespace(send=self.send)

    async def send(self, request, retry=0):
        self.requests.append((request, retry))
        return "sent"

    def performRequest(self, request, retry=0):
        async def authenticate():
            self.authenticate()

        # Match pytapo's separate authentication and transport execution steps.
        with self.asyncHandler._lock:
            asyncio.run(authenticate())
        with self.asyncHandler._lock:
            return asyncio.run(self.transport.send(request, retry=retry))


def test_latch_during_authentication_rechecks_at_transport_send():
    allowed = [True]
    client = AsyncClient(lambda: allowed.__setitem__(0, False))
    request = {
        "method": "multipleRequest",
        "params": {
            "requests": [
                {
                    "method": "setTargetTrackConfig",
                    "params": {"target_track": {"target_track_info": {"enabled": "on"}}},
                }
            ]
        },
    }
    before = deepcopy(request)
    protect_client(client, lambda: allowed[0])
    transport_wrapper = client.transport.send
    protect_client(client, lambda: allowed[0])
    assert client.transport.send is transport_wrapper
    assert client.performRequest(request, retry=2) == "sent"
    sent, retry = client.requests[0]
    assert sent["params"]["requests"][0]["params"]["target_track"]["target_track_info"]["enabled"] == "off"
    assert retry == 2
    assert request == before


def test_retirement_during_authentication_prevents_transport_send():
    client = AsyncClient(lambda: retire_client(client, lambda: None))
    protect_client(client, lambda: True)
    with pytest.raises(SessionRetiredError):
        client.performRequest({"method": "getTargetTrackConfig"})
    assert not client.requests


def test_transport_guard_preserves_existing_request_denial():
    client = AsyncClient(lambda: pytest.fail("denied request must not authenticate"))
    original = client.performRequest

    def restricted(request):
        if request["method"] == "denied":
            raise ValueError("Denied")
        return original(request)

    client.performRequest = restricted
    protect_client(client, lambda: False)
    with pytest.raises(ValueError, match="Denied"):
        client.performRequest({"method": "denied"})
    assert not client.requests


def test_off_diagnostics_separate_lock_wait_from_request_time(monkeypatch):
    from tapo_monitor import sessionguard

    client = Client()
    protect_client(client, lambda: False)
    clock = iter([10.0, 12.0, 13.5, 15.0])
    monkeypatch.setattr(sessionguard.time, "monotonic", lambda: next(clock))
    diagnostics = {}
    assert native_tracking_off(client, diagnostics=diagnostics)
    assert diagnostics == {"result": "verified", "lock_wait_s": 2.0, "set_s": 1.5, "request_s": 3.0}
