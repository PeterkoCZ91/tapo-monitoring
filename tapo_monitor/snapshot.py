"""RTSP frame capture via ffmpeg.

The pure pieces — :func:`rtsp_url` (URL building with credential quoting) and
:func:`ffmpeg_args` (the argv for a single-frame grab) — are tested. The actual
subprocess in :func:`capture_rtsp` is a thin I/O wrapper kept deliberately untested.
"""

import logging
import os
import shutil
import subprocess
import time as _time
import urllib.parse

log = logging.getLogger(__name__)


def rtsp_url(host, user, password, stream="stream1", port=554):
    """Build an RTSP URL with URL-encoded credentials. Pure."""
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(password, safe="")
    return f"rtsp://{u}:{p}@{host}:{port}/{stream}"


def rotate_filter(degrees):
    """ffmpeg video-filter fragment for a clockwise rotation; "" for 0/unknown. Pure."""
    return {0: "", 90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}.get(
        int(degrees), "")


DELIVERY_WIDTH = 1280
SCALE_VF = f"scale={DELIVERY_WIDTH}:-1"


class Frame(str):
    """A frame path that may carry the native-resolution original it was reduced from.

    It *is* the path string, so every consumer (scorer, captioner, Telegram, cleanup)
    keeps working unchanged and sees the delivery-width frame. Only the subject crop asks
    for ``native``, and only :func:`safe_unlink` knows to remove it — which is what stops
    a sampler that discards five of six frames from leaking five native originals.

    ``native_height`` exists so the crop can clamp a scaled rect inside the original;
    without it an overflowing rect kills ffmpeg and the alert silently loses its zoom.
    """

    __slots__ = ("native", "native_width", "native_height")

    def __new__(cls, path, native=None, native_width=None, native_height=None):
        self = super().__new__(cls, path)
        self.native = native
        self.native_width = native_width
        self.native_height = native_height
        return self


def scaled_vf(rotate=0, scale=True):
    """The ``-vf`` chain used by every frame extractor: rotate (if any) then scale. Pure.

    ``scale=False`` keeps the camera's native resolution, for the one case that needs it:
    cropping to a subject. A crop taken from an already-downscaled frame throws away the
    very detail the zoom exists to show. Returns "" when there is nothing to do — the
    caller must then omit ``-vf`` entirely, since ffmpeg rejects an empty filter value.
    """
    parts = [p for p in (rotate_filter(rotate), SCALE_VF if scale else "") if p]
    return ",".join(parts)


def ffmpeg_args(rtsp_url, out_path, rotate=0, scale=True):
    """Return the ffmpeg argv to grab a single high-quality JPEG frame. Pure."""
    vf = scaled_vf(rotate, scale)
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        *(("-vf", vf) if vf else ()),
        "-q:v", "2",
        "-update", "1",
        "-y", out_path,
    ]


def downscale_args(src_path, out_path):
    """Return the ffmpeg argv to scale an existing image down to the delivery width. Pure.

    Used after a native-resolution crop: the zoom is cropped at full detail, then reduced
    once for Telegram, instead of the frame being reduced before there is anything to crop.
    """
    return [
        "ffmpeg", "-y",
        "-i", src_path,
        "-vf", SCALE_VF,
        "-q:v", "2",
        "-update", "1",
        out_path,
    ]


def safe_unlink(path):
    """Remove a temp frame and the native original it may carry. Never raises.

    :class:`Frame` hands the full-resolution twin along for the crop to use; whoever
    drops the delivery frame must drop that twin too, or every alert leaks a native JPEG.
    """
    if not path:
        return
    twin = getattr(path, "native", None)
    if twin:
        safe_unlink(twin)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("failed to remove temp file %s", path, exc_info=True)


_safe_unlink = safe_unlink      # historical name, still used inside this module


def image_width(path):  # pragma: no cover - subprocess I/O
    """Pixel width of an image, or None. Used to map a box scored on a downscaled copy."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        return int(out.split(",")[0]) or None
    except Exception:
        return None


def image_size(path):  # pragma: no cover - subprocess I/O
    """``(width, height)`` of an image via ffprobe, or ``(None, None)`` on any failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        width, height = out.split("x")[:2]
        return int(width) or None, int(height) or None
    except Exception:
        return None, None


def capture_rtsp(rtsp_url, out_dir="/tmp", timeout=15, _run=subprocess.run, rotate=0,
                 scale=True):
    """Grab one frame from an RTSP stream. Returns the image path or None.

    On a slow camera (e.g. Pi Zero) ffmpeg can time out *after* partially writing
    the output file. We treat anything but a non-empty result as a failure and
    remove the orphan on every failure path, so repeated timeouts don't leak
    ``snap_*.jpg`` into /tmp until it fills.
    """
    out_path = os.path.join(out_dir, f"snap_{int(_time.time() * 1000)}.jpg")
    try:
        _run(
            ffmpeg_args(rtsp_url, out_path, rotate=rotate, scale=scale),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        ok = False
    if ok:
        return out_path
    _safe_unlink(out_path)
    return None


GO2RTC_HOST = "127.0.0.1"
GO2RTC_PORT = 1984


def go2rtc_frame_url(src, host=GO2RTC_HOST, port=GO2RTC_PORT):
    """URL of go2rtc's single-frame JPEG endpoint for one source. Pure."""
    return f"http://{host}:{port}/api/frame.jpeg?src={urllib.parse.quote(src, safe='')}"


def _http_get(url, timeout):  # pragma: no cover - network I/O
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def capture_go2rtc(src, out_dir="/tmp", timeout=15, host=GO2RTC_HOST, port=GO2RTC_PORT,
                   _fetch=_http_get):
    """Grab one frame from a go2rtc source. Returns the image path or None.

    The battery cameras this exists for have no usable RTSP of their own: go2rtc speaks
    their native protocol as a sidecar and reconnects when motion wakes one, so a frame is
    an HTTP GET rather than an ffmpeg pull. An unreachable sidecar and a 200 with an empty
    body (go2rtc answering before the stream is back up) are both plain failures — and
    neither may leave a zero-byte ``snap_*.jpg`` behind, which is what once filled /tmp on
    the RTSP path.
    """
    out_path = os.path.join(out_dir, f"snap_{int(_time.time() * 1000)}.jpg")
    try:
        body = _fetch(go2rtc_frame_url(src, host=host, port=port), timeout)
    except Exception:  # noqa: BLE001 - a sleeping camera is a routine miss, not an error
        log.debug("go2rtc frame fetch failed for %s", src, exc_info=True)
        body = None
    if not body:
        _safe_unlink(out_path)
        return None
    try:
        with open(out_path, "wb") as f:
            f.write(body)
    except OSError:
        log.debug("writing go2rtc frame failed for %s", src, exc_info=True)
        _safe_unlink(out_path)
        return None
    return out_path


DECODER = "ffmpeg"


def decoder_available(which=shutil.which):
    """Is the external decoder that turns a clip into a frame on PATH?

    Looked up by name at the moment of an event, which is the wrong time to find out it
    is missing: a daemon started from cron gets a PATH without the operator's own bin
    directory, and every clip then silently yields no frame, hours after startup.
    """
    return which(DECODER) is not None


def ts_frame_args(clip_path, out_path, rotate=0, skip=1.0):
    """ffmpeg argv extracting one JPEG from a downloaded clip. Pure.

    Seeks a moment past the start: the first frames of a motion recording are the ones the
    encoder produced while the sensor was still settling, so they are the blurriest of the
    clip.
    """
    vf = scaled_vf(rotate)
    return [
        DECODER,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{skip:g}",
        "-i", clip_path,
        "-frames:v", "1",
        *(("-vf", vf) if vf else ()),
        "-q:v", "2",
        "-update", "1",
        "-y", out_path,
    ]


def frame_from_clip(clip_path, out_dir="/tmp", timeout=30, rotate=0, skip=1.0,
                    _run=subprocess.run):
    """Extract one frame from a clip file. Returns the image path or None.

    This is the frame of last resort for a hub-backed battery camera: the clip *is* the
    event, so its frame carries the detection moment even when the camera went back to
    sleep before a live grab could reach it. Failures clean up after themselves, so a
    camera whose clips never decode cannot fill the disk with empty JPEGs.
    """
    if not clip_path or not os.path.exists(clip_path):
        return None
    out_path = os.path.join(out_dir, f"snapclip_{int(_time.time() * 1000)}.jpg")
    try:
        _run(
            ts_frame_args(clip_path, out_path, rotate=rotate, skip=skip),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as exc:  # noqa: BLE001 - the caller only needs "no frame"
        # Loud on purpose: a whole site ran for two days dropping every event because the
        # decoder was not on the daemon's PATH, and this failure was only visible at debug.
        log.info("extracting a frame from %s failed: %s", clip_path, exc)
        ok = False
    if ok:
        return out_path
    _safe_unlink(out_path)
    return None


def _env_float(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def latest_recording_frame(
    host, root=None, out_dir="/tmp", timeout=20, max_age=None, now=None,
    _run=subprocess.run, rotate=0
):
    """Extract one JPEG from the newest local recorder segment for a camera host.

    This is an optional fallback for deployments that already record RTSP continuously
    and where the camera refuses a second concurrent RTSP reader. It is disabled unless
    ``root`` or ``RECORDING_ROOT`` is set. Expected layout:
    ``<root>/<host>/<YYYY-MM-DD>/<HH>/*.mkv``.

    ``max_age`` defaults to ``RECORDING_MAX_AGE`` seconds, or 300 seconds when unset.
    Stale recorder output is ignored so a frozen recorder cannot produce old alerts.
    """
    root = root or os.getenv("RECORDING_ROOT", "")
    if not root:
        return None
    max_age = _env_float("RECORDING_MAX_AGE", 300.0) if max_age is None else max_age
    now = _time.time() if now is None else now
    cam_dir = os.path.join(root, host)
    if not os.path.isdir(cam_dir):
        return None
    latest = None
    latest_mtime = -1.0
    for dirpath, _dirnames, filenames in os.walk(cam_dir):
        for name in filenames:
            if not name.lower().endswith((".mkv", ".mp4", ".ts")):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest, latest_mtime = path, mtime
    if not latest:
        return None
    if max_age is not None and now - latest_mtime > max_age:
        return None
    out_path = os.path.join(out_dir, f"snaprec_{host.replace('.', '_')}_{int(now * 1000)}.jpg")
    rot = rotate_filter(rotate)
    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-sseof",
                "-2",
                "-i",
                latest,
                "-frames:v",
                "1",
                *(["-vf", rot] if rot else []),
                "-q:v",
                "2",
                "-y",
                out_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        ok = False
    if ok:
        return out_path
    _safe_unlink(out_path)
    return None
