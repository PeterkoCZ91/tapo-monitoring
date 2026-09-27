"""Opt-in outbound MQTT bridge with Home Assistant MQTT discovery.

Off unless the config has an ``mqtt:`` block. With it, the daemon announces each camera
to Home Assistant as a device (person and motion binary sensors, reachability, privacy
mode, "detection off", twin health, last alert time and score, optionally the last alert
photo) and keeps their retained state current. See ``docs/mqtt.md``.

This is a publish-only client, deliberately not a listener: it subscribes to nothing and
no broker message can change what the daemon does. The main loop never waits for it.
Every hook (:func:`note_detect`, :func:`note_alert`, :func:`observe`, :func:`note_tick`)
only updates an in-memory cache and appends to a bounded queue; a worker thread owns the
connection, reconnects with backoff, and drains the queue. A full queue drops its oldest
message and counts it. A slow or absent broker therefore costs the loop a lock and a
deque append, never a socket call.

The retained-state cache doubles as the recovery path: on every (re)connect the worker
discards what was queued and republishes the whole cache (discovery, then state), so a
broker restart or a dropped message heals on the next connection instead of leaving a
stale sensor behind.

``paho-mqtt`` is an optional extra (``pip install 'tapo-monitor[mqtt]'``) and is imported
only when the block is set, so nothing changes for a config without it.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time as _time
from datetime import datetime, timezone

from .config import mqtt_slug

log = logging.getLogger(__name__)

QUEUE_SIZE = 256          # messages waiting for the broker; the oldest go first when full
KEEPALIVE = 60            # seconds between MQTT pings
BACKOFF_MIN = 1.0         # first reconnect delay; doubles per failure up to BACKOFF_MAX
BACKOFF_MAX = 300.0
CONNACK_TIMEOUT = 30.0    # a socket that never gets its CONNACK counts as a failed attempt
LOOP_WAIT = 0.25          # worker poll: the latency between a hook and its publish
STOP_TIMEOUT = 3.0        # how long shutdown waits to say "offline" before giving up
INSTALL_HINT = "pip install 'tapo-monitor[mqtt]'"

ON, OFF = "ON", "OFF"


class MqttUnavailable(RuntimeError):
    """The ``mqtt:`` block is set but paho-mqtt is not importable."""


def paho_available() -> bool:
    """Whether paho-mqtt can be imported (does not import it)."""
    import importlib.util

    try:
        return importlib.util.find_spec("paho.mqtt.client") is not None
    except (ImportError, ValueError):
        return False


def paho_client_factory():
    """Return ``factory(client_id) -> paho client`` for paho-mqtt 1.x and 2.x.

    Raises :class:`MqttUnavailable` with the install hint when the library is missing.
    """
    try:
        import paho.mqtt.client as paho
    except ImportError as exc:
        raise MqttUnavailable(
            f"mqtt: block is set but paho-mqtt is not installed; install it with "
            f"{INSTALL_HINT}") from exc

    def factory(client_id):
        version = getattr(paho, "CallbackAPIVersion", None)
        if version is not None:            # paho-mqtt >= 2.0
            return paho.Client(version.VERSION2, client_id=client_id)
        return paho.Client(client_id=client_id)
    return factory


def _rc_value(rc) -> int:
    """An int for paho's result: an int (1.x), an IntEnum or a ReasonCode (2.x)."""
    value = getattr(rc, "value", rc)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _iso(ts) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")


class Topics:
    """Every topic the bridge publishes, derived from the config. Pure."""

    def __init__(self, base, discovery_prefix):
        self.base = base
        self.discovery_prefix = discovery_prefix
        self.node = mqtt_slug(base.replace("/", "_"))
        self.availability = f"{base}/status"
        self.tick_problem = f"{base}/daemon/tick_problem"
        self.dropped = f"{base}/daemon/dropped"

    def camera(self, camera, key):
        return f"{self.base}/{mqtt_slug(camera)}/{key}"

    def discovery(self, component, object_id, key):
        return f"{self.discovery_prefix}/{component}/{self.node}_{object_id}/{key}/config"


# (key, component, name, extra discovery fields) per camera entity.
_CAMERA_ENTITIES = (
    ("person", "binary_sensor", "Person", {"device_class": "occupancy"}),
    ("motion", "binary_sensor", "Motion", {"device_class": "motion"}),
    ("connectivity", "binary_sensor", "Reachable",
     {"device_class": "connectivity", "entity_category": "diagnostic"}),
    ("privacy", "binary_sensor", "Privacy mode", {"icon": "mdi:eye-off"}),
    ("detection_off", "binary_sensor", "Detection off",
     {"device_class": "problem", "json_attributes": True}),
    ("health", "sensor", "Health",
     {"icon": "mdi:heart-pulse", "entity_category": "diagnostic", "json_attributes": True}),
    ("last_alert", "sensor", "Last alert", {"device_class": "timestamp"}),
    ("last_alert_score", "sensor", "Last alert score",
     {"state_class": "measurement", "suggested_display_precision": 2, "icon": "mdi:account-eye"}),
)


def discovery_messages(mqtt_cfg, camera_names, version="0"):
    """``[(topic, payload_json)]`` announcing the daemon and every camera. Pure.

    Every camera entity names the daemon's availability topic, so Home Assistant shows
    them unavailable while the daemon is down (the broker publishes the last will).
    """
    t = Topics(mqtt_cfg.base_topic, mqtt_cfg.discovery_prefix)
    origin = {"name": "tapo-monitor", "sw_version": version}
    daemon_id = f"{t.node}_daemon"
    # Home Assistant builds entity ids from device + entity name: "tapo-monitor" gives
    # binary_sensor.tapo_monitor_running; a non-default base_topic is appended to tell
    # several daemons on one broker apart.
    daemon_name = ("tapo-monitor" if mqtt_cfg.base_topic == "tapo_monitor"
                   else f"tapo-monitor {mqtt_cfg.base_topic}")
    daemon_device = {"identifiers": [daemon_id], "name": daemon_name,
                     "manufacturer": "tapo-monitor", "model": "daemon", "sw_version": version}
    availability = {"availability_topic": t.availability, "payload_available": "online",
                    "payload_not_available": "offline"}
    out = []

    def add(component, object_id, key, payload):
        payload = {**payload, "origin": origin}
        out.append((t.discovery(component, object_id, key),
                    json.dumps(payload, sort_keys=True)))

    add("binary_sensor", "daemon", "running", {
        "name": "Running", "unique_id": f"{daemon_id}_running", "device": daemon_device,
        "state_topic": t.availability, "payload_on": "online", "payload_off": "offline",
        "device_class": "connectivity"})
    add("binary_sensor", "daemon", "tick_problem", {
        "name": "Loop failing", "unique_id": f"{daemon_id}_tick_problem",
        "device": daemon_device, "state_topic": t.tick_problem, "device_class": "problem",
        "entity_category": "diagnostic", **availability})
    add("sensor", "daemon", "dropped", {
        "name": "Dropped MQTT messages", "unique_id": f"{daemon_id}_dropped",
        "device": daemon_device, "state_topic": t.dropped, "state_class": "total_increasing",
        "entity_category": "diagnostic", "icon": "mdi:message-alert", **availability})

    for name in camera_names:
        slug = mqtt_slug(name)
        object_id = f"cam_{slug}"
        device = {"identifiers": [f"{t.node}_{object_id}"], "name": str(name),
                  "manufacturer": "TP-Link", "model": "Tapo camera (tapo-monitor)",
                  "via_device": daemon_id}
        for key, component, label, extra in _CAMERA_ENTITIES:
            extra = dict(extra)
            attributes = extra.pop("json_attributes", False)
            payload = {"name": label, "unique_id": f"{t.node}_{slug}_{key}", "device": device,
                       "state_topic": t.camera(name, key), **availability, **extra}
            if attributes:
                payload["json_attributes_topic"] = t.camera(name, f"{key}/attributes")
            add(component, object_id, key, payload)
        if mqtt_cfg.publish_images:
            add("image", object_id, "image", {
                "name": "Last alert photo", "unique_id": f"{t.node}_{slug}_image",
                "device": device, "image_topic": t.camera(name, "image"),
                "content_type": "image/jpeg", **availability})
    return out


def privacy_state(state, name):
    """``True``/``False`` for the camera's privacy switch, ``None`` when never read. Pure.

    The control pass's own read wins; without one the twin's last probe answers.
    """
    seen = (getattr(state, "privacy_seen", None) or {}).get(name)
    if isinstance(seen, bool):
        return seen
    entry = (getattr(state, "twin_fleet", None) or {}).get(name)
    actual = entry.get("actual") if isinstance(entry, dict) else None
    value = actual.get("privacy.enabled") if isinstance(actual, dict) else None
    return value if isinstance(value, bool) else None


def detection_state(state, name):
    """``(off, attributes)`` from the control pass's detection read; ``off`` None if unread.

    ``off`` is True while motion or person detection is off. A person switch the
    self-heal turned back on (``"restored"``) counts as on: it is watching again.
    """
    seen = (getattr(state, "detection_seen", None) or {}).get(name) or {}
    motion, person = seen.get("motion"), seen.get("person")
    if motion is None and person is None:
        return None, None
    attributes = {"motion_detection": motion,
                  "person_detection": "restored" if person == "restored" else person}
    return (motion is False or person is False), attributes


class Bridge:
    """The retained-state cache, the bounded queue and the worker that drains them.

    Hooks run on the daemon's main thread and never touch the network. ``client_factory``
    and ``clock`` are injectable so tests drive :meth:`step` without a thread or broker.
    """

    def __init__(self, mqtt_cfg, camera_names, *, client_factory, clock=None,
                 queue_size=QUEUE_SIZE, env=None, version=None):
        if version is None:
            from . import __version__ as version
        self.cfg = mqtt_cfg
        self.camera_names = list(camera_names)
        self.topics = Topics(mqtt_cfg.base_topic, mqtt_cfg.discovery_prefix)
        self._clock = clock or _time.time
        self._client_factory = client_factory
        self._env = os.environ if env is None else env
        self._lock = threading.Lock()
        self._queue: collections.deque = collections.deque()
        self._queue_size = int(queue_size)
        self.dropped = 0
        # topic -> payload, in first-publish order (discovery first); republished whole
        # on every connect.
        self._cache: dict = {}
        self._off_at: dict = {}         # (camera, key) -> wall time the pulse turns OFF
        for topic, payload in discovery_messages(mqtt_cfg, self.camera_names, version):
            self._cache[topic] = payload
        self._client = None
        self._socket_open = False       # connect() returned; CONNACK may still be pending
        self._ready = False             # CONNACK accepted: publishing is allowed
        self._fresh_session = False
        self._connect_started = 0.0
        self._next_attempt = 0.0
        self._fails = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── main-thread hooks: cache + queue only ────────────────────────────────
    def _set(self, topic, payload):
        """Record ``payload`` as the retained state of ``topic``; queue it if it changed."""
        with self._lock:
            if self._cache.get(topic) == payload:
                return
            self._cache[topic] = payload
            self._enqueue_locked(topic, payload)

    def _enqueue_locked(self, topic, payload):
        if len(self._queue) >= self._queue_size:
            self._queue.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning("mqtt: queue full, dropped %d message(s) so far (oldest first)",
                            self.dropped)
        self._queue.append((topic, payload))

    def queued(self) -> int:
        with self._lock:
            return len(self._queue)

    def cached(self, topic):
        with self._lock:
            return self._cache.get(topic)

    def _pulse(self, camera, key, now):
        with self._lock:
            self._off_at[(camera, key)] = now + self.cfg.motion_off_after
        self._set(self.topics.camera(camera, key), ON)

    def note_detect(self, camera):
        """The camera reported a detection event: motion on for ``motion_off_after``."""
        self._pulse(camera, "motion", self._clock())

    def note_alert(self, camera, image=None, score=None):
        """An alert went out (or was recorded): person on, last alert time/score, photo."""
        now = self._clock()
        self._pulse(camera, "person", now)
        self._set(self.topics.camera(camera, "last_alert"), _iso(now))
        if score is not None:
            try:
                self._set(self.topics.camera(camera, "last_alert_score"),
                          f"{float(score):.3f}")
            except (TypeError, ValueError):
                pass
        if self.cfg.publish_images and image:
            try:
                with open(image, "rb") as fh:
                    frame = fh.read()
            except OSError as exc:
                log.debug("mqtt: alert photo for %s unreadable: %s", camera,
                          type(exc).__name__)
            else:
                if frame:
                    self._set(self.topics.camera(camera, "image"), frame)

    def observe(self, app, state):
        """Publish what changed in the per-camera view (reachability, health, switches)."""
        from . import statusd

        view = statusd.state_view(app, state)["cameras"]
        for name in self.camera_names:
            cam = view.get(name) or {}
            reachable = cam.get("reachable")
            if isinstance(reachable, bool):
                self._set(self.topics.camera(name, "connectivity"), ON if reachable else OFF)
            health = cam.get("health")
            if isinstance(health, dict):
                self._set(self.topics.camera(name, "health"), str(health.get("status")))
                self._set(self.topics.camera(name, "health/attributes"), json.dumps(
                    {"layers": health.get("layers") or {},
                     "drift_count": cam.get("drift_count"),
                     "probed_at": cam.get("probed_at")}, sort_keys=True, default=str))
            privacy = privacy_state(state, name)
            if privacy is not None:
                self._set(self.topics.camera(name, "privacy"), ON if privacy else OFF)
            off, attributes = detection_state(state, name)
            if off is not None:
                self._set(self.topics.camera(name, "detection_off"), ON if off else OFF)
                self._set(self.topics.camera(name, "detection_off/attributes"),
                          json.dumps(attributes, sort_keys=True))

    def note_tick(self, ok):
        """The daemon's own loop outcome, plus the dropped-message counter."""
        if ok is not None:
            self._set(self.topics.tick_problem, OFF if ok else ON)
        self._set(self.topics.dropped, str(self.dropped))

    # ── worker thread ────────────────────────────────────────────────────────
    def _expire_pulses(self, now):
        with self._lock:
            due = [key for key, at in self._off_at.items() if now >= at]
            for key in due:
                del self._off_at[key]
        for camera, key in due:
            self._set(self.topics.camera(camera, key), OFF)

    def _make_client(self):
        client = self._client_factory(f"tapo-monitor-{self.topics.node}")
        client.will_set(self.topics.availability, "offline", qos=1, retain=True)
        user = self._env.get(self.cfg.user_env, "") if self.cfg.user_env else ""
        password = self._env.get(self.cfg.password_env, "") if self.cfg.password_env else ""
        if user:
            client.username_pw_set(user, password or None)
        if self.cfg.tls:
            client.tls_set()
        if hasattr(client, "max_queued_messages_set"):
            client.max_queued_messages_set(self._queue_size)
        client.on_connect = self._on_connect
        return client

    def _on_connect(self, _client, _userdata, _flags, rc, *_rest):
        code = _rc_value(rc)
        if code == 0:
            self._ready = True
            self._fresh_session = True
            if self._fails:
                log.info("mqtt: connected to %s:%d after %d failed attempt(s)",
                         self.cfg.host, self.cfg.effective_port, self._fails)
            else:
                log.info("mqtt: connected to %s:%d", self.cfg.host, self.cfg.effective_port)
            self._fails = 0
        else:
            log.warning("mqtt: broker refused the connection (code %s); check mqtt.user_env/"
                        "password_env", code)

    def _failed(self, now, reason):
        if self._fails == 0 or self._ready:
            log.warning("mqtt: %s; retrying with backoff", reason)
        delay = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** min(self._fails, 16)))
        self._fails += 1
        self._next_attempt = now + delay
        self._socket_open = self._ready = False
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - the socket is already gone
                pass

    def _connect(self, now):
        try:
            if self._client is None:
                self._client = self._make_client()
            self._client.connect(self.cfg.host, self.cfg.effective_port, KEEPALIVE)
        except Exception as exc:  # noqa: BLE001 - any connect failure is a retry
            self._failed(now, f"cannot reach broker {self.cfg.host}:"
                              f"{self.cfg.effective_port} ({type(exc).__name__})")
            return
        self._socket_open = True
        self._connect_started = now

    def _publish(self, topic, payload) -> bool:
        assert self._client is not None
        try:
            info = self._client.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:  # noqa: BLE001 - a publish error means the link is bad
            self._failed(self._clock(), f"publish failed ({type(exc).__name__})")
            return False
        rc = _rc_value(getattr(info, "rc", info))
        if rc == 0:
            return True
        if rc == 15:              # MQTT_ERR_QUEUE_SIZE: paho's own buffer is full
            with self._lock:
                self.dropped += 1
            return True
        self._failed(self._clock(), f"publish refused (code {rc})")
        return False

    def _resync(self):
        """A new session: drop the queue (the cache is newer) and republish everything."""
        with self._lock:
            self._queue.clear()
            snapshot = list(self._cache.items())
        if not self._publish(self.topics.availability, "online"):
            return
        for topic, payload in snapshot:
            if not self._publish(topic, payload):
                return

    def _drain(self):
        while self._ready:
            with self._lock:
                if not self._queue:
                    return
                topic, payload = self._queue.popleft()
            if not self._publish(topic, payload):
                return

    def step(self, wait=LOOP_WAIT):
        """One worker iteration: connect or run the network loop, then publish."""
        now = self._clock()
        self._expire_pulses(now)
        if not self._socket_open:
            if now >= self._next_attempt:
                self._connect(now)
            else:
                self._stop.wait(max(0.0, min(wait, self._next_attempt - now)))
            return
        assert self._client is not None
        try:
            rc = _rc_value(self._client.loop(timeout=wait))
        except Exception as exc:  # noqa: BLE001 - paho raises on some socket errors
            self._failed(now, f"connection lost ({type(exc).__name__})")
            return
        if rc != 0:
            self._failed(now, f"connection lost (code {rc})")
            return
        if not self._ready:
            if now - self._connect_started > CONNACK_TIMEOUT:
                self._failed(now, "no CONNACK from the broker")
            return
        if self._fresh_session:
            self._fresh_session = False
            self._resync()
        self._drain()

    def _say_offline(self):
        """Clean shutdown: a graceful disconnect suppresses the will, so say it ourselves."""
        client = self._client
        if client is None or not self._ready:
            return
        try:
            client.publish(self.topics.availability, "offline", qos=1, retain=True)
            deadline = _time.monotonic() + 1.0
            while _time.monotonic() < deadline:
                if _rc_value(client.loop(timeout=0.1)) != 0:
                    break
                if not getattr(client, "want_write", lambda: False)():
                    break
            client.disconnect()
        except Exception:  # noqa: BLE001 - best effort; the will covers a failure
            pass

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 - the bridge must never die silently
                log.exception("mqtt: worker iteration failed")
                self._stop.wait(1.0)
        self._say_offline()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="tapo-mqtt", daemon=True)
        self._thread.start()
        log.info("mqtt: bridge started for %d camera(s), broker %s:%d, base topic %r%s",
                 len(self.camera_names), self.cfg.host, self.cfg.effective_port,
                 self.cfg.base_topic, ", images on" if self.cfg.publish_images else "")
        return self

    def stop(self, timeout=STOP_TIMEOUT):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


# The daemon runs one bridge per process. Module-level, like daemon._app_silenced, because
# the hooks sit in paths (the send path, the audit line) that are not handed the state.
_bridge: Bridge | None = None


def active() -> Bridge | None:
    return _bridge


def start(app, *, client_factory=None, clock=None):
    """Start the bridge when ``app.mqtt`` is configured; return it or ``None``. Never raises.

    Without the block nothing is imported and every hook stays a no-op. With it but no
    paho-mqtt, the error names the extra to install and the daemon runs on without MQTT:
    camera monitoring must not stop over an integration.
    """
    global _bridge
    cfg = getattr(app, "mqtt", None)
    if cfg is None or not cfg.enabled:
        return None
    try:
        factory = client_factory or paho_client_factory()
        bridge = Bridge(cfg, [c.name for c in app.cameras], client_factory=factory,
                        clock=clock)
    except MqttUnavailable as exc:
        log.error("%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - an integration must not block startup
        log.error("mqtt: bridge not started: %s", type(exc).__name__)
        return None
    _bridge = bridge
    return bridge.start()


def stop(timeout=STOP_TIMEOUT):
    """Publish ``offline`` and stop the worker (bounded). Never raises."""
    global _bridge
    bridge, _bridge = _bridge, None
    if bridge is not None:
        try:
            bridge.stop(timeout)
        except Exception:  # noqa: BLE001 - shutdown must finish
            pass


def _guarded(name, call):
    bridge = _bridge
    if bridge is None:
        return
    try:
        call(bridge)
    except Exception as exc:  # noqa: BLE001 - a hook must never break the daemon
        log.warning("mqtt: %s hook failed: %s", name, type(exc).__name__)


def note_detect(camera):
    _guarded("detect", lambda b: b.note_detect(camera))


def note_alert(camera, image=None, score=None):
    _guarded("alert", lambda b: b.note_alert(camera, image=image, score=score))


def observe(app, state):
    _guarded("observe", lambda b: b.observe(app, state))


def note_tick(ok):
    _guarded("tick", lambda b: b.note_tick(ok))
