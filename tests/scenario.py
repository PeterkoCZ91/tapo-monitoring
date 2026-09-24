"""Scenario harness: drive the real ``daemon.loop_step`` through scripted multi-tick stories.

Unit tests pin one pass at a time; the failures that hurt this fleet came from passes that
were each correct on their own and wrong together (a pan guard yanking a lens a hold was
keeping on a subject, a recall re-sent to a lens privacy mode had parked). A scenario
states the world tick by tick and asserts on the ordered list of things the daemon did.

What is real: ``loop_step`` and every pass it runs — control (``run_once``/``apply_plan``,
motion arbiter), camera connect (``_connect_camera``), outage watchdog, digital twin,
live monitor pass, hub poll, sampler, SD drain and the pan-limit guard. What is faked is
only the edge of the world:

* :class:`Clock` — the tick's ``now``; nothing sleeps (auto-track verify and connect
  retries are patched to return at once);
* :class:`FakeCamera` — a scripted pytapo client: queued events, preset positions, a pan
  axis auto-track can push past its span, privacy mode, motor refusals, going offline;
* ONVIF — ``daemon.panlimit`` is patched to read and move the same :class:`FakeCamera`;
* the notifier — ``notify.send_photo``/``send_text`` record instead of calling Telegram;
* the RTSP grab — ``snapshot_for`` writes a small file, or fails while ``rtsp_ok`` is off;
  the live pass and the sampler's follow-up grabs share it.

The daily review digest is stubbed (it is opt-in via env and asks the shared scorer over
HTTP); pass ``digest=`` to :class:`Scenario` to run something else.

Every motor command, delivery and auto-track change lands in :attr:`Scenario.timeline` as
``(now, action)``; ``action`` is one of::

    ("recall", cam, preset)     pytapo preset recall attempted by the control pass
    ("goto", cam, preset)       ONVIF GotoPreset sent by the pan-limit guard
    ("autotrack", cam, on)      auto-track master switch changed on the camera
    ("send", cam)               alert photo delivered
    ("send_failed", cam)        alert photo refused (notifier down)
    ("text", message)           operational text delivered (outage, drift, ...)
"""

from __future__ import annotations

import functools
import types

from tapo_monitor import camera as camera_api
from tapo_monitor import config as cfg_mod
from tapo_monitor import daemon, enrich, health, notify, runtime_state, tracking, twin

START = 1_790_000_000.0          # a fixed epoch (2026-09) so captions/windows are stable
PERSON_BIT = 524288              # events_1 bit 19: camera-confirmed AI person
MOTION_BIT = 2                   # bare motion, no person, no PIR

# Pan positions of the camera's own presets; the outermost two are the guard's span.
DEFAULT_PRESETS = {"1": 0.39, "2": 0.50, "3": 0.61}

MOTOR_BUSY = "Exception: Error: MOTOR_BUSY, Error Code: -64304"


def person(at, **extra):
    """A camera-confirmed person event starting at ``at``."""
    return {"start_time": at, "events_1": PERSON_BIT, **extra}


def motion(at, **extra):
    """A bare motion event (no person bit) starting at ``at``."""
    return {"start_time": at, "events_1": MOTION_BIT, **extra}


def camera_dict(name, host, **overrides):
    """One camera's raw config: raw-mode enrich (no Groq), everything else default.

    Top-level keys in ``overrides`` replace the defaults outright.
    """
    base = {"name": name, "host": host, "enrich": {"groq": False}}
    base.update(overrides)
    return base


def pan_limit(**overrides):
    """A ``pan_limit`` block with the guard on (6 s poll, 20 s hold grace by default)."""
    block = {"enabled": True, "margin": 0.01, "poll_interval": 6,
             "onvif_user_env": "SCENARIO_ONVIF_USER",
             "onvif_password_env": "SCENARIO_ONVIF_PASSWORD"}
    block.update(overrides)
    return block


class Clock:
    """The scenario's wall clock. Only ``Scenario.tick`` hands it to the daemon."""

    def __init__(self, start=START):
        self.now = float(start)

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FakeCamera:
    """A scripted pytapo client plus the ONVIF view of the same lens.

    Script it between ticks: :meth:`push` queues events for the next ``getEvents``,
    :attr:`pan_x` is where the lens points (auto-track moving it = assigning it), and
    :attr:`online`, :attr:`privacy`, :attr:`motor_refusal`, :attr:`events_error` and
    :attr:`rtsp_ok` switch failure modes on and off.

    A class attribute set to ``None`` models firmware without that call: the twin reads
    it as ``missing_method`` and ``set_autotrack`` falls through to ``executeFunction``.
    """

    def __init__(self, name, host, record, presets=None):
        self.name = name
        self.host = host
        self._record = record
        self.presets = dict(presets or DEFAULT_PRESETS)
        self.pan_x = self.presets.get("2", 0.5)
        self.online = True
        self.privacy = False
        self.motor_refusal = None       # exception raised by every preset recall
        self.events_error = None        # exception raised by getEvents
        self.rtsp_ok = True
        self.autotrack = None           # unknown until the daemon asserts it
        self.back_time = None
        self.refuse_autotrack = False   # firmware that accepts no auto-track call at all
        self._pending = []

    # ── scripting ────────────────────────────────────────────────────────────
    def push(self, *events):
        """Queue events for the next ``getEvents`` poll."""
        self._pending.extend(events)

    def _require_online(self):
        if not self.online:
            raise ConnectionError(f"{self.name}: camera did not answer")

    # ── pytapo surface used by the daemon ────────────────────────────────────
    def getEvents(self):
        self._require_online()
        if self.events_error is not None:
            raise self.events_error
        batch, self._pending = self._pending, []
        return batch

    def setPreset(self, preset):
        self._record(("recall", self.name, str(preset)))
        self._require_online()
        if self.privacy:
            raise Exception(MOTOR_BUSY)
        if self.motor_refusal is not None:
            raise self.motor_refusal
        self.pan_x = self.presets[str(preset)]

    def setMotionDetection(self, **_):
        self._require_online()

    def setDayNightMode(self, _mode):
        self._require_online()

    def setPersonDetection(self, *_a, **_k):
        self._require_online()

    def setVehicleDetection(self, *_a, **_k):
        self._require_online()

    def reboot(self):
        self._require_online()

    def _set_autotrack(self, enabled, back_time=None):
        self._require_online()
        if self.refuse_autotrack:
            raise Exception("-40106 unsupported")
        if back_time is not None:
            self.back_time = str(back_time)
        if enabled != self.autotrack:
            self._record(("autotrack", self.name, enabled))
        self.autotrack = enabled

    def setAutoTrackTarget(self, enabled):
        self._set_autotrack(bool(enabled))

    def executeFunction(self, method, params):
        self._require_online()
        if method == "setSmartTrackConfig":
            return {}
        if method in ("setTargetTrackConfig", "setAutoTrackTarget"):
            info = (params.get("target_track", {}).get("target_track_info")
                    or params.get("auto_track_target") or {})
            self._set_autotrack(info.get("enabled") == "on", info.get("back_time"))
            return {}
        raise Exception(f"-40210 {method} not supported by scenario camera")

    def getAutoTrackTarget(self):
        self._require_online()
        info = {"enabled": "on" if self.autotrack else "off"}
        if self.back_time is not None:
            info["back_time"] = self.back_time
        return info

    def getPrivacyMode(self):
        self._require_online()
        return {"enabled": "on" if self.privacy else "off"}


class Notifier:
    """Records Telegram traffic instead of sending it. ``down`` refuses every send.

    ``send_paths`` lists the delivery path each photo send named for the sent log.
    """

    def __init__(self, record):
        self._record = record
        self.down = False
        self.send_paths = []

    def send_photo(self, token, chat, image, caption, *a, camera=None, **k):
        self.send_paths.append(k.get("send_path"))
        if self.down:
            self._record(("send_failed", camera))
            return False
        self._record(("send", camera))
        return True

    def send_text(self, token, chat, message, *a, **k):
        if self.down:
            return False
        self._record(("text", message))
        return True


class Scenario:
    """A fleet config, its fake cameras and a clock; :meth:`tick` runs one real loop step.

    ``night`` and ``raining`` are plain attributes the scenario flips between ticks; the
    control pass, mute gate and watchdog all read them through the daemon's own injection
    points. Collaborator overrides (``digest=``, ``inspect=``, ...) go straight through to
    ``loop_step``.
    """

    def __init__(self, monkeypatch, tmp_path, cameras, *, alerts=None, observability=None,
                 control_interval=60, start=START, presets=None, **collaborators):
        raw = {"cameras": list(cameras),
               "telegram": {"token_env": "SCENARIO_TG_TOKEN",
                            "chat_id_env": "SCENARIO_TG_CHAT"}}
        if alerts is not None:
            raw["alerts"] = alerts
        if observability is not None:
            raw["observability"] = observability
        self.app = cfg_mod.load_config_from_dict(raw)
        self.clock = Clock(start)
        self.timeline = []
        self._tmp = tmp_path
        self.state = self._fresh_state()
        self.cam_clients = {}
        self.last_control = None
        self.control_interval = control_interval
        self.night = True
        self.raining = False
        self.secrets = {"telegram_token": "t", "telegram_chat": "c", "groq_key": "",
                        "face_names": {}}
        self.notifier = Notifier(self._record)
        self.cams = {c.name: FakeCamera(c.name, c.host, self._record, presets)
                     for c in self.app.cameras}
        self._by_host = {c.host: self.cams[c.name] for c in self.app.cameras}
        self._shots = 0
        self._collaborators = {"digest": lambda **_: None, **collaborators}
        self._patch(monkeypatch)

    def _fresh_state(self):
        """A MonitorState wired to durable files under tmp, restored the way ``main`` does."""
        state = daemon.MonitorState()
        state.health_path = str(self._tmp / "health.json")
        health.load_state(state.health_path, state)
        state.twin_path = str(self._tmp / "twin.json")
        state.twin_fleet = twin.load_state(state.twin_path)
        state.twin_alerted = {name: set(entry.get("alerted_keys", []))
                              for name, entry in state.twin_fleet.items()}
        state.runtime_path = str(self._tmp / "runtime.json")
        runtime_state.load(state.runtime_path, state, self.clock.now)
        return state

    def restart(self):
        """Simulate a daemon restart: memory is gone, only what ``main`` reloads survives.

        Mirrors the startup in ``daemon.main`` (health, twin and runtime state files). When
        startup learns to restore more, teach :meth:`_fresh_state` the same.
        """
        self.state = self._fresh_state()
        self.cam_clients = {}
        self.last_control = None
        daemon._recall_state.clear()

    # ── wiring ───────────────────────────────────────────────────────────────
    def _record(self, action):
        self.timeline.append((self.clock.now, action))

    def _patch(self, mp):
        for var in ("TAPO_REVIEW_LOG_DIR", "TAPO_SENT_LOG_DIR", "RECORDING_ROOT",
                    "TAPO_REVIEW_DIGEST_TIME"):
            mp.delenv(var, raising=False)
        # Module-level throttles that would otherwise leak between scenarios.
        mp.setattr(daemon, "_recall_state", {})
        mp.setattr(tracking, "_BACK_TIME_WARNED", set())
        # Nothing sleeps: auto-track verify waits 1-6 s, connect retries 5 s apart.
        mp.setattr(tracking, "_time", types.SimpleNamespace(sleep=lambda _s: None))
        mp.setattr(camera_api, "connect",
                   functools.partial(camera_api.connect, sleep=lambda _s: None))
        # Network edge: ping, login, Telegram, Groq.
        mp.setattr(camera_api, "ping_reachable",
                   lambda host, *a, **k: self._by_host[host].online)
        mp.setattr(camera_api, "tapo_factory",
                   lambda host, *a, **k: functools.partial(self._login, host))
        mp.setattr(notify, "send_photo", self.notifier.send_photo)
        mp.setattr(notify, "send_text", self.notifier.send_text)
        mp.setattr(enrich, "groq_describe", lambda *a, **k: "")
        # Any grab outside snapshot_for (the guard's evidence frame once a review log is
        # set) would start a real ffmpeg against the fake host: it finds nothing.
        mp.setattr(daemon.snapshot, "capture_rtsp", lambda *a, **k: None)
        # ONVIF: the guard reads and moves the same fake lens the control pass recalls.
        bounds_from_presets = daemon.panlimit.bounds_from_presets
        mp.setattr(daemon.panlimit, "build_ptz", self._build_ptz)
        mp.setattr(daemon.panlimit, "read_preset_bounds",
                   lambda ptz, tok: bounds_from_presets(list(ptz.presets.items())))
        mp.setattr(daemon.panlimit, "read_pan_x", self._read_pan_x)
        mp.setattr(daemon.panlimit, "goto_preset", self._goto_preset)

    def _login(self, host):
        cam = self._by_host[host]
        cam._require_online()
        return cam

    def _build_ptz(self, host, port, user, password):
        cam = self._by_host[host]
        cam._require_online()
        return cam, "profile"

    @staticmethod
    def _read_pan_x(cam, _token):
        cam._require_online()
        return cam.pan_x

    def _goto_preset(self, cam, _token, preset):
        self._record(("goto", cam.name, str(preset)))
        cam._require_online()
        if cam.privacy:
            raise Exception(MOTOR_BUSY)
        cam.pan_x = cam.presets[str(preset)]

    def _snapshot_for(self, cfg):
        cam = self.cams[cfg.name]

        def grab(_client, _event):
            if not (cam.online and cam.rtsp_ok):
                return None
            self._shots += 1
            path = self._tmp / f"{cfg.name}-{self._shots}.jpg"
            path.write_bytes(b"\xff\xd8scenario")
            return str(path)
        return grab

    # ── running ──────────────────────────────────────────────────────────────
    def tick(self, advance=0.0):
        """Run one ``loop_step`` at the current time, then move the clock by ``advance``.

        Calls ``loop_step`` directly (not the daemon's ``tick`` wrapper, which swallows
        exceptions), so a pass that raises fails the scenario instead of hiding in a log.
        """
        now = self.clock.now
        def night():
            return self.night

        def raining(_now, *, threshold, poll_interval):
            return self.raining

        control = functools.partial(daemon.run_once, is_night=night, is_raining=raining)
        monitor = functools.partial(daemon.run_monitor_pass, snapshot_for=self._snapshot_for,
                                    time_str=lambda _e: "scenario")
        sample = functools.partial(daemon.process_sampler, snapshot_for=self._snapshot_for,
                                   time_str=lambda _e: "scenario")
        kwargs = {"run_control": control, "monitor": monitor, "sample": sample,
                  "is_night": night, **self._collaborators}
        self.last_control = daemon.loop_step(
            self.app, self.cam_clients, self.state, now=now, secrets=self.secrets,
            last_control=self.last_control, control_interval=self.control_interval,
            **kwargs)
        self.clock.advance(advance)
        return now

    def run(self, seconds, every=5.0):
        """Tick every ``every`` seconds for ``seconds``; the clock ends ``seconds`` later."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.tick(advance=every)

    # ── reading the record ───────────────────────────────────────────────────
    def actions(self, *kinds, since=None):
        """Recorded actions in order, optionally only the given kinds / from ``since``."""
        return [a for t, a in self.timeline
                if (not kinds or a[0] in kinds) and (since is None or t >= since)]

    def when(self, action):
        """Times at which exactly ``action`` was recorded."""
        return [t for t, a in self.timeline if a == action]

    def motor(self, since=None):
        """Every motor command (recall and goto), in order."""
        return self.actions("recall", "goto", since=since)


def collapse(actions):
    """Drop consecutive repeats: the control pass re-sends its preset every pass by design,
    and most stories care about the order of distinct moves, not the re-assertions."""
    out = []
    for action in actions:
        if not out or out[-1] != action:
            out.append(action)
    return out
