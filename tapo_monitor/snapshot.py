"""RTSP frame capture via ffmpeg.

The pure pieces — :func:`rtsp_url` (URL building with credential quoting) and
:func:`ffmpeg_args` (the argv for a single-frame grab) — are tested. The actual
subprocess in :func:`capture_rtsp` is a thin I/O wrapper kept deliberately untested.
"""

import os
import subprocess
import time as _time
import urllib.parse


def rtsp_url(host, user, password, stream="stream1", port=554):
    """Build an RTSP URL with URL-encoded credentials. Pure."""
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(password, safe="")
    return f"rtsp://{u}:{p}@{host}:{port}/{stream}"


def ffmpeg_args(rtsp_url, out_path):
    """Return the ffmpeg argv to grab a single high-quality JPEG frame. Pure."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-vf", "scale=1280:-1",
        "-q:v", "2",
        "-update", "1",
        "-y", out_path,
    ]


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def capture_rtsp(rtsp_url, out_dir="/tmp", timeout=15, _run=subprocess.run):
    """Grab one frame from an RTSP stream. Returns the image path or None.

    On a slow camera (e.g. Pi Zero) ffmpeg can time out *after* partially writing
    the output file. We treat anything but a non-empty result as a failure and
    remove the orphan on every failure path, so repeated timeouts don't leak
    ``snap_*.jpg`` into /tmp until it fills.
    """
    out_path = os.path.join(out_dir, f"snap_{int(_time.time() * 1000)}.jpg")
    try:
        _run(
            ffmpeg_args(rtsp_url, out_path),
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


def _env_float(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def latest_recording_frame(
    host, root=None, out_dir="/tmp", timeout=20, max_age=None, now=None, _run=subprocess.run
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
