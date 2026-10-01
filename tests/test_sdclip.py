import asyncio
import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import sdclip


class _Cam:
    def getTimeCorrection(self):
        return 0


def _cfg(**kw):
    base = {"host": "203.0.113.12", "user_env": "U", "password_env": "P",
            "cloud_password_env": "C"}
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── SD_FRESH_DELAY vs pytapo's freshness guard ────────────────────────────────────

def test_fresh_delay_clears_pytapo_freshness_guard_for_full_span():
    """The download window END (seg_start + SD_SPAN) must already be past pytapo's
    FRESH_RECORDING_TIME_SECONDS guard when the follow-up fires, or the Downloader
    yields "Recording in progress" and produces an empty file (regression seen live
    2026-07-02..05: SD_SPAN went 12->36 while SD_FRESH_DELAY stayed 75, so ~3 of 4
    downloads returned no segment and the alert fell back to a stale live photo)."""
    from pytapo.media_stream.downloader import Downloader

    assert sdclip.SD_FRESH_DELAY >= (
        sdclip.SD_SPAN + Downloader.FRESH_RECORDING_TIME_SECONDS + 5
    ), "SD follow-up fires before pytapo will serve the window end"


# ── event_span: window sized from the camera's own event seconds ──────────────────

def test_event_span_follows_camera_end_time():
    """The camera reports the event's real duration (start_time..end_time); the window
    must cover it instead of assuming a fixed span (live 2026-07-06 01:01: a 73 s person
    event had its subject-relevant footage outside the first 36 s), capped by the Pi Zero
    download budget and never smaller than the proven default."""
    assert sdclip.event_span({"start_time": 1000, "end_time": 1073}) == sdclip.SD_SPAN_CAP
    assert sdclip.event_span({"start_time": 1000, "end_time": 1042}) == 42
    assert sdclip.event_span({"start_time": 1000, "end_time": 1020}) == sdclip.SD_SPAN
    assert sdclip.event_span({"start_time": 1000}) == sdclip.SD_SPAN
    assert sdclip.event_span({"start_time": 1000, "end_time": 900}) == sdclip.SD_SPAN
    assert sdclip.event_span({}) == sdclip.SD_SPAN


def test_fresh_delay_scales_with_event_span():
    """Whatever span the event dictates, the follow-up must fire only once the window
    END is past pytapo's freshness guard — otherwise the download yields an empty file."""
    from pytapo.media_stream.downloader import Downloader

    for span in (sdclip.SD_SPAN, 42, sdclip.SD_SPAN_CAP):
        assert sdclip.fresh_delay(span) >= (
            span + Downloader.FRESH_RECORDING_TIME_SECONDS + 5
        )


# ── fetch_sd_frames_subprocess: run the download in a FRESH process ───────────────

def test_fetch_subprocess_parses_marked_frame_paths():
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(
            returncode=0,
            stdout="some pytapo noise\nFRAME:/tmp/a_00.jpg\nFRAME:/tmp/a_06.jpg\n",
            stderr="RuntimeWarning ...")

    out = sdclip.fetch_sd_frames_subprocess(
        _cfg(), 1000, out_dir="/tmp", span=12, every=6, run=run, python="PY")
    assert out == ["/tmp/a_00.jpg", "/tmp/a_06.jpg"]   # only FRAME:-marked lines
    argv = captured["argv"]
    assert argv[:4] == ["PY", "-m", "tapo_monitor.sdclip", "download"]
    assert "203.0.113.12" in argv and "1000" in argv        # host + start passed through


def test_fetch_subprocess_empty_on_nonzero_exit():
    def run(argv, **kw):
        return types.SimpleNamespace(returncode=4, stdout="FRAME:/tmp/a.jpg", stderr="boom")
    assert sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY") == []


def test_fetch_subprocess_passes_camera_rotation():
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    sdclip.fetch_sd_frames_subprocess(_cfg(rotate=180), 1000, span=12, every=6,
                                      run=run, python="PY")
    assert captured["argv"][-1] == "180"   # rotate reaches the download subprocess


def test_fetch_sd_frames_threads_rotate_to_extractor():
    seen = {}

    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        seen["rotate"] = rotate
        return ["/tmp/x_00.jpg"]

    sdclip.fetch_sd_frames(
        _Cam(), 1000, span=12, every=6,
        download=lambda *a, **k: "/tmp/seg.mp4",
        segment_bounds=lambda *a, **k: None,
        extract_frames=extract_frames, rotate=270)
    assert seen["rotate"] == 270


def test_fetch_subprocess_empty_when_run_raises():
    def run(argv, **kw):
        raise OSError("spawn failed")
    assert sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY") == []


def test_fetch_subprocess_logs_stderr_on_nonzero_exit(caplog):
    def run(argv, **kw):
        return types.SimpleNamespace(returncode=4, stdout="", stderr="SD connect failed: boom")
    with caplog.at_level("WARNING", logger="tapo_monitor.sdclip"):
        sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY")
    assert "exit=4" in caplog.text and "SD connect failed: boom" in caplog.text


def test_fetch_subprocess_logs_stderr_when_no_frames(caplog):
    # exit 0 but no FRAME: lines -> download silently produced nothing; surface stderr.
    def run(argv, **kw):
        return types.SimpleNamespace(
            returncode=0, stdout="pytapo noise", stderr="SD fetch: download returned no segment")
    with caplog.at_level("WARNING", logger="tapo_monitor.sdclip"):
        out = sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY")
    assert out == []
    assert "no frames" in caplog.text and "download returned no segment" in caplog.text


# ── _run_in_fresh_loop: isolate the async SD download from the daemon's loop ──────

def test_run_in_fresh_loop_returns_result():
    async def coro():
        return 42
    assert sdclip._run_in_fresh_loop(lambda: coro()) == 42


def test_run_in_fresh_loop_propagates_exception():
    async def coro():
        raise ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        sdclip._run_in_fresh_loop(lambda: coro())


def test_run_in_fresh_loop_works_inside_running_loop():
    # Regression for the daemon nested-loop bug: the SD download is triggered from a
    # thread whose event loop is already running (the getEvents poller). A plain
    # asyncio.run()/run_until_complete raises "cannot run loop while another is
    # running" there; running the coroutine on its own thread must still complete.
    async def coro():
        return "ok"

    async def caller():
        return sdclip._run_in_fresh_loop(lambda: coro())

    assert asyncio.run(caller()) == "ok"


def test_run_in_fresh_loop_uses_separate_thread():
    main = threading.get_ident()

    async def coro():
        return threading.get_ident()

    assert sdclip._run_in_fresh_loop(lambda: coro()) != main


def test_fetch_frames_returns_candidates_on_success():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        calls["tc"] = tc
        return "/tmp/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        calls["mp4"] = mp4
        calls["span"] = span
        calls["every"] = every
        return ["/tmp/a_00.jpg", "/tmp/a_06.jpg", "/tmp/a_12.jpg"]
    out = sdclip.fetch_sd_frames(_Cam(), 1000, out_dir="/tmp", span=12, every=6,
                                 download=download, extract_frames=extract_frames)
    assert out == ["/tmp/a_00.jpg", "/tmp/a_06.jpg", "/tmp/a_12.jpg"]
    assert calls["window"] == (1000, 1012)   # end = start + span
    assert calls["tc"] == 0                   # time correction read from the client
    assert calls["mp4"] == "/tmp/clip.mp4"
    assert (calls["span"], calls["every"]) == (12, 6)


def test_fetch_frames_returns_empty_when_download_fails():
    def download(client, start, end, tc, out_dir):
        return None
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        raise AssertionError("extract must not run when download failed")
    assert sdclip.fetch_sd_frames(_Cam(), 1000, download=download,
                                  extract_frames=extract_frames) == []


def test_fetch_frames_removes_downloaded_mp4(tmp_path):
    mp4 = tmp_path / "clip.mp4"

    def download(client, start, end, tc, out_dir):
        mp4.write_bytes(b"mp4")
        return str(mp4)

    def extract_frames(mp4_path, out_dir, base, span, every, rotate=0, clip_start=None):
        assert mp4_path == str(mp4)
        return [str(tmp_path / "frame.jpg")]

    out = sdclip.fetch_sd_frames(_Cam(), 1000, out_dir=str(tmp_path),
                                 download=download, extract_frames=extract_frames)
    assert out == [str(tmp_path / "frame.jpg")]
    assert not mp4.exists()


def test_fetch_frames_empty_when_no_frames_extracted():
    def download(client, start, end, tc, out_dir):
        return "/tmp/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        return []
    assert sdclip.fetch_sd_frames(_Cam(), 1000, download=download,
                                  extract_frames=extract_frames) == []


def test_fetch_frames_tolerates_time_correction_error():
    class BadCam:
        def getTimeCorrection(self):
            raise RuntimeError("boom")
    seen = {}
    def download(client, start, end, tc, out_dir):
        seen["tc"] = tc
        return "/tmp/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        return ["/tmp/x_00.jpg"]
    assert sdclip.fetch_sd_frames(BadCam(), 1000, download=download,
                                  extract_frames=extract_frames) == ["/tmp/x_00.jpg"]
    assert seen["tc"] == 0   # defaults to 0 when the camera call raises


# ── segment alignment: download the camera's real recorded bounds ────────────────

def test_fetch_frames_uses_real_segment_bounds():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/tmp/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        calls["span"] = span
        return ["/tmp/a_00.jpg"]
    out = sdclip.fetch_sd_frames(
        _Cam(), 1000, span=12, every=3, download=download, extract_frames=extract_frames,
        segment_bounds=lambda c, s: (2000, 2008))   # real segment, shorter than span cap
    assert out == ["/tmp/a_00.jpg"]
    assert calls["window"] == (2000, 2008)   # real bounds, not guessed (1000, 1012)
    assert calls["span"] == 8                 # extract across the real downloaded span


def test_fetch_frames_caps_long_segment_to_span():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/tmp/clip.mp4"
    sdclip.fetch_sd_frames(
        _Cam(), 1000, span=12, download=download,
        extract_frames=lambda *a, **k: ["/tmp/a.jpg"],
        segment_bounds=lambda c, s: (2000, 9999))   # very long segment
    assert calls["window"] == (2000, 2012)   # capped at start + span


def test_fetch_frames_default_span_covers_mid_clip_subject():
    # Regression (2026-07-02): the camera fires the event at motion start, but the person
    # often only walks into clear view 15-25 s into the recorded clip. A default window of
    # ~12 s extracts only empty frames -> Groq sees "empty scene" -> a blank photo is sent
    # for a real person. The default must span most of the segment so a mid-clip subject
    # is captured. (Capped below the full segment to stay within the Pi Zero download
    # budget -- a 60 s pull takes ~107 s, too close to SD_DOWNLOAD_TIMEOUT.)
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/tmp/clip.mp4"
    sdclip.fetch_sd_frames(
        _Cam(), 1000, download=download,               # no span= -> exercise the default
        extract_frames=lambda *a, **k: ["/tmp/a.jpg"],
        segment_bounds=lambda c, s: (2000, 2100))      # 100 s recorded segment
    dl_start, dl_end = calls["window"]
    assert dl_end - dl_start >= 30    # cover >= 30 s so a subject appearing ~20 s in is caught


def test_fetch_frames_falls_back_to_guess_when_no_segment():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/tmp/clip.mp4"
    sdclip.fetch_sd_frames(
        _Cam(), 1000, span=12, download=download,
        extract_frames=lambda *a, **k: ["/tmp/a.jpg"],
        segment_bounds=lambda c, s: None)        # lookup failed
    assert calls["window"] == (1000, 1012)   # guessed window


def test_segment_bounds_picks_closest_segment():
    class Cam:
        def getRecordingsUTC(self, start, end):
            return [{"startTime": 900, "endTime": 950, "vedio_type": "x"},
                    {"startTime": 1005, "endTime": 1060, "vedio_type": "x"},
                    {"startTime": 1200, "endTime": 1260, "vedio_type": "x"}]
    assert sdclip._segment_bounds(Cam(), 1000) == (1005, 1060)   # start nearest 1000


def test_segment_bounds_none_when_api_missing():
    assert sdclip._segment_bounds(_Cam(), 1000) is None   # _Cam has no getRecordingsUTC


# ── per-camera span cap + frame spacing (wide events on capable hardware) ─────────

def test_event_span_honors_camera_cap():
    """App clips run ~2 min (live 2026-07-06: 01:58, 02:20) but the default cap scans
    only the first 48 s — a Pi 4 can afford the whole event, a Pi Zero cannot, so the
    cap is per-camera. No cap -> the proven default."""
    long_event = {"start_time": 1000, "end_time": 1140}
    assert sdclip.event_span(long_event, cap=120) == 120
    assert sdclip.event_span(long_event) == sdclip.SD_SPAN_CAP
    assert sdclip.event_span(long_event, cap=None) == sdclip.SD_SPAN_CAP
    assert sdclip.event_span({"start_time": 1000, "end_time": 1040}, cap=120) == 40
    assert sdclip.event_span({}, cap=120) == sdclip.SD_SPAN


def test_frame_every_keeps_groq_call_count_flat():
    """A wider window must spread the same ~8 candidate frames, not multiply Groq calls:
    span 48 -> every 6 (today's behaviour, unchanged), span 120 -> every 15."""
    assert sdclip.frame_every(36) == sdclip.SD_FRAME_EVERY
    assert sdclip.frame_every(48) == 6
    assert sdclip.frame_every(120) == 15
    assert 120 // sdclip.frame_every(120) == 48 // sdclip.frame_every(48) == 8


def test_fetch_subprocess_derives_every_from_span():
    captured = {}

    def run(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=120, run=run, python="PY")
    assert captured["argv"][-3:] == ["120", "15", "0"]   # span, every (derived), rotate


def test_frame_capture_time_reads_the_stamp_the_extractor_wrote():
    assert sdclip.frame_capture_time("/tmp/sdf_1788316600_9_12_at1788316593.jpg") == 1788316593.0
    assert sdclip.frame_capture_time("sdf_1_2_00_at1000.jpg") == 1000.0


def test_frame_capture_time_is_none_when_the_name_carries_nothing():
    # Unknown, not "fine": the caller must score such a frame rather than skip it.
    assert sdclip.frame_capture_time("/tmp/sdf_1788316600_9_12.jpg") is None
    assert sdclip.frame_capture_time("/tmp/whatever.jpg") is None


def test_extractor_is_told_the_segment_start_not_the_event_start():
    # The offsets count from the segment the camera had on its card, which can begin
    # minutes before the event. Stamping frames with the event start would put every one
    # of them in the wrong place on the clock — and the pan-limit window compares clocks.
    seen = {}

    def segment_bounds(client, start, **k):
        return (940, 1200)                      # segment opened 60 s before the event

    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        seen["clip_start"] = clip_start
        return ["/tmp/a_00_at940.jpg"]

    sdclip.fetch_sd_frames(_Cam(), 1000, span=30, every=6,
                           download=lambda *a, **k: "/tmp/seg.mp4",
                           extract_frames=extract_frames, segment_bounds=segment_bounds)

    assert seen["clip_start"] == 940


def test_download_timeout_scales_with_span():
    assert sdclip.download_timeout(36) == 150
    assert sdclip.download_timeout(48) == 150
    assert sdclip.download_timeout(80) == 230
    assert sdclip.download_timeout(100) == 280


def test_fetch_subprocess_scales_timeout_with_span():
    captured = {}

    def run(argv, **kw):
        captured["timeout"] = kw.get("timeout")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=80, run=run, python="PY")
    assert captured["timeout"] == 230

    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=80, run=run, python="PY", timeout=99)
    assert captured["timeout"] == 99


# ── dense start: more frames over the event's opening seconds ────────────────────

def test_frame_offsets_default_is_the_old_grid():
    assert sdclip.frame_offsets(36, 6) == [0, 6, 12, 18, 24, 30]
    assert sdclip.frame_offsets(36, 6) == list(range(0, 36, 6))


def test_frame_offsets_dense_start_adds_to_the_grid():
    dense = (sdclip.DENSE_START_SECONDS, sdclip.DENSE_START_EVERY)
    assert sdclip.frame_offsets(36, 6, dense) == [0, 2, 4, 6, 8, 10, 12, 18, 24, 30]
    # A wide window keeps its whole old grid; the dense frames only add.
    wide = sdclip.frame_offsets(120, 15, dense)
    assert set(range(0, 120, 15)) <= set(wide) and len(wide) == 13
    # Counted from where the event sits in the downloaded segment, clipped to the span.
    assert sdclip.frame_offsets(12, 6, (12, 2), dense_from=7) == [0, 6, 7, 9, 11]


def test_fetch_frames_dense_counts_from_the_event_not_the_segment():
    seen = {}
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None, **kw):
        seen.update(kw, clip_start=clip_start)
        return ["/tmp/a.jpg"]
    sdclip.fetch_sd_frames(
        _Cam(), 1000, span=36, every=6, download=lambda *a, **k: "/tmp/clip.mp4",
        extract_frames=extract_frames, segment_bounds=lambda c, s: (995, 1100),
        dense=(12, 2))
    assert seen == {"dense": (12, 2), "dense_from": 5, "clip_start": 995}


def test_fetch_frames_without_dense_calls_the_extractor_as_before():
    seen = {}
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        seen["ok"] = True
        return ["/tmp/a.jpg"]
    sdclip.fetch_sd_frames(_Cam(), 1000, span=12, every=6,
                           download=lambda *a, **k: "/tmp/clip.mp4",
                           extract_frames=extract_frames, segment_bounds=lambda c, s: None)
    assert seen == {"ok": True}


def test_fetch_subprocess_passes_dense_as_two_trailing_args():
    argvs = []
    def run(argv, **kw):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=36, run=run, python="PY")
    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=36, run=run, python="PY",
                                      dense=(12, 2))
    assert argvs[0][-3:] == ["36", "6", "0"]          # unchanged argv without dense
    assert argvs[1] == argvs[0] + ["12", "2"]


def test_download_main_reads_old_and_new_argv(monkeypatch):
    from tapo_monitor import camera
    seen = []
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: None)
    monkeypatch.setattr(camera, "connect", lambda factory: (object(), None))
    monkeypatch.setattr(sdclip, "fetch_sd_frames",
                        lambda client, start, **kw: seen.append(kw) or [])
    base = ["203.0.113.12", "", "", "", "1000", "/tmp", "36", "6"]
    assert sdclip.download_main(base) == 0
    assert sdclip.download_main(base + ["90"]) == 0
    assert sdclip.download_main(base + ["90", "12", "2"]) == 0
    assert [(k["rotate"], k["dense"]) for k in seen] == [(0, None), (90, None),
                                                         (90, (12, 2))]


# ── the card's early look: a shorter first window, then the rest by offset ─────────

def test_card_early_span_splits_only_a_window_with_room_for_another_frame():
    assert sdclip.card_early_span(36) == sdclip.CARD_EARLY_SPAN == 18
    assert sdclip.card_early_span(24) == 18
    assert sdclip.card_early_span(23) is None


def test_card_early_span_takes_a_configured_look():
    assert sdclip.card_early_span(36, 12) == 12
    assert sdclip.card_early_span(36, 30) == 30
    assert sdclip.card_early_span(36, 31) is None     # no room for another frame
    assert sdclip.card_early_span(18, 12) == 12


def test_fresh_delay_takes_a_guard():
    assert sdclip.fresh_delay(18) == 18 + sdclip.PYTAPO_FRESH_GUARD + sdclip.FRESH_SLACK
    assert sdclip.fresh_delay(18, guard=30) == 18 + 30 + sdclip.FRESH_SLACK


def test_fetch_frames_offset_skips_into_the_aligned_segment():
    calls, stats = {}, {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/nonexistent/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every, rotate=0, clip_start=None):
        calls.update(span=span, clip_start=clip_start)
        return ["/tmp/a.jpg"]
    out = sdclip.fetch_sd_frames(
        _Cam(), 1000, span=18, offset=18, download=download,
        extract_frames=extract_frames, segment_bounds=lambda c, s: (995, 1100),
        stats=stats)
    assert out == ["/tmp/a.jpg"]
    # Looked up by the event start, window from segment start + offset.
    assert calls == {"window": (1013, 1031), "span": 18, "clip_start": 1013}
    assert stats["window"] == [1013, 1031] and stats["offset"] == 18
    assert stats["lead"] == 5 and stats["aligned"] is True
    assert stats["bytes"] is None and "download_s" in stats and "extract_s" in stats


def test_fetch_frames_offset_clips_to_the_segment_end():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return "/tmp/clip.mp4"
    sdclip.fetch_sd_frames(_Cam(), 1000, span=30, offset=18, download=download,
                           extract_frames=lambda *a, **k: ["/tmp/a.jpg"],
                           segment_bounds=lambda c, s: (1000, 1030))
    assert calls["window"] == (1018, 1030)


def test_fetch_frames_offset_past_the_segment_reads_nothing():
    def download(*a):
        raise AssertionError("nothing to download past the segment end")
    stats = {}
    assert sdclip.fetch_sd_frames(_Cam(), 1000, span=18, offset=18, download=download,
                                  segment_bounds=lambda c, s: (1000, 1012),
                                  stats=stats) == []
    assert stats["window"] == [1018, 1012]


def test_fetch_frames_offset_on_a_guessed_window():
    calls = {}
    def download(client, start, end, tc, out_dir):
        calls["window"] = (start, end)
        return None
    stats = {}
    sdclip.fetch_sd_frames(_Cam(), 1000, span=18, offset=18, download=download,
                           segment_bounds=lambda c, s: None, stats=stats)
    assert calls["window"] == (1018, 1036)
    assert stats["aligned"] is False and stats["lead"] is None and "bytes" not in stats


def test_fetch_frames_stats_carry_the_file_size(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    def download(client, start, end, tc, out_dir):
        mp4.write_bytes(b"x" * 1234)
        return str(mp4)
    stats = {}
    sdclip.fetch_sd_frames(_Cam(), 1000, span=18, download=download,
                           extract_frames=lambda *a, **k: [],
                           segment_bounds=lambda c, s: (1000, 1100), stats=stats)
    assert stats["bytes"] == 1234 and stats["window"] == [1000, 1018]


def test_fetch_subprocess_passes_offset_as_a_trailing_option():
    argvs = []
    def run(argv, **kw):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=18, run=run, python="PY")
    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=18, run=run, python="PY",
                                      offset=18)
    assert argvs[1] == argvs[0] + ["offset=18"]


def test_fetch_subprocess_logs_one_line_per_card_read(caplog):
    stdout = ('FRAME:/tmp/a.jpg\nSTATS:{"window": [1000, 1018], "offset": 0, '
              '"lead": 0, "aligned": true, "bytes": 2500000, "connect_s": 2.1, '
              '"download_s": 20.5, "extract_s": 3.2}\n')
    run = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")  # noqa: E731
    with caplog.at_level("INFO", logger="tapo_monitor.sdclip"):
        out = sdclip.fetch_sd_frames_subprocess(_cfg(name="cam"), 1000, span=18,
                                                run=run, python="PY")
    assert out == ["/tmp/a.jpg"]
    line = [r.getMessage() for r in caplog.records if "SD read" in r.getMessage()]
    assert len(line) == 1
    assert ("SD read cam: window=18s offset=0 lead=0 aligned=True bytes=2500000 "
            "connect=2.1s download=20.5s extract=3.2s frames=1") in line[0]


def test_card_read_log_ignores_a_garbled_stats_line(caplog):
    with caplog.at_level("INFO", logger="tapo_monitor.sdclip"):
        sdclip.log_card_read("cam", "not json", 0, 1.0)
        sdclip.log_card_read("cam", '{"offset": 18}', 0, 1.0)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "window=0s offset=18" in msgs[0] and "bytes=-" in msgs[0]


def test_download_main_reads_the_offset_option(monkeypatch):
    from tapo_monitor import camera
    seen = []
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: None)
    monkeypatch.setattr(camera, "connect", lambda factory: (object(), None))
    monkeypatch.setattr(sdclip, "fetch_sd_frames",
                        lambda client, start, **kw: seen.append(kw) or [])
    base = ["203.0.113.12", "", "", "", "1000", "/tmp", "18", "6"]
    assert sdclip.download_main(base + ["offset=18"]) == 0
    assert sdclip.download_main(base + ["90", "12", "2", "offset=6"]) == 0
    assert [(k["rotate"], k["dense"], k["offset"]) for k in seen] == [
        (0, None, 18), (90, (12, 2), 6)]


# ── a transient camera error on the segment lookup (-40214) gets one more try ─────

class _FlakyCam(_Cam):
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def getRecordingsUTC(self, start, end):
        self.calls += 1
        if self.calls <= self.failures:
            raise Exception('Error: -40214, Response: {"error_code": -40214}')
        return [{"search_video_results_1": {"startTime": 995, "endTime": 1055}}]


def test_segment_lookup_retries_once_after_a_transient_error(capsys):
    cam, pauses = _FlakyCam(1), []
    assert sdclip._segment_bounds(cam, 1000, sleep=pauses.append,
                                  retry_pause=sdclip.SD_LOOKUP_RETRY_PAUSE) == (995, 1055)
    assert cam.calls == 2 and pauses == [sdclip.SD_LOOKUP_RETRY_PAUSE]
    assert "-40214" in capsys.readouterr().err       # the real error reaches the journal


def test_segment_lookup_gives_up_after_the_second_error():
    cam, pauses = _FlakyCam(5), []
    assert sdclip._segment_bounds(cam, 1000, sleep=pauses.append, retry_pause=5) is None
    assert cam.calls == 2 and len(pauses) == 1


def test_segment_lookup_does_not_pause_when_it_works_first_time():
    cam, pauses = _FlakyCam(0), []
    assert sdclip._segment_bounds(cam, 1000, sleep=pauses.append) == (995, 1055)
    assert cam.calls == 1 and pauses == []


def test_fetch_frames_hands_a_guard_to_the_download_only_when_set():
    seen = []
    def download(client, start, end, tc, out_dir, **kw):
        seen.append(kw)
        return None
    for guard in (None, 30):
        sdclip.fetch_sd_frames(_Cam(), 1000, span=18, download=download,
                               segment_bounds=lambda c, s: (1000, 1100), guard=guard)
    assert seen == [{}, {"guard": 30}]


def test_fetch_subprocess_passes_a_guard_option():
    argvs = []
    def run(argv, **kw):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, span=18, run=run, python="PY",
                                      offset=18, guard=30)
    assert argvs[0][-2:] == ["offset=18", "guard=30"]


def test_download_main_reads_the_guard_option(monkeypatch):
    from tapo_monitor import camera
    seen = []
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: None)
    monkeypatch.setattr(camera, "connect", lambda factory: (object(), None))
    monkeypatch.setattr(sdclip, "fetch_sd_frames",
                        lambda client, start, **kw: seen.append(kw) or [])
    base = ["203.0.113.12", "", "", "", "1000", "/tmp", "18", "6"]
    sdclip.download_main(base)
    sdclip.download_main(base + ["guard=30"])
    assert [k["guard"] for k in seen] == [None, 30]


def test_segment_lookup_without_a_pause_fails_once_as_before():
    cam, pauses = _FlakyCam(1), []
    assert sdclip._segment_bounds(cam, 1000, sleep=pauses.append) is None
    assert cam.calls == 1 and pauses == []


def test_fetch_frames_asks_for_the_lookup_retry_only_when_on():
    seen = []
    def segment_bounds(client, start, **kw):
        seen.append(kw)
        return None
    for on in (False, True):
        sdclip.fetch_sd_frames(_Cam(), 1000, download=lambda *a, **k: None,
                               segment_bounds=segment_bounds, lookup_retry=on)
    assert seen == [{}, {"retry_pause": sdclip.SD_LOOKUP_RETRY_PAUSE}]


def test_fetch_subprocess_asks_for_the_lookup_retry_only_with_the_early_look():
    argvs = []
    def run(argv, **kw):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    sdclip.fetch_sd_frames_subprocess(_cfg(sd_early_look=False), 1000, run=run, python="PY")
    sdclip.fetch_sd_frames_subprocess(_cfg(sd_early_look=True), 1000, run=run, python="PY")
    assert "lookup_retry=1" not in argvs[0]
    assert argvs[1] == argvs[0] + ["lookup_retry=1"]


def test_download_main_reads_the_lookup_retry_option(monkeypatch):
    from tapo_monitor import camera
    seen = []
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: None)
    monkeypatch.setattr(camera, "connect", lambda factory: (object(), None))
    monkeypatch.setattr(sdclip, "fetch_sd_frames",
                        lambda client, start, **kw: seen.append(kw) or [])
    base = ["203.0.113.12", "", "", "", "1000", "/tmp", "18", "6"]
    sdclip.download_main(base)
    sdclip.download_main(base + ["lookup_retry=1"])
    assert [k["lookup_retry"] for k in seen] == [False, True]
