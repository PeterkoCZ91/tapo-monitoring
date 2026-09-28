"""The opt-in outbox: alerts Telegram did not take are kept on disk and delivered late."""

import json
import os

import pytest

from tapo_monitor import config as cfg
from tapo_monitor import daemon, outbox, scorer, sentlog
from tests.conftest import FakeResponse

SECRETS = {"telegram_token": "t", "telegram_chat": "c", "groq_key": ""}
T0 = 1_790_000_000.0            # a fixed "now" for the drains


def _app(outbox_block=None, **camera):
    data = {"cameras": [{"name": "yard", "host": "203.0.113.10", **camera}]}
    if outbox_block is not None:
        data["outbox"] = outbox_block
    return cfg.load_config_from_dict(data)


def _frame(tmp_path, name="frame.jpg", data=b"\xff\xd8JPEG"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _box(tmp_path, **kw):
    return outbox.Outbox(str(tmp_path / "outbox"), **kw)


def _capture(box, tmp_path, start, *, etype="person", score=None, failed_at=None,
             camera="yard"):
    image = _frame(tmp_path, f"f{start}.jpg")
    return box.capture(camera=camera, image=image, caption=f"👤 {start}",
                       failed_at=failed_at if failed_at is not None else start + 5,
                       incident=f"{camera}-{start}", etype=etype, score=score,
                       send_path="live")


class _Recorder:
    """Records the drain's Telegram / review-log calls in one ordered list."""

    def __init__(self, text_ok=True, photo_ok=None):
        self.calls = []
        self.text_ok = text_ok
        self.photo_ok = photo_ok or (lambda n: True)

    def send_text(self, text):
        self.calls.append(("text", text))
        return self.text_ok

    def send_photo(self, entry, caption):
        n = sum(1 for c in self.calls if c[0] == "photo")
        self.calls.append(("photo", entry.incident, caption))
        return self.photo_ok(n)

    def archive_review(self, entry, meta):
        self.calls.append(("review", entry.incident, meta))

    def drain(self, box, now=T0, *, score_for=None, threshold=0.5, busy=frozenset(),
              probe=lambda: True):
        return outbox.drain(box, now=now, known_cameras={"yard", "gate"},
                            send_text=self.send_text, send_photo=self.send_photo,
                            archive_review=self.archive_review,
                            score_for=score_for or (lambda camera: None),
                            threshold_for=lambda camera, ts: threshold,
                            probe=probe, busy=busy)


# ── config ───────────────────────────────────────────────────────────────────

def test_outbox_is_off_by_default():
    app = _app()
    assert app.outbox.enabled is False
    assert daemon.configure_outbox(app) is None


def test_outbox_block_parses_and_validates():
    app = _app({"enabled": True, "max_age": 3600, "max_entries": 50, "max_photos": 3,
                "summary": False, "dir": "~/ob"})
    ob = app.outbox
    assert (ob.enabled, ob.max_age, ob.max_entries, ob.max_photos, ob.summary) == (
        True, 3600, 50, 3, False)
    assert ob.dir == os.path.expanduser("~/ob")
    for bad in ({"enabled": "yes"}, {"max_age": 10}, {"max_entries": 0},
                {"max_photos": True}, {"dir": ""}, {"surprise": 1}):
        with pytest.raises(cfg.ConfigError):
            _app(bad)


def test_default_dir_sits_beside_the_state_files(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert daemon.outbox_default_dir(env) == str(tmp_path / "tapo-monitor" / "outbox")
    app = _app({"enabled": True})
    box = daemon.configure_outbox(app, env=env)
    try:
        assert box.dir == str(tmp_path / "tapo-monitor" / "outbox")
    finally:
        daemon._outbox = None


# ── capture and dedupe through the send funnel ──────────────────────────────

@pytest.fixture
def box(tmp_path, monkeypatch):
    b = _box(tmp_path)
    monkeypatch.setattr(daemon, "_outbox", b)
    return b


def test_failed_send_is_captured_with_its_metadata(box, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_deliver_alert_photo", lambda *a, **k: False)
    cam = _app().cameras[0]
    image = _frame(tmp_path)
    ok = daemon.send_alert_photo(cam, SECRETS, image, "👤 caption",
                                 score=scorer.SubjectScore(0.8, 0.1, 1),
                                 incident="yard-1790000000", send_path="live", etype="person")
    assert ok is False
    [entry] = box.entries()
    assert entry.incident == "yard-1790000000"
    assert entry.event_start == 1_790_000_000
    assert (entry.camera, entry.etype, entry.caption, entry.send_path) == (
        "yard", "person", "👤 caption", "live")
    assert entry.score == {"person": 0.8, "animal": 0.1, "persons": 1}
    with open(entry.image, "rb") as fh:
        assert fh.read() == b"\xff\xd8JPEG"
    assert not [n for n in os.listdir(box.dir) if n.endswith(".tmp")]


def test_nothing_is_captured_when_the_outbox_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_outbox", None)
    monkeypatch.setattr(daemon, "_deliver_alert_photo", lambda *a, **k: False)
    daemon.send_alert_photo(_app().cameras[0], SECRETS, _frame(tmp_path), "x",
                            incident="yard-1790000000")
    assert not (tmp_path / "outbox").exists()


def test_a_collect_only_camera_is_never_captured(box, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_deliver_alert_photo", lambda *a, **k: False)
    cam = _app(telegram_alerts=False).cameras[0]
    daemon.send_alert_photo(cam, SECRETS, _frame(tmp_path), "x", incident="yard-1790000000")
    assert box.entries() == []


def test_a_later_delivery_of_the_incident_removes_its_entry(box, tmp_path, monkeypatch):
    results = iter([False, True])
    monkeypatch.setattr(daemon, "_deliver_alert_photo", lambda *a, **k: next(results))
    cam = _app().cameras[0]
    daemon.send_alert_photo(cam, SECRETS, _frame(tmp_path), "live", incident="yard-1790000000",
                            send_path="live")
    assert len(box.entries()) == 1
    # The SD follow-up of the same event gets through a minute later.
    daemon.send_alert_photo(cam, SECRETS, _frame(tmp_path, "sd.jpg"), "sd",
                            incident="yard-1790000000", send_path="sd")
    assert box.entries() == []
    assert box.last_ok_at is not None


def test_a_repeated_failure_updates_the_frame_but_keeps_the_first_failure_time(box, tmp_path):
    box.capture(camera="yard", image=_frame(tmp_path, "a.jpg", b"A"), caption="live",
                failed_at=100.0, incident="yard-90", etype="person")
    box.capture(camera="yard", image=_frame(tmp_path, "b.jpg", b"B"), caption="sd",
                failed_at=160.0, incident="yard-90", etype="person",
                score=scorer.SubjectScore(0.9, 0.0))
    [entry] = box.entries()
    assert (entry.failed_at, entry.caption, entry.score["person"]) == (100.0, "sd", 0.9)
    with open(entry.image, "rb") as fh:
        assert fh.read() == b"B"


def test_max_entries_drops_the_oldest(tmp_path):
    box = _box(tmp_path, max_entries=2)
    for start in (1000, 2000, 3000):
        _capture(box, tmp_path, start)
    assert [e.incident for e in box.entries()] == ["yard-2000", "yard-3000"]


def test_entries_survive_a_restart(tmp_path):
    _capture(_box(tmp_path), tmp_path, 1000)
    assert [e.incident for e in _box(tmp_path).entries()] == ["yard-1000"]


def test_a_sidecar_without_its_frame_is_dropped(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, 1000)
    os.remove(os.path.join(box.dir, "yard-1000.jpg"))
    assert box.entries() == []
    assert os.listdir(box.dir) == []


# ── drain ────────────────────────────────────────────────────────────────────

def test_drain_sends_the_summary_then_persons_and_reviews_the_rest(tmp_path):
    box = _box(tmp_path)
    base = T0 - 5 * 3600
    _capture(box, tmp_path, int(base), etype="motion", score=scorer.SubjectScore(0.9, 0))
    _capture(box, tmp_path, int(base) + 60, etype="motion", score=scorer.SubjectScore(0.1, 0))
    _capture(box, tmp_path, int(base) + 120, etype="person")          # unscored person
    rec = _Recorder()
    report = rec.drain(box)
    kinds = [c[0] for c in rec.calls]
    assert kinds == ["text", "review", "photo", "photo"]
    text = rec.calls[0][1]
    assert "nedoručeno 3 události, z toho 2 s osobou" in text
    assert "Posílám zpožděně 2 fotky s osobou" in text
    assert "yard" in text and "+1 další jen v review logu" in text
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [
        f"yard-{int(base)}", f"yard-{int(base) + 120}"]
    assert rec.calls[2][2].startswith("⏳ zpožděno o 5 h 0 min · 👤 ")
    assert rec.calls[1][2]["verdict"] == "outbox" and rec.calls[1][2]["person"] == 0.1
    assert report == {"sent": 2, "reviewed": 1, "dropped": 0, "failed": 0, "summaries": 1,
                      "stopped": False, "budget": False}
    assert box.entries() == []


def test_summary_text_is_html_escaped():
    entry = outbox.Entry(key="k", camera="a<b>&c", image="x", caption="", failed_at=T0)
    text = outbox.summary_text("a<b>&c", [entry], 0, 0, T0)
    assert "a&lt;b&gt;&amp;c" in text


def test_entries_past_max_age_are_dropped_unsent(tmp_path):
    box = _box(tmp_path, max_age=3600)
    _capture(box, tmp_path, int(T0 - 7200))
    _capture(box, tmp_path, int(T0 - 1800))
    rec = _Recorder()
    report = rec.drain(box)
    assert report["dropped"] == 1
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [f"yard-{int(T0 - 1800)}"]


def test_persons_past_the_cap_go_to_the_review_log(tmp_path):
    box = _box(tmp_path, max_photos=2)
    for i in range(4):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60)
    rec = _Recorder()
    rec.drain(box)
    assert [c[0] for c in rec.calls] == ["text", "review", "review", "photo", "photo"]
    assert "z toho 4 s osobou" in rec.calls[0][1]
    assert "Posílám zpožděně 2 fotky s osobou" in rec.calls[0][1]
    assert "+2 další jen v review logu" in rec.calls[0][1]
    assert box.entries() == []


def test_a_failed_late_send_stops_the_drain_and_keeps_the_rest(tmp_path):
    box = _box(tmp_path)
    for i in range(3):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60)
    rec = _Recorder(photo_ok=lambda n: n == 0)        # the second photo fails
    report = rec.drain(box)
    assert report["stopped"] and report["sent"] == 1
    left = box.entries()
    assert [e.incident for e in left] == [f"yard-{int(T0 - 3600) + 60}",
                                          f"yard-{int(T0 - 3600) + 120}"]
    assert all(e.announced for e in left)
    # Backed off: the next tick does not retry at once.
    rec2 = _Recorder()
    assert rec2.drain(box, now=T0 + 5, probe=lambda: False)["sent"] == 0
    assert rec2.calls == []
    # A minute later Telegram answers: the rest go, without a second summary.
    rec3 = _Recorder()
    rec3.drain(box, now=T0 + 120)
    assert [c[0] for c in rec3.calls] == ["photo", "photo"]
    assert box.entries() == []


def test_a_failed_summary_sends_nothing_and_removes_nothing(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="motion")
    rec = _Recorder(text_ok=False)
    assert rec.drain(box)["stopped"]
    assert [c[0] for c in rec.calls] == ["text"]
    assert len(box.entries()) == 1


def test_unscored_entries_are_rescored_before_the_decision(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="motion")
    _capture(box, tmp_path, int(T0 - 3000), etype="person")
    scores = {f"f{int(T0 - 3600)}": 0.92, f"f{int(T0 - 3000)}": 0.05}
    seen = []

    def score(path):
        seen.append(path)
        return scorer.SubjectScore(scores[_stem(path)], 0.0)

    rec = _Recorder()
    rec.drain(box, score_for=lambda camera: score)
    assert len(seen) == 2
    # The motion frame showed a person; the "person" event scored 0.05 and is reviewed.
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [f"yard-{int(T0 - 3600)}"]
    review = [c[2] for c in rec.calls if c[0] == "review"]
    assert review[0]["person"] == 0.05


def _stem(path):
    # Stored frames are named after the entry key; map back to the source frame's name.
    return "f" + os.path.basename(path)[:-4].rsplit("-", 1)[1]


def test_a_still_dead_scorer_leaves_entries_unscored_and_is_asked_once(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="person")
    _capture(box, tmp_path, int(T0 - 3000), etype="motion")
    calls = []
    rec = _Recorder()
    rec.drain(box, score_for=lambda camera: (lambda path: calls.append(path)))
    assert len(calls) == 1
    # Unscored: the camera's own event type decides.
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [f"yard-{int(T0 - 3600)}"]


def test_fresh_and_busy_entries_wait_for_the_in_memory_retries(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600))
    _capture(box, tmp_path, int(T0 - 30), failed_at=T0 - 30)           # too fresh
    rec = _Recorder()
    rec.drain(box, busy={f"yard-{int(T0 - 3600)}"})
    assert rec.calls == []
    assert len(box.entries()) == 2


def test_the_probe_runs_at_most_once_a_minute_while_telegram_is_down(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600))
    probes = []

    def probe():
        probes.append(1)
        return False

    rec = _Recorder()
    for dt in (0, 4, 8, 30):
        rec.drain(box, now=T0 + dt, probe=probe)
    assert len(probes) == 1
    rec.drain(box, now=T0 + 61, probe=probe)
    assert len(probes) == 2 and rec.calls == []


def test_a_recent_live_delivery_skips_the_probe(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600))
    box.note_delivered(T0 - 10)
    rec = _Recorder()
    rec.drain(box, probe=lambda: pytest.fail("probe not needed"))
    assert [c[0] for c in rec.calls] == ["text", "photo"]


def test_empty_outbox_costs_nothing(tmp_path):
    rec = _Recorder()
    assert rec.drain(_box(tmp_path), probe=lambda: pytest.fail("no probe"))["sent"] == 0


def test_delay_formatting():
    assert outbox.format_delay(125) == "2 min"
    assert outbox.format_delay(3 * 3600 + 7 * 60 + 5) == "3 h 7 min"


# ── daemon wiring ────────────────────────────────────────────────────────────

def test_outbox_pass_is_a_no_op_when_off(monkeypatch):
    monkeypatch.setattr(daemon, "_outbox", None)
    assert daemon.outbox_pass(_app(), daemon.MonitorState(), now=T0, secrets=SECRETS) is None


def test_outbox_pass_delivers_through_the_real_path_without_arming_cooldowns(
        tmp_path, monkeypatch):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="person",
             score=scorer.SubjectScore(0.7, 0.0))
    # Unscored motion, past the wait for the scorer: its event type decides now.
    _capture(box, tmp_path, int(T0 - outbox.UNSCORED_WAIT - 60), etype="motion")
    sent_dir = tmp_path / "sent"
    review_dir = tmp_path / "review"
    monkeypatch.setenv(sentlog.ENV_DIR, str(sent_dir))
    monkeypatch.setenv(sentlog.ENV_REVIEW_DIR, str(review_dir))
    requests = []

    def urlopen(req, timeout=None):
        requests.append(req if isinstance(req, str) else req.full_url)
        return FakeResponse()

    monkeypatch.setattr(daemon.notify.urllib.request, "urlopen", urlopen)
    # Day threshold 0.8, night 0.6: the 0.7 person at night counts although the link
    # came back in daylight.
    app = _app(scorer={"url": "http://x/score", "threshold": 0.8, "night_threshold": 0.6})
    state = daemon.MonitorState()
    report = daemon.outbox_pass(app, state, now=T0, secrets=SECRETS, box=box,
                                probe=lambda: True, night_at=lambda ts: True,
                                score_for_fn=lambda c: (lambda p: None))
    assert report["sent"] == 1 and report["reviewed"] == 1
    assert [u.rsplit("/", 1)[1] for u in requests] == ["sendMessage", "sendPhoto"]
    rec = json.loads((sent_dir / "index.jsonl").read_text().strip())
    assert rec["path"] == "outbox" and rec["incident"] == f"yard-{int(T0 - 3600)}"
    review = json.loads((review_dir / "index.jsonl").read_text().strip())
    assert review["verdict"] == "outbox" and "person" not in review
    assert state.last_alert == {}
    assert box.entries() == []


def test_outbox_pass_leaves_an_incident_to_its_pending_sd_follow_up(tmp_path, monkeypatch):
    box = _box(tmp_path)
    start = int(T0 - 300)
    _capture(box, tmp_path, start)
    state = daemon.MonitorState()
    state.pending_sd.append({"camera": "yard", "etype": "person",
                             "event": {"start_time": start}})
    monkeypatch.setattr(daemon.notify.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("nothing to send yet"))
    report = daemon.outbox_pass(_app(), state, now=T0, secrets=SECRETS, box=box,
                                probe=lambda: True)
    assert report["sent"] == 0 and len(box.entries()) == 1


def test_tick_runs_the_outbox_after_the_sd_drain():
    order = []
    app = _app()
    state = daemon.MonitorState()
    noop = lambda *a, **k: None  # noqa: E731
    daemon.loop_step(
        app, {}, state, now=T0, secrets=SECRETS, last_control=T0, control_interval=60,
        monitor=noop, hubpoll=noop, sample=noop, guard=noop, digest=noop,
        drain=lambda *a, **k: order.append("sd"),
        late=lambda *a, **k: order.append("outbox"),
        is_night=lambda: False)
    assert order == ["sd", "outbox"]


# ── review fixes: budget, per-entry failures, downgrades, truthfulness ──────

def test_plural_forms():
    forms = ("událost", "události", "událostí")
    assert [outbox.plural(n, *forms) for n in (0, 1, 2, 4, 5, 11, 22)] == [
        "událostí", "událost", "události", "události", "událostí", "událostí", "událostí"]


def test_summary_grammar_for_one_and_five():
    one = [outbox.Entry(key="k", camera="yard", image="x", caption="", failed_at=T0)]
    text = outbox.summary_text("yard", one, 1, 1, T0)
    assert "nedoručeno 1 událost," in text and "Posílám zpožděně 1 fotku s osobou" in text
    five = one * 6
    text = outbox.summary_text("yard", five, 5, 5, T0)
    assert "6 událostí" in text and "5 fotek" in text and "+1 další jen v review logu" in text


def test_summary_says_not_sent_when_there_is_no_review_log():
    entries = [outbox.Entry(key="k", camera="yard", image="x", caption="", failed_at=T0)] * 7
    text = outbox.summary_text("yard", entries, 0, 0, T0, review_enabled=False)
    assert "+7 dalších neodesláno" in text and "review" not in text


def test_the_drain_sends_at_most_a_few_photos_per_tick_and_continues(tmp_path):
    box = _box(tmp_path)
    for i in range(5):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60)
    rec = _Recorder()
    report = outbox.drain(
        box, now=T0, known_cameras={"yard"}, send_text=rec.send_text,
        send_photo=rec.send_photo, archive_review=rec.archive_review,
        score_for=lambda c: None, threshold_for=lambda c, ts: 0.5, probe=lambda: True)
    assert report["sent"] == 3 and report["budget"] is True
    assert [c[0] for c in rec.calls] == ["text", "photo", "photo", "photo"]
    rec2 = _Recorder()
    rec2.drain(box, now=T0 + 4)           # the next tick: the rest, no second summary
    assert [c[0] for c in rec2.calls] == ["photo", "photo"]
    assert box.entries() == []


def test_rescoring_is_budgeted_and_the_summary_waits_for_the_whole_batch(tmp_path):
    box = _box(tmp_path)
    for i in range(7):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60, etype="motion")
    scored = []

    def score(path):
        scored.append(path)
        return scorer.SubjectScore(0.1, 0.0)

    rec = _Recorder()
    report = rec.drain(box, score_for=lambda c: score)
    assert len(scored) == outbox.MAX_RESCORES_PER_TICK and report["budget"]
    assert rec.calls == []                        # no summary for half a batch
    rec.drain(box, now=T0 + 4, score_for=lambda c: score)
    assert len(scored) == 7
    assert [c[0] for c in rec.calls] == ["text"] + ["review"] * 7


def test_the_wall_clock_budget_stops_a_slow_drain(tmp_path):
    box = _box(tmp_path)
    for i in range(3):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60)
    ticks = iter(range(0, 1000, 6))               # every clock read is 6 s later
    rec = _Recorder()
    report = outbox.drain(box, now=T0, known_cameras={"yard"}, send_text=rec.send_text,
                          send_photo=rec.send_photo, archive_review=rec.archive_review,
                          score_for=lambda c: None, threshold_for=lambda c, ts: 0.5,
                          probe=lambda: True, clock=lambda: next(ticks))
    assert report["budget"] and report["sent"] < 3


def test_an_undeliverable_photo_goes_to_the_review_log_after_three_attempts(tmp_path):
    box = _box(tmp_path)
    bad, good = int(T0 - 3600), int(T0 - 3000)
    _capture(box, tmp_path, bad)
    _capture(box, tmp_path, good)
    now = T0
    for attempt in range(1, 4):
        rec = _Recorder()
        rec.photo_ok = lambda n, rec=rec: rec.calls[-1][1] != f"yard-{bad}"
        rec.drain(box, now=now)
        now += outbox.PROBE_INTERVAL + 1
        if attempt < 3:
            assert [e.attempts for e in box.entries() if e.incident == f"yard-{bad}"] == [
                attempt]
    reviewed = [c for c in rec.calls if c[0] == "review"]
    assert reviewed[0][1] == f"yard-{bad}" and reviewed[0][2]["reason"] == "send_failed"
    assert [c[1] for c in rec.calls if c[0] == "photo"][-1] == f"yard-{good}"
    assert box.entries() == []


def test_a_failed_photo_stops_only_its_camera(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), camera="yard")
    _capture(box, tmp_path, int(T0 - 3000), camera="gate")
    rec = _Recorder()
    rec.photo_ok = lambda n: not rec.calls[-1][1].startswith("yard")
    report = rec.drain(box)
    assert report["sent"] == 1 and report["stopped"]
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [
        f"yard-{int(T0 - 3600)}", f"gate-{int(T0 - 3000)}"]
    assert [e.camera for e in box.entries()] == ["yard"]


def test_a_failed_summary_stops_every_camera(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), camera="yard")
    _capture(box, tmp_path, int(T0 - 3000), camera="gate")
    rec = _Recorder(text_ok=False)
    rec.drain(box)
    assert [c[0] for c in rec.calls] == ["text"]
    assert len(box.entries()) == 2


def test_a_later_summary_counts_only_entries_not_reported_before(tmp_path):
    box = _box(tmp_path)
    for i in range(2):
        _capture(box, tmp_path, int(T0 - 3600) + i * 60)
    rec = _Recorder(photo_ok=lambda n: False)
    rec.drain(box)
    assert "nedoručeno 2 události" in rec.calls[0][1]
    _capture(box, tmp_path, int(T0 - 600), failed_at=T0 - 300)
    rec2 = _Recorder()
    rec2.drain(box, now=T0 + outbox.PROBE_INTERVAL + 1)
    texts = [c[1] for c in rec2.calls if c[0] == "text"]
    assert len(texts) == 1 and "nedoručeno 1 událost," in texts[0]
    assert "Posílám zpožděně 1 fotku" in texts[0]
    assert len([c for c in rec2.calls if c[0] == "photo"]) == 3


def test_an_unscored_motion_entry_waits_for_the_scorer(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="motion")
    _capture(box, tmp_path, int(T0 - 3000), etype="person")
    dead = lambda c: (lambda path: None)  # noqa: E731
    rec = _Recorder()
    rec.drain(box, score_for=dead)
    # The unscored person goes out at once; the motion waits for a working scorer.
    assert [c[1] for c in rec.calls if c[0] == "photo"] == [f"yard-{int(T0 - 3000)}"]
    assert "nedoručeno 1 událost," in rec.calls[0][1]
    assert [e.etype for e in box.entries()] == ["motion"]
    # Six hours after its failure the event type decides alone: the review log.
    late = T0 - 3600 + 5 + outbox.UNSCORED_WAIT + 1
    rec2 = _Recorder()
    rec2.drain(box, now=late, score_for=dead)
    assert [c[0] for c in rec2.calls] == ["text", "review"]
    assert box.entries() == []


def test_a_rescored_score_is_persisted(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, int(T0 - 3600), etype="motion")
    rec = _Recorder(text_ok=False)                # stops after scoring, before sending
    rec.drain(box, score_for=lambda c: (lambda p: scorer.SubjectScore(0.77, 0.2, 1)))
    [entry] = _box(tmp_path).entries()
    assert entry.score == {"person": 0.77, "animal": 0.2, "persons": 1}


def test_a_repeat_capture_never_downgrades_etype_or_score(box, tmp_path):
    box.capture(camera="yard", image=_frame(tmp_path, "a.jpg"), caption="live",
                failed_at=100.0, incident="yard-90", etype="person",
                score=scorer.SubjectScore(0.9, 0.0))
    box.capture(camera="yard", image=_frame(tmp_path, "b.jpg"), caption="sd",
                failed_at=160.0, incident="yard-90", etype="motion", score=None)
    [entry] = box.entries()
    assert (entry.etype, entry.score["person"], entry.caption) == ("person", 0.9, "sd")


def test_max_entries_evicts_non_persons_first(tmp_path):
    box = _box(tmp_path, max_entries=2)
    _capture(box, tmp_path, 1000, etype="person")
    _capture(box, tmp_path, 2000, etype="motion")
    _capture(box, tmp_path, 3000, etype="motion")
    assert [e.incident for e in box.entries()] == ["yard-1000", "yard-3000"]


def test_a_collect_only_success_does_not_mark_telegram_reachable(box, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_deliver_alert_photo", lambda *a, **k: True)
    cam = _app(telegram_alerts=False).cameras[0]
    daemon.send_alert_photo(cam, SECRETS, _frame(tmp_path), "x", incident="yard-1790000000")
    assert box.last_ok_at is None
    daemon.send_alert_photo(_app().cameras[0], SECRETS, _frame(tmp_path), "x",
                            incident="yard-1790000001")
    assert box.last_ok_at is not None


def test_the_hub_retry_passes_the_stored_event_type(monkeypatch, tmp_path):
    seen = {}

    def fake_send(cfg_, secrets, image, caption, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(daemon, "send_alert_photo", fake_send)
    monkeypatch.setattr(daemon.monitor, "audit_event", lambda *a, **k: None)
    state = daemon.MonitorState()
    state.pending_hub.append({"camera": "yard", "start_time": T0 - 60,
                              "image": _frame(tmp_path), "caption": "c", "score": None,
                              "etype": "person", "attempts": 1, "queued_at": T0 - 60,
                              "due_at": T0 - 1})
    daemon.process_pending_hub(_app(), state, now=T0, secrets=SECRETS)
    assert seen["etype"] == "person" and seen["send_path"] == "hubpoll_retry"


def test_sd_decisions_not_to_send_clear_the_outbox_entry(box, tmp_path):
    app = _app()
    cam = app.cameras[0]
    start = int(T0 - 200)
    _capture(box, tmp_path, start, etype="motion")
    daemon._skip_sd_within_cooldown(cam, {"start_time": start}, "person", None, None, None)
    assert box.entries() == []
    # The same passage already alerted: the follow-up is dropped, and so is the entry.
    _capture(box, tmp_path, start, etype="motion")
    state = daemon.MonitorState()
    state.last_event_start[("yard", "motion")] = start
    entry = {"camera": "yard", "etype": "motion", "event": {"start_time": start}}
    assert daemon._sd_followup_blocked(app, cam, state, entry, T0) == "drop"
    assert box.entries() == []


def test_configure_warns_without_a_review_log(tmp_path, caplog):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    try:
        with caplog.at_level("WARNING"):
            daemon.configure_outbox(_app({"enabled": True}), env=env)
        assert any("TAPO_REVIEW_LOG_DIR is unset" in r.message for r in caplog.records)
        caplog.clear()
        with caplog.at_level("WARNING"):
            daemon.configure_outbox(_app({"enabled": True}),
                                    env={**env, "TAPO_REVIEW_LOG_DIR": str(tmp_path / "r")})
        assert not caplog.records
    finally:
        daemon._outbox = None


def test_an_unreadable_sidecar_is_kept_a_corrupt_one_is_dropped(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, 1000)
    _capture(box, tmp_path, 2000)
    _capture(box, tmp_path, 3000)
    unreadable = os.path.join(box.dir, "yard-1000.json")
    os.chmod(unreadable, 0)
    with open(os.path.join(box.dir, "yard-2000.json"), "w") as fh:
        fh.write("")
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("running as root: chmod does not stop reads")
        assert [e.incident for e in box.entries()] == ["yard-3000"]
        assert os.path.exists(unreadable)
        assert not os.path.exists(os.path.join(box.dir, "yard-2000.json"))
    finally:
        os.chmod(unreadable, 0o600)


def test_sweep_removes_stale_temp_files_and_orphan_frames(tmp_path):
    box = _box(tmp_path)
    _capture(box, tmp_path, 1000)
    old = [os.path.join(box.dir, n) for n in ("x.json.tmp", "orphan.jpg")]
    fresh = os.path.join(box.dir, "fresh.jpg")
    for path in old + [fresh]:
        open(path, "wb").close()
    for path in old:
        os.utime(path, (T0 - 2 * outbox.STALE_AFTER,) * 2)
    os.utime(fresh, (T0 - 10,) * 2)
    box.sweep(T0)
    assert not any(os.path.exists(p) for p in old)
    assert os.path.exists(fresh)
    assert [e.incident for e in box.entries()] == ["yard-1000"]


def test_writes_are_fsynced(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(outbox.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    _capture(_box(tmp_path), tmp_path, 1000)
    assert len(synced) == 2                        # frame and sidecar
