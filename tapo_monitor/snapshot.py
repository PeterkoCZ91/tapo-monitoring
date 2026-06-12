"""RTSP frame capture via ffmpeg.

The pure pieces — :func:`rtsp_url` (URL building with credential quoting) and
:func:`ffmpeg_args` (the argv for a single-frame grab) — are tested. The actual
subprocess in :func:`capture_rtsp` is a thin I/O wrapper kept deliberately untested.
"""

import os
import subprocess
import time as _time
import urllib.parse


def rtsp_url(host, user, password, stream="stream1"):
    """Build an RTSP URL with URL-encoded credentials. Pure."""
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(password, safe="")
    return f"rtsp://{u}:{p}@{host}:554/{stream}"


def ffmpeg_args(rtsp_url, out_path):
    """Return the ffmpeg argv to grab a single high-quality JPEG frame. Pure."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-y", out_path,
    ]


def capture_rtsp(rtsp_url, out_dir="/tmp", timeout=8):  # pragma: no cover - thin subprocess I/O
    """Grab one frame from an RTSP stream. Returns the image path or None."""
    out_path = os.path.join(out_dir, f"snap_{int(_time.time() * 1000)}.jpg")
    try:
        subprocess.run(
            ffmpeg_args(rtsp_url, out_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except Exception:
        return None
    return out_path if os.path.exists(out_path) else None
