import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import notify, scorer
from tests.conftest import FakeResponse as _FakeResp


def test_operational_destination_does_not_change_detection_destination(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_text", lambda *args: calls.append(args) or True)
    monkeypatch.setenv("TAPO_OPS_TELEGRAM_TOKEN", "ops-bot")
    monkeypatch.setenv("TAPO_OPS_TELEGRAM_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TAPO_OPS_LABEL", "Site <A>")
    assert notify.send_operational("camera-bot", "camera-chat", "offline")
    assert calls == [("ops-bot", "ops-chat", "[Site &lt;A&gt;] offline")]
    assert notify.send_text("camera-bot", "camera-chat", "digest")
    assert calls[-1] == ("camera-bot", "camera-chat", "digest")


def test_partial_operational_destination_fails_without_crossing_chats(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_text", lambda *args: calls.append(args) or True)
    monkeypatch.setenv("TAPO_OPS_TELEGRAM_TOKEN", "ops-bot")
    monkeypatch.delenv("TAPO_OPS_TELEGRAM_CHAT_ID", raising=False)
    assert not notify.send_operational("camera-bot", "camera-chat", "offline")
    assert calls == []


def test_operational_destination_defaults_to_existing_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_text", lambda *args: calls.append(args) or True)
    for key in ("TAPO_OPS_TELEGRAM_TOKEN", "TAPO_OPS_TELEGRAM_CHAT_ID", "TAPO_OPS_LABEL"):
        monkeypatch.delenv(key, raising=False)
    assert notify.send_operational("camera-bot", "camera-chat", "offline")
    assert calls == [("camera-bot", "camera-chat", "offline")]

# ── is_empty_scene ───────────────────────────────────────────────────────────

def test_empty_scene_detected():
    assert notify.is_empty_scene("empty scene") is True

def test_empty_scene_with_real_description():
    assert notify.is_empty_scene("a person in a dark jacket walking left") is False

def test_empty_scene_none_or_blank_is_empty():
    # No description = vision model returned nothing (timeout/failure); we can't confirm
    # a subject, so treat it as empty rather than sending a content-free alert.
    assert notify.is_empty_scene(None) is True
    assert notify.is_empty_scene("") is True
    assert notify.is_empty_scene("   ") is True


# ── build_caption ────────────────────────────────────────────────────────────

def test_caption_minimal():
    assert notify.build_caption("👤", "23:14") == "👤 23:14"

def test_caption_with_description():
    cap = notify.build_caption("👤", "23:14", description="person walking")
    assert '"person walking"' in cap
    assert cap.startswith("👤 23:14")

def test_caption_with_count_and_since():
    cap = notify.build_caption("👤", "23:14", description="x", count=3, minutes_since_last=12)
    assert "detection #3 today" in cap
    assert "last 12 min ago" in cap

def test_caption_with_detail():
    cap = notify.build_caption("👤", "23:14", detail="Jana")
    assert cap.startswith("👤 Jana 23:14")

def test_caption_notes_the_light_when_on():
    cap = notify.build_caption("👤", "23:14", light=True)
    assert cap.startswith("👤🔦 23:14")

def test_caption_says_nothing_when_light_is_off():
    cap = notify.build_caption("👤", "23:14", light=False)
    assert "🔦" not in cap

def test_caption_says_nothing_when_light_is_unknown():
    cap = notify.build_caption("👤", "23:14", light=None)
    assert "🔦" not in cap


# ── outage_alert_due ─────────────────────────────────────────────────────────

def test_outage_no_failure():
    assert notify.outage_alert_due(None, 1000.0, False, threshold=900) is False

def test_outage_below_threshold():
    assert notify.outage_alert_due(1000.0, 1100.0, False, threshold=900) is False

def test_outage_above_threshold():
    assert notify.outage_alert_due(1000.0, 1901.0, False, threshold=900) is True

def test_outage_already_alerted_suppressed():
    assert notify.outage_alert_due(1000.0, 5000.0, True, threshold=900) is False


# ── format_duration ──────────────────────────────────────────────────────────

def test_format_duration_compact_two_units():
    assert notify.format_duration(0) == "0s"
    assert notify.format_duration(65) == "1m 5s"
    assert notify.format_duration(3 * 3600 + 61) == "3h 1m"
    assert notify.format_duration(2 * 86400 + 3 * 3600 + 60) == "2d 3h"


# ── should_send_alert (detection cooldown) ───────────────────────────────────

def test_alert_first_ever_allowed():
    assert notify.should_send_alert(None, 1000.0, cooldown=120) is True

def test_alert_within_cooldown_suppressed():
    assert notify.should_send_alert(1000.0, 1050.0, cooldown=120) is False

def test_alert_at_cooldown_boundary_allowed():
    assert notify.should_send_alert(1000.0, 1120.0, cooldown=120) is True

def test_alert_after_cooldown_allowed():
    assert notify.should_send_alert(1000.0, 2000.0, cooldown=120) is True


# ── send_photo archiving (opt-in sent-frame log) ─────────────────────────────

def test_send_photo_archives_sent_frame_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b'{"ok":true}'))
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")

    assert notify.send_photo("tok", "chat", str(img), "\U0001f464 23:14") is True

    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8IMG"


def test_send_photo_archives_uncropped_frame_when_given(monkeypatch, tmp_path):
    # crop_to_subject cameras send a zoom; the archive is only useful for judging a false
    # positive if it keeps the whole scene, so the pre-crop frame wins over the sent one.
    posted = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (posted.append(req.data), _FakeResp(b'{"ok":true}'))[1])
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"\xff\xd8CROP")
    full = tmp_path / "full.jpg"
    full.write_bytes(b"\xff\xd8FULL")

    assert notify.send_photo("tok", "chat", str(crop), "c", archive_path=str(full)) is True

    assert b"CROP" in posted[0] and b"FULL" not in posted[0]
    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8FULL"


def test_send_photo_archives_sent_frame_when_uncropped_unreadable(monkeypatch, tmp_path):
    # A vanished pre-crop temp file must degrade to archiving the crop, never to no archive.
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b'{"ok":true}'))
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"\xff\xd8CROP")

    assert notify.send_photo("tok", "chat", str(crop), "c",
                             archive_path=str(tmp_path / "gone.jpg")) is True

    saved = list(archive.glob("*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\xff\xd8CROP"


def test_send_photo_archive_false_skips_sent_log(monkeypatch, tmp_path):
    # Non-alert traffic (e.g. the review digest re-sending suppressed frames) must not
    # pollute the sent log, which is read as the record of delivered alerts.
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b'{"ok":true}'))
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")

    assert notify.send_photo("tok", "chat", str(img), "digest", archive=False) is True

    assert not archive.exists()


def test_send_photo_does_not_archive_when_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b'{"ok":true}'))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"x")

    assert notify.send_photo("tok", "chat", str(img), "c") is True


def test_send_photo_retries_once_on_failure(monkeypatch, tmp_path):
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("boom")
        return _FakeResp(b'{"ok":true}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    assert notify.send_photo("tok", "chat", str(img), "c") is True
    assert len(calls) == 2       # retried once, then succeeded


# ── animal in the caption ────────────────────────────────────────────────────

def test_caption_flags_a_dog_walker_who_scores_high_on_both():
    # Measured on a real frame: person 0.91, animal 0.80. Comparing the two scores would
    # miss this, which is exactly the case the paw exists for.
    cap = notify.build_caption("👤", "23:14", score=scorer.SubjectScore(0.91, 0.80))
    assert cap.startswith("👤🐾 23:14")


def test_caption_flags_an_animal_on_its_own():
    cap = notify.build_caption("👁", "23:14", score=scorer.SubjectScore(0.10, 0.88))
    assert cap.startswith("👁🐾 23:14")


def test_caption_leaves_a_person_alone():
    # Real negatives sit at 0.00-0.13 animal; a lone figure must stay a bare person.
    assert notify.build_caption(
        "👤", "23:14", score=scorer.SubjectScore(0.91, 0.13)) == "👤 23:14"


def test_caption_ignores_a_marginal_animal_score():
    # 0.47 was measured on a frame with no animal in it at all.
    assert notify.build_caption(
        "👤", "23:14", score=scorer.SubjectScore(0.88, 0.47)) == "👤 23:14"


def test_caption_ignores_a_plain_float_score():
    assert notify.build_caption("👁", "23:14", score=0.8) == "👁 23:14"


def test_caption_without_score_is_unchanged():
    assert notify.build_caption("👤", "23:14") == "👤 23:14"


def test_caption_keeps_detail_and_description_with_an_animal():
    cap = notify.build_caption("👤", "23:14", description="a dog on a lead",
                               detail="Jana", score=scorer.SubjectScore(0.3, 0.9))
    assert cap.startswith("👤🐾 Jana 23:14")
    assert '"a dog on a lead"' in cap


# ── person count and lens ────────────────────────────────────────────────────

def test_caption_names_two_or_more_people():
    cap = notify.build_caption("👤", "23:14", description="two walkers",
                               score=scorer.SubjectScore(0.9, 0.0, persons=2))
    assert cap == '👤 23:14 · 2 people\n"two walkers"'


def test_caption_says_nothing_about_one_person_or_an_unknown_count():
    for persons in (None, 0, 1):
        assert notify.build_caption(
            "👤", "23:14", score=scorer.SubjectScore(0.9, 0.0, persons=persons)) == "👤 23:14"


def test_caption_names_the_lens_after_the_count():
    cap = notify.build_caption("👤", "23:14", detail="Jana", lens="pan/tilt",
                               score=scorer.SubjectScore(0.9, 0.9, persons=3), light=True)
    assert cap == "👤🐾🔦 Jana 23:14 · 3 people · pan/tilt lens"
    assert notify.build_caption("👤", "23:14", lens=None) == "👤 23:14"


def test_caption_names_frame_time_and_unverified_marker():
    text = notify.build_caption("👁", "T", frame_time="10:11:12", unverified=True)
    assert text.splitlines()[0] == "👁 T · frame 10:11:12 (unverified)"
    assert notify.build_caption("👁", "T") == "👁 T"


# ── delivered message id (thread-local) ──────────────────────────────────────

def _ok_with_id(message_id):
    return _FakeResp(notify.json.dumps(
        {"ok": True, "result": {"message_id": message_id, "chat": {"id": 1}}},
        separators=(",", ":")).encode())


def test_last_message_id_captured_from_send_photo(monkeypatch, tmp_path):
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _ok_with_id(4242))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    notify.clear_last_message_id()

    assert notify.send_photo("tok", "chat", str(img), "c") is True
    assert notify.last_message_id() == 4242

    notify.clear_last_message_id()
    assert notify.last_message_id() is None


def test_last_message_id_captured_from_album_first_element(monkeypatch, tmp_path):
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    body = b'{"ok":true,"result":[{"message_id":71},{"message_id":72}]}'
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(body))
    paths = []
    for i in range(2):
        p = tmp_path / f"s{i}.jpg"
        p.write_bytes(b"\xff\xd8" + bytes([i]))
        paths.append(str(p))
    notify.clear_last_message_id()

    assert notify.send_album("tok", "chat", paths, "c") is True
    assert notify.last_message_id() == 71


def test_last_message_id_is_per_thread(monkeypatch, tmp_path):
    # Each camera runs on its own thread; one camera's send must never hand its id to
    # another camera's clip.
    import threading
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _ok_with_id(9))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    notify.clear_last_message_id()
    seen = []

    def other():
        notify.send_photo("tok", "chat", str(img), "c")
        seen.append(notify.last_message_id())

    t = threading.Thread(target=other)
    t.start()
    t.join()

    assert seen == [9]
    assert notify.last_message_id() is None


def test_send_photo_record_id_false_keeps_previous_id(monkeypatch, tmp_path):
    # The zoom photo follows the main alert; the clip must reply to the main one.
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    ids = iter([100, 101])
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _ok_with_id(next(ids)))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    notify.clear_last_message_id()

    assert notify.send_photo("tok", "chat", str(img), "main") is True
    assert notify.send_photo("tok", "chat", str(img), "zoom", archive=False,
                             record_id=False) is True
    assert notify.last_message_id() == 100


def test_unparseable_ok_body_still_succeeds_without_id(monkeypatch, tmp_path):
    monkeypatch.delenv("TAPO_SENT_LOG_DIR", raising=False)
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b'{"ok":true, garbage'))
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    notify.clear_last_message_id()

    assert notify.send_photo("tok", "chat", str(img), "c") is True
    assert notify.last_message_id() is None


# ── send_video (alert clip) ──────────────────────────────────────────────────

def _video(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)))
    return clip


def test_send_video_posts_reply_with_streaming_and_bytes(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (posted.append(req), _FakeResp())[1])
    clip = _video(tmp_path)

    assert notify.send_video("tok", "chat", str(clip), "🎬 clip", reply_to=4242,
                             width=1280, height=720, duration=30) is True

    req = posted[0]
    assert req.full_url.endswith("/bottok/sendVideo")
    body = req.data
    assert clip.read_bytes() in body
    assert b'name="video"; filename="clip.mp4"' in body
    assert b"Content-Type: video/mp4" in body
    assert b'name="supports_streaming"\r\n\r\ntrue\r\n' in body
    assert b'name="width"\r\n\r\n1280\r\n' in body
    assert b'name="height"\r\n\r\n720\r\n' in body
    assert b'name="duration"\r\n\r\n30\r\n' in body
    assert "🎬 clip".encode() in body
    marker = b'name="reply_parameters"\r\n\r\n'
    start = body.index(marker) + len(marker)
    params = notify.json.loads(body[start:body.index(b"\r\n", start)])
    assert params == {"message_id": 4242, "allow_sending_without_reply": True}
    boundary = req.get_header("Content-type").split("boundary=")[1]
    assert boundary.encode() not in clip.read_bytes()


def test_send_video_without_reply_omits_reply_parameters(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (posted.append(req), _FakeResp())[1])
    clip = _video(tmp_path)

    assert notify.send_video("tok", "chat", str(clip), "c") is True
    assert b"reply_parameters" not in posted[0].data
    assert b'name="width"' not in posted[0].data


def test_send_video_retries_once_on_failure(monkeypatch, tmp_path):
    calls = []

    def flaky(req, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise OSError("boom")
        return _FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    assert notify.send_video("tok", "chat", str(_video(tmp_path)), "c") is True
    assert calls == [120, 120]


def test_send_video_gives_up_after_second_failure(monkeypatch, tmp_path):
    calls = []

    def dead(req, timeout=None):
        calls.append(1)
        raise OSError("boom")

    monkeypatch.setattr(notify.urllib.request, "urlopen", dead)
    monkeypatch.setattr(notify.time, "sleep", lambda *_: None)
    assert notify.send_video("tok", "chat", str(_video(tmp_path)), "c") is False
    assert len(calls) == 2


def test_send_video_missing_file_returns_false(monkeypatch, tmp_path):
    def boom(req, timeout=None):
        raise AssertionError("must not post")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send_video("tok", "chat", str(tmp_path / "gone.mp4"), "c") is False


def test_send_video_does_not_touch_the_sent_log(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp())
    archive = tmp_path / "sent"
    monkeypatch.setenv("TAPO_SENT_LOG_DIR", str(archive))
    assert notify.send_video("tok", "chat", str(_video(tmp_path)), "c") is True
    assert not archive.exists()


def test_send_video_does_not_change_last_message_id(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: _ok_with_id(555))
    notify.clear_last_message_id()
    assert notify.send_video("tok", "chat", str(_video(tmp_path)), "c") is True
    assert notify.last_message_id() is None
