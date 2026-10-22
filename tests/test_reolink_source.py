"""The ``reolink`` detection source: config, push edge tracking, client, daemon wiring."""

import time

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, reolink

SECRETS = {"telegram_token": "", "telegram_chat": "", "groq_key": ""}


def _load(**camera):
    base = {"name": "r", "host": "192.0.2.15", "detection": {"sources": ["reolink"]}}
    base.update(camera)
    return cfg.load_config_from_dict({"cameras": [base]})


IDLE = {"motion": False, "people": False, "vehicle": False, "dog_cat": False}


def _st(**on):
    return {**IDLE, **on}


# ── config ──────────────────────────────────────────────────────────────────

def test_config_accepts_reolink_and_defaults_the_reolink_stream():
    c = _load().cameras[0]
    assert c.detection.sources == ["reolink"]
    assert c.rtsp_stream == reolink.DEFAULT_STREAM
    assert reolink.is_reolink(c)


def test_explicit_stream_is_kept():
    assert _load(rtsp_stream="h264Preview_01_sub").cameras[0].rtsp_stream == "h264Preview_01_sub"


def test_tapo_cameras_keep_stream1_and_are_not_reolink():
    c = cfg.load_config_from_dict({"cameras": [{"name": "t", "host": "192.0.2.9"}]}).cameras[0]
    assert c.rtsp_stream == "stream1" and not reolink.is_reolink(c)


def test_config_rejects_reolink_mixed_with_tapo_sources():
    with pytest.raises(cfg.ConfigError, match="reolink"):
        _load(detection={"sources": ["reolink", "getevents"]})


def test_config_rejects_reolink_with_sd_options():
    with pytest.raises(cfg.ConfigError, match="SD-card"):
        _load(sd_snapshot=True)


# ── edge tracker ────────────────────────────────────────────────────────────

def test_idle_to_motion_emits_one_motion_event():
    t = reolink.EdgeTracker()
    (e,) = t.update(_st(motion=True), 1000.0)
    assert (e["type"], e["start_time"], e["source"]) == ("motion", 1000, "reolink")
    assert t.update(_st(motion=True), 1001.0) == []


def test_people_from_idle_is_a_person_event():
    (e,) = reolink.EdgeTracker().update(_st(motion=True, people=True), 5.0)
    assert e["type"] == "person" and e["camera_ai"] == ["people"]


def test_people_later_in_the_spell_adds_one_person_event():
    t = reolink.EdgeTracker()
    t.update(_st(motion=True), 1.0)
    (e,) = t.update(_st(motion=True, people=True), 2.0)
    assert e["type"] == "person"
    assert t.update(_st(people=True), 3.0) == []


def test_camera_ai_other_than_people_stays_motion():
    # The camera labelled a face close up as dog_cat in the bench test: still a candidate.
    (e,) = reolink.EdgeTracker().update(_st(dog_cat=True), 1.0)
    assert e["type"] == "motion" and e["camera_ai"] == ["dog_cat"]


def test_idle_ends_the_spell():
    t = reolink.EdgeTracker()
    t.update(_st(motion=True), 1.0)
    assert t.update(IDLE, 2.0) == []
    assert len(t.update(_st(motion=True), 3.0)) == 1


# ── client (fake reolink_aio host) ──────────────────────────────────────────

class _FakeBaichuan:
    def __init__(self, fail_login=None):
        self.fail_login = fail_login
        self.callback = None
        self.subscribed = False
        self.unsubscribed = False

    async def login(self):
        if self.fail_login:
            raise self.fail_login

    def register_callback(self, _id, cb, cmd_id=None):
        self.callback = cb

    async def subscribe_events(self):
        self.subscribed = True

    async def check_subscribe_events(self):
        pass

    async def unsubscribe_events(self):
        self.unsubscribed = True


class _FakeHost:
    def __init__(self, fail_login=None):
        self.baichuan = _FakeBaichuan(fail_login)
        self._ai_detection_states = {}
        self.motion = {}

    def motion_detected(self, ch):
        return self.motion.get(ch, False)

    def ai_detected(self, ch, kind):
        return self._ai_detection_states[ch][kind]


def _client(host):
    return reolink.ReolinkClient("192.0.2.15", "u", "p", host_factory=lambda *a: host,
                                 clock=lambda: 1234.0)


def test_client_primes_ai_states_queues_pushes_and_closes():
    host = _FakeHost()
    c = _client(host).start(timeout=5)
    try:
        assert host.baichuan.subscribed
        primed = {"people": False, "vehicle": False, "dog_cat": False}
        assert host._ai_detection_states == {0: primed, 1: primed}
        assert c.getEvents() == []
        host._ai_detection_states[0]["people"] = True
        host.baichuan.callback()
        (e,) = c.getEvents()
        assert e["type"] == "person" and e["start_time"] == 1234
        assert c.getEvents() == []
    finally:
        c.close()
    assert host.baichuan.unsubscribed and not c.alive


def test_tele_lens_ai_or_motion_counts_as_the_cameras_event():
    # The RLC-81MA pushes its tele lens as channel 1; a person only it sees must trigger.
    host = _FakeHost()
    c = _client(host).start(timeout=5)
    try:
        host._ai_detection_states[1]["people"] = True
        host.baichuan.callback()
        (e,) = c.getEvents()
        assert e["type"] == "person" and e["camera_ai"] == ["people"]
        host._ai_detection_states[1]["people"] = False
        host.baichuan.callback()                       # idle on both lenses ends the spell
        host.motion[1] = True
        host.baichuan.callback()
        (e,) = c.getEvents()
        assert e["type"] == "motion"
    finally:
        c.close()


def test_login_failure_raises_from_start():
    with pytest.raises(RuntimeError, match="bad login"):
        _client(_FakeHost(fail_login=RuntimeError("bad login"))).start(timeout=5)


def test_get_events_raises_once_the_subscription_is_gone():
    c = _client(_FakeHost()).start(timeout=5)
    c.close()
    with pytest.raises(ConnectionError):
        c.getEvents()


# ── daemon wiring ───────────────────────────────────────────────────────────

class _QueuedCam:
    def __init__(self, events):
        self.events = list(events)

    def getEvents(self):  # noqa: N802
        out, self.events = self.events, []
        return out


def test_monitor_pass_takes_pushed_events_and_never_queues_sd():
    app = _load()
    object.__setattr__(app.cameras[0], "sd_snapshot", True)
    ts = int(time.time())
    snaps = []

    def snapshot_for(c):
        def snap(camera, event):
            snaps.append(event)
            return None
        return snap

    cam = _QueuedCam([{"start_time": ts, "end_time": None, "event_type": "person",
                       "type": "person", "source": "reolink"}])
    state = daemon.MonitorState()
    daemon.run_monitor_pass(app, {"r": cam}, state, now=time.time(), secrets=SECRETS,
                            snapshot_for=snapshot_for, time_str=lambda e: "t")
    assert {e["start_time"] for e in snaps} == {ts}     # None frame: one retry
    assert state.pending_sd == []


def test_control_pass_never_calls_the_tapo_api_on_a_reolink_camera():
    app = _load()

    class _NoApi:
        def __getattr__(self, name):
            raise AssertionError(f"Tapo API call {name} on a Reolink camera")

    calls = []

    def connect(c):
        calls.append(c.name)
        return _NoApi(), None

    daemon.run_once(app, now=time.time(), connect=connect, is_night=lambda: False,
                    is_raining=lambda *a, **k: False)
    assert calls == ["r"]


def test_connect_reuses_a_live_client_and_backs_off_on_failure(monkeypatch):
    app = _load(user_env="RL_U", password_env="RL_P")
    c = app.cameras[0]
    state = daemon.MonitorState()
    clients = {}
    made = []

    class _Live:
        alive = True

        def close(self):
            self.alive = False

    def fake_connect(host, user, password, triggers=None):
        made.append((host, user, password))
        if len(made) == 1:
            return _Live()
        raise RuntimeError("login refused")

    monkeypatch.setenv("RL_U", "adam")
    monkeypatch.setenv("RL_P", "pw")
    monkeypatch.setattr(reolink, "connect", fake_connect)
    first, err = daemon._connect_reolink(c, clients, state, 100.0)
    assert err is None and clients["r"] is first
    again, _ = daemon._connect_reolink(c, clients, state, 101.0)
    assert again is first and len(made) == 1
    first.alive = False
    gone, err = daemon._connect_reolink(c, clients, state, 102.0)
    assert gone is None and "refused" in str(err)
    assert "r" not in clients and state.connect_backoff_until["r"] > 102.0
    assert made[0] == ("192.0.2.15", "adam", "pw")


def test_triggers_ignore_bare_motion_and_cars():
    t = reolink.EdgeTracker(("people", "dog_cat"))
    assert t.update(_st(motion=True, vehicle=True), 1.0) == []
    (e,) = t.update(_st(motion=True, dog_cat=True), 2.0)
    assert e["type"] == "motion" and e["camera_ai"] == ["dog_cat"]


def test_config_reads_and_checks_triggers():
    assert _load(reolink_triggers=["people", "dog_cat"]).cameras[0].reolink_triggers == (
        "people", "dog_cat")
    with pytest.raises(cfg.ConfigError, match="reolink_triggers"):
        _load(reolink_triggers=["face"])
