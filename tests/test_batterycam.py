import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import batterycam as bc
from tapo_monitor.config import BatteryWakeConfig

T0 = 1_800_000_000


class Clock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


def _cfg(**kw):
    return types.SimpleNamespace(name="door", rotate=0, battery_wake=BatteryWakeConfig(**kw))


class FakeClient:
    def __init__(self, clock, events, recordings=None, tc=0):
        self.clock, self.events, self.tc = clock, events, tc
        self.recordings = recordings if recordings is not None else []
        self.calls = []

    def getTimeCorrection(self):
        return self.tc

    def getEvents(self, start, end):
        self.calls.append(("events", start, end))
        return [dict(e) for e in self.events]

    def getRecordings(self, date):
        self.calls.append(("rec", date))
        return self.recordings


def _rec(start, end):
    return [{"search_video_results_0": {"startTime": start, "endTime": end, "vedio_type": 2}}]


def _run(cfg, client, clock, cursor, tmp_path, frames=("a.jpg",), downloaded=True,
         snapshot=None):
    got = {}

    def download(c, s, e, out):
        got["dl"] = (s, e)
        if downloaded:
            with open(out, "wb") as f:
                f.write(b"x")
            return out
        return None

    def extract(clip, d, dur):
        got["dur"] = dur
        out = []
        for n in frames:
            p = os.path.join(d, n)
            with open(p, "wb") as f:
                f.write(b"j")
            out.append(p)
        return out

    jobs, cur = bc.collect(cfg, lambda: client, cursor, now=clock.now, sleep=clock.sleep,
                           download=download, extract=extract, job_dir=lambda: str(tmp_path),
                           snapshot=snapshot or (lambda c, s, o: None))
    return jobs, cur, got


def test_is_ring_and_ring_state():
    assert bc.is_ring({"events_1": 66050})
    assert not bc.is_ring({"events_1": 34})
    assert not bc.is_ring({})
    assert bc.ring_state({"events_1": 66050, "alarm_type": 17}) == "missed"
    assert bc.ring_state({"events_1": 131618, "alarm_type": 18}) == "answered"
    assert bc.ring_state({"events_1": 34, "alarm_type": 18}) is None
    assert bc.ring_state({"events_1": 512, "alarm_type": 99}) is None


def test_jittered_event_is_not_new():
    assert not bc.is_new(101, 100, [])
    assert not bc.is_new(99, 100, [])
    assert bc.is_new(110, 100, [])
    assert not bc.is_new(111, 100, [110])
    assert bc.is_new(120, 100, [110])


def test_match_recording_tolerance():
    recs = [{"start_time": 100, "end_time": 120}, {"start_time": 200, "end_time": 220}]
    assert bc.match_recording(103, recs) == recs[0]
    assert bc.match_recording(106, recs) is None
    assert bc.match_recording(197, recs) == recs[1]


def test_parse_recordings():
    raw = _rec(200, 220) + _rec(100, 120)
    assert bc.parse_recordings(raw) == [{"start_time": 100, "end_time": 120},
                                        {"start_time": 200, "end_time": 220}]
    assert bc.parse_recordings(None) == []
    assert bc.parse_recordings([{"x": 1}, "junk"]) == []


def test_cursor_none_starts_at_now():
    clock = Clock()
    called = []
    jobs, cur = bc.collect(_cfg(), lambda: called.append(1), None, now=clock.now,
                           sleep=clock.sleep)
    assert (jobs, cur, called) == ([], T0, [])


def test_collect_waits_for_event_end(tmp_path):
    clock = Clock()
    client = FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 30,
                                 "events_1": 34, "alarm_type": 6}], _rec(T0 + 11, T0 + 31))
    jobs, cur, got = _run(_cfg(), client, clock, T0, tmp_path, frames=("1.jpg", "2.jpg"))
    assert clock.sleeps >= 1 and clock.now() >= T0 + 30
    assert len(jobs) == 1 and cur == T0 + 10
    job = jobs[0]
    assert job.camera == "door" and job.clip.endswith(".mp4")
    assert [os.path.basename(f) for f in job.frames] == ["1.jpg", "2.jpg"]
    assert got["dl"] == (T0 + 11, T0 + 31) and got["dur"] == 20
    assert [c for c in client.calls if c[0] == "rec"]


def test_frames_limited_to_config(tmp_path):
    clock = Clock(T0 + 25)
    client = FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 20}],
                        _rec(T0 + 10, T0 + 20))
    names = tuple(f"{i}.jpg" for i in range(6))
    jobs, _, _ = _run(_cfg(frames=2), client, clock, T0, tmp_path, frames=names)
    assert [os.path.basename(f) for f in jobs[0].frames] == ["0.jpg", "5.jpg"]
    assert sorted(os.listdir(tmp_path)) == ["0.jpg", "5.jpg", f"battery_{T0 + 10}.mp4"]


def test_time_correction_applied(tmp_path):
    clock = Clock(T0 + 25)
    client = FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 20}],
                        _rec(T0 + 3, T0 + 13), tc=7)
    jobs, _, got = _run(_cfg(), client, clock, T0, tmp_path)
    assert got["dl"] == (T0 + 3, T0 + 13)  # camera seconds
    assert client.calls[0][1:] == (T0 + 25 - 600 - 7, T0 + 25 + 60 - 7)
    assert jobs[0].clip


def test_unfinished_event_left_for_next_wake(tmp_path):
    clock = Clock(T0 + 30)
    events = [{"start_time": T0 + 10, "end_time": T0 + 20},
              {"start_time": T0 + 100, "end_time": T0 + 5000},
              {"start_time": T0 + 200, "end_time": T0 + 210}]
    client = FakeClient(clock, events, _rec(T0 + 10, T0 + 20))
    jobs, cur, _ = _run(_cfg(event_wait=30), client, clock, T0, tmp_path)
    assert [j.event["start_time"] for j in jobs] == [T0 + 10]
    assert cur == T0 + 10  # stops before the running one
    assert clock.now() >= T0 + 60


def test_collect_gives_up_when_login_fails():
    clock = Clock()
    attempts = []

    def factory():
        attempts.append(1)
        raise OSError("asleep")

    jobs, cur = bc.collect(_cfg(), factory, T0, now=clock.now, sleep=clock.sleep)
    assert (jobs, cur, len(attempts), clock.sleeps) == ([], T0, 1, 0)


def test_missing_recording_yields_job_without_frames(tmp_path):
    clock = Clock(T0 + 40)
    client = FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 20,
                                 "events_1": 512}], [])
    jobs, cur, got = _run(_cfg(), client, clock, T0, tmp_path)
    assert len(jobs) == 1 and jobs[0].frames == [] and jobs[0].clip is None
    assert cur == T0 + 10 and "dl" not in got


def test_failed_download_keeps_event(tmp_path):
    clock = Clock(T0 + 40)
    client = FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 20}],
                        _rec(T0 + 10, T0 + 20))
    jobs, _, _ = _run(_cfg(), client, clock, T0, tmp_path, downloaded=False)
    assert jobs[0].frames == [] and jobs[0].clip is None


def test_old_and_jittered_events_ignored(tmp_path):
    clock = Clock(T0 + 500)
    client = FakeClient(clock, [{"start_time": T0 - 50, "end_time": T0 - 40},
                                {"start_time": T0 + 1, "end_time": T0 + 11}], [])
    jobs, cur, _ = _run(_cfg(event_wait=20), client, clock, T0, tmp_path)
    assert jobs == [] and cur == T0


def test_event_list_failure_never_raises(tmp_path):
    clock = Clock()

    class Boom(FakeClient):
        def getEvents(self, s, e):
            raise RuntimeError("nope")

    jobs, cur, _ = _run(_cfg(), Boom(clock, []), clock, T0, tmp_path)
    assert (jobs, cur) == ([], T0)


def test_spread():
    assert bc.spread(list("abc"), 6) == list("abc")
    assert bc.spread(list("abcdef"), 1) == ["d"]
    assert len(bc.spread(list("abcdef"), 3)) == 3


def _snap_ok(calls):
    def snap(c, start, out):
        calls.append(start)
        with open(out, "wb") as f:
            f.write(b"s")
        return out
    return snap


def _one_event_client(clock, tc=0, recs=True):
    return FakeClient(clock, [{"start_time": T0 + 10, "end_time": T0 + 20}],
                      _rec(T0 + 10 - tc, T0 + 20 - tc) if recs else [], tc=tc)


def test_snapshot_used_when_download_fails(tmp_path):
    clock = Clock(T0 + 40)
    calls = []
    jobs, _, _ = _run(_cfg(), _one_event_client(clock), clock, T0, tmp_path,
                      downloaded=False, snapshot=_snap_ok(calls))
    assert len(jobs[0].frames) == 1 and jobs[0].frames[0].endswith("_snap.jpg")
    assert os.path.exists(jobs[0].frames[0]) and jobs[0].clip is None
    assert calls == [T0 + 10]


def test_snapshot_used_when_extract_returns_nothing(tmp_path):
    clock = Clock(T0 + 40)
    calls = []
    jobs, _, _ = _run(_cfg(), _one_event_client(clock), clock, T0, tmp_path,
                      frames=(), snapshot=_snap_ok(calls))
    assert len(jobs[0].frames) == 1 and len(calls) == 1


def test_snapshot_not_called_when_clip_frames_exist(tmp_path):
    clock = Clock(T0 + 40)
    calls = []
    jobs, _, _ = _run(_cfg(), _one_event_client(clock), clock, T0, tmp_path,
                      snapshot=_snap_ok(calls))
    assert calls == [] and len(jobs[0].frames) == 1 and jobs[0].clip


def test_snapshot_failure_leaves_no_frames(tmp_path):
    clock = Clock(T0 + 40)

    def boom(c, s, o):
        raise RuntimeError("x")

    jobs, _, _ = _run(_cfg(), _one_event_client(clock), clock, T0, tmp_path,
                      downloaded=False, snapshot=boom)
    assert jobs[0].frames == [] and jobs[0].clip is None


def test_snapshot_without_recording_uses_camera_time(tmp_path):
    clock = Clock(T0 + 40)
    calls = []
    client = _one_event_client(clock, tc=7, recs=False)
    jobs, _, _ = _run(_cfg(), client, clock, T0, tmp_path, snapshot=_snap_ok(calls))
    assert calls == [T0 + 10 - 7] and len(jobs[0].frames) == 1
