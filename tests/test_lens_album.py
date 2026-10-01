"""Opt-in lens album: both lens frames in one Telegram message when their subjects differ."""

import copy
import json
import os
from pathlib import Path

import pytest

from tapo_monitor import config as cfg_mod
from tapo_monitor import daemon, notify, scorer, snapshot

from .test_c545d import PERSON, _Cam, _frame, c545d_camera

SCORER = {"url": "http://127.0.0.1:1/score"}


def _cam(**extra):
    return cfg_mod.load_camera_config(c545d_camera(
        rtsp_stream="stream2", lens_pick_stream="stream7", scorer=SCORER, **extra))


def _score(person, persons):
    return scorer.SubjectScore(person, 0.0, persons=persons)


# ── config ──────────────────────────────────────────────────────────────────

def test_lens_album_is_off_by_default():
    assert _cam().lens_album is False


def test_lens_album_needs_lens_pick_stream():
    with pytest.raises(cfg_mod.ConfigError, match="lens_pick_stream"):
        cfg_mod.load_camera_config(c545d_camera(lens_album=True, scorer=SCORER))


def test_lens_album_must_be_a_boolean():
    with pytest.raises(cfg_mod.ConfigError, match="lens_album"):
        cfg_mod.load_camera_config(c545d_camera(
            rtsp_stream="stream2", lens_pick_stream="stream7", scorer=SCORER,
            lens_album="yes"))


def test_lens_album_accepted_with_lens_pick():
    assert _cam(lens_album=True).lens_album is True


# ── what "differ" means ─────────────────────────────────────────────────────

def test_subjects_differ_when_person_counts_differ():
    assert daemon.lens_subjects_differ(_score(0.9, 1), _score(0.8, 2)) is True


def test_subjects_do_not_differ_with_equal_counts():
    assert daemon.lens_subjects_differ(_score(0.9, 1), _score(0.8, 1)) is False


def test_unknown_count_never_counts_as_different():
    assert daemon.lens_subjects_differ(_score(0.9, None), _score(0.8, 2)) is False
    assert daemon.lens_subjects_differ(0.9, 0.8) is False


# ── pick_lens_frame ─────────────────────────────────────────────────────────

def _scores(table):
    def score(image):
        return table[os.path.basename(image)]
    score.boxes = {}
    return score


def test_pick_keeps_both_frames_when_album_and_subjects_differ(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, s = daemon.pick_lens_frame(
        _cam(lens_album=True), snapshot.with_lens(wide, "wide"),
        snapshot.with_lens(pt, "pan/tilt"),
        _scores({"wide.jpg": _score(0.9, 1), "pt.jpg": _score(0.8, 2)}))
    assert image == wide and s == 0.9
    assert snapshot.frame_companion(image) == pt
    assert os.path.exists(wide) and os.path.exists(pt)
    assert snapshot.album_lens(image) == "wide + pan/tilt"
    snapshot.safe_unlink(image)
    assert not os.path.exists(wide) and not os.path.exists(pt)


def test_pick_is_unchanged_when_album_is_off(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, _s = daemon.pick_lens_frame(
        _cam(), wide, pt, _scores({"wide.jpg": _score(0.9, 1), "pt.jpg": _score(0.8, 2)}))
    assert snapshot.frame_companion(image) is None
    assert not os.path.exists(pt)


def test_pick_is_unchanged_when_only_one_lens_sees_a_person(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, _s = daemon.pick_lens_frame(
        _cam(lens_album=True), wide, pt,
        _scores({"wide.jpg": _score(0.9, 1), "pt.jpg": _score(0.05, 0)}))
    assert snapshot.frame_companion(image) is None
    assert not os.path.exists(pt)


def test_pick_is_unchanged_when_counts_match(tmp_path):
    wide, pt = _frame(tmp_path, "wide.jpg"), _frame(tmp_path, "pt.jpg")
    image, _s = daemon.pick_lens_frame(
        _cam(lens_album=True), wide, pt,
        _scores({"wide.jpg": _score(0.9, 1), "pt.jpg": _score(0.8, 1)}))
    assert snapshot.frame_companion(image) is None


def test_with_lens_keeps_the_companion(tmp_path):
    f = snapshot.with_companion(snapshot.with_lens(_frame(tmp_path, "a.jpg"), "wide"),
                                _frame(tmp_path, "b.jpg"), "pan/tilt", _score(0.8, 2))
    again = snapshot.with_lens(f, "wide")
    assert snapshot.frame_companion(again) == snapshot.frame_companion(f)


# ── caption ─────────────────────────────────────────────────────────────────

def test_caption_names_both_lenses_in_plural():
    cap = notify.build_caption("👤", "12:00:00", lens="wide + pan/tilt")
    assert cap == "👤 12:00:00 · wide + pan/tilt lenses"


# ── transport ───────────────────────────────────────────────────────────────

class _Resp:
    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _jpg(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_send_album_posts_one_media_group_with_the_caption_on_the_first(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.append(req) or _Resp())
    a, b = _jpg(tmp_path, "a.jpg", b"AAA"), _jpg(tmp_path, "b.jpg", b"BBB")
    assert notify.send_album("tok", "chat", [a, b], "cap") is True
    assert len(seen) == 1 and seen[0].full_url.endswith("/bottok/sendMediaGroup")
    body = seen[0].data
    assert b"AAA" in body and b"BBB" in body
    media = json.loads(body.split(b'name="media"\r\n\r\n')[1].split(b"\r\n--")[0])
    assert [m["media"] for m in media] == ["attach://p0", "attach://p1"]
    assert media[0]["caption"] == "cap" and "caption" not in media[1]


def test_send_album_archives_every_frame_under_one_incident(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    a, b = _jpg(tmp_path, "a.jpg", b"AAA"), _jpg(tmp_path, "b.jpg", b"BBB")
    assert notify.send_album("tok", "chat", [a, b], "cap", camera="front",
                             scores=[_score(0.9, 1), _score(0.8, 2)],
                             incident="front-1", send_path="live") is True
    lines = (archive / "index.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 2
    assert {r["camera"] for r in rows} == {"front"} and {r["path"] for r in rows} == {"live"}
    assert len({r["incident"] for r in rows}) == 1
    assert sorted(p.read_bytes() for p in archive.glob("*.jpg")) == [b"AAA", b"BBB"]


def test_send_album_failure_archives_nothing_and_retries_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(1) or _Resp(b'{"ok":false}'))
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    a, b = _jpg(tmp_path, "a.jpg", b"AAA"), _jpg(tmp_path, "b.jpg", b"BBB")
    assert notify.send_album("tok", "chat", [a, b], "cap") is False
    assert len(calls) == 2 and not archive.exists()


# ── end to end through the live pass ────────────────────────────────────────

def _run(monkeypatch, tmp_path, *, album=True, album_ok=True, counts=(1, 2)):
    app = cfg_mod.load_config_from_dict({"cameras": [c545d_camera(
        rtsp_stream="stream2", lens_pick_stream="stream7", scorer=SCORER,
        lens_album=album)]})
    grabs = {"stream2": _frame(tmp_path, "wide.jpg"), "stream7": _frame(tmp_path, "pt.jpg")}
    monkeypatch.setattr(daemon.snapshot, "capture_rtsp",
                        lambda url, **kw: grabs[url.rsplit("/", 1)[-1]])
    monkeypatch.setattr(daemon, "resolve_rtsp_credentials", lambda _c: ("u", "p"))
    table = {"wide.jpg": _score(0.9, counts[0]), "pt.jpg": _score(0.8, counts[1])}

    def score_for(_cfg):
        def score(image):
            return table[os.path.basename(image)]
        score.boxes = {}
        return score

    monkeypatch.setattr(daemon, "score_for", score_for)
    photos, albums = [], []
    monkeypatch.setattr(daemon.notify, "send_photo",
                        lambda token, chat, image, caption, *a, **k: photos.append(
                            (os.path.basename(image), caption, k.get("incident"))) or True)

    def send_album(token, chat, images, caption, **k):
        albums.append(([os.path.basename(i) for i in images], caption, k.get("incident"),
                       k.get("send_path")))
        return album_ok

    monkeypatch.setattr(daemon.notify, "send_album", send_album)
    daemon.run_monitor_pass(app, {"front": _Cam([copy.deepcopy(PERSON)])},
                            daemon.MonitorState(), now=PERSON["start_time"],
                            secrets={"telegram_token": "t", "telegram_chat": "c",
                                     "groq_key": ""},
                            time_str=lambda _e: "12:00:00")
    return photos, albums, tmp_path


def test_live_pass_sends_one_album_when_subjects_differ(monkeypatch, tmp_path):
    photos, albums, root = _run(monkeypatch, tmp_path)
    assert photos == []
    assert albums == [(["wide.jpg", "pt.jpg"], "👤 12:00:00 · wide + pan/tilt lenses",
                       f"front-{PERSON['start_time']}", "live")]
    assert not (Path(root) / "wide.jpg").exists() and not (Path(root) / "pt.jpg").exists()


def test_live_pass_sends_a_single_photo_when_the_subjects_match(monkeypatch, tmp_path):
    photos, albums, _ = _run(monkeypatch, tmp_path, counts=(1, 1))
    assert albums == [] and [p[0] for p in photos] == ["wide.jpg"]


def test_live_pass_sends_a_single_photo_with_album_off(monkeypatch, tmp_path):
    photos, albums, _ = _run(monkeypatch, tmp_path, album=False)
    assert albums == [] and len(photos) == 1


def test_a_failed_album_falls_back_to_the_single_photo(monkeypatch, tmp_path):
    photos, albums, _ = _run(monkeypatch, tmp_path, album_ok=False)
    assert len(albums) == 1
    assert photos == [("wide.jpg", "👤 12:00:00 · wide lens", f"front-{PERSON['start_time']}")]
