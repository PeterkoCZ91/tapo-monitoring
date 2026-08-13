import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import notify, scorer
from tests.conftest import FakeResponse as _FakeResp

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
