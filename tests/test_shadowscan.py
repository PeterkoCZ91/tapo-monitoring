import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import shadowscan


def _local(y, mo, d, h=0, mi=0, s=0):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def test_resolve_date_defaults_to_yesterday():
    now = _local(2026, 8, 13, 3, 0)
    assert shadowscan.resolve_date(None, now=now) == "2026-08-12"
    assert shadowscan.resolve_date("yesterday", now=now) == "2026-08-12"
    assert shadowscan.resolve_date("2026-08-01", now=now) == "2026-08-01"


def test_resolve_date_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        shadowscan.resolve_date("last tuesday")


def test_segments_for_date_sorted_and_parsed(tmp_path):
    host = "192.0.2.10"
    hour_dir = tmp_path / host / "2026-08-12" / "07"
    hour_dir.mkdir(parents=True)
    (hour_dir / "zaznam_20260812T071500.mkv").write_bytes(b"x")
    (hour_dir / "zaznam_20260812T070000.mkv").write_bytes(b"x")
    (hour_dir / "notes.txt").write_bytes(b"x")
    segs = shadowscan.segments_for_date(str(tmp_path), host, "2026-08-12")
    assert [os.path.basename(p) for p, _ in segs] == [
        "zaznam_20260812T070000.mkv", "zaznam_20260812T071500.mkv"]
    assert segs[0][1] == _local(2026, 8, 12, 7, 0)


def test_segments_for_date_empty_tree(tmp_path):
    assert shadowscan.segments_for_date(str(tmp_path), "192.0.2.10", "2026-08-12") == []
