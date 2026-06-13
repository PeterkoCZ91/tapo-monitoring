import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import snapshot


def test_rtsp_url_basic():
    url = snapshot.rtsp_url("192.168.1.50", "admin", "secret")
    assert url == "rtsp://admin:secret@192.168.1.50:554/stream1"


def test_rtsp_url_quotes_credentials():
    url = snapshot.rtsp_url("192.168.1.50", "user@host", "p/w d")
    assert "user%40host" in url
    assert "p%2Fw%20d" in url
    assert "@192.168.1.50:554" in url


def test_rtsp_url_custom_stream():
    url = snapshot.rtsp_url("192.168.1.50", "admin", "secret", stream="stream2")
    assert url.endswith("/stream2")


def test_rtsp_url_custom_port():
    url = snapshot.rtsp_url("192.168.1.50", "admin", "secret", port=8554)
    assert url == "rtsp://admin:secret@192.168.1.50:8554/stream1"


def test_rtsp_url_custom_port_and_stream():
    url = snapshot.rtsp_url("192.168.1.50", "admin", "secret", stream="stream2", port=10554)
    assert url == "rtsp://admin:secret@192.168.1.50:10554/stream2"


def test_ffmpeg_args_shape():
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg")
    assert args[0] == "ffmpeg"
    assert args[-2:] == ["-y", "/tmp/out.jpg"]
    assert "-rtsp_transport" in args
    assert args[args.index("-rtsp_transport") + 1] == "tcp"
    assert args[args.index("-i") + 1] == "rtsp://x"
    assert args[args.index("-frames:v") + 1] == "1"
    assert args[args.index("-q:v") + 1] == "2"


def test_ffmpeg_args_scales_frame_down():
    # Full 4K frames are slow to upload to Groq from a Pi and heavy for Telegram;
    # scale to 1280 wide like the proven legacy pipeline.
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg")
    assert args[args.index("-vf") + 1] == "scale=1280:-1"


def test_ffmpeg_args_single_image_update():
    # ffmpeg 7.x errors on a fixed single-image filename without -update 1.
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg")
    assert args[args.index("-update") + 1] == "1"
