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
    base = {"host": "1.2.3.4", "user_env": "U", "password_env": "P",
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
    assert "1.2.3.4" in argv and "1000" in argv        # host + start passed through


def test_fetch_subprocess_empty_on_nonzero_exit():
    def run(argv, **kw):
        return types.SimpleNamespace(returncode=4, stdout="FRAME:/tmp/a.jpg", stderr="boom")
    assert sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY") == []


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
    def extract_frames(mp4, out_dir, base, span, every):
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
    def extract_frames(mp4, out_dir, base, span, every):
        raise AssertionError("extract must not run when download failed")
    assert sdclip.fetch_sd_frames(_Cam(), 1000, download=download,
                                  extract_frames=extract_frames) == []


def test_fetch_frames_removes_downloaded_mp4(tmp_path):
    mp4 = tmp_path / "clip.mp4"

    def download(client, start, end, tc, out_dir):
        mp4.write_bytes(b"mp4")
        return str(mp4)

    def extract_frames(mp4_path, out_dir, base, span, every):
        assert mp4_path == str(mp4)
        return [str(tmp_path / "frame.jpg")]

    out = sdclip.fetch_sd_frames(_Cam(), 1000, out_dir=str(tmp_path),
                                 download=download, extract_frames=extract_frames)
    assert out == [str(tmp_path / "frame.jpg")]
    assert not mp4.exists()


def test_fetch_frames_empty_when_no_frames_extracted():
    def download(client, start, end, tc, out_dir):
        return "/tmp/clip.mp4"
    def extract_frames(mp4, out_dir, base, span, every):
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
    def extract_frames(mp4, out_dir, base, span, every):
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
    def extract_frames(mp4, out_dir, base, span, every):
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
        extract_frames=lambda *a: ["/tmp/a.jpg"],
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
        extract_frames=lambda *a: ["/tmp/a.jpg"],
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
        extract_frames=lambda *a: ["/tmp/a.jpg"],
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
