"""Hub client: request shaping, response parsing, and one held session.

The response fixtures below are the shapes a real hub answered with (device ids, MACs and
aliases replaced): indexed single-key wrappers (``search_results_1``) rather than plain
lists, which is what the parsers exist to flatten.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import hubclient

DEV = "8021AAAA1111BBBB2222CCCC3333DDDD4444EEEE"
MAC = "AABBCCDDEEFF"

DEVICE_LIST_RESULT = {
    "general_camera_manage": {
        "max_bound": 4,
        "current_bound": 2,
        "paired_general_device_list": [
            {"alias": "gate", "mac": MAC, "device_id": DEV, "network_mode": "wireless",
             "hub_storage_enabled": True, "plan_24h_record": False,
             "device_model": "C410", "device_type": "SMART.IPCAMERA", "category": "camera"},
            {"alias": "yard", "mac": "112233445566", "device_id": "8021FFFF", "network_mode": "NONE",
             "hub_storage_enabled": True, "plan_24h_record": False,
             "device_model": "C460", "device_type": "SMART.IPCAMERA", "category": "camera"},
        ],
    }
}

DAYS_RESULT = {
    "playback": {
        "search_results": [
            {"search_results_1": {"date": "20260807"}},
            {"search_results_2": {"date": "20260813"}},
        ]
    }
}

CLIPS_RESULT = {
    "playback": {
        "search_video_results": [
            {"search_video_results_1": {"startTime": 1786665978, "endTime": 1786665989,
                                        "video_type": "2"}},
            {"search_video_results_2": {"startTime": 1786669259, "endTime": 1786669272,
                                        "video_type": "2"}},
        ],
        "filter_enable": True,
    }
}


def _raw(result, error_code=0):
    """The transport-level envelope a multipleRequest call comes back in."""
    return {"result": {"responses": [{"method": "x", "result": result,
                                      "error_code": error_code}]}, "error_code": 0}


# ── request shaping (pure) ────────────────────────────────────────────────────

def test_wrap_puts_one_method_in_a_multiple_request():
    # Single-call requests hit a framing bug on this firmware, so everything is wrapped.
    payload = hubclient.wrap("getDeviceInfo", {"device_info": {"name": ["basic_info"]}})
    assert payload == {"method": "multipleRequest", "params": {"requests": [
        {"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}}]}}


def test_device_list_params_ask_for_the_paired_general_devices():
    assert hubclient.DEVICE_LIST_PARAMS == {
        "general_camera_manage": {"paired_general_device_list": {}}}


def test_day_search_params_address_the_camera_by_id_and_mac():
    params = hubclient.day_search_params(DEV, MAC, "20260801", "20260814")
    assert params == {"playback": {"search_year_utility": {
        "channel": [0], "child_device_id": DEV, "child_device_mac": MAC,
        "start_date": "20260801", "end_date": "20260814"}}}


def test_clip_search_params_carry_the_time_window_and_player_id():
    params = hubclient.clip_search_params(DEV, MAC, 100, 200, player_id="PID")
    inner = params["playback"]["search_video_with_utc"]
    assert inner["channel"] == 0
    assert (inner["child_device_id"], inner["child_device_mac"]) == (DEV, MAC)
    assert (inner["start_time"], inner["end_time"]) == (100, 200)
    assert (inner["start_index"], inner["end_index"]) == (0, 999)
    assert inner["player_id"] == "PID"


def test_new_player_id_is_uppercase_hex_without_dashes():
    pid = hubclient.new_player_id()
    assert pid == pid.upper() and "-" not in pid and len(pid) == 32
    assert pid != hubclient.new_player_id()


# ── response parsing (pure) ───────────────────────────────────────────────────

def test_unwrap_returns_the_inner_error_code_and_result():
    assert hubclient.unwrap(_raw({"a": 1})) == (0, {"a": 1})
    assert hubclient.unwrap(_raw({}, error_code=-40106)) == (-40106, {})


def test_unwrap_of_a_malformed_response_is_not_an_error_code_of_zero():
    # A hub that answers something unexpected must not be mistaken for a successful call.
    assert hubclient.unwrap({}) == (None, {})
    assert hubclient.unwrap({"result": {"responses": []}}) == (None, {})
    assert hubclient.unwrap(None) == (None, {})


def test_parse_cameras_flattens_the_paired_device_list():
    cams = hubclient.parse_cameras(DEVICE_LIST_RESULT)
    assert [c["alias"] for c in cams] == ["gate", "yard"]
    assert cams[0]["device_id"] == DEV
    assert cams[0]["mac"] == MAC
    assert cams[0]["model"] == "C410"
    assert cams[0]["hub_storage"] is True


def test_parse_cameras_ignores_non_camera_children():
    result = {"general_camera_manage": {"paired_general_device_list": [
        {"alias": "sensor", "category": "sensor", "mac": "1", "device_id": "2"}]}}
    assert hubclient.parse_cameras(result) == []


def test_parse_cameras_of_an_empty_or_malformed_result_is_empty():
    assert hubclient.parse_cameras({}) == []
    assert hubclient.parse_cameras({"general_camera_manage": {}}) == []
    assert hubclient.parse_cameras(None) == []


def test_parse_days_flattens_the_indexed_wrappers():
    assert hubclient.parse_days(DAYS_RESULT) == ["20260807", "20260813"]


def test_parse_days_of_a_camera_with_no_footage_is_empty():
    assert hubclient.parse_days({"playback": {}}) == []
    assert hubclient.parse_days({}) == []


def test_parse_clips_reads_start_and_end_times_in_order():
    clips = hubclient.parse_clips(CLIPS_RESULT)
    assert [c["start_time"] for c in clips] == [1786665978.0, 1786669259.0]
    assert clips[0]["end_time"] == 1786665989.0
    assert clips[0]["video_type"] == "2"


def test_parse_clips_sorts_by_start_time():
    result = {"playback": {"search_video_results": [
        {"search_video_results_1": {"startTime": 300, "endTime": 310}},
        {"search_video_results_2": {"startTime": 100, "endTime": 110}},
    ]}}
    assert [c["start_time"] for c in hubclient.parse_clips(result)] == [100.0, 300.0]


def test_parse_clips_skips_entries_without_a_usable_start_time():
    result = {"playback": {"search_video_results": [
        {"search_video_results_1": {"endTime": 110}},
        {"search_video_results_2": {"startTime": "nonsense"}},
        {"search_video_results_3": {"startTime": 200, "endTime": 210}},
    ]}}
    assert [c["start_time"] for c in hubclient.parse_clips(result)] == [200.0]


def test_day_string_is_the_local_calendar_day():
    # 1786665978 is 2026-08-14 in the hub's local time; the parameter is a local date.
    assert hubclient.day_string(1786665978, localtime=lambda t: __import__("time").gmtime(t)) \
        == "20260814"


# ── camera matching (pure) ───────────────────────────────────────────────────

def test_match_camera_prefers_the_configured_mac_ignoring_separators():
    cam = hubclient.match_camera(hubclient.parse_cameras(DEVICE_LIST_RESULT),
                                 name="whatever", mac="aa-bb-cc-dd-ee-ff")
    assert cam["device_id"] == DEV


def test_match_camera_falls_back_to_the_alias():
    cam = hubclient.match_camera(hubclient.parse_cameras(DEVICE_LIST_RESULT), name="YARD")
    assert cam["mac"] == "112233445566"


def test_match_camera_uses_the_only_bound_camera_when_nothing_else_matches():
    only = hubclient.parse_cameras(DEVICE_LIST_RESULT)[:1]
    assert hubclient.match_camera(only, name="unrelated")["device_id"] == DEV


def test_match_camera_refuses_to_guess_between_several_cameras():
    assert hubclient.match_camera(hubclient.parse_cameras(DEVICE_LIST_RESULT),
                                  name="unrelated") is None
    assert hubclient.match_camera([], name="gate") is None


# ── held session ─────────────────────────────────────────────────────────────

class _FakeSession:
    """A hub session that answers scripted results and can be made to fail."""

    def __init__(self, results=None, fail_after=None):
        self.results = list(results or [])
        self.fail_after = fail_after
        self.calls = []
        self.closed = False

    def send(self, method, params):
        self.calls.append((method, params))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise ConnectionError("Server disconnected")
        return _raw(self.results.pop(0) if self.results else {})

    def close(self):
        self.closed = True


def _connector(*sessions):
    """A connect() handing out the given sessions in order; raises when exhausted."""
    made = []
    queue = list(sessions)

    def connect():
        if not queue:
            raise ConnectionError("hub refused the handshake")
        session = queue.pop(0)
        made.append(session)
        return session
    connect.made = made
    return connect


def test_client_connects_once_and_reuses_the_session():
    # The hub's handshake is what is rate-limited, not the queries inside a session.
    session = _FakeSession([DEVICE_LIST_RESULT, DAYS_RESULT])
    connect = _connector(session)
    client = hubclient.HubClient(connect)

    client.list_cameras(now=0)
    client.search_days(DEV, MAC, "20260801", "20260814", now=1)

    assert len(connect.made) == 1
    assert [m for m, _ in session.calls] == ["getGeneralDeviceList", "searchDateWithVideo"]


def test_a_dropped_session_is_not_reused_and_backs_off():
    session = _FakeSession([DEVICE_LIST_RESULT], fail_after=1)
    connect = _connector(session)          # only one session available
    client = hubclient.HubClient(connect, backoff_base=60)

    assert client.list_cameras(now=0) != []          # first query works
    assert client.search_days(DEV, MAC, "1", "2", now=1) == []   # second drops the session
    assert client.connected is False
    # Within the backoff window the client must not even try to reconnect.
    assert client.list_cameras(now=30) == []
    assert len(connect.made) == 1


def test_the_client_reconnects_once_the_backoff_expires():
    first = _FakeSession([], fail_after=0)
    second = _FakeSession([DEVICE_LIST_RESULT])
    connect = _connector(first, second)
    client = hubclient.HubClient(connect, backoff_base=60)

    assert client.list_cameras(now=0) == []      # first session dies immediately
    assert client.list_cameras(now=61) != []     # after the wait, a fresh session works
    assert len(connect.made) == 2


def test_backoff_grows_with_consecutive_failures_and_is_capped():
    connect = _connector()               # every connect fails
    client = hubclient.HubClient(connect, backoff_base=60, backoff_cap=200)

    assert client.ensure_session(now=0) is False
    assert client.retry_at == 60
    assert client.ensure_session(now=60) is False
    assert client.retry_at == 180          # 60 + 120
    assert client.ensure_session(now=180) is False
    assert client.retry_at == 380          # capped at 200, not 240


def test_a_successful_query_clears_the_backoff():
    session = _FakeSession([DEVICE_LIST_RESULT, DEVICE_LIST_RESULT], fail_after=None)
    connect = _connector(_FakeSession([], fail_after=0), session)
    client = hubclient.HubClient(connect, backoff_base=60)

    client.list_cameras(now=0)            # fails, arms the backoff
    assert client.list_cameras(now=61) != []
    assert client.fails == 0
    assert client.retry_at == 0


def test_an_error_code_from_the_hub_keeps_the_session():
    # A refused method is an answer, not a dead session: -40106 must not cost a handshake.
    class _ErrSession(_FakeSession):
        def send(self, method, params):
            self.calls.append((method, params))
            return _raw({}, error_code=-40106)

    session = _ErrSession()
    connect = _connector(session)
    client = hubclient.HubClient(connect)

    assert client.search_clips(DEV, MAC, 0, 10, now=0) == []
    assert client.connected is True
    assert client.fails == 0


def test_search_clips_returns_clips_newer_than_the_cursor_only():
    session = _FakeSession([CLIPS_RESULT])
    client = hubclient.HubClient(_connector(session))
    clips = client.search_clips(DEV, MAC, 1786665978, 1786700000, now=0)
    # The window is inclusive on the hub side; the cursor clip itself must not repeat.
    assert [c["start_time"] for c in clips] == [1786669259.0]


def test_client_close_releases_the_session():
    session = _FakeSession([DEVICE_LIST_RESULT])
    client = hubclient.HubClient(_connector(session))
    client.list_cameras(now=0)
    client.close()
    assert session.closed is True
    assert client.connected is False


def test_clip_search_params_send_whole_seconds():
    # The firmware answered an empty list for a window sent as floats (time.time()), while
    # the same window as integers returned the clips. Start floors and end ceils, so
    # rounding can only widen the window, never hide a clip.
    inner = hubclient.clip_search_params(DEV, MAC, 100.7, 200.2,
                                         player_id="PID")["playback"]["search_video_with_utc"]
    assert (inner["start_time"], inner["end_time"]) == (100, 201)
    assert isinstance(inner["start_time"], int) and isinstance(inner["end_time"], int)
