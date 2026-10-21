import importlib.util
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "probe_last_alarm", Path(__file__).parent.parent / "tools" / "probe_last_alarm.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

SECRET_HOST = "10.9.8.7"


class FakeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def executeFunction(self, name, params):
        self.calls.append((name, params))
        return self.answer


def _run(monkeypatch, answer, argv, tmp_path, sleeps=None):
    client = FakeClient(answer)
    cfg = SimpleNamespace(name="cam")
    monkeypatch.setattr(probe, "make_client", lambda c: client)
    import tapo_monitor.config as config
    monkeypatch.setattr(config, "load_config", lambda p: SimpleNamespace(cameras=[cfg]))
    code = probe.main(["--camera", "cam", *argv],
                      sleep=(sleeps.append if sleeps is not None else (lambda s: None)))
    return code, client


def good():
    return {"system": {"last_alarm_info": {
        "last_alarm_time": str(int(time.time()) - 5), "last_alarm_type": "Person",
        "mac": "AA:BB:CC:DD:EE:FF", "ip": SECRET_HOST, "serial": "ABC123DEF4567890"}}}


def test_flag_required(monkeypatch, tmp_path, capsys):
    code, client = _run(monkeypatch, good(), [], tmp_path)
    assert code == 2 and client.calls == []


def test_parsed_redacted_and_only_alarm_call(monkeypatch, tmp_path, capsys):
    out = tmp_path / "o.json"
    code, client = _run(monkeypatch, good(),
                        ["--i-stopped-the-daemon", "--count", "2", "--out", str(out)], tmp_path)
    text = capsys.readouterr().out
    assert code == 0 and "parsed 1 event(s)" in text
    assert all(c[0] == "getLastAlarmInfo" for c in client.calls) and len(client.calls) == 2
    for leak in ("AA:BB", SECRET_HOST, "ABC123DEF4567890"):
        assert leak not in text and leak not in out.read_text()
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert json.loads(out.read_text())


def test_unrecognised_shape(monkeypatch, tmp_path, capsys):
    code, _ = _run(monkeypatch, {"weird": 1}, ["--i-stopped-the-daemon", "--count", "1"],
                   tmp_path)
    assert code == 1
    assert "shape not recognised: send this redacted answer" in capsys.readouterr().out


def test_spacing_enforced_and_used(monkeypatch, tmp_path, capsys):
    code, _ = _run(monkeypatch, good(),
                   ["--i-stopped-the-daemon", "--interval", "1"], tmp_path)
    assert code == 2
    sleeps = []
    code, _ = _run(monkeypatch, good(),
                   ["--i-stopped-the-daemon", "--count", "3", "--interval", "2"],
                   tmp_path, sleeps)
    assert code == 0 and sleeps == [2.0, 2.0]


def test_connect_error_prints_class_only(monkeypatch, tmp_path, capsys):
    def boom(cfg):
        raise RuntimeError(f"cannot reach {SECRET_HOST} detail=LEAK-MARKER")
    monkeypatch.setattr(probe, "make_client", boom)
    import tapo_monitor.config as config
    monkeypatch.setattr(config, "load_config",
                        lambda p: SimpleNamespace(cameras=[SimpleNamespace(name="cam")]))
    code = probe.main(["--camera", "cam", "--i-stopped-the-daemon"])
    cap = capsys.readouterr()
    assert code == 2 and "RuntimeError" in cap.err
    assert "LEAK-MARKER" not in cap.err + cap.out and SECRET_HOST not in cap.err + cap.out
