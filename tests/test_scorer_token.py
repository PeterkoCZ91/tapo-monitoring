"""Opt-in shared token between the scorer service and its clients."""

import io
import json
import logging
import os
import threading
import urllib.error
import urllib.request

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, scorer

np = pytest.importorskip("numpy")

from tapo_monitor import scorer_service  # noqa: E402

DUMMY_TOKEN = "dummy-scorer-auth-0123456789abcdef"
URL = "http://203.0.113.10:8766/score"


# -- service ------------------------------------------------------------------

def _serve(token):
    srv = scorer_service.make_server(
        lambda body, tiles=1: {"person": 0.8, "animal": 0.0}, port=0, token=token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _req(base, path, auth=None, post=False):
    headers = {"Content-Type": "image/jpeg"}
    if auth is not None:
        headers["Authorization"] = auth
    req = urllib.request.Request(base + path, data=b"jpg" if post else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def secured():
    srv, base = _serve(DUMMY_TOKEN)
    yield base
    srv.shutdown()


@pytest.mark.parametrize("auth", [None, "Bearer wrong", f"Basic {DUMMY_TOKEN}", DUMMY_TOKEN, "Bearer ",
                                  "Bearer é"])
def test_score_and_metrics_refuse_without_the_token(secured, auth):
    for path, post in (("/score", True), ("/metrics", False)):
        try:
            status, body = _req(secured, path, auth, post)
        except (UnicodeEncodeError, ValueError):
            continue    # the client cannot even send a non-latin-1 header
        assert status == 401
        assert json.loads(body) == {"error": "unauthorized"}
        assert DUMMY_TOKEN not in body.decode()


def test_score_and_metrics_accept_the_token(secured):
    assert _req(secured, "/score", f"Bearer {DUMMY_TOKEN}", True)[0] == 200
    assert _req(secured, "/metrics", f"Bearer {DUMMY_TOKEN}")[0] == 200


def test_health_stays_open_and_reveals_nothing_secret(secured):
    status, body = _req(secured, "/health")
    assert status == 200
    assert DUMMY_TOKEN not in body.decode()


def test_unauthorized_requests_are_not_counted_as_scoring_traffic(secured):
    _req(secured, "/score", None, True)
    status, body = _req(secured, "/metrics", f"Bearer {DUMMY_TOKEN}")
    assert status == 200
    assert json.loads(body)["requests"] == 0


def test_without_a_token_nothing_changes():
    srv, base = _serve(None)
    try:
        assert _req(base, "/score", None, True)[0] == 200
        assert _req(base, "/metrics")[0] == 200
    finally:
        srv.shutdown()


def _write(path, text, mode):
    path.write_text(text)
    os.chmod(path, mode)
    return str(path)


def test_load_token_unset_is_none():
    assert scorer_service.load_token({}) is None
    assert scorer_service.load_token({"TAPO_SCORER_TOKEN": "  "}) is None


def test_load_token_value_and_file_prefers_file(tmp_path):
    f = _write(tmp_path / "t", "dummy-from-file\n", 0o600)
    assert scorer_service.load_token({"TAPO_SCORER_TOKEN": "dummy-from-env"}) == "dummy-from-env"
    assert scorer_service.load_token({"TAPO_SCORER_TOKEN": "dummy-from-env",
                                      "TAPO_SCORER_TOKEN_FILE": f}) == "dummy-from-file"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_load_token_refuses_a_readable_file(tmp_path, mode):
    f = _write(tmp_path / "t", "x", mode)
    with pytest.raises(scorer_service.TokenConfigError) as exc:
        scorer_service.load_token({"TAPO_SCORER_TOKEN_FILE": f})
    assert "0600" in str(exc.value)


def test_load_token_fails_closed_on_missing_or_empty_file(tmp_path):
    with pytest.raises(scorer_service.TokenConfigError):
        scorer_service.load_token({"TAPO_SCORER_TOKEN_FILE": str(tmp_path / "gone")})
    f = _write(tmp_path / "e", "\n", 0o600)
    with pytest.raises(scorer_service.TokenConfigError):
        scorer_service.load_token({"TAPO_SCORER_TOKEN_FILE": f})


# -- client -------------------------------------------------------------------

@pytest.fixture
def img(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"\xff\xd8x")
    return str(p)


def _capture(monkeypatch, exc=None):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        if exc is not None:
            raise exc
        return io.BytesIO(json.dumps({"person": 0.9, "animal": 0.0}).encode())
    monkeypatch.setattr(scorer.urllib.request, "urlopen", fake)
    return seen


def _app(**scorer_keys):
    return cfg.load_config_from_dict({"cameras": [{
        "name": "c", "host": "203.0.113.50", "scorer": {"url": URL, **scorer_keys}}]})


def test_score_image_sends_bearer_only_when_given(monkeypatch, img):
    seen = _capture(monkeypatch)
    scorer.score_image(URL, img, token=DUMMY_TOKEN)
    scorer.score_image(URL, img)
    assert seen[0].get_header("Authorization") == f"Bearer {DUMMY_TOKEN}"
    assert seen[1].get_header("Authorization") is None


def test_score_resolves_token_from_the_named_env(monkeypatch, img):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("MY_SCORER_TOKEN", DUMMY_TOKEN)
    scorer.score(_app(token_env="MY_SCORER_TOKEN").cameras[0].scorer, img)
    assert seen[0].get_header("Authorization") == f"Bearer {DUMMY_TOKEN}"


def test_score_resolves_token_file_before_env(monkeypatch, img, tmp_path):
    seen = _capture(monkeypatch)
    f = _write(tmp_path / "t", DUMMY_TOKEN + "\n", 0o600)
    monkeypatch.setenv("MY_SCORER_TOKEN", "stale")
    monkeypatch.setenv("MY_SCORER_TOKEN_FILE", f)
    scorer.score(_app(token_env="MY_SCORER_TOKEN").cameras[0].scorer, img)
    assert seen[0].get_header("Authorization") == f"Bearer {DUMMY_TOKEN}"


def test_no_token_env_means_no_header(monkeypatch, img):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("TAPO_SCORER_TOKEN", DUMMY_TOKEN)    # not named by the config: not used
    scorer.score(_app().cameras[0].scorer, img)
    assert seen[0].get_header("Authorization") is None


def _unauthorized(url=URL):
    return urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))


def test_401_is_not_a_transport_failure_and_is_logged_once(monkeypatch, img, caplog):
    _capture(monkeypatch, exc=_unauthorized())
    scorer._warned_unauthorized.clear()
    caplog.set_level(logging.DEBUG)
    for _ in range(4):
        assert scorer._score_once(URL, img, 5, 1, token=DUMMY_TOKEN) == (None, False)
    assert DUMMY_TOKEN not in caplog.text
    assert caplog.text.count("rejected the token") == 1


def test_repeated_401_never_fails_over(monkeypatch, img):
    standby = "http://198.51.100.20:8766/score"
    scorer.reset_pools()

    def fake(req, timeout=None):
        raise _unauthorized(req.full_url)
    monkeypatch.setattr(scorer.urllib.request, "urlopen", fake)
    monkeypatch.setattr(scorer, "_health_ok", lambda url, timeout=2.0: True)
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "c", "host": "203.0.113.50", "scorer": {"url": [URL, standby]}}]})
    for _ in range(scorer.FAIL_THRESHOLD + 2):
        scorer.score(app.cameras[0].scorer, img)
    assert scorer.active_url(app.cameras[0].scorer) == URL
    scorer.reset_pools()


def test_token_never_appears_in_logs_for_other_failures(monkeypatch, img, caplog):
    _capture(monkeypatch, exc=urllib.error.URLError("refused"))
    caplog.set_level(logging.DEBUG)
    scorer.score_image(URL, img, token=DUMMY_TOKEN)
    assert DUMMY_TOKEN not in caplog.text


def test_client_against_real_service_end_to_end(secured, img):
    assert scorer.score_image(secured + "/score", img) is None
    assert scorer.score_image(secured + "/score", img, token="dummy-invalid") is None
    assert scorer.score_image(secured + "/score", img, token=DUMMY_TOKEN)["person"] == 0.8


def test_fetch_metrics_sends_the_token(secured):
    assert "requests" in daemon._fetch_scorer_metrics(secured + "/metrics", token=DUMMY_TOKEN)
    with pytest.raises(urllib.error.HTTPError):
        daemon._fetch_scorer_metrics(secured + "/metrics")


def test_digest_snapshot_uses_the_configured_token(monkeypatch, secured):
    monkeypatch.setenv("MY_SCORER_TOKEN", DUMMY_TOKEN)
    app = cfg.load_config_from_dict({"cameras": [{
        "name": "c", "host": "203.0.113.50",
        "scorer": {"url": secured + "/score", "token_env": "MY_SCORER_TOKEN"}}]})
    snap = daemon.fleet_health_snapshot(app, daemon.MonitorState(), now=1000)
    assert snap["scorer"]["ok"] is True
    assert DUMMY_TOKEN not in json.dumps(snap)


# -- config -------------------------------------------------------------------

def test_token_env_default_none_and_accepts_a_name():
    assert _app().cameras[0].scorer.token_env is None
    assert _app(token_env="TAPO_SCORER_TOKEN").cameras[0].scorer.token_env == "TAPO_SCORER_TOKEN"


@pytest.mark.parametrize("bad", ["", "has space", "1ABC", "a-b", 5, ["X"], DUMMY_TOKEN + "=1"])
def test_token_env_must_be_an_env_variable_name(bad):
    with pytest.raises(cfg.ConfigError):
        _app(token_env=bad)
