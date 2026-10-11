#!/usr/bin/env python3
"""Accelerated soak: the real daemon loop against fake cameras on a fast fake clock.

Runs ``daemon.loop_step`` (the function production ticks) through N simulated days of a
generated event stream: people and bare motion, rain, camera outages, a scorer outage,
the whole network dropping, and daemon restarts that reload ``runtime.json``/``health.json``.
Nothing sleeps and no network is touched; the edge of the world is the scenario harness of
``tests/scenario.py`` (fake pytapo client, fake clock, recording notifier) plus a stand-in
scoring service injected under ``urllib.request.urlopen``.

It looks for what a long run would reveal and a unit test cannot: memory that keeps
growing, state files that keep growing, threads that pile up, exceptions out of a tick,
ticks that take far too long, a loop that never returns. A summary is printed and the exit
status is non-zero when a limit is exceeded.

    python3 tools/soak_sim.py --days 7 --seed 3 --cameras 3 --features all
    python3 tools/soak_sim.py --days 1 --max-rss-growth-mb 30 --max-threads 20 --json

``--wrapper`` goes through ``daemon.tick`` (stall watchdog with injected failing ticks, the
``/status`` view) instead of ``loop_step``; the file counts of the archives and ``sdjob_*``
temp dirs are always tracked (``--max-files-growth-per-day``, ``--inject filesleak``).

Must run from a checkout (it imports ``tests.scenario``) with the dev extras installed
(pytest provides the monkeypatch that undoes the harness patches). What it cannot show:
real network timing, real pytapo/ffmpeg behaviour, wall-clock-driven code that ignores the
``now`` the loop hands down, and the host's own retention timers (archive files are given
the simulated time as mtime hourly, so the daily prune runs; see docs/operations.md).
"""

from __future__ import annotations

import argparse
import faulthandler
import functools
import gc
import io
import json
import logging
import math
import os
import random
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from tapo_monitor import camera as camera_api  # noqa: E402
from tapo_monitor import daemon, ledger, notify, scorer, sdworker, sentlog, statusd  # noqa: E402
from tests.scenario import FakeCamera, Scenario, camera_dict, motion, pan_limit, person  # noqa: E402

FEATURES = ("zones", "loiter", "vehicle", "hub", "host", "scorer")
DAY = 86400.0
# Documentation address ranges only: nothing here names a real host.
HOST_BASE, HOST_ALT = "192.0.2.{}", "198.51.100.{}"
SCORER_URLS = ("http://203.0.113.10:8766/score", "http://203.0.113.11:8766/score")
ZONE = [[0.75, 0.0], [1.0, 0.0], [1.0, 1.0], [0.75, 1.0]]
STATE_FILES = ("health.json", "runtime.json", "twin.json")
# The archives and temp dirs whose file count is tracked, and each one's retention in days
# (the daemon's own constants) for the "oldest file" check.
ARCHIVES = ("sent", "review", "panlimit", "tmp")
RETENTION_DAYS = {"sent": sentlog.DEFAULT_RETENTION_DAYS,
                  "review": sentlog.DEFAULT_REVIEW_RETENTION_DAYS,
                  "panlimit": sentlog.PANLIMIT_RETENTION_DAYS}
# Daily maintenance plus the hourly granularity of the simulated mtimes.
RETENTION_SLACK_DAYS = 1.1
STALL_MESSAGE = "injected stall"


def parse_features(text):
    names = [t.strip() for t in text.split(",") if t.strip()]
    if names == ["all"]:
        return set(FEATURES)
    if names in ([], ["none"]):
        return set()
    unknown = set(names) - set(FEATURES)
    if unknown:
        raise ValueError(f"unknown feature(s): {', '.join(sorted(unknown))}; "
                         f"choose from {', '.join(FEATURES)}, all, none")
    return set(names)


# -- fake world ---------------------------------------------------------------

class SoakCamera(FakeCamera):
    """A scenario camera that answers on one address at a time and has a MAC."""

    def __init__(self, name, hosts, record, mac):
        super().__init__(name, hosts[0], record)
        self.address = hosts[0]
        self.mac = mac

    def getBasicInfo(self):  # noqa: N802 - pytapo's name
        self._require_online()
        return {"device_info": {"basic_info": {"mac": self.mac}}}


class FakeScorer:
    """The scoring service as the monitor sees it: random scores, outages, delays."""

    def __init__(self, rng):
        self.rng = rng
        self.down = set()           # URLs that refuse
        self.requests = 0
        self.failures = 0
        self.delay = 0.0            # simulated seconds the tick spent waiting on it

    def answer(self):
        r = self.rng
        n = r.choice((0, 0, 1, 1, 2))
        scores = sorted((round(r.uniform(0.05, 0.97), 3) for _ in range(n)), reverse=True)
        boxes = [[r.randint(0, 800), r.randint(0, 300), r.randint(850, 990), r.randint(310, 490)]
                 for _ in scores]
        veh = [round(r.uniform(0.3, 0.95), 3)] if r.random() < 0.15 else []
        vboxes = [[100, 200, 400, 480]] if veh else []
        return {"person": scores[0] if scores else 0.0, "animal": round(r.random() * 0.2, 3),
                "classes": {"person": scores[0] if scores else 0.0},
                "box": boxes[0] if boxes else None, "person_scores": scores,
                "person_boxes": boxes, "vehicle": veh[0] if veh else 0.0,
                "vehicle_scores": veh, "vehicle_boxes": vboxes, "w": 1000, "h": 500}

    def urlopen(self, req, timeout=None):
        self.requests += 1
        self.delay += self.rng.expovariate(1 / 0.4)
        url = req.full_url.split("?")[0]
        if url in self.down:
            self.failures += 1
            raise urllib.error.URLError("refused")
        return io.BytesIO(json.dumps(self.answer()).encode())

    def health_ok(self, url, timeout=2.0):
        return url not in self.down

    def score_for(self, _cfg):
        def score(_image):
            self.requests += 1
            if self.down >= set(SCORER_URLS):
                self.failures += 1
                return None
            return self.answer()["person"]
        score.boxes = {}
        return score


class FakeHub:
    """A hub client: clips are appended by the generator, searched by time range."""

    cameras = [{"alias": "hubcam", "device_id": "DEV1", "mac": "AABBCCDDEEFF",
                "model": "C410", "hub_storage": True, "record_24h": False}]

    def __init__(self):
        self.clips = []
        self.connected = True

    def add(self, start):
        self.clips.append({"start_time": float(start), "end_time": float(start) + 11,
                           "video_type": "2"})
        del self.clips[:-200]

    def list_cameras(self, now):
        return list(self.cameras)

    def search_clips(self, device_id, mac, since, until, now):
        return [c for c in self.clips if since <= c["start_time"] <= until]


class FakeTime:
    """``time`` as the sent-log module sees it: ``time()`` is the simulated clock, the rest
    is the real module. Frame names, index timestamps and retention then agree with the
    ``now`` the daemon hands down instead of mixing two epochs."""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock.now

    def __getattr__(self, name):
        return getattr(time, name)


class ErrorCounter(logging.Handler):
    """Counts ERROR records (the daemon logs and swallows many failures) and keeps a few.

    Also tallies the daemon's audit lines by ``action=`` so the summary shows which paths
    the run really exercised (a feature that never fired proves nothing).
    """

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.count = 0
        self.samples = []
        self.actions = {}
        self.stall_ticks = self.stall_alerts = self.stall_recoveries = 0

    def emit(self, record):
        message = record.getMessage()
        if STALL_MESSAGE in message and record.levelno >= logging.ERROR:
            self.stall_ticks += 1               # the point of the stall episode, not a finding
        elif record.levelno >= logging.ERROR:
            self.count += 1
            if len(self.samples) < 5:
                self.samples.append(message[:300])
        if message == "stall alert sent":
            self.stall_alerts += 1
        elif message == "stall recovery sent":
            self.stall_recoveries += 1
        if message.startswith("audit "):
            fields = dict(part.split("=", 1) for part in message.split()[1:] if "=" in part)
            if "action" in fields:
                key = f"{fields.get('path', '?')}/{fields['action']}"
                self.actions[key] = self.actions.get(key, 0) + 1
        elif message.startswith(("host switch", "scorer switch")):
            key = message.split(" ", 2)[0] + "_switch"
            self.actions[key] = self.actions.get(key, 0) + 1


# -- the simulation -----------------------------------------------------------

def build_cameras(n, features):
    cams = []
    for i in range(n):
        scorer_cfg = {"url": list(SCORER_URLS) if "scorer" in features else SCORER_URLS[0],
                      "threshold": 0.3, "motion_send_threshold": 0.6}
        sampler = {"enabled": True, "interval": 10, "max_frames": 4, "group_gap": 20}
        if "zones" in features:
            scorer_cfg.update(alert_zones=[ZONE], alert_zones_mode="observe")
        if "vehicle" in features:
            scorer_cfg.update(alert_vehicles=True, vehicle_mode="observe")
        if "loiter" in features:
            sampler.update(loiter_frames=3, loiter_mode="observe")
        hosts = [HOST_BASE.format(10 + i)]
        if "host" in features:
            hosts.append(HOST_ALT.format(10 + i))
        extra = {}
        if i % 2:
            extra = {"tracking": {"track_hold": 180, "back_time": 180,
                                  "day_preset": "2", "night_preset": "2"},
                     "pan_limit": pan_limit()}
        cams.append(camera_dict(f"cam{i}", hosts if len(hosts) > 1 else hosts[0],
                                scorer=scorer_cfg, sampler=sampler, **extra))
    if "hub" in features:
        cams.append(camera_dict("hubcam", HOST_BASE.format(200),
                                detection={"sources": ["hubpoll"]},
                                hub_host=HOST_BASE.format(201), go2rtc_src="hubcam",
                                hub_clip_worker=True))
    return cams


class Soak:
    def __init__(self, args, mp, tmp):
        self.args, self.mp, self.tmp = args, mp, Path(tmp)
        self.rng = random.Random(args.seed)
        self.features = parse_features(args.features)
        for var in ("SCENARIO_ONVIF_USER", "SCENARIO_ONVIF_PASSWORD"):
            mp.setenv(var, "x")
        mp.setenv(sentlog.ENV_DROP_SAMPLE, "0")
        # Temp dirs (the ``sdjob_*`` job dirs) land inside the run's own directory, where
        # they can be counted and are removed with it.
        (self.tmp / "tmp").mkdir(exist_ok=True)
        mp.setattr(tempfile, "tempdir", str(self.tmp / "tmp"))
        self.sc = Scenario(mp, self.tmp, build_cameras(args.cameras, self.features),
                           alerts={"cooldown": 120},
                           observability={"ledger": True})
        mp.setenv("TAPO_SENT_LOG_DIR", str(self.tmp / "sent"))
        mp.setenv("TAPO_REVIEW_LOG_DIR", str(self.tmp / "review"))
        mp.setenv("TAPO_LEDGER_FILE", str(self.tmp / "ledger.db"))
        mp.setattr(sentlog, "time", FakeTime(self.sc.clock))
        self.stop_threads = threading.Event()
        self.sleepers = []
        mp.setattr(notify, "send_photo", self._send_photo)
        self.fake_scorer = FakeScorer(self.rng)
        self.hub = FakeHub()
        self._wire_world()
        self.sd_worker = sdworker.SdWorker()
        self.hub_worker = sdworker.SdWorker() if "hub" in self.features else None
        self.stalled = False
        self.stalls = []
        self.status = {"snapshots": 0, "errors": 0, "max_seconds": 0.0, "samples": []}
        self.seen = {name: set() for name in ARCHIVES}   # every file ever observed, per dir
        self.stamped = set()
        self.last_stamp = self.sc.clock.now
        self.last_status = -1
        self.started_at = self.sc.clock.now
        if args.wrapper:
            self._wrap_loop_step()
        self.state_files = [self.tmp / n for n in STATE_FILES]
        self.leak = []
        self.restarts = 0
        self.exceptions = 0
        self.tracebacks = []
        self._ledger = self._ledger_handler = None
        self._start_ledger()
        self.schedule = self._plan()

    # wiring ------------------------------------------------------------------
    def _wire_world(self):
        sc, mp = self.sc, self.mp
        cams = {}
        by_host = {}
        for cfg in sc.app.cameras:
            hosts = list(camera_api.candidate_hosts(cfg))
            cam = SoakCamera(cfg.name, hosts, sc._record, f"AA-BB-CC-DD-EE-{len(cams):02X}")
            cams[cfg.name] = cam
            for h in hosts:
                by_host[h] = cam
        sc.cams, sc._by_host = cams, by_host

        def login(host, *_a, **_k):
            cam = by_host[host]
            if host != cam.address:
                raise ConnectionError("no route")
            cam._require_online()
            return cam

        mp.setattr(camera_api, "ping_reachable",
                   lambda host, *a, **k: by_host[host].online and host == by_host[host].address)
        mp.setattr(camera_api, "tapo_factory", lambda host, *a, **k: functools.partial(login, host))
        mp.setattr(camera_api, "find_alternate_host",
                   lambda cfg, connect=None, skip=(): next(
                       (h for h in camera_api.candidate_hosts(cfg)
                        if h != camera_api.active_host(cfg) and h not in skip
                        and by_host[h].online and h == by_host[h].address), None))
        mp.setattr(scorer.urllib.request, "urlopen", self.fake_scorer.urlopen)
        mp.setattr(scorer, "_health_ok", self.fake_scorer.health_ok)
        scorer._pools.clear()
        if "scorer" in self.features:
            scorer.restore_active([c.scorer for c in sc.app.cameras], {})
        if "hub" in self.features:
            tmp = self.tmp

            def frame_for(_cfg):
                return lambda: _jpeg(tmp, "hubframe")

            def clip_frame_for(_cfg, _device):
                # The real pipeline's shape: clip and frames share one ``sdjob_*`` dir that
                # the delivery removes with the last frame. A dir left behind is a leak.
                def fetch(_clip):
                    path = os.path.join(sdworker.job_dir(), "frame.jpg")
                    Path(path).write_bytes(b"\xff\xd8soak")
                    return path
                return fetch

            sc._collaborators["hubpoll"] = functools.partial(
                daemon.run_hubpoll_pass, hub_for=lambda _c, _s, _n: self.hub,
                frame_for=frame_for, clip_frame_for=clip_frame_for,
                score_for=self.fake_scorer.score_for)
        # Real frames are never read; rotating names keep the harness from filling the disk.
        sc._snapshot_for = self._snapshot_for
        # The pan guard's evidence grab (copied into panlimit-log, the original removed).
        mp.setattr(daemon.snapshot, "capture_rtsp", self._grab_evidence)

    def _send_photo(self, token, chat, image, caption, *a, camera=None, score=None,
                    incident=None, send_path=None, archive=True, archive_path=None, **k):
        """The scenario's recording notifier plus what the real ``send_photo`` does after
        Telegram answers: keep the frame in the sent log."""
        ok = self.sc.notifier.send_photo(token, chat, image, caption, *a, camera=camera,
                                         send_path=send_path, **k)
        if archive:
            try:
                data = Path(archive_path or image).read_bytes()
            except OSError:
                return ok
            sentlog.archive_if_configured(data, caption, delivered=ok, camera=camera,
                                          score=score, incident=incident,
                                          send_path=send_path)
        return ok

    def _grab_evidence(self, *_a, **_k):
        grabs = self.tmp / "grabs"
        grabs.mkdir(exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".jpg", dir=grabs)
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xff\xd8soak")
        return path

    def _wrap_loop_step(self):
        """``daemon.tick`` calls the module's ``loop_step`` with none of the harness's
        injection points: put them back, and fail on demand to exercise the stall watchdog."""
        real = daemon.loop_step
        sc = self.sc

        def loop_step(app, cam_clients, state, **kwargs):
            if self.stalled:
                raise RuntimeError(STALL_MESSAGE)
            injected = {k: v for k, v in sc.loop_kwargs().items()
                        if k not in ("sd_worker", "hub_worker")}
            return real(app, cam_clients, state, **{**injected, **kwargs})
        self.mp.setattr(daemon, "loop_step", loop_step)

    def _snapshot_for(self, cfg):
        cam = self.sc.cams[cfg.name]

        def grab(_client, _event):
            if not (cam.online and cam.rtsp_ok):
                return None
            return _jpeg(self.tmp, f"{cfg.name}-grab")
        return grab

    def _start_ledger(self):
        """What ``main`` does at start. One ledger and handler serve every simulated run of
        the daemon: the handler's writer thread never exits, and a real restart is a new
        process, so a handler per restart would be a leak of the harness's own making."""
        state = self.sc.state
        if self._ledger is None:
            self._ledger = ledger.EventLedger(self.tmp / "ledger.db")
            self._ledger_handler = ledger.AuditLedgerHandler(self._ledger)
            daemon.monitor.log.addHandler(self._ledger_handler)
        state.event_ledger = self._ledger
        state.ledger_handler = self._ledger_handler
        state.retention_maintenance = sentlog.RetentionMaintenance(
            event_ledger=self._ledger, ledger_retention_days=30)

    # schedule ------------------------------------------------------------------
    def _plan(self):
        """All world events of the run as a time-sorted list of ``(time, kind, payload)``."""
        r, a = self.rng, self.args
        plan = []
        self.stalls = []
        t0 = self.sc.clock.now
        names = [c.name for c in self.sc.app.cameras if c.name != "hubcam"]
        for day in range(math.ceil(a.days)):
            base = t0 + day * DAY
            for name in names:
                for _ in range(a.events_per_day):
                    # Night-weighted: two thirds in the dark hours.
                    hour = r.uniform(19, 30) % 24 if r.random() < 0.66 else r.uniform(6, 19)
                    kind = "person" if r.random() < 0.55 else "motion"
                    plan.append((base + hour * 3600, kind, name))
            for _ in range(_poisson(r, a.outages_per_day)):
                plan.append((base + r.uniform(0, DAY), "camera_out",
                             (r.choice(names), r.uniform(300, 2400))))
            for _ in range(_poisson(r, 1.0)):
                plan.append((base + r.uniform(0, DAY), "scorer_out",
                             (r.choice(("all", "primary")), r.uniform(180, 1200))))
            for _ in range(_poisson(r, a.net_drops_per_day)):
                plan.append((base + r.uniform(0, DAY), "net_out", r.uniform(120, 900)))
            for _ in range(_poisson(r, a.restarts_per_day)):
                plan.append((base + r.uniform(0, DAY), "restart", None))
            for _ in range(_poisson(r, 2.0)):
                plan.append((base + r.uniform(0, DAY), "rain", r.uniform(900, 5400)))
            if "host" in self.features:
                for _ in range(_poisson(r, 1.5)):
                    plan.append((base + r.uniform(0, DAY), "ip_change", r.choice(names)))
            if "hub" in self.features:
                for _ in range(a.events_per_day // 2):
                    plan.append((base + r.uniform(0, DAY), "hub_clip", None))
            if a.wrapper:
                # One stretch of failing ticks a day, longer than the stall threshold.
                length = self.sc.app.alerts.stall_threshold * r.uniform(1.3, 2.0)
                at = base + r.uniform(0, DAY - length - 3600)
                plan.append((at, "stall_on", length))
                self.stalls.append((at, at + length))
            # A lens swinging out of its span now and then exercises the pan guard.
            for _ in range(_poisson(r, 2.0)):
                plan.append((base + r.uniform(0, DAY), "pan_swing", r.choice(names)))
        plan.sort(key=lambda e: e[0])
        # Closing every interval is the generator's job: expand "start" items into a
        # start and an end so overlapping outages restore correctly.
        out = []
        for t, kind, payload in plan:
            if kind == "camera_out":
                out += [(t, "camera_down", payload[0]), (t + payload[1], "camera_up", payload[0])]
            elif kind == "scorer_out":
                out += [(t, "scorer_down", payload[0]), (t + payload[1], "scorer_up", payload[0])]
            elif kind == "net_out":
                out += [(t, "net_down", None), (t + payload, "net_up", None)]
            elif kind == "rain":
                out += [(t, "rain_on", None), (t + payload, "rain_off", None)]
            elif kind == "stall_on":
                out += [(t, "stall_on", None), (t + payload, "stall_off", None)]
            else:
                out.append((t, kind, payload))
        out.sort(key=lambda e: e[0])
        # A restart inside a stall would reset the watchdog's clock; keep the two apart.
        return [e for e in out if e[1] != "restart"
                or not any(lo - 60 <= e[0] <= hi + 60 for lo, hi in self.stalls)]

    # applying world events -------------------------------------------------------
    def apply(self, kind, payload, now):
        sc = self.sc
        if kind == "person":
            sc.cams[payload].push(person(now))
        elif kind == "motion":
            sc.cams[payload].push(motion(now))
        elif kind == "camera_down":
            sc.cams[payload].online = False
        elif kind == "camera_up":
            sc.cams[payload].online = True
        elif kind == "scorer_down":
            urls = SCORER_URLS if payload == "all" else SCORER_URLS[:1]
            self.fake_scorer.down |= set(urls)
        elif kind == "scorer_up":
            self.fake_scorer.down -= set(SCORER_URLS if payload == "all" else SCORER_URLS[:1])
        elif kind == "net_down":
            for cam in sc.cams.values():
                cam.online = False
            self.fake_scorer.down |= set(SCORER_URLS)
            sc.notifier.down = True
        elif kind == "net_up":
            for cam in sc.cams.values():
                cam.online = True
            self.fake_scorer.down -= set(SCORER_URLS)
            sc.notifier.down = False
        elif kind == "rain_on":
            sc.raining = True
        elif kind == "rain_off":
            sc.raining = False
        elif kind == "stall_on":
            self.stalled = True
        elif kind == "stall_off":
            self.stalled = False
        elif kind == "restart":
            self.restart(now)
        elif kind == "ip_change":
            cam = sc.cams[payload]
            hosts = list(camera_api.candidate_hosts(sc.app.cameras[
                [c.name for c in sc.app.cameras].index(payload)]))
            cam.address = hosts[(hosts.index(cam.address) + 1) % len(hosts)]
        elif kind == "hub_clip":
            self.hub.add(now)
        elif kind == "pan_swing":
            if sc.cams[payload].online:
                sc.cams[payload].pan_x = 0.63

    def restart(self, now):
        """What a stop and start does: shutdown() saves state, memory is lost, files reload."""
        sc = self.sc
        daemon.shutdown(sc.state, now=now, sd_worker=self.sd_worker, hub_worker=self.hub_worker)
        sc.restart()
        self._start_ledger()
        self.sd_worker = sdworker.SdWorker()
        if self.hub_worker is not None:
            self.hub_worker = sdworker.SdWorker()
        self.restarts += 1

    # running -------------------------------------------------------------------
    def step(self, now):
        sc = self.sc
        kwargs = {"sd_worker": self.sd_worker}
        if self.hub_worker is not None:
            kwargs["hub_worker"] = self.hub_worker
        sc._collaborators.update(kwargs)
        if self.args.wrapper:
            sc.last_control = daemon.tick(
                sc.app, sc.cam_clients, sc.state, now=now, secrets=sc.secrets,
                last_control=sc.last_control, control_interval=sc.control_interval,
                sd_worker=self.sd_worker, hub_worker=self.hub_worker)
            self.read_status(now)
        else:
            sc.tick()
        self.inject()
        if now - self.last_stamp >= 3600:
            self.stamp_mtimes(now)

    def read_status(self, now):
        """What the endpoint thread does per request, every ``--status-every`` ticks."""
        self.last_status += 1
        if self.last_status % self.args.status_every:
            return
        began = time.perf_counter()
        try:
            json.dumps(statusd.served_snapshot(self.sc.app, self.sc.state,
                                               started_at=self.started_at, now=now))
        except Exception as exc:  # noqa: BLE001 - the endpoint must survive anything
            self.status["errors"] += 1
            if len(self.status["samples"]) < 3:
                self.status["samples"].append(f"{type(exc).__name__}: {exc}"[:200])
        took = time.perf_counter() - began
        self.status["snapshots"] += 1
        self.status["max_seconds"] = max(self.status["max_seconds"], took)

    def inject(self):
        a = self.args
        if a.inject == "leak":
            self.leak.append(bytearray(a.inject_rate))
        elif a.inject == "statefile":
            with open(self.tmp / "runtime.json", "ab") as f:
                f.write(b"x" * a.inject_rate)
        elif a.inject == "threads":
            t = threading.Thread(target=self.stop_threads.wait, args=(3600,), daemon=True)
            t.start()
            self.sleepers.append(t)
        elif a.inject == "filesleak":
            # What a killed download leaves: a job dir with a segment in it, never removed.
            job = Path(sdworker.job_dir())
            (job / "clip.ts").write_bytes(b"x")

    # files -----------------------------------------------------------------------
    def archive_dirs(self):
        return {"sent": self.tmp / "sent", "review": self.tmp / "review",
                "panlimit": self.tmp / sentlog.PANLIMIT_DIR_NAME}

    def stamp_mtimes(self, now):
        """Give files written since the last pass the simulated time as their mtime.

        Retention compares a file's mtime with the ``now`` the daemon hands down, which
        here is a clock in the harness's 2026 epoch while the files are written in real
        time: without this nothing would ever look old and the daily prune would never be
        exercised. Hourly granularity is far below the retention windows (days). Names
        stay in ``seen`` so deletions can be counted.
        """
        self.last_stamp = now
        for name, path in self.archive_dirs().items():
            try:
                names = os.listdir(path)
            except OSError:
                continue
            for n in names:
                if not n.endswith(".jpg"):
                    continue
                full = os.path.join(path, n)
                self.seen[name].add(full)
                if full not in self.stamped:
                    self.stamped.add(full)
                    try:
                        os.utime(full, (now, now))
                    except OSError:
                        pass

    def count_files(self):
        out = {name: count_tree(path, suffix=".jpg") for name, path in self.archive_dirs().items()}
        out["tmp"] = self.count_job_dirs()
        return out

    def count_job_dirs(self):
        """``sdjob_*`` entries nobody owns. Deterministic, whatever the machine's speed.

        A job dir is legitimately alive while its job runs on a worker thread, while the
        finished result waits for the next ``poll`` and while a failed alert's frame waits
        in the retry queue. Those are not counted: the harness lets the running jobs
        finish (the worker threads are real) and sets aside the dirs of the results and
        retry frames it can see. What remains is a leak.
        """
        workers = [w for w in (self.sd_worker, self.hub_worker) if w is not None]
        deadline = time.monotonic() + 30
        while (time.monotonic() < deadline
               and any(w.in_flight() > len(w._done.queue) for w in workers)):
            time.sleep(0.005)
        owned = set()
        paths = [e.get("image") for e in self.sc.state.pending_hub]
        for w in workers:
            for _key, value, _error in list(w._done.queue):
                paths.extend(value if isinstance(value, (list, tuple)) else [value])
        for path in paths:
            if path:
                owned.add(os.path.dirname(str(path)))
        return sum(count_tree(p) + 1 for p in (self.tmp / "tmp").glob(
            sdworker.JOB_DIR_PREFIX + "*") if str(p) not in owned)

    def oldest_age_days(self, now):
        """Age of the oldest archived jpeg by simulated mtime (the stamped ones)."""
        out = {}
        for name, path in self.archive_dirs().items():
            ages = []
            for n in (os.listdir(path) if path.exists() else ()):
                full = os.path.join(path, n)
                if n.endswith(".jpg") and full in self.stamped:
                    ages.append(now - os.path.getmtime(full))
            out[name] = round(max(ages, default=0.0) / DAY, 2)
        return out

    def measure(self):
        # Compare retained memory, not cyclic garbage waiting for an interpreter-
        # dependent collection schedule (notably different on Python 3.14).
        # Referenced leaks, including the injected leak, remain visible in RSS.
        gc.collect()
        files = {p.name: p.stat().st_size if p.exists() else 0 for p in self.state_files}
        return {"rss_mb": rss_mb(), "threads": threading.active_count(),
                "files": files,
                "dirs": {n: dir_size(self.tmp / n) for n in ("sent", "review")},
                "ledger": file_size(self.tmp / "ledger.db"),
                "counts": self.count_files(),
                "tracemalloc_mb": (tracemalloc.get_traced_memory()[0] / 1e6
                                   if tracemalloc.is_tracing() else None)}


def _jpeg(tmp, stem, slots=16, _n=[0]):  # noqa: B006 - deliberate counter
    _n[0] += 1
    path = tmp / "frames" / f"{stem}-{_n[0] % slots}.jpg"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"\xff\xd8soak")
    return str(path)


def _poisson(rng, lam):
    n, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return n
        n += 1


def rss_mb():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def count_tree(path, suffix=""):
    """Files below ``path`` (ending in ``suffix``); 0 when it does not exist."""
    return sum(1 for _r, _d, names in os.walk(path) for n in names if n.endswith(suffix))


def dir_size(path):
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            total += file_size(Path(root) / n)
    return total


def run(args):
    """Run the simulation; returns the summary dict (see :func:`evaluate` for the verdict)."""
    errors = ErrorCounter()
    package_log = logging.getLogger("tapo_monitor")
    package_log.addHandler(errors)
    saved_log = (package_log.level, package_log.propagate)
    package_log.setLevel(logging.INFO)
    package_log.propagate = False          # the daemon's warnings are thousands of lines
    started = time.monotonic()
    ticks, durations, samples = 0, [], []
    total = int(args.days * DAY / args.tick)
    every = max(total // args.samples, 1)
    if args.tracemalloc:
        tracemalloc.start(8)
    snap0 = None
    with tempfile.TemporaryDirectory(prefix="soak-sim-", ignore_cleanup_errors=True) as tmp, pytest.MonkeyPatch.context() as mp:
        soak = Soak(args, mp, tmp)
        sc = soak.sc
        plan, idx = soak.schedule, 0
        end = sc.clock.now + args.days * DAY
        faulthandler.dump_traceback_later(args.hang_timeout, exit=True, file=sys.__stderr__)
        try:
            while sc.clock.now < end:
                now = sc.clock.now
                while idx < len(plan) and plan[idx][0] <= now:
                    soak.apply(plan[idx][1], plan[idx][2], now)
                    idx += 1
                t = time.perf_counter()
                try:
                    soak.step(now)
                except Exception as exc:  # noqa: BLE001 - the daemon's own tick wrapper does the same
                    soak.exceptions += 1
                    if len(soak.tracebacks) < 5:
                        soak.tracebacks.append(f"{type(exc).__name__}: {exc}"[:300])
                durations.append(time.perf_counter() - t)
                sc.clock.advance(args.tick + soak.fake_scorer.delay)
                soak.fake_scorer.delay = 0.0
                ticks += 1
                if ticks % every == 0:
                    samples.append({"tick": ticks, "sim_days": ticks * args.tick / DAY,
                                    **soak.measure()})
                    if args.tracemalloc and snap0 is None and len(samples) >= args.samples // 10:
                        snap0 = tracemalloc.take_snapshot()
                    faulthandler.dump_traceback_later(args.hang_timeout, exit=True, file=sys.__stderr__)
        finally:
            faulthandler.cancel_dump_traceback_later()
        top = []
        if snap0 is not None:
            diff = tracemalloc.take_snapshot().compare_to(snap0, "lineno")[:5]
            top = [f"{d.traceback[0].filename.split('/')[-1]}:{d.traceback[0].lineno} "
                   f"{d.size_diff / 1e3:+.0f} KB" for d in diff]
        if args.tracemalloc:
            tracemalloc.stop()
        soak.stop_threads.set()              # let the injected sleepers go
        for t in soak.sleepers:
            t.join(5)
        soak.stamp_mtimes(sc.clock.now)
        oldest = soak.oldest_age_days(sc.clock.now)
        seen = {name: len(files) for name, files in soak.seen.items()}
        final_counts = soak.count_files()
        shutdown_ok = True
        worker = getattr(sc.state.retention_maintenance, "_worker", None)
        if worker is not None:
            worker.join(10)                  # it writes into the directory removed below
        try:
            daemon.shutdown(sc.state, now=sc.clock.now, sd_worker=soak.sd_worker,
                            hub_worker=soak.hub_worker)
        except Exception:  # noqa: BLE001
            shutdown_ok = False
        soak_summary = {
            "alerts_sent": len(sc.actions("send")), "alerts_failed": len(sc.actions("send_failed")),
            "texts": len(sc.actions("text")), "restarts": soak.restarts,
            "scorer_requests": soak.fake_scorer.requests,
            "scorer_failures": soak.fake_scorer.failures,
            "world_events": idx}
        episodes = sum(1 for lo, hi in soak.stalls if lo < end)
        stall = {"episodes": episodes, "ticks": errors.stall_ticks,
                 "alerts": errors.stall_alerts, "recoveries": errors.stall_recoveries}
        status = {k: soak.status[k] for k in ("snapshots", "errors", "samples")}
        status["max_seconds"] = round(soak.status["max_seconds"], 4)
        package_log.removeHandler(errors)
        package_log.setLevel(saved_log[0])
        package_log.propagate = saved_log[1]
        daemon.monitor.log.removeHandler(sc.state.ledger_handler)
    return {"days": args.days, "seed": args.seed, "cameras": args.cameras,
            "features": sorted(soak.features), "ticks": ticks,
            "wall_seconds": round(time.monotonic() - started, 1),
            "samples": samples, "tick_seconds": tick_stats(durations),
            "exceptions": soak.exceptions + errors.count,
            "exception_samples": soak.tracebacks + errors.samples,
            "wrapper": bool(args.wrapper), "stall": stall, "status": status,
            "seen": seen, "final_counts": final_counts, "oldest_age_days": oldest,
            "shutdown_ok": shutdown_ok, "tracemalloc_top": top, "audit_actions": dict(
                sorted(errors.actions.items())), **soak_summary}


def tick_stats(durations):
    if not durations:
        return {"max": 0, "p99": 0, "mean": 0}
    ordered = sorted(durations)
    return {"max": round(ordered[-1], 4), "p99": round(ordered[int(len(ordered) * 0.99) - 1], 4),
            "mean": round(statistics.fmean(durations), 5)}


# -- verdict --------------------------------------------------------------------

def _window(series, lo, hi):
    n = len(series)
    part = series[int(n * lo):max(int(n * hi), int(n * lo) + 1)]
    return statistics.median(part) if part else 0.0


def growth(series):
    """Median of the last 10 % minus the median of the 10-30 % window (past warm-up)."""
    if len(series) < 4:
        return 0.0
    return _window(series, 0.9, 1.0) - _window(series, 0.1, 0.3)


def evaluate(summary, args):
    """Return ``(findings, metrics)``; findings are the exceeded limits, empty when clean."""
    samples = summary["samples"]
    rss = [s["rss_mb"] for s in samples]
    metrics = {
        "rss_start_mb": round(_window(rss, 0.1, 0.3), 1),
        "rss_end_mb": round(_window(rss, 0.9, 1.0), 1),
        "rss_growth_mb": round(growth(rss), 1),
        "threads_max": max((s["threads"] for s in samples), default=0),
        "state_file_kb_max": {name: round(max((s["files"][name] for s in samples), default=0) / 1024, 1)
                              for name in STATE_FILES},
        "dir_growth_mb_per_day": {}, "ledger_kb": round(samples[-1]["ledger"] / 1024, 1)
        if samples else 0,
        "files_final": {}, "files_growth_per_day": {}, "files_pruned": {},
    }
    days = max(summary["days"], 1e-9)
    for name in ("sent", "review"):
        metrics["dir_growth_mb_per_day"][name] = round(
            growth([s["dirs"][name] for s in samples]) / 1e6 / (days * 0.7), 2)
    final = summary.get("final_counts") or (samples[-1]["counts"] if samples else {})
    for name in ARCHIVES:
        series = [s["counts"][name] for s in samples]
        metrics["files_final"][name] = final.get(name, 0)
        metrics["files_growth_per_day"][name] = round(growth(series) / (days * 0.7), 1)
        # Files that existed once and are gone: retention (or the pipeline) removed them.
        metrics["files_pruned"][name] = max(summary.get("seen", {}).get(name, 0)
                                            - final.get(name, 0), 0) if name != "tmp" else 0
    findings = []
    if metrics["rss_growth_mb"] > args.max_rss_growth_mb:
        findings.append(f"RSS grew {metrics['rss_growth_mb']} MB (limit {args.max_rss_growth_mb})")
    if metrics["threads_max"] > args.max_threads:
        findings.append(f"{metrics['threads_max']} threads (limit {args.max_threads})")
    for name, kb in metrics["state_file_kb_max"].items():
        if kb > args.max_state_kb:
            findings.append(f"{name} reached {kb} KB (limit {args.max_state_kb})")
    for name, mb in metrics["dir_growth_mb_per_day"].items():
        if mb > args.max_dir_growth_mb_per_day:
            findings.append(f"{name} dir grows {mb} MB/day (limit {args.max_dir_growth_mb_per_day})")
    for name, per_day in metrics["files_growth_per_day"].items():
        if per_day > args.max_files_growth_per_day:
            findings.append(f"{name} file count grows {per_day} files/day "
                            f"(limit {args.max_files_growth_per_day})")
    for name, age in summary.get("oldest_age_days", {}).items():
        limit = RETENTION_DAYS.get(name)
        if limit is not None and age > limit + args.retention_slack_days:
            findings.append(f"{name} keeps a {age}-day-old file (retention {limit:g} days "
                            f"+ {args.retention_slack_days:g} slack): retention is not deleting")
    if summary.get("wrapper"):
        stall, status = summary["stall"], summary["status"]
        if stall["episodes"] and not stall["alerts"]:
            findings.append(f"stall watchdog: {stall['episodes']} stalled stretch(es), no alert")
        if stall["alerts"] and not stall["recoveries"]:
            findings.append("stall watchdog: alerts sent but never a recovery notice")
        if status["errors"]:
            findings.append(f"status snapshot raised {status['errors']} time(s): "
                            f"{status['samples'][:2]}")
        if status["max_seconds"] > args.max_status_seconds:
            findings.append(f"slowest status snapshot {status['max_seconds']} s "
                            f"(limit {args.max_status_seconds})")
    if summary["tick_seconds"]["max"] > args.max_tick_seconds:
        findings.append(f"slowest tick {summary['tick_seconds']['max']} s "
                        f"(limit {args.max_tick_seconds})")
    if summary["exceptions"] > args.max_exceptions:
        findings.append(f"{summary['exceptions']} exception/ERROR record(s) "
                        f"(limit {args.max_exceptions}): {summary['exception_samples'][:2]}")
    if not summary["shutdown_ok"]:
        findings.append("daemon.shutdown raised")
    return findings, metrics


def make_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--days", type=float, default=1, help="simulated days (default 1)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--cameras", type=int, default=2)
    p.add_argument("--features", default="all",
                   help=f"comma list of {', '.join(FEATURES)}, or all / none (default all)")
    p.add_argument("--tick", type=float, default=10.0, help="simulated seconds per loop step")
    p.add_argument("--events-per-day", type=int, default=60, help="per camera")
    p.add_argument("--outages-per-day", type=float, default=1.0, help="camera outages")
    p.add_argument("--net-drops-per-day", type=float, default=0.3)
    p.add_argument("--restarts-per-day", type=float, default=1.5)
    p.add_argument("--samples", type=int, default=100, help="measurements over the run")
    p.add_argument("--tracemalloc", action="store_true", help="also list top allocation growth")
    p.add_argument("--wrapper", action="store_true",
                   help="run through daemon.tick (stall watchdog, status view, injected "
                        "failing ticks) instead of calling loop_step directly")
    p.add_argument("--status-every", type=int, default=6,
                   help="with --wrapper, build the /status payload every N ticks (default 6)")
    p.add_argument("--json", action="store_true")
    g = p.add_argument_group("limits (non-zero exit when exceeded)")
    g.add_argument("--max-rss-growth-mb", type=float, default=20.0)
    g.add_argument("--max-threads", type=int, default=32)
    g.add_argument("--max-state-kb", type=float, default=256.0, help="health/runtime/twin json")
    g.add_argument("--max-dir-growth-mb-per-day", type=float, default=100.0)
    g.add_argument("--max-files-growth-per-day", type=float, default=1000.0,
                   help="per archive/temp dir: sent, review, panlimit, tmp (sdjob_*)")
    g.add_argument("--retention-slack-days", type=float, default=RETENTION_SLACK_DAYS,
                   help="how far past its retention the oldest archived file may be")
    g.add_argument("--max-status-seconds", type=float, default=0.5,
                   help="with --wrapper: slowest status snapshot")
    g.add_argument("--max-tick-seconds", type=float, default=5.0)
    g.add_argument("--max-exceptions", type=int, default=0)
    g.add_argument("--hang-timeout", type=float, default=600.0,
                   help="abort with a stack dump when no 1%% of the run completes in this long")
    d = p.add_argument_group("self-test")
    d.add_argument("--inject", choices=("leak", "statefile", "threads", "filesleak"),
                   help="deliberately misbehave, to prove the limits fire")
    d.add_argument("--inject-rate", type=int, default=65536, help="bytes per tick (leak, statefile)")
    return p


def render(summary, metrics, findings):
    s = summary
    lines = [f"soak-sim: {s['days']:g} day(s), {s['cameras']} camera(s), seed {s['seed']}, "
             f"features {','.join(s['features']) or 'none'}",
             f"  {s['ticks']} ticks in {s['wall_seconds']} s wall; tick mean/p99/max "
             f"{s['tick_seconds']['mean']}/{s['tick_seconds']['p99']}/{s['tick_seconds']['max']} s",
             f"  RSS {metrics['rss_start_mb']} -> {metrics['rss_end_mb']} MB "
             f"(growth {metrics['rss_growth_mb']} MB); threads max {metrics['threads_max']}",
             f"  state files max KB {metrics['state_file_kb_max']}; ledger {metrics['ledger_kb']} KB; "
             f"dir growth MB/day {metrics['dir_growth_mb_per_day']}",
             f"  alerts sent {s['alerts_sent']} (refused {s['alerts_failed']}), texts {s['texts']}, "
             f"restarts {s['restarts']}, scorer requests {s['scorer_requests']} "
             f"(failed {s['scorer_failures']}), exceptions {s['exceptions']}"]
    lines.append(f"  files final {metrics['files_final']}, growth/day "
                 f"{metrics['files_growth_per_day']}, pruned {metrics['files_pruned']}")
    if s["wrapper"]:
        lines.append(f"  wrapper: stall {s['stall']}, status snapshots {s['status']['snapshots']} "
                     f"(errors {s['status']['errors']}, slowest {s['status']['max_seconds']} s)")
    lines.append("  paths exercised: " + ", ".join(f"{k}={v}" for k, v in s["audit_actions"].items()))
    if s["tracemalloc_top"]:
        lines.append("  tracemalloc growth: " + "; ".join(s["tracemalloc_top"]))
    lines += [f"  FINDING: {f}" for f in findings] or ["  OK: no limit exceeded"]
    return "\n".join(lines)


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        parse_features(args.features)
    except ValueError as exc:
        print(f"soak-sim: {exc}", file=sys.stderr)
        return 2
    summary = run(args)
    findings, metrics = evaluate(summary, args)
    if args.json:
        print(json.dumps({**summary, "samples": len(summary["samples"]), "metrics": metrics,
                          "findings": findings}, indent=1))
    else:
        print(render(summary, metrics, findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
