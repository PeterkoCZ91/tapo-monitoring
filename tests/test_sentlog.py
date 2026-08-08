import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import scorer, sentlog

# ── archive_sent: writing ────────────────────────────────────────────────────

def test_archive_sent_writes_jpeg_with_exact_bytes(tmp_path):
    path = sentlog.archive_sent(str(tmp_path), b"\xff\xd8JPEG", "\U0001f464 23:14",
                                now=1785200000.25, delivered=True)
    assert path is not None
    saved = tmp_path / os.path.basename(path)
    assert saved.exists()
    assert saved.read_bytes() == b"\xff\xd8JPEG"
    assert saved.suffix == ".jpg"


def test_archive_sent_appends_index_line_with_caption_and_delivered(tmp_path):
    sentlog.archive_sent(str(tmp_path), b"x", 'line1\n"quoted"', now=1785200000.0,
                         delivered=False)
    index = tmp_path / "index.jsonl"
    assert index.exists()
    rec = json.loads(index.read_text().strip())
    assert rec["caption"] == 'line1\n"quoted"'
    assert rec["delivered"] is False
    assert rec["file"].endswith(".jpg")
    assert rec["ts"] == 1785200000.0


def test_two_sends_in_the_same_second_get_distinct_files(tmp_path):
    p1 = sentlog.archive_sent(str(tmp_path), b"a", "c", now=1785200000.10)
    p2 = sentlog.archive_sent(str(tmp_path), b"b", "c", now=1785200000.20)
    assert p1 != p2
    assert (tmp_path / os.path.basename(p1)).read_bytes() == b"a"
    assert (tmp_path / os.path.basename(p2)).read_bytes() == b"b"


# ── prune / rotation ─────────────────────────────────────────────────────────

def test_prune_old_removes_files_older_than_retention(tmp_path):
    old = tmp_path / "20200101-000000-000000.jpg"
    new = tmp_path / "20200103-000000-000000.jpg"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    now = time.time()
    os.utime(old, (now - 3 * 86400, now - 3 * 86400))
    os.utime(new, (now - 3600, now - 3600))
    removed = sentlog.prune_old(str(tmp_path), now=now, retention_days=2.0)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_archive_sent_prunes_stale_files_on_write(tmp_path):
    stale = tmp_path / "20200101-000000-000000.jpg"
    stale.write_bytes(b"o")
    long_ago = time.time() - 10 * 86400
    os.utime(stale, (long_ago, long_ago))
    sentlog.archive_sent(str(tmp_path), b"new", "c", now=time.time(), retention_days=2.0)
    assert not stale.exists()


# ── never break the send path ────────────────────────────────────────────────

def test_archive_sent_returns_none_and_does_not_raise_on_unwritable_dir(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_bytes(b"x")
    # A path *under a regular file* cannot be created as a directory.
    result = sentlog.archive_sent(str(blocker / "nope"), b"x", "c", now=1785200000.0)
    assert result is None


# ── env gating ───────────────────────────────────────────────────────────────

def test_archive_dir_from_env_reads_env():
    assert sentlog.archive_dir_from_env({}) is None
    assert sentlog.archive_dir_from_env({sentlog.ENV_DIR: "/x/y"}) == "/x/y"
    assert sentlog.archive_dir_from_env({sentlog.ENV_DIR: "  "}) is None


def test_retention_days_from_env_default_and_override():
    assert sentlog.retention_days_from_env({}) == sentlog.DEFAULT_RETENTION_DAYS
    assert sentlog.retention_days_from_env({sentlog.ENV_RETENTION: "5"}) == 5.0
    # Garbage falls back to the default rather than crashing the send path.
    assert sentlog.retention_days_from_env(
        {sentlog.ENV_RETENTION: "abc"}) == sentlog.DEFAULT_RETENTION_DAYS


def test_archive_if_configured_is_noop_when_dir_unset():
    assert sentlog.archive_if_configured(b"x", "c", env={}) is None


def test_archive_if_configured_writes_when_dir_set(tmp_path):
    env = {sentlog.ENV_DIR: str(tmp_path)}
    path = sentlog.archive_if_configured(b"x", "c", delivered=True, now=1785200000.0, env=env)
    assert path is not None
    assert (tmp_path / os.path.basename(path)).exists()


# ── review log: suppressed (held) frames for visual FP verification ───────────

def test_archive_review_noop_when_dir_unset(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8IMG")
    assert sentlog.archive_review_if_configured(str(img), {"camera": "a"}, env={}) is None


def test_archive_review_writes_frame_and_index(tmp_path):
    src = tmp_path / "f.jpg"
    src.write_bytes(b"\xff\xd8IMG")
    out = tmp_path / "review"
    env = {sentlog.ENV_REVIEW_DIR: str(out)}
    meta = {"camera": "yard", "verdict": "hold", "person": 0.42, "animal": 0.01,
            "etype": "motion"}
    path = sentlog.archive_review_if_configured(str(src), meta, now=1785200000.5, env=env)
    assert path is not None
    saved = out / os.path.basename(path)
    assert saved.read_bytes() == b"\xff\xd8IMG"
    assert "yard" in saved.name and "hold" in saved.name
    rec = json.loads((out / "index.jsonl").read_text().strip())
    assert rec["camera"] == "yard" and rec["verdict"] == "hold" and rec["person"] == 0.42


def test_archive_review_best_effort_on_missing_source(tmp_path):
    env = {sentlog.ENV_REVIEW_DIR: str(tmp_path / "review")}
    assert sentlog.archive_review_if_configured("/no/such/frame.jpg", {}, env=env) is None


def test_review_meta_shape_for_plain_and_structured_scores():
    # One shape for both writers (live pass and sampler), from a float or a scorer result.
    assert sentlog.review_meta("a", "hold", "motion", 0.42) == {
        "camera": "a", "verdict": "hold", "etype": "motion", "person": 0.42, "animal": 0.0}

    class Score(float):
        person = 0.42
        animal = 0.31

    assert sentlog.review_meta("a", "hold", "motion", Score(0.42))["animal"] == 0.31


def test_archive_review_default_retention_is_weekly():
    assert sentlog.DEFAULT_REVIEW_RETENTION_DAYS == 7.0


# ── camera and scores in the index ───────────────────────────────────────────

def test_archive_sent_records_camera_and_scores(tmp_path):
    # A host running two cameras could not tell from the index which one sent a frame.
    sentlog.archive_sent(str(tmp_path), b"jpeg", "cap", now=1785200000.0,
                         camera="yard", score=scorer.SubjectScore(0.87, 0.12))
    rec = json.loads((tmp_path / "index.jsonl").read_text().strip())
    assert rec["camera"] == "yard"
    assert rec["person"] == 0.87
    assert rec["animal"] == 0.12


def test_archive_sent_omits_absent_camera_and_score(tmp_path):
    sentlog.archive_sent(str(tmp_path), b"jpeg", "cap", now=1785200000.0)
    rec = json.loads((tmp_path / "index.jsonl").read_text().strip())
    assert set(rec) == {"ts", "file", "caption", "delivered"}


def test_archive_sent_omits_scores_for_a_plain_float(tmp_path):
    sentlog.archive_sent(str(tmp_path), b"jpeg", "cap", now=1785200000.0,
                         camera="yard", score=0.8)
    rec = json.loads((tmp_path / "index.jsonl").read_text().strip())
    assert rec["camera"] == "yard"
    assert "person" not in rec


def test_prune_old_rotates_an_index_that_outlived_its_frames(tmp_path):
    # prune_old only ever removed .jpg files, so the index grew without bound and kept
    # pointing at frames deleted nights ago.
    index = tmp_path / "index.jsonl"
    index.write_text('{"ts": 1, "file": "a.jpg"}\n' * 5)
    os.utime(index, (0, 0))

    sentlog.prune_old(str(tmp_path), now=1785200000.0, retention_days=2.0)

    assert not index.exists()


def test_prune_old_keeps_a_fresh_index(tmp_path):
    index = tmp_path / "index.jsonl"
    index.write_text('{"ts": 1}\n')
    sentlog.prune_old(str(tmp_path), now=1785200000.0, retention_days=2.0)
    assert index.exists()
