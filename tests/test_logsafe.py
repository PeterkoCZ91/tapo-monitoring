"""Camera call failures must reach the log as class + category, never as camera text."""

import types

import pytest
import requests

from tapo_monitor import (
    alertclip,
    camera,
    daemon,
    logsafe,
    sdclip,
    sdworker,
    tracking,
)
from tapo_monitor import (
    config as cfg,
)
from tapo_monitor.stopguard import StopGuard


def _cred_url(scheme, user, secret, rest):
    """Build a URL with embedded credentials at runtime (no literal credentialed URL in the file)."""
    return scheme + "://" + user + ":" + secret + "@" + rest


LEAK = _cred_url("http", "admin", "hunter2", "203.0.113.7:443/stok=abc123token/ds") + " failed host=203.0.113.7"
SECRETS = ("abc123token", "hunter2", "203.0.113.7")


def assert_clean(text):
    for secret in SECRETS:
        assert secret not in text, secret


def read_timeout():
    return requests.exceptions.ReadTimeout(LEAK)


# ── the helper ───────────────────────────────────────────────────────────────────

def test_mask_text_masks_urls_tokens_credentials_and_addresses():
    text = (_cred_url("rtsp", "u", "p4ss", "203.0.113.7:554/stream1") + ": Connection refused\n"
            "GET /stok=abc123token/ds " + "pass" + "word=hunter2 from 203.0.113.9 " + "user:" + "pw@203.0.113.8")
    masked = logsafe.mask_text(text)
    assert_clean(masked)
    for leaked in ("p4ss", "203.0.113.9", "pw@"):
        assert leaked not in masked
    assert "Connection refused" in masked          # the diagnosis stays


def test_requests_timeouts_are_timeouts_though_not_builtin():
    exc = read_timeout()
    assert not isinstance(exc, TimeoutError)
    assert logsafe.is_timeout(exc)
    assert logsafe.is_timeout(requests.exceptions.ConnectTimeout("x"))
    assert logsafe.is_timeout(TimeoutError("x"))
    assert not logsafe.is_timeout(RuntimeError("x"))


def test_describe_is_class_and_fixed_category_only():
    assert logsafe.describe(read_timeout()) == "class=ReadTimeout reason=read_timeout"
    assert logsafe.describe(requests.exceptions.ConnectTimeout(LEAK)) == (
        "class=ConnectTimeout reason=connect_timeout")
    assert logsafe.describe(Exception("Invalid stok: " + LEAK)).endswith(
        "reason=authentication_failure")
    assert logsafe.describe(RuntimeError(LEAK)) == "class=RuntimeError reason=other"
    assert logsafe.describe(None) == "class=none reason=no_result"
    assert_clean(logsafe.describe(RuntimeError(LEAK)))


def test_where_names_the_raise_site_without_the_message():
    try:
        raise RuntimeError(LEAK)
    except RuntimeError as exc:
        site = logsafe.where(exc)
    assert site.startswith("test_logsafe.py:") and site.endswith(":test_where_names_the_raise_site_without_the_message")
    assert_clean(site)


# ── sdworker / daemon follow-up ───────────────────────────────────────────────────

def test_a_failing_sd_job_logs_class_category_and_site_not_text_or_traceback(caplog):
    def fail():
        raise RuntimeError(LEAK)

    with caplog.at_level("DEBUG", logger=sdworker.__name__):
        key, value, error = sdworker.run_job(3, fail)
    assert (key, value) == (3, None) and isinstance(error, RuntimeError)
    assert_clean(caplog.text)
    assert "Traceback" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    assert "class=RuntimeError reason=other" in caplog.text and "where=" in caplog.text


def test_a_requests_read_timeout_in_a_sd_job_is_a_quiet_sd_timeout(caplog):
    def slow():
        raise read_timeout()

    with caplog.at_level("DEBUG", logger=sdworker.__name__):
        sdworker.run_job(3, slow)
    assert_clean(caplog.text)
    assert "Traceback" not in caplog.text
    assert [r.getMessage() for r in caplog.records] == ["SD follow-up job outcome=sd_timeout"]


def _pending_with(fetch_error, monkeypatch, caplog):
    app = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "a", "host": "203.0.113.10", "sd_snapshot": True}]})
    state = daemon.MonitorState()
    state.pending_sd = [{"camera": "a", "etype": "person", "event": {"start_time": 1000},
                         "due_at": 1075, "live_sent": False}]
    sent = []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda tok, chat, img, cap, **k: sent.append(img) or True)
    monkeypatch.setattr(daemon.enrich, "groq_describe", lambda *a, **k: "Person")

    def fetch(*args, **kwargs):
        raise fetch_error

    secrets = {"groq_key": "k", "telegram_token": "t", "telegram_chat": "c", "face_names": {}}
    with caplog.at_level("DEBUG"):
        daemon.process_pending_sd(
            app, {"a": object()}, state, now=1080, secrets=secrets,
            snapshot_for=lambda c: (lambda cam, ev: "/tmp/rtsp.jpg"),
            time_str=lambda ev: "T", fetch_frames=fetch)
    return sent


def test_pending_follow_up_with_a_generic_error_logs_no_camera_text(monkeypatch, caplog):
    sent = _pending_with(RuntimeError(LEAK), monkeypatch, caplog)
    assert len(sent) == 1                           # falls back to the live frame as before
    assert_clean(caplog.text)
    assert "Traceback" not in caplog.text
    assert "class=RuntimeError reason=other" in caplog.text


def test_pending_follow_up_with_a_requests_read_timeout_audits_sd_timeout(monkeypatch, caplog):
    sent = _pending_with(read_timeout(), monkeypatch, caplog)
    assert len(sent) == 1
    assert_clean(caplog.text)
    assert "reason=sd_timeout" in caplog.text and "Traceback" not in caplog.text


# ── sdclip subprocess output ──────────────────────────────────────────────────────

def _cfg():
    return types.SimpleNamespace(
        name="cam", host="203.0.113.12", user_env="U", password_env="P",
        cloud_password_env="C", rotate=0, hosts=None, failover_hosts=[], sd_fresh_guard=None)


FFMPEG_STDERR = ("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized\n" +
                 _cred_url("rtsp", "admin", "hunter2", "203.0.113.7:554/stream1") + ": Server returned 401\n" +
                 "request /stok=abc123token/ds\n")


@pytest.mark.parametrize("returncode,stdout", [(4, ""), (0, "")])
def test_frames_subprocess_masks_urls_and_tokens_in_the_stderr_tail(caplog, returncode, stdout):
    def run(argv, **kw):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=FFMPEG_STDERR)

    with caplog.at_level("WARNING", logger="tapo_monitor.sdclip"):
        sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY")
    assert_clean(caplog.text)
    assert "401" in caplog.text and "stderr:" in caplog.text     # still diagnostic


def test_frames_subprocess_run_failure_logs_class_not_message(caplog):
    def run(argv, **kw):
        raise requests.exceptions.ReadTimeout(LEAK)

    with caplog.at_level("INFO", logger="tapo_monitor.sdclip"):
        assert sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY") == []
    assert_clean(caplog.text)
    assert "class=ReadTimeout reason=read_timeout" in caplog.text


@pytest.mark.parametrize("returncode", [4, 0])
def test_clip_subprocess_masks_stderr_and_run_failure(caplog, returncode):
    def run(argv, **kw):
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr=FFMPEG_STDERR)

    def boom(argv, **kw):
        raise OSError(LEAK)

    with caplog.at_level("WARNING", logger="tapo_monitor.sdclip"):
        assert sdclip.fetch_sd_clip_subprocess(_cfg(), 1000, "/tmp", run=run,
                                               python="PY") == (None, None)
        assert sdclip.fetch_sd_clip_subprocess(_cfg(), 1000, "/tmp", run=boom,
                                               python="PY") == (None, None)
    assert_clean(caplog.text)
    assert "class=OSError" in caplog.text


def test_a_credential_cut_by_the_tail_limit_is_not_left_behind(caplog):
    noise = "x" * 790
    stderr = noise + " " + _cred_url("rtsp", "admin", "hunter2", "203.0.113.7:554/s") + "\n"

    def run(argv, **kw):
        return types.SimpleNamespace(returncode=4, stdout="", stderr=stderr)

    with caplog.at_level("WARNING", logger="tapo_monitor.sdclip"):
        sdclip.fetch_sd_frames_subprocess(_cfg(), 1000, run=run, python="PY")
    assert_clean(caplog.text)
    assert "hunter2" not in caplog.text and "p4ss" not in caplog.text


# ── camera control helpers ────────────────────────────────────────────────────────

class _FailingClient:
    def __getattr__(self, name):
        if name.startswith(("set", "reverse", "execute", "get")):
            def call(*args, **kwargs):
                raise Exception("ReadTimeout: " + LEAK)
            return call
        raise AttributeError(name)


@pytest.mark.parametrize("call", [
    lambda c: camera.set_lens_distortion_correction(c, True),
    lambda c: camera.set_tamper_detection(c, True),
    lambda c: camera.trigger_whitelamp(c),
])
def test_camera_control_failures_log_a_category_not_the_exception(caplog, call):
    with caplog.at_level("DEBUG", logger="tapo_monitor.camera"):
        call(_FailingClient())
    assert caplog.records
    assert_clean(caplog.text)
    assert "reason=read_timeout" in caplog.text


def test_set_autotrack_refusal_warning_has_no_camera_text(caplog):
    tracking._BACK_TIME_WARNED.clear()

    class Cam:
        host = "203.0.113.7"

        def executeFunction(self, method, params):
            raise Exception(LEAK)

        def setAutoTrackTarget(self, enabled):
            pass

        def getAutoTrackTarget(self):
            return {"enabled": "off", "back_time": "30"}

    with caplog.at_level("DEBUG", logger="tapo_monitor.tracking"):
        tracking.set_autotrack(Cam(), True, back_time=180)
    assert "refused the combined" in caplog.text
    assert_clean(caplog.text)


def test_card_read_failure_for_a_clip_logs_no_camera_text(caplog):
    cfgx = types.SimpleNamespace(name="cam", clip_pre_roll=5, clip_seconds=10,
                                 sd_fresh_guard=None, clip_height=360)
    entry = types.SimpleNamespace(incident="i1", start=1000)

    def fetch(*args):
        raise RuntimeError(LEAK)

    with caplog.at_level("WARNING", logger="tapo_monitor.alertclip"):
        alertclip.read_clip(cfgx, entry, fetch=fetch)
    assert "card read failed" in caplog.text
    assert_clean(caplog.text)


# ── generic loops: traceback stays, camera text goes ──────────────────────────────

def test_tick_error_keeps_a_traceback_with_urls_and_tokens_masked(monkeypatch, caplog):
    app = cfg.load_config_from_dict({"groq": {}, "cameras": [
        {"name": "a", "host": "203.0.113.10"}]})
    state = daemon.MonitorState()
    monkeypatch.setattr(daemon.notify, "send_text", lambda *a, **k: True)

    def boom(*_a, **_k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(daemon, "loop_step", boom)
    with caplog.at_level("ERROR", logger="tapo_monitor.daemon"):
        daemon.tick(app, {}, state, now=1060,
                    secrets={"telegram_token": "t", "telegram_chat": "c"},
                    last_control=None, control_interval=60)
    assert "tick error" in caplog.text and "Traceback" in caplog.text
    assert "RuntimeError" in caplog.text and "boom" in caplog.text
    assert_clean(caplog.text)
    assert all(r.exc_info is None for r in caplog.records)


def test_stop_guard_loop_error_keeps_a_masked_traceback(caplog):
    guard = StopGuard(lambda: 0.5, lambda: None, lambda: True, right_limit=0.8, margin=0.1,
                      clock=lambda: 0.0)
    try:
        raise RuntimeError(LEAK)
    except RuntimeError as exc:
        with caplog.at_level("ERROR", logger="tapo_monitor.stopguard"):
            guard._loop_failed(exc)
    guard.close()
    assert "PTZ stop guard loop error" in caplog.text and "Traceback" in caplog.text
    assert_clean(caplog.text)
    assert all(r.exc_info is None for r in caplog.records)


def test_a_numeric_error_code_stays_but_its_message_does_not():
    class TapoError(Exception):
        code = -40401

    assert logsafe.describe(TapoError(LEAK)) == "class=TapoError reason=other code=-40401"


def test_connect_failure_warning_has_class_and_category_not_the_error_text(monkeypatch, caplog):
    monkeypatch.setattr(camera, "ping_reachable", lambda host: True)
    monkeypatch.setattr(camera, "tapo_factory", lambda *a: lambda: None)
    monkeypatch.setattr(camera, "connect",
                        lambda factory: (None, requests.exceptions.ConnectTimeout(LEAK)))
    state = daemon.MonitorState()
    cfgx = cfg.load_config_from_dict(
        {"groq": {}, "cameras": [{"name": "c", "host": "203.0.113.10"}]}).cameras[0]

    with caplog.at_level("WARNING", logger="tapo_monitor.daemon"):
        client, _err = daemon._connect_camera({}, state, now=100)(cfgx)

    assert client is None
    assert "connect c failed (#1): class=ConnectTimeout reason=connect_timeout" in caplog.text
    assert_clean(caplog.text)


class _RefusingReads:
    def _boom(self, *args, **kwargs):
        raise Exception("ReadTimeout: " + LEAK)

    getPrivacyMode = getMotionDetection = getPersonDetection = _boom
    getVehicleDetection = getMsgPushConfig = _boom


def test_optional_camera_reads_log_a_category_not_the_exception(caplog):
    cam = _RefusingReads()
    with caplog.at_level("DEBUG", logger="tapo_monitor.daemon"):
        assert daemon.read_privacy(cam) is None
        assert daemon.read_detection(cam) == {"motion": None, "person": None}
        daemon.read_lenses(cam, (1, 2))
        assert daemon.read_app_notifications(cam, "cam") is None
    assert caplog.text.count("reason=read_timeout") >= 4
    assert_clean(caplog.text)
