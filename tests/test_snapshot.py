import os
import subprocess
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


def test_capture_rtsp_returns_path_on_success(tmp_path):
    # A real grab writes a non-empty JPEG; we return its path and keep the file.
    def fake_run(args, **kwargs):
        out = args[-1]
        with open(out, "wb") as f:
            f.write(b"\xff\xd8jpegbytes")
        return subprocess.CompletedProcess(args, 0)

    path = snapshot.capture_rtsp("rtsp://x", out_dir=str(tmp_path), _run=fake_run)
    assert path is not None
    assert os.path.exists(path)


def test_capture_rtsp_cleans_orphan_on_timeout(tmp_path):
    # On a slow Pi Zero ffmpeg times out AFTER partially writing the file. The
    # orphan must be removed (not leaked into /tmp) and None returned.
    def fake_run(args, **kwargs):
        out = args[-1]
        with open(out, "wb") as f:
            f.write(b"partial")  # ffmpeg got part-way before the kill
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 15))

    path = snapshot.capture_rtsp("rtsp://x", out_dir=str(tmp_path), _run=fake_run)
    assert path is None
    assert os.listdir(tmp_path) == []  # no snap_*.jpg leaked


def test_capture_rtsp_cleans_orphan_on_nonzero_exit(tmp_path):
    def fake_run(args, **kwargs):
        out = args[-1]
        with open(out, "wb") as f:
            f.write(b"partial")
        raise subprocess.CalledProcessError(1, args)

    path = snapshot.capture_rtsp("rtsp://x", out_dir=str(tmp_path), _run=fake_run)
    assert path is None
    assert os.listdir(tmp_path) == []


def test_capture_rtsp_treats_empty_file_as_failure(tmp_path):
    # ffmpeg can exit 0 yet leave a 0-byte file; that is not a usable frame and
    # must not be returned (downstream would send a corrupt image) nor leaked.
    def fake_run(args, **kwargs):
        out = args[-1]
        open(out, "wb").close()
        return subprocess.CompletedProcess(args, 0)

    path = snapshot.capture_rtsp("rtsp://x", out_dir=str(tmp_path), _run=fake_run)
    assert path is None
    assert os.listdir(tmp_path) == []
