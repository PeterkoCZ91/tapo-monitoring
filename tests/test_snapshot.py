import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import snapshot


def test_rtsp_url_basic():
    url = snapshot.rtsp_url("192.0.2.50", "admin", "secret")
    assert url == "rtsp://admin:secret@192.0.2.50:554/stream1"


def test_rtsp_url_quotes_credentials():
    url = snapshot.rtsp_url("192.0.2.50", "user@host", "p/w d")
    assert "user%40host" in url
    assert "p%2Fw%20d" in url
    assert "@192.0.2.50:554" in url


def test_rtsp_url_custom_stream():
    url = snapshot.rtsp_url("192.0.2.50", "admin", "secret", stream="stream2")
    assert url.endswith("/stream2")


def test_rtsp_url_custom_port():
    url = snapshot.rtsp_url("192.0.2.50", "admin", "secret", port=8554)
    assert url == "rtsp://admin:secret@192.0.2.50:8554/stream1"


def test_rtsp_url_custom_port_and_stream():
    url = snapshot.rtsp_url("192.0.2.50", "admin", "secret", stream="stream2", port=10554)
    assert url == "rtsp://admin:secret@192.0.2.50:10554/stream2"


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


def test_ffmpeg_args_keeps_native_resolution_when_asked():
    # Cropping to a subject from an already-downscaled frame throws away the detail the
    # zoom exists to show: a distant figure is ~64px wide at 1280 but ~200px at 4K.
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg", scale=False)
    assert "scale=1280:-1" not in args


def test_ffmpeg_args_native_still_rotates():
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg", rotate=180, scale=False)
    assert args[args.index("-vf") + 1] == "hflip,vflip"


def test_ffmpeg_args_native_unrotated_passes_no_filter():
    # An empty -vf value makes ffmpeg fail; with nothing to do the flag must be absent.
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg", rotate=0, scale=False)
    assert "-vf" not in args


def test_downscale_args_shape():
    args = snapshot.downscale_args("/tmp/in.jpg", "/tmp/out.jpg")
    assert args[args.index("-vf") + 1] == "scale=1280:-1"
    assert args[args.index("-i") + 1] == "/tmp/in.jpg"
    assert args[-1] == "/tmp/out.jpg"


def test_rotate_filter_maps_quarter_turns():
    assert snapshot.rotate_filter(0) == ""
    assert snapshot.rotate_filter(90) == "transpose=1"
    assert snapshot.rotate_filter(180) == "hflip,vflip"
    assert snapshot.rotate_filter(270) == "transpose=2"


def test_ffmpeg_args_applies_rotation_before_scale():
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg", rotate=180)
    assert args[args.index("-vf") + 1] == "hflip,vflip,scale=1280:-1"


def test_ffmpeg_args_no_rotation_when_zero():
    args = snapshot.ffmpeg_args("rtsp://x", "/tmp/out.jpg", rotate=0)
    assert args[args.index("-vf") + 1] == "scale=1280:-1"


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


def _recording_file(root, host, name="segment.mkv", mtime=1000):
    path = root / host / "2026-07-09" / "22" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video")
    os.utime(path, (mtime, mtime))
    return path


def test_latest_recording_frame_extracts_from_fresh_segment(tmp_path):
    host = "192.0.2.50"
    segment = _recording_file(tmp_path, host, mtime=1000)

    def fake_run(args, **kwargs):
        assert args[args.index("-i") + 1] == str(segment)
        out = args[-1]
        with open(out, "wb") as f:
            f.write(b"jpeg")
        return subprocess.CompletedProcess(args, 0)

    path = snapshot.latest_recording_frame(
        host, root=str(tmp_path), out_dir=str(tmp_path), now=1040, max_age=60, _run=fake_run
    )

    assert path is not None
    assert os.path.exists(path)


def test_latest_recording_frame_ignores_stale_segment(tmp_path):
    host = "192.0.2.50"
    _recording_file(tmp_path, host, mtime=1000)
    called = {"run": False}

    def fake_run(args, **kwargs):
        called["run"] = True

    path = snapshot.latest_recording_frame(
        host, root=str(tmp_path), out_dir=str(tmp_path), now=1401, max_age=300, _run=fake_run
    )

    assert path is None
    assert called["run"] is False


def test_latest_recording_frame_uses_env_max_age(tmp_path, monkeypatch):
    host = "192.0.2.50"
    _recording_file(tmp_path, host, mtime=1000)
    monkeypatch.setenv("RECORDING_MAX_AGE", "30")

    path = snapshot.latest_recording_frame(
        host, root=str(tmp_path), out_dir=str(tmp_path), now=1040, _run=lambda *a, **k: None
    )

    assert path is None


def test_safe_unlink_removes_the_native_twin(tmp_path):
    native = tmp_path / "native.jpg"
    reduced = tmp_path / "reduced.jpg"
    native.write_bytes(b"big")
    reduced.write_bytes(b"small")
    frame = snapshot.Frame(str(reduced), native=str(native), native_width=3840)

    snapshot.safe_unlink(frame)

    assert not reduced.exists()
    assert not native.exists()


def test_safe_unlink_tolerates_missing_and_empty(tmp_path):
    snapshot.safe_unlink(None)
    snapshot.safe_unlink(str(tmp_path / "gone.jpg"))


def test_frame_carries_native_height():
    frame = snapshot.Frame("small.jpg", native="big.jpg", native_width=3840,
                           native_height=2160)
    assert frame == "small.jpg"
    assert (frame.native_width, frame.native_height) == (3840, 2160)


def test_frame_defaults_have_no_twin():
    frame = snapshot.Frame("only.jpg")
    assert frame.native is None
    assert frame.native_width is None
    assert frame.native_height is None
