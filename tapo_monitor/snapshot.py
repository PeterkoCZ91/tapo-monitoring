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
