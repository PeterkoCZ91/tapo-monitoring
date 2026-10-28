"""Opt-in ``scorer.url`` candidate list: one active URL, failover, return to the primary."""

import io
import json
import logging
import urllib.error

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, health, scorer

P = "http://203.0.113.10:8766/score"
S = "http://198.51.100.20:8766/score"
T = "http://198.51.100.30:8766/score"


def _cams(url, **scorer_extra):
    data = {"name": "c", "host": "203.0.113.50", "scorer": {"url": url, **scorer_extra}}
    return cfg.load_config_from_dict({"cameras": [data]}).cameras


def _scorer_cfg(url):
    return _cams(url)[0].scorer


class Net:
    """Fake network: ``down`` URLs refuse, ``bad`` URLs answer HTTP 400; /health follows ``down``."""

    def __init__(self, monkeypatch, down=()):
        self.down = set(down)
        self.bad = set()
        self.posts = []
        self.health = []
        monkeypatch.setattr(scorer.urllib.request, "urlopen", self._open)
        monkeypatch.setattr(scorer, "_health_ok", self._health_ok)

    def _open(self, req, timeout=None):
        url = req.full_url.split("?")[0]
        self.posts.append(url)
        if url in self.down:
            raise urllib.error.URLError("refused")
        if url in self.bad:
            raise urllib.error.HTTPError(url, 400, "bad image", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"person": 0.9, "animal": 0.0}).encode())

    def _health_ok(self, url, timeout=2.0):
        self.health.append(url)
        return url not in self.down


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "f.jpg"
    p.write_bytes(b"\xff\xd8x")
    return str(p)


# -- config ------------------------------------------------------------------

def test_string_url_is_unchanged():
    s = _scorer_cfg(P)
    assert s.url == P
    assert scorer.active_url(s) == P


def test_list_url_keeps_first_as_url_and_all_as_candidates():
    s = _scorer_cfg([P, S])
    assert s.url == P
    assert s.urls == (P, S)
    assert scorer.active_url(s) == P


@pytest.mark.parametrize("bad", [[], [P, ""], [P, 3], [P, P], 7])
def test_bad_urls_are_refused(bad):
    with pytest.raises(cfg.ConfigError):
        _scorer_cfg(bad)


def test_urls_is_not_a_user_key():
    with pytest.raises(cfg.ConfigError):
        _cams(P, urls=[P, S])


# -- failover ----------------------------------------------------------------

def test_single_url_never_probes_health(monkeypatch, img):
    net = Net(monkeypatch, down=[P])
    s = _scorer_cfg(P)
    for _ in range(10):
        assert scorer.score(s, img) is None
    assert net.health == []


def test_switches_after_n_consecutive_transport_failures(monkeypatch, img, caplog):
    net = Net(monkeypatch, down=[P])
    s = _scorer_cfg([P, S])
    caplog.set_level(logging.INFO)
    for _ in range(scorer.FAIL_THRESHOLD - 1):
        assert scorer.score(s, img) is None
    assert scorer.active_url(s) == P                      # not yet
    assert scorer.score(s, img) is None                   # the Nth failure triggers it
    assert scorer.active_url(s) == S
    assert net.health == [S]
    assert scorer.score(s, img) == {"person": 0.9, "animal": 0.0}
    assert net.posts[-1] == S
    switches = [r.getMessage() for r in caplog.records if "scorer switch" in r.getMessage()]
    assert len(switches) == 1
    assert P in switches[0] and S in switches[0]
    audit = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("audit ") and "scorer_switch" in r.getMessage()]
    assert len(audit) == 1
    assert "scorer_index=1" in audit[0]
    assert "203.0.113" not in audit[0] and "198.51.100" not in audit[0]


def test_success_resets_the_failure_streak(monkeypatch, img):
    net = Net(monkeypatch)
    s = _scorer_cfg([P, S])
    for _ in range(5):
        net.down = {P}
        for _ in range(scorer.FAIL_THRESHOLD - 1):
            scorer.score(s, img)
        net.down = set()
        assert scorer.score(s, img) is not None
    assert scorer.active_url(s) == P and net.health == []


def test_http_4xx_on_the_image_is_not_a_failure(monkeypatch, img):
    net = Net(monkeypatch)
    net.bad = {P}
    s = _scorer_cfg([P, S])
    for _ in range(scorer.FAIL_THRESHOLD * 3):
        assert scorer.score(s, img) is None
    assert scorer.active_url(s) == P and net.health == []


def test_no_live_candidate_keeps_the_active_url_and_does_not_probe_every_call(
        monkeypatch, img):
    net = Net(monkeypatch, down=[P, S])
    s = _scorer_cfg([P, S])
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(s, img)
    assert scorer.active_url(s) == P
    assert net.health == [S]
    scorer.score(s, img)                                  # one more failure: no new probe yet
    assert net.health == [S]


def test_tries_candidates_in_order_and_skips_dead_ones(monkeypatch, img):
    net = Net(monkeypatch, down=[P, S])
    s = _scorer_cfg([P, S, T])
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(s, img)
    assert net.health == [S, T]
    assert scorer.active_url(s) == T


def test_cameras_with_the_same_candidates_share_one_active_url(monkeypatch, img):
    Net(monkeypatch, down=[P])
    a, b = _scorer_cfg([P, S]), _scorer_cfg([P, S])
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(a, img)
    assert scorer.active_url(b) == S


def test_tiles_query_goes_to_the_active_url(monkeypatch, img):
    net = Net(monkeypatch, down=[P])
    s = _scorer_cfg([P, S])
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(s, img, tiles=2)
    scorer.score(s, img, tiles=2)
    assert net.posts[-1] == S


# -- return to the primary ---------------------------------------------------

def test_returns_to_primary_when_it_recovers_checked_every_five_minutes(monkeypatch, img,
                                                                       caplog):
    net = Net(monkeypatch, down=[P])
    now = [1000.0]
    monkeypatch.setattr(scorer, "_clock", lambda: now[0])
    s = _scorer_cfg([P, S])
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(s, img)
    assert scorer.active_url(s) == S
    net.health.clear()
    caplog.set_level(logging.INFO)
    now[0] += scorer.PRIMARY_RECHECK_SECONDS
    scorer.score(s, img)                                   # still down: probed once
    assert net.health == [P] and scorer.active_url(s) == S
    now[0] += 60
    scorer.score(s, img)                                   # inside the 5 min window: no probe
    assert net.health == [P]
    net.down = set()
    now[0] += scorer.PRIMARY_RECHECK_SECONDS
    scorer.score(s, img)
    assert scorer.active_url(s) == P
    assert net.posts[-1] == P
    assert any("scorer switch" in r.getMessage() for r in caplog.records)


def test_active_primary_is_never_probed_for_recovery(monkeypatch, img):
    net = Net(monkeypatch)
    now = [0.0]
    monkeypatch.setattr(scorer, "_clock", lambda: now[0])
    s = _scorer_cfg([P, S])
    for _ in range(5):
        now[0] += 1000
        scorer.score(s, img)
    assert net.health == []


# -- health URL --------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://203.0.113.10:8766/score", "http://203.0.113.10:8766/health"),
    ("http://203.0.113.10:8766/score?tiles=2", "http://203.0.113.10:8766/health"),
    ("http://203.0.113.10:8766", "http://203.0.113.10:8766/health"),
    ("http://203.0.113.10:8766/api/score", "http://203.0.113.10:8766/api/health"),
])
def test_health_url_is_the_services_own(url, expected):
    assert scorer.health_url(url) == expected


# -- persistence -------------------------------------------------------------

def test_switch_is_persisted_in_health_state_and_restored(monkeypatch, img, tmp_path):
    Net(monkeypatch, down=[P])
    state = daemon.MonitorState()
    state.health_path = str(tmp_path / "health.json")
    s = _scorer_cfg([P, S])
    daemon.watch_scorer_switches(state)
    for _ in range(scorer.FAIL_THRESHOLD):
        scorer.score(s, img)
    assert state.active_scorers == {P: 1}
    payload = json.loads((tmp_path / "health.json").read_text())
    assert payload["active_scorers"] == {P: 1}

    scorer.reset_pools()
    fresh = daemon.MonitorState()
    assert health.load_state(state.health_path, fresh)
    cams = _cams([P, S])
    daemon.apply_active_scorers(cams, fresh)
    assert scorer.active_url(cams[0].scorer) == S


def test_restore_ignores_an_index_that_is_no_longer_a_candidate():
    state = daemon.MonitorState()
    state.active_scorers = {P: 5}
    cams = _cams([P, S])
    daemon.apply_active_scorers(cams, state)
    assert scorer.active_url(cams[0].scorer) == P


def test_health_file_without_active_scorers_stays_unchanged(tmp_path):
    state = daemon.MonitorState()
    path = tmp_path / "health.json"
    assert health.save_state(str(path), state)
    assert "active_scorers" not in json.loads(path.read_text())


# -- one access point --------------------------------------------------------

def test_daemon_scoring_paths_read_the_url_through_the_pool(monkeypatch, img):
    net = Net(monkeypatch, down=[P])
    cam = _cams([P, S])[0]
    score = daemon.score_for(cam)
    for _ in range(scorer.FAIL_THRESHOLD):
        score(img)
    net.posts.clear()
    score(img)
    assert set(net.posts) == {S}


def test_scene_probe_reads_the_url_through_the_pool(monkeypatch, img):
    from tapo_monitor import scene_probe
    net = Net(monkeypatch, down=[P])
    cam = _cams([P, S])[0]
    probe = scene_probe._real_score(1)
    for _ in range(scorer.FAIL_THRESHOLD):
        probe(cam, img)
    net.posts.clear()
    probe(cam, img)
    assert set(net.posts) == {S}


def test_audit_summary_ignores_the_scorer_switch_line():
    from tapo_monitor import audit
    line = "audit path=scorer action=scorer_switch etype=system start=1 scorer_index=1"
    assert audit.summarize([line]) == {}


# -- background /health probe (outside of scoring) -----------------------------------------


def test_probe_moves_off_a_dead_candidate_before_any_frame_is_scored(monkeypatch, caplog):
    net = Net(monkeypatch, down={P})
    pool = scorer.pool_for(_scorer_cfg([P, S, T]))
    caplog.set_level(logging.WARNING, logger="tapo_monitor.scorer")
    pool.probe_once()
    assert pool.active == P                 # one failed round is not yet "down"
    pool.probe_once()
    assert pool.active == S
    assert not net.posts                    # nothing was scored to find out
    assert any("not answering /health" in r.getMessage() for r in caplog.records)


def test_probe_returns_to_the_primary_when_it_answers_again(monkeypatch):
    net = Net(monkeypatch, down={P})
    pool = scorer.pool_for(_scorer_cfg([P, S]))
    pool.probe_once()
    pool.probe_once()
    assert pool.active == S
    net.down.clear()
    pool.probe_once()
    assert pool.active == P


def test_probe_with_every_candidate_dead_keeps_the_active_url(monkeypatch):
    Net(monkeypatch, down={P, S})
    pool = scorer.pool_for(_scorer_cfg([P, S]))
    for _ in range(3):
        pool.probe_once()
    assert pool.active == P


def test_probe_reports_down_and_up_transitions_once(monkeypatch):
    net = Net(monkeypatch, down={P})
    events = []
    scorer.add_health_listener(lambda url, healthy: events.append((url, healthy)))
    pool = scorer.pool_for(_scorer_cfg([P, S]))
    for _ in range(4):
        pool.probe_once()
    assert events == [(P, False)]
    net.down.clear()
    pool.probe_once()
    pool.probe_once()
    assert events == [(P, False), (P, True)]


def test_start_probes_runs_in_the_background_and_stops_on_reset(monkeypatch):
    import threading

    net = Net(monkeypatch, down={P})
    monkeypatch.setattr(scorer, "PROBE_FAILS", 1)
    scorer.start_probes([_scorer_cfg([P, S])], interval=0.01)
    pool = scorer.pool_for(_scorer_cfg([P, S]))
    deadline = threading.Event()
    for _ in range(200):
        if pool.active == S:
            break
        deadline.wait(0.01)
    assert pool.active == S and net.health
    scorer.reset_pools()
    pool.probe_thread.join(2)
    assert not pool.probe_thread.is_alive()


def _watch_health(monkeypatch, notice_after):
    from types import SimpleNamespace

    from tapo_monitor import daemon, notify

    sent = []
    monkeypatch.setattr(notify, "send_operational", lambda tok, chat, text: sent.append(text))
    monkeypatch.setattr(scorer, "start_probes", lambda cfgs, interval=None: None)
    app = SimpleNamespace(cameras=[])
    daemon.watch_scorer_health(app, {"telegram_token": "t", "telegram_chat": "c"},
                               notice_after=notice_after)
    return sent, scorer._health_listeners[-1]


def test_scorer_blip_shorter_than_the_notice_delay_sends_nothing(monkeypatch):
    sent, listener = _watch_health(monkeypatch, notice_after=60)
    listener(P, False)
    listener(P, True)
    assert sent == []


def test_scorer_outage_is_announced_after_the_delay_and_recovery_follows(monkeypatch):
    import time

    sent, listener = _watch_health(monkeypatch, notice_after=0.05)
    listener(P, False)
    deadline = time.time() + 2
    while not sent and time.time() < deadline:
        time.sleep(0.01)
    assert len(sent) == 1 and "not answering" in sent[0]
    listener(P, True)
    assert len(sent) == 2 and "answers again" in sent[1]
