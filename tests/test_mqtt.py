"""The opt-in MQTT bridge: discovery payloads, retained state, the bounded queue, the
worker's reconnect, config validation, and that nothing changes without the block.

No network anywhere: :class:`FakeClient` stands in for paho and the worker is driven by
calling ``Bridge.step`` directly, except where a test is about the real thread.
"""

import json
import sys
import threading
import time
import types

import pytest

from tapo_monitor import config as cfg_mod
from tapo_monitor import daemon, monitor, mqtt
from tests.scenario import START, Scenario, camera_dict, person

HOST = "192.0.2.10"
BROKER = "broker.example.org"


class FakeClient:
    """The slice of paho the bridge uses. CONNACK arrives on the first ``loop``."""

    def __init__(self, client_id=None):
        self.client_id = client_id
        self.published = []
        self.connect_calls = []
        self.fail_connect = False
        self.refuse = False
        self.lose_next_loop = False
        self.connected = False
        self._connack_pending = False
        self.will = None
        self.auth = None
        self.tls = False
        self.on_connect = None
        self.disconnects = 0

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, user, password=None):
        self.auth = (user, password)

    def tls_set(self):
        self.tls = True

    def max_queued_messages_set(self, n):
        self.max_queued = n

    def connect(self, host, port, keepalive):
        self.connect_calls.append((host, port, keepalive))
        if self.fail_connect:
            raise ConnectionRefusedError("down")
        self._connack_pending = True
        return 0

    def loop(self, timeout=1.0):
        if self._connack_pending:
            self._connack_pending = False
            self.connected = not self.refuse
            self.on_connect(self, None, {}, 5 if self.refuse else 0)
            return 0
        if self.lose_next_loop:
            self.lose_next_loop = False
            self.connected = False
            return 7                      # MQTT_ERR_CONN_LOST
        return 0 if self.connected else 4

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return types.SimpleNamespace(rc=0)

    def disconnect(self):
        self.connected = False
        self.disconnects += 1

    # reading the record
    def topics(self):
        return [t for t, *_ in self.published]

    def last(self, topic):
        values = [p for t, p, *_ in self.published if t == topic]
        return values[-1] if values else None


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


def app_with(mqtt_block=None, cameras=("front", "back yard")):
    raw = {"cameras": [camera_dict(name, f"192.0.2.{i + 10}") for i, name in enumerate(cameras)]}
    if mqtt_block is not None:
        raw["mqtt"] = mqtt_block
    return cfg_mod.load_config_from_dict(raw)


def make_bridge(block=None, *, cameras=("front", "back yard"), clock=None, client=None,
                env=None, queue_size=mqtt.QUEUE_SIZE):
    app = app_with({"host": BROKER, **(block or {})}, cameras)
    client = client or FakeClient()
    bridge = mqtt.Bridge(app.mqtt, [c.name for c in app.cameras],
                         client_factory=lambda _cid: client, clock=clock or Clock(),
                         env=env or {}, version="9.9.9", queue_size=queue_size)
    return app, bridge, client


def connect(bridge):
    bridge.step(wait=0)          # connect()
    bridge.step(wait=0)          # CONNACK -> resync + drain


# ── config ───────────────────────────────────────────────────────────────────

def test_mqtt_is_off_without_the_block():
    app = app_with()
    assert app.mqtt.enabled is False
    assert app_with(None).mqtt.host is None


def test_mqtt_block_defaults():
    app = app_with({"host": BROKER})
    m = app.mqtt
    assert (m.enabled, m.effective_port, m.tls, m.discovery_prefix, m.base_topic,
            m.publish_images, m.motion_off_after) == (
        True, 1883, False, "homeassistant", "tapo_monitor", False, 60)
    assert app_with({"host": BROKER, "tls": True}).mqtt.effective_port == 8883
    assert app_with({"host": BROKER, "tls": True, "port": 1884}).mqtt.effective_port == 1884


@pytest.mark.parametrize("block, message", [
    ({}, "missing required field 'host'"),
    ({"host": ""}, "missing required field 'host'"),
    ({"host": BROKER, "port": 0}, "mqtt.port"),
    ({"host": BROKER, "port": "1883"}, "mqtt.port"),
    ({"host": BROKER, "tls": "yes"}, "'tls' must be true or false"),
    ({"host": BROKER, "publish_images": 1}, "'publish_images' must be true or false"),
    ({"host": BROKER, "motion_off_after": 0}, "motion_off_after"),
    ({"host": BROKER, "base_topic": "a/+/b"}, "mqtt.base_topic"),
    ({"host": BROKER, "base_topic": "tapo/"}, "mqtt.base_topic"),
    ({"host": BROKER, "discovery_prefix": "home assistant"}, "mqtt.discovery_prefix"),
    ({"host": BROKER, "password_env": "MQTT_PASS"}, "needs mqtt.user_env"),
    ({"host": BROKER, "user_env": ""}, "mqtt.user_env"),
    ({"host": BROKER, "hots": "x"}, "mqtt.hots: unknown key"),
    ("broker", "mqtt must be a mapping"),
])
def test_mqtt_block_is_validated(block, message):
    with pytest.raises(cfg_mod.ConfigError, match=message.replace("+", r"\+")):
        app_with(block)


def test_unknown_mqtt_key_suggests_the_real_one():
    with pytest.raises(cfg_mod.ConfigError, match="did you mean 'publish_images'"):
        app_with({"host": BROKER, "publish_image": True})


def test_camera_names_that_collide_as_topic_levels_are_refused():
    with pytest.raises(cfg_mod.ConfigError, match="same topic level 'back_yard'"):
        app_with({"host": BROKER}, cameras=("back yard", "back/yard"))
    app_with(None, cameras=("back yard", "back/yard"))      # only matters with MQTT on


# ── discovery ────────────────────────────────────────────────────────────────

def discovery(bridge):
    return {t: json.loads(p) for t, p in mqtt.discovery_messages(bridge.cfg, bridge.camera_names)}


def test_discovery_announces_each_camera_as_a_device_with_its_entities():
    _app, bridge, _client = make_bridge()
    msgs = discovery(bridge)
    cam = {t.split("/")[-2]: p for t, p in msgs.items() if "_cam_back_yard/" in t}
    assert sorted(cam) == sorted(["person", "motion", "connectivity", "privacy",
                                  "detection_off", "health", "last_alert",
                                  "last_alert_score"])
    person_cfg = cam["person"]
    assert person_cfg["state_topic"] == "tapo_monitor/back_yard/person"
    assert person_cfg["device_class"] == "occupancy"
    assert person_cfg["availability_topic"] == "tapo_monitor/status"
    assert person_cfg["device"]["name"] == "back yard"
    assert person_cfg["device"]["identifiers"] == ["tapo_monitor_cam_back_yard"]
    assert person_cfg["device"]["via_device"] == "tapo_monitor_daemon"
    assert person_cfg["unique_id"] == "tapo_monitor_back_yard_person"
    assert cam["motion"]["device_class"] == "motion"
    assert cam["connectivity"]["device_class"] == "connectivity"
    assert cam["last_alert"]["device_class"] == "timestamp"
    assert cam["health"]["json_attributes_topic"] == "tapo_monitor/back_yard/health/attributes"
    assert ("homeassistant/binary_sensor/tapo_monitor_cam_back_yard/person/config" in msgs)
    # The daemon device: its availability as a sensor of its own.
    running = msgs["homeassistant/binary_sensor/tapo_monitor_daemon/running/config"]
    assert running["state_topic"] == "tapo_monitor/status"
    assert (running["payload_on"], running["payload_off"]) == ("online", "offline")
    unique = [p["unique_id"] for p in msgs.values()]
    assert len(unique) == len(set(unique))


def test_image_entity_exists_only_with_publish_images():
    _app, bridge, _client = make_bridge()
    assert not [t for t in discovery(bridge) if "/image/" in t]
    _app, bridge, _client = make_bridge({"publish_images": True})
    image = [p for t, p in discovery(bridge).items() if t.startswith("homeassistant/image/")]
    assert len(image) == 2
    assert image[0]["image_topic"].endswith("/image")
    assert image[0]["content_type"] == "image/jpeg"


def test_custom_prefix_and_base_topic_shape_every_topic():
    _app, bridge, _client = make_bridge({"base_topic": "site/tapo",
                                         "discovery_prefix": "ha"})
    msgs = discovery(bridge)
    assert all(t.startswith("ha/") for t in msgs)
    assert "ha/binary_sensor/site_tapo_cam_front/person/config" in msgs
    assert msgs["ha/binary_sensor/site_tapo_cam_front/person/config"]["state_topic"] == \
        "site/tapo/front/person"


# ── connection and state ─────────────────────────────────────────────────────

def test_connect_sets_will_auth_and_publishes_discovery_then_online_retained():
    env = {"MQTT_USER": "u", "MQTT_PASS": "p"}
    _app, bridge, client = make_bridge({"user_env": "MQTT_USER", "password_env": "MQTT_PASS",
                                        "tls": True}, env=env)
    connect(bridge)
    assert client.will == ("tapo_monitor/status", "offline", 1, True)
    assert client.auth == ("u", "p")
    assert client.tls is True
    assert client.connect_calls == [(BROKER, 8883, mqtt.KEEPALIVE)]
    assert client.published[0][:2] == ("tapo_monitor/status", "online")
    assert all(retain for *_x, retain in client.published)
    assert len([t for t in client.topics() if t.endswith("/config")]) == 3 + 2 * 8


def test_alert_turns_person_on_then_off_after_the_configured_seconds(tmp_path):
    clock = Clock()
    _app, bridge, client = make_bridge({"motion_off_after": 30}, clock=clock)
    connect(bridge)
    bridge.note_alert("front", image=None, score=0.8123)
    bridge.step(wait=0)
    assert client.last("tapo_monitor/front/person") == "ON"
    assert client.last("tapo_monitor/front/last_alert_score") == "0.812"
    assert client.last("tapo_monitor/front/last_alert") == "2026-09-21T14:13:20+00:00"
    clock.now += 29
    bridge.step(wait=0)
    assert client.last("tapo_monitor/front/person") == "ON"
    clock.now += 1
    bridge.step(wait=0)
    assert client.last("tapo_monitor/front/person") == "OFF"


def test_a_second_alert_extends_the_pulse_without_republishing_on():
    clock = Clock()
    _app, bridge, client = make_bridge({"motion_off_after": 30}, clock=clock)
    connect(bridge)
    bridge.note_detect("front")
    bridge.step(wait=0)
    clock.now += 20
    bridge.note_detect("front")
    bridge.step(wait=0)
    clock.now += 20                                  # 40 s after the first, 20 after the last
    bridge.step(wait=0)
    motion = [p for t, p, *_ in client.published if t == "tapo_monitor/front/motion"]
    assert motion == ["ON"]
    clock.now += 10
    bridge.step(wait=0)
    assert [p for t, p, *_ in client.published if t == "tapo_monitor/front/motion"] == ["ON", "OFF"]


def test_photo_is_read_and_published_only_with_publish_images(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"\xff\xd8photo")
    _app, bridge, client = make_bridge()
    connect(bridge)
    bridge.note_alert("front", image=str(photo), score=0.5)
    bridge.step(wait=0)
    assert not [t for t in client.topics() if t.endswith("/image")]

    _app, bridge, client = make_bridge({"publish_images": True})
    connect(bridge)
    bridge.note_alert("front", image=str(photo), score=0.5)
    bridge.step(wait=0)
    assert client.last("tapo_monitor/front/image") == b"\xff\xd8photo"


def test_state_is_published_on_change_only():
    _app, bridge, client = make_bridge()
    connect(bridge)
    state = daemon.MonitorState()
    app = app_with({"host": BROKER})
    state.network_reachable["front"] = True
    for _ in range(3):
        bridge.observe(app, state)
        bridge.step(wait=0)
    assert client.topics().count("tapo_monitor/front/connectivity") == 1
    state.network_reachable["front"] = False
    bridge.observe(app, state)
    bridge.step(wait=0)
    assert [p for t, p, *_ in client.published
            if t == "tapo_monitor/front/connectivity"] == ["ON", "OFF"]


def test_observe_maps_health_privacy_and_detection():
    _app, bridge, client = make_bridge()
    connect(bridge)
    app = app_with({"host": BROKER})
    state = daemon.MonitorState()
    state.twin_fleet["front"] = {"health": {"status": "degraded", "layers": {"network": "ok"}},
                                 "drift": {"counts": {"drift": 2}}, "captured_at": 123.0,
                                 "actual": {"privacy.enabled": True}}
    state.detection_seen["front"] = {"motion": True, "person": False}
    state.privacy_seen["back yard"] = False
    state.detection_seen["back yard"] = {"motion": True, "person": "restored"}
    bridge.observe(app, state)
    bridge.step(wait=0)
    assert client.last("tapo_monitor/front/health") == "degraded"
    assert json.loads(client.last("tapo_monitor/front/health/attributes")) == {
        "layers": {"network": "ok"}, "drift_count": 2, "probed_at": 123.0}
    assert client.last("tapo_monitor/front/privacy") == "ON"          # from the twin
    assert client.last("tapo_monitor/front/detection_off") == "ON"
    assert client.last("tapo_monitor/back_yard/privacy") == "OFF"
    assert client.last("tapo_monitor/back_yard/detection_off") == "OFF"   # self-healed
    assert json.loads(client.last("tapo_monitor/back_yard/detection_off/attributes")) == {
        "motion_detection": True, "person_detection": "restored"}
    # Never read: nothing published, Home Assistant keeps it unknown.
    assert client.last("tapo_monitor/back_yard/health") is None
    assert client.last("tapo_monitor/front/connectivity") is None


def test_tick_outcome_and_drop_counter():
    _app, bridge, client = make_bridge()
    connect(bridge)
    bridge.note_tick(True)
    bridge.note_tick(False)
    bridge.step(wait=0)
    assert [p for t, p, *_ in client.published
            if t == "tapo_monitor/daemon/tick_problem"] == ["OFF", "ON"]
    assert client.last("tapo_monitor/daemon/dropped") == "0"


# ── bounded queue, broker down, reconnect ───────────────────────────────────

def test_full_queue_drops_the_oldest_and_counts_it():
    _app, bridge, client = make_bridge(queue_size=3)
    for i in range(5):
        bridge._set(f"tapo_monitor/test/{i}", str(i))
    assert bridge.queued() == 3
    assert bridge.dropped == 2
    assert [t for t, _p in bridge._queue] == [f"tapo_monitor/test/{i}" for i in (2, 3, 4)]


def test_reconnect_backs_off_and_resyncs_the_whole_cache():
    clock = Clock()
    client = FakeClient()
    client.fail_connect = True
    _app, bridge, _ = make_bridge(clock=clock, client=client)
    bridge.note_alert("front")                       # happens while the broker is down
    delays = []
    for _ in range(5):
        bridge.step(wait=0)
        delays.append(bridge._next_attempt - clock.now)
        clock.now = bridge._next_attempt
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert len(client.connect_calls) == 5
    client.fail_connect = False
    connect(bridge)
    assert client.last("tapo_monitor/front/person") == "ON"
    assert client.published[0][:2] == ("tapo_monitor/status", "online")
    assert bridge.queued() == 0

    # A lost connection is noticed on the loop and the next session republishes everything.
    first = len(client.published)
    client.lose_next_loop = True
    bridge.step(wait=0)
    assert bridge._ready is False
    clock.now = bridge._next_attempt
    connect(bridge)
    again = client.topics()[first:]
    assert again[0] == "tapo_monitor/status"
    assert "tapo_monitor/front/person" in again
    assert "homeassistant/binary_sensor/tapo_monitor_cam_front/person/config" in again


def test_pulse_that_expires_while_offline_arrives_as_off():
    clock = Clock()
    client = FakeClient()
    client.fail_connect = True
    _app, bridge, _ = make_bridge({"motion_off_after": 10}, clock=clock, client=client)
    bridge.note_alert("front")
    bridge.step(wait=0)
    clock.now += 60
    client.fail_connect = False
    connect(bridge)
    assert [p for t, p, *_ in client.published if t == "tapo_monitor/front/person"] == ["OFF"]


def test_refused_login_backs_off_and_publishes_nothing():
    clock = Clock()
    client = FakeClient()
    client.refuse = True
    _app, bridge, _ = make_bridge(clock=clock, client=client)
    bridge.step(wait=0)                  # connect
    bridge.step(wait=0)                  # CONNACK refused
    bridge.step(wait=0)                  # loop reports the closed socket
    assert client.published == []
    assert bridge._next_attempt > clock.now


def test_broker_that_hangs_never_blocks_the_hooks():
    release = threading.Event()

    class HangingClient(FakeClient):
        def connect(self, host, port, keepalive):
            release.wait(5)              # a black-holed broker: connect() just sits
            raise TimeoutError

    app = app_with({"host": BROKER, "motion_off_after": 1})
    bridge = mqtt.Bridge(app.mqtt, ["front"], client_factory=lambda _c: HangingClient(),
                         queue_size=4, version="0")
    bridge.start()
    try:
        time.sleep(0.05)                 # the worker is now stuck inside connect()
        started = time.monotonic()
        for i in range(50):
            bridge.note_alert("front", score=i / 100)
            bridge.note_detect("front")
            bridge.note_tick(True)
        assert time.monotonic() - started < 0.5
        assert bridge.queued() == 4
        assert bridge.dropped > 0
    finally:
        release.set()
        bridge.stop(timeout=2)


def test_stop_says_offline_before_disconnecting():
    app = app_with({"host": BROKER})
    client = FakeClient()
    bridge = mqtt.Bridge(app.mqtt, ["front"], client_factory=lambda _c: client, version="0")
    bridge.start()
    deadline = time.monotonic() + 3
    while "tapo_monitor/status" not in client.topics() and time.monotonic() < deadline:
        time.sleep(0.01)
    bridge.stop(timeout=3)
    assert client.published[-1][:2] == ("tapo_monitor/status", "offline")
    assert client.disconnects >= 1


# ── start/stop wiring and the no-block pin ──────────────────────────────────

def test_without_the_block_nothing_starts_and_hooks_are_no_ops(monkeypatch):
    monkeypatch.setattr(mqtt, "_bridge", None)
    for name in [m for m in sys.modules if m.startswith("paho")]:
        monkeypatch.delitem(sys.modules, name)
    app = app_with()
    assert mqtt.start(app, client_factory=lambda _c: pytest.fail("client built")) is None
    mqtt.note_detect("front")
    mqtt.note_alert("front", image="/nonexistent", score=0.9)
    mqtt.observe(app, daemon.MonitorState())
    mqtt.note_tick(True)
    mqtt.stop()
    assert mqtt.active() is None
    assert not [m for m in sys.modules if m.startswith("paho")]


def test_missing_paho_is_a_clear_error_not_a_crash(monkeypatch, caplog):
    monkeypatch.setattr(mqtt, "_bridge", None)
    monkeypatch.setitem(sys.modules, "paho", None)           # import fails
    monkeypatch.setitem(sys.modules, "paho.mqtt", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    with caplog.at_level("ERROR", logger="tapo_monitor.mqtt"):
        assert mqtt.start(app_with({"host": BROKER})) is None
    assert "paho-mqtt is not installed" in caplog.text
    assert "tapo-monitor[mqtt]" in caplog.text


def test_hook_errors_are_contained(monkeypatch, caplog):
    class Broken:
        def note_alert(self, *a, **k):
            raise RuntimeError("boom")
    monkeypatch.setattr(mqtt, "_bridge", Broken())
    mqtt.note_alert("front")
    assert "alert hook failed" in caplog.text


def test_send_alert_photo_tells_the_bridge_only_after_a_delivery(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(mqtt, "note_alert", lambda cam, image=None, score=None:
                        seen.append((cam, image, score)))
    cfg = app_with(None, cameras=("front",)).cameras[0]
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8")
    secrets = {"telegram_token": "t", "telegram_chat": "c"}
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: False)
    assert daemon.send_alert_photo(cfg, secrets, str(image), "cap", score=0.7) is False
    assert seen == []
    monkeypatch.setattr(daemon.notify, "send_photo", lambda *a, **k: True)
    assert daemon.send_alert_photo(cfg, secrets, str(image), "cap", score=0.7) is True
    assert seen == [("front", str(image), 0.7)]


def test_monitor_detect_audit_reaches_the_bridge(monkeypatch):
    seen = []
    monkeypatch.setattr(monitor.mqtt_bridge, "note_detect", seen.append)
    assert monitor.mqtt_bridge is mqtt
    cfg = app_with(None, cameras=("front",)).cameras[0]
    cam = types.SimpleNamespace(getEvents=lambda *a, **k: [person(START)])
    monitor.run_monitor(cam, cfg, 0, now=START + 5, groq_key="", telegram_token="t",
                        telegram_chat="c", snapshot=lambda *_: None,
                        time_str=lambda _e: "t")
    assert seen == ["front"]


# ── scenario: the real loop with a bridge attached ──────────────────────────

def test_scenario_person_alert_reaches_home_assistant_and_clears(monkeypatch, tmp_path):
    sc = Scenario(monkeypatch, tmp_path, [camera_dict("a", HOST)],
                  mqtt={"host": BROKER, "motion_off_after": 30})
    client = FakeClient()
    bridge = mqtt.Bridge(sc.app.mqtt, ["a"], client_factory=lambda _c: client,
                         clock=lambda: sc.clock.now, version="0")
    monkeypatch.setattr(mqtt, "_bridge", bridge)
    connect(bridge)

    sc.run(10)
    sc.cams["a"].push(person(START + 10))
    sc.tick(advance=5)
    bridge.step(wait=0)
    assert sc.actions("send") == [("send", "a")]
    assert client.last("tapo_monitor/a/motion") == "ON"
    assert client.last("tapo_monitor/a/person") == "ON"
    assert client.last("tapo_monitor/a/last_alert") is not None
    assert client.last("tapo_monitor/a/connectivity") == "ON"   # the watchdog's ping
    sc.run(40)
    bridge.step(wait=0)
    assert client.last("tapo_monitor/a/person") == "OFF"
    assert client.last("tapo_monitor/a/motion") == "OFF"


def test_scenario_without_the_block_behaves_exactly_as_before(monkeypatch, tmp_path):
    def story(mqtt_block, path):
        sc = Scenario(monkeypatch, path, [camera_dict("a", HOST)], mqtt=mqtt_block)
        if mqtt_block:
            client = FakeClient()
            bridge = mqtt.Bridge(sc.app.mqtt, ["a"], client_factory=lambda _c: client,
                                 clock=lambda: sc.clock.now, version="0")
            monkeypatch.setattr(mqtt, "_bridge", bridge)
            connect(bridge)
        else:
            monkeypatch.setattr(mqtt, "_bridge", None)
        sc.run(10)
        sc.cams["a"].push(person(START + 10))
        sc.run(90)
        return [(t - START, a) for t, a in sc.timeline]

    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    without = story(None, tmp_path / "off")
    with_bridge = story({"host": BROKER}, tmp_path / "on")
    assert without == with_bridge
    assert ("send", "a") in [a for _t, a in without]


def test_daemon_device_name_gives_clean_entity_ids():
    msgs = mqtt.discovery_messages(cfg_mod.MqttConfig(host="broker"), ["front"])
    running = next(json.loads(p) for t, p in msgs if t.endswith("/running/config"))
    assert running["device"]["name"] == "tapo-monitor"
    other = mqtt.discovery_messages(cfg_mod.MqttConfig(host="broker", base_topic="site_b"), [])
    running = next(json.loads(p) for t, p in other if t.endswith("/running/config"))
    assert running["device"]["name"] == "tapo-monitor site_b"
