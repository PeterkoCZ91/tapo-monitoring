"""Pull the camera-recorded frames around an event's timestamp from the SD card.

Unlike a live RTSP grab (which is ~30 s late and usually empty), this fetches the
segment the camera recorded *around* the event and extracts several candidate frames, so
a confirmed-person alert can show the moment the subject is actually in view — the camera
fires the event when motion starts, but the person may only walk into clear view a few
seconds later, so the very first frame is often empty.

Two lessons baked in (2026-06-15):
  * Use a **dedicated, freshly-logged-in client** for the download, not the long-lived
    client the daemon uses for ``getEvents`` — the reused session makes the media
    download fall into a broken auth path ("Cannot run the event loop ...") and silently
    fail. A fresh client per (low-rate) confirmed person is cheap and reliable.
  * Extract **several frames across the segment**, not just the first one, and let the
    caller (Groq) pick the one with the subject.

The async download (pytapo media stream) and the ffmpeg subprocess are thin I/O kept out
of tests; the orchestrators take injectable collaborators so their logic is unit-tested.
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time as _time

log = logging.getLogger(__name__)

# How long to wait for the download thread before giving up (live-RTSP fallback then).
# A 36 s pull takes ~64 s on the Pi Zero (a 60 s pull ~107 s); 150 gives headroom under
# real load. The follow-up is already deferred, so a slightly later photo is fine.
SD_DOWNLOAD_TIMEOUT = 150

# Seconds of recording to pull from the event start. The camera fires the event at motion
# start, but the subject often only walks into clear view 15-25 s in (confirmed 2026-07-02:
# a real person+dog appeared ~20 s into a 60 s clip while offsets 0-9 s were empty grass),
# so a 12 s window extracted only empty frames and sent a blank photo. 36 s covers the
# subject with margin; kept below the full segment because a 60 s pull (~107 s, ~25 MB) is
# too close to SD_DOWNLOAD_TIMEOUT on the Pi Zero.
SD_SPAN = 36
# pytapo's Downloader refuses any window whose END is younger than this ("Recording in
# progress" -> it yields once and writes an empty file). Mirrors
# Downloader.FRESH_RECORDING_TIME_SECONDS (kept literal here so importing this module
# never needs pytapo; a test cross-checks the value).
PYTAPO_FRESH_GUARD = 60
# Wait this long past the event before fetching. The window end sits up to SD_SPAN past
# the (segment-aligned ~ event) start, so the wait must cover the span PLUS the guard,
# with slack for camera-clock skew. 75 was tuned for the old 12 s span; left at 75 when
# SD_SPAN went 12->36, the guard rejected ~3 of 4 downloads (live 2026-07-02..05) and the
# alerts fell back to stale live photos.
SD_FRESH_DELAY = SD_SPAN + PYTAPO_FRESH_GUARD + 9  # = 105
# Extract one candidate frame every N seconds of the downloaded span (36/6 -> 7 candidates,
# so a mid-clip subject is caught without exploding the per-event Groq call count).
SD_FRAME_EVERY = 6


def _safe_unlink(path):
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to remove temp file %s", path, exc_info=True)


def build_client(host, user, password, cloud_password):  # pragma: no cover - network I/O
    """A fresh pytapo client dedicated to one SD download (separate from the poller)."""
    from pytapo import Tapo

    return Tapo(host, user, password, cloud_password or password)


def _run_in_fresh_loop(make_coro, timeout=SD_DOWNLOAD_TIMEOUT):
    """Run ``make_coro()`` to completion on a dedicated thread with its own event loop.

    The daemon triggers the SD download from the same thread its ``getEvents`` poller
    uses; pytapo leaves that thread's event loop in a state where ``asyncio.run`` /
    ``run_until_complete`` on the download raises "cannot run the event loop while
    another loop is running" (with a "coroutine 'Transport.authenticate' was never
    awaited" warning) and the download silently fails. A brand-new thread has no
    current loop, so we create one there and run the coroutine in isolation. The
    coroutine is built *inside* the worker (via ``make_coro``) so it binds to that loop.
    """
    box = {}

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["result"] = loop.run_until_complete(make_coro())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc
        finally:
            # Cleanly close the download's async generators (pytapo's HttpMediaSession
            # stream) before tearing down the loop; otherwise closing leaves a pending
            # "async_generator_athrow" task and logs "coroutine 'aclose' was never
            # awaited" / "Task was destroyed but it is pending!".
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            asyncio.set_event_loop(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"SD download did not finish within {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _download_segment(client, start, end, time_correction, out_dir):  # pragma: no cover - I/O
    """Download an SD segment [start, end] to an mp4. Returns the path or None."""
    from pytapo.media_stream.downloader import Downloader

    out_dir = out_dir.rstrip("/")
    file_name = f"sd_{int(start)}.mp4"
    out_path = os.path.join(out_dir, file_name)
    try:
        # window_size=50 (not pytapo's default 200): pytapo's own comment warns "with
        # higher values camera sometimes stops sending data", and its internal retry
        # drops to 50. The C560WS mostly returned an empty segment at 200 (2 of 3 live
        # downloads failed with no data); 50 is the camera-friendly value.
        downloader = Downloader(client, start, end, time_correction, out_dir + "/",
                                overwriteFiles=True, window_size=50, fileName=file_name)

        # Pre-warm pytapo's synchronous control calls BEFORE any event loop is running.
        # Downloader.download() calls client.getUserID() from *inside* the running
        # download loop; with hass=None that routes through asyncHandler ->
        # loop.run_until_complete and raises "Cannot run the event loop while another
        # loop is running" (the real cause of every empty SD frame — confirmed live
        # 2026-06-22). getUserID memoises on the client, so calling it here (no loop
        # running) means download() finds it cached and issues no control request inside
        # the loop. (getAudioConfig runs in an executor thread, so it doesn't collide;
        # getMediaSession only reads attributes cached at auth time.)
        client.getUserID()

        async def run():
            async for _status in downloader.download():
                pass

        # Run on a dedicated thread+loop so the daemon poller's loop can't break it.
        _run_in_fresh_loop(lambda: run())
    except Exception:
        # DIAGNOSTIC (2026-06-22): the real download error was being swallowed into a
        # silent None, so the journal only ever showed "SD found no subject". Print the
        # full traceback to stderr — in the download subprocess this is captured by the
        # parent and surfaced in the daemon journal (see fetch_sd_frames_subprocess).
        import traceback
        traceback.print_exc()
        return None
    exists = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    if not exists:
        print(f"SD download produced no/empty file: {out_path}", file=sys.stderr)
    return out_path if exists else None


def _extract_frames(mp4_path, out_dir, base, span, every):  # pragma: no cover - subprocess I/O
    """Extract one JPEG every ``every`` seconds across ``span``. Returns paths (in order)."""
    out_dir = out_dir.rstrip("/")
    paths = []
    for offset in range(0, max(span, 1), every):
        out_path = os.path.join(out_dir, f"{base}_{offset:02d}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(offset), "-i", mp4_path, "-frames:v", "1",
                 "-vf", "scale=1280:-1", "-q:v", "2", "-update", "1", out_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True,
            )
        except Exception:
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            paths.append(out_path)
    return paths


# How far around the event to search the recording index for the matching segment.
SD_SEG_LOOKBACK = 180
SD_SEG_LOOKAHEAD = 60


def _segment_bounds(client, event_start, lookback=SD_SEG_LOOKBACK, lookahead=SD_SEG_LOOKAHEAD):
    """Bounds (start, end) of the recorded SD segment nearest the event, or None.

    The camera records event-triggered clips; asking the download API for those *actual*
    recorded bounds is more reliable than guessing ``[event, event+span]``, which often
    returns an empty segment (the C560WS delivered no data for ~3 of 4 guessed windows).
    ``getRecordingsUTC`` is a control call, so this runs before the download loop (no
    event loop yet). Tolerant of a missing API / odd result shapes -> None (caller falls
    back to the guessed window). It also warms ``getUserID`` for the download.
    """
    try:
        results = client.getRecordingsUTC(int(event_start - lookback), int(event_start + lookahead))
    except Exception:
        return None
    best = None
    for item in results or []:
        seg = item if isinstance(item, dict) and "startTime" in item else None
        if seg is None and isinstance(item, dict):  # some firmwares nest under a numeric key
            seg = next((v for v in item.values() if isinstance(v, dict) and "startTime" in v), None)
        if not seg:
            continue
        try:
            s, e = int(seg["startTime"]), int(seg["endTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or abs(s - event_start) < abs(best[0] - event_start):
            best = (s, e)   # the segment whose start sits closest to the event
    return best


def fetch_sd_frames(client, start_time, out_dir="/tmp", span=SD_SPAN, every=SD_FRAME_EVERY,
                    download=_download_segment, extract_frames=_extract_frames,
                    segment_bounds=_segment_bounds):
    """Return candidate JPEG paths spanning the event, oldest first (empty list on failure).

    ``client`` should be a dedicated, freshly-connected pytapo client (see
    :func:`build_client`).
    """
    try:
        time_correction = client.getTimeCorrection() or 0
    except Exception:
        time_correction = 0
    # Align to the camera's real recorded segment when we can find it; otherwise fall back
    # to the guessed window. The real bounds download reliably where the guess returns
    # nothing. Cap the span so a long segment doesn't pull a huge file for a few frames.
    seg = segment_bounds(client, start_time)
    if seg:
        dl_start, dl_end = seg[0], min(seg[1], seg[0] + span)
    else:
        dl_start, dl_end = start_time, start_time + span
    dl_span = max(int(dl_end - dl_start), 1)
    mp4 = download(client, dl_start, dl_end, time_correction, out_dir)
    if not mp4:
        print(f"SD fetch: download returned no segment "
              f"(start={int(dl_start)}, end={int(dl_end)}, aligned={bool(seg)}, "
              f"tc={time_correction})", file=sys.stderr)
        return []
    try:
        base = f"sdf_{int(start_time)}_{int(_time.time() * 1000)}"
        frames = extract_frames(mp4, out_dir, base, dl_span, every)
        if not frames:
            print(f"SD fetch: extracted 0 frames from {mp4}", file=sys.stderr)
        return frames
    finally:
        _safe_unlink(mp4)


# Stdout marker the download subprocess prints for each extracted frame, so the parent
# can pick the paths out of any pytapo/ffmpeg chatter on stdout.
_FRAME_MARKER = "FRAME:"


def fetch_sd_frames_subprocess(cfg, start_time, out_dir="/tmp", span=SD_SPAN,
                               every=SD_FRAME_EVERY, run=None, python=None):
    """Download SD frames in a FRESH subprocess and return its JPEG paths ([] on failure).

    The in-process download silently fails inside the daemon: its long-lived getEvents
    poller leaves the process's pytapo event-loop state broken, so the media download
    falls into a never-awaited-auth path (running it on a worker thread isn't enough —
    the client built in-process is already tainted). A standalone process has no such
    state and downloads reliably, so we shell out to one. Credentials are inherited from
    the daemon's environment (systemd EnvironmentFile); we pass only the env-var *names*.
    """
    import subprocess as _sp
    import sys as _sys

    run = run or _sp.run
    python = python or _sys.executable
    argv = [python, "-m", "tapo_monitor.sdclip", "download",
            cfg.host, cfg.user_env or "", cfg.password_env or "",
            cfg.cloud_password_env or "", str(int(start_time)), out_dir,
            str(int(span)), str(int(every))]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=SD_DOWNLOAD_TIMEOUT)
    except Exception as exc:
        log.warning("SD subprocess failed to run: %r", exc)
        return []
    # DIAGNOSTIC (2026-06-22): the download subprocess swallowed its real failure into an
    # empty result, so the journal never showed *why* SD produced nothing. Log its exit
    # code + stderr tail whenever it yields no frames, so the next daytime person reveals
    # the actual cause (auth/media-session/freshness/extraction) in `journalctl`.
    stderr_tail = (proc.stderr or "").strip()[-800:]
    if proc.returncode != 0:
        log.warning("SD subprocess exit=%s; stderr: %s", proc.returncode, stderr_tail)
        return []
    frames = [ln[len(_FRAME_MARKER):].strip()
              for ln in (proc.stdout or "").splitlines()
              if ln.startswith(_FRAME_MARKER)]
    if not frames:
        log.warning("SD subprocess returned no frames; stderr: %s", stderr_tail)
    return frames


def download_main(argv):  # pragma: no cover - subprocess entry, real camera I/O
    """Entry for the download subprocess: build a fresh client, fetch frames, print paths.

    argv: host user_env password_env cloud_env start out_dir span every
    """
    from . import camera

    host, user_env, pass_env, cloud_env, start, out_dir, span, every = argv[:8]
    user = os.environ.get(user_env, "") if user_env else ""
    password = os.environ.get(pass_env, "") if pass_env else ""
    cloud = (os.environ.get(cloud_env, "") if cloud_env else "") or password
    factory = camera.tapo_factory(host, user, password, cloud)
    client, _err = camera.connect(factory)
    if client is None:
        print(f"SD connect failed: {_err}", file=sys.stderr)
        return 4
    frames = fetch_sd_frames(client, int(start), out_dir=out_dir,
                             span=int(span), every=int(every))
    for path in frames:
        print(f"{_FRAME_MARKER}{path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI dispatch
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "download":
        raise SystemExit(download_main(sys.argv[2:]))
    raise SystemExit(2)
