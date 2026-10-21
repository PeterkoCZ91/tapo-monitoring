import hashlib
import os

import tapo_monitor
from tapo_monitor import cli, report

SECRET_HOST = "198.51.100.77"


def _old_fingerprint(package_dir):
    digest = hashlib.sha256()
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package_dir, name), "rb") as module_file:
            digest.update(name.encode())
            digest.update(module_file.read())
    return digest.hexdigest()[:12]


def test_fingerprint_is_unchanged_for_the_flat_package():
    package_dir = os.path.dirname(tapo_monitor.__file__)
    assert cli.package_fingerprint() == _old_fingerprint(package_dir)


def test_fingerprint_covers_subpackages_and_ignores_pycache(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    before = cli.package_fingerprint(str(tmp_path))
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.py").write_text("junk")
    assert cli.package_fingerprint(str(tmp_path)) == before
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y = 2\n")
    with_sub = cli.package_fingerprint(str(tmp_path))
    assert with_sub != before
    (sub / "b.py").write_text("y = 3\n")
    assert cli.package_fingerprint(str(tmp_path)) != with_sub


def test_selfcheck_config_failure_prints_class_only(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras: []\n", encoding="utf-8")

    def boom(path):
        raise ValueError(f"cannot reach {SECRET_HOST}")

    monkeypatch.setattr(cli, "load_config", boom, raising=False)
    import tapo_monitor.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", boom)
    cli.main(["selfcheck", str(config_path)])
    out = capsys.readouterr().out
    assert "config: FAILED (ValueError)" in out
    assert SECRET_HOST not in out


def test_probe_failure_prints_class_only(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: front\n    host: 203.0.113.10\n",
                           encoding="utf-8")

    def boom(cam, night):
        raise ConnectionError(f"http://{SECRET_HOST}/stok=abc")

    monkeypatch.setattr(cli, "_probe_camera", boom)
    cli.main(["probe", str(config_path)])
    err = capsys.readouterr().err
    assert "front: probe failed: ConnectionError" in err
    assert SECRET_HOST not in err


def _face_setup(tmp_path, monkeypatch, events):
    from tapo_monitor import camera as camera_mod

    config_path = tmp_path / "cameras.yaml"
    config_path.write_text("cameras:\n  - name: lab\n    host: 203.0.113.10\n",
                           encoding="utf-8")
    closed = []

    class Client:
        def getEvents(self, startTime=None, endTime=None):
            return events()

    client = Client()
    monkeypatch.setattr(camera_mod, "connect", lambda factory, retries=1: (client, None))
    monkeypatch.setattr(camera_mod, "close_client", lambda c: closed.append(c))
    return config_path, closed, client


def test_learn_face_ignores_events_from_before_the_command(tmp_path, capsys, monkeypatch):
    import time

    config_path, closed, client = _face_setup(
        tmp_path, monkeypatch,
        lambda: [{"start_time": time.time() - 30, "event_info": [{"face_id": 111}]}])
    rc = cli.main(["learn-face", "alice", "--config", str(config_path), "--timeout", "1"])
    assert rc == 1
    assert "FACE_ID_NAMES" not in capsys.readouterr().out
    assert closed == [client]


def test_learn_face_accepts_a_fresh_event_and_closes_client(tmp_path, capsys, monkeypatch):
    import time

    config_path, closed, client = _face_setup(
        tmp_path, monkeypatch,
        lambda: [{"start_time": time.time() + 1, "event_info": [{"face_id": 222}]}])
    assert cli.main(["learn-face", "alice", "--config", str(config_path)]) == 0
    assert "FACE_ID_NAMES=222:alice" in capsys.readouterr().out
    assert closed == [client]


def test_self_check_scans_short_literals_as_whole_tokens():
    assert report.self_check('{"user": "bob"}', literals=["bob"])
    assert not report.self_check('{"k": "bobcat"}', literals=["bob"])
    assert not report.self_check('{"k": "x"}', literals=["x"])
