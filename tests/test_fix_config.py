import logging
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import config as cfg
from tapo_monitor import ledger, mqtt, statusd


def _minimal(**extra):
    return {"cameras": [{"name": "front", "host": "192.0.2.50"}], **extra}


# -- quoted YAML booleans must not coerce to True ---------------------------------------

@pytest.mark.parametrize("patch", [
    lambda d: d["cameras"][0].update(telegram_alerts="false"),
    lambda d: d["cameras"][0].update(night_only="no"),
    lambda d: d.update(alerts={"event_restart_enabled": "false"}),
    lambda d: d.update(reliability={"auto_fix": "false"}),
    lambda d: d.update(reliability={"enabled": "no"}),
    lambda d: d.update(observability={"digital_twin": "false"}),
    lambda d: d.update(observability={"drift_alerts": "no"}),
    lambda d: d.update(observability={"ledger": "false"}),
    lambda d: d["cameras"][0].update(pan_limit={"enabled": "false"}),
    lambda d: d["cameras"][0].update(pan_limit={"enabled": True, "tilt": "false"}),
])
def test_quoted_bool_strings_are_rejected(patch):
    data = _minimal()
    patch(data)
    with pytest.raises(cfg.ConfigError, match="true or false"):
        cfg.load_config_from_dict(data)


def test_real_bools_still_load():
    data = _minimal(alerts={"event_restart_enabled": False},
                    reliability={"auto_fix": False},
                    observability={"ledger": True})
    app = cfg.load_config_from_dict(data)
    assert app.alerts.event_restart_enabled is False
    assert app.reliability.auto_fix is False
    assert app.observability.ledger is True


# -- numeric ranges and section shapes --------------------------------------------------

@pytest.mark.parametrize("section,key,value", [
    ("loop", "event_interval", 0),
    ("loop", "control_interval", -5),
    ("loop", "event_interval", "abc"),
    ("alerts", "cooldown", -1),
    ("alerts", "outage_threshold", -1),
    ("alerts", "cooldown", "soon"),
    ("alerts", "outage_threshold", True),
    ("alerts", "stall_threshold", [1]),
])
def test_bad_numbers_raise_config_error(section, key, value):
    with pytest.raises(cfg.ConfigError):
        cfg.load_config_from_dict(_minimal(**{section: {key: value}}))


@pytest.mark.parametrize("section", ["alerts", "loop", "observability"])
def test_non_mapping_sections_raise_config_error(section):
    with pytest.raises(cfg.ConfigError, match=section):
        cfg.load_config_from_dict(_minimal(**{section: [1, 2]}))


def test_valid_numbers_and_zero_cooldown_load():
    app = cfg.load_config_from_dict(
        _minimal(alerts={"cooldown": 0, "outage_threshold": "30"}, loop={"event_interval": 2}))
    assert (app.alerts.cooldown, app.alerts.outage_threshold) == (0, 30)
    assert app.loop.event_interval == 2


# -- ledger identifiers -----------------------------------------------------------------

def test_ledger_accepts_non_ascii_camera_names(tmp_path):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    events.record_camera_event(camera="Dvůr", event_type="person", event_at=1000.0,
                               observed_at=1000.5)
    assert ledger._safe_identifier("Dvůr", "camera") == "Dvůr"


@pytest.mark.parametrize("bad", ["", "a'b", 'a"b', "a;b", "a\nb", "_x", "a/b", " x", "x" * 129,
                                 "a\x00b"])
def test_ledger_still_rejects_unsafe_identifiers(bad):
    with pytest.raises(ValueError):
        ledger._safe_identifier(bad, "camera")


# -- statusd ----------------------------------------------------------------------------

def test_fingerprint_is_computed_once(monkeypatch):
    import tapo_monitor.cli as cli
    calls = []
    monkeypatch.setattr(cli, "package_fingerprint", lambda *a: calls.append(1) or "abc123")
    monkeypatch.setattr(statusd, "_fingerprint_cache", (None, float("-inf")))
    assert [statusd._cached_fingerprint() for _ in range(5)] == ["abc123"] * 5
    assert len(calls) == 1


def test_server_handlers_have_a_timeout():
    srv = statusd.make_server(lambda: {}, port=0)
    try:
        assert srv.RequestHandlerClass.timeout
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/status"
        assert urllib.request.urlopen(url, timeout=5).status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_non_loopback_status_bind_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=cfg.log.name):
        cfg.load_config_from_dict(_minimal(observability={"status_bind": "0.0.0.0"}))
    assert "not loopback" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=cfg.log.name):
        cfg.load_config_from_dict(_minimal(observability={"status_bind": "127.0.0.1"}))
    assert "not loopback" not in caplog.text


# -- mqtt -------------------------------------------------------------------------------

class _Client:
    def will_set(self, *a, **k): ...
    def username_pw_set(self, *a, **k): ...
    def tls_set(self, *a, **k): ...


def _bridge(tls):
    app = cfg.load_config_from_dict(_minimal(mqtt={
        "host": "broker.invalid", "user_env": "U", "password_env": "P", "tls": tls}))
    return mqtt.Bridge(app.mqtt, ["front"], client_factory=lambda _cid: _Client(),
                       env={"U": "user", "P": "pw"}, version="9.9.9")


def test_plaintext_credentials_warn_once(caplog):
    bridge = _bridge(tls=False)
    with caplog.at_level(logging.WARNING, logger=mqtt.log.name):
        bridge._make_client()
        bridge._make_client()
    assert caplog.text.count("clear text") == 1


def test_tls_credentials_do_not_warn(caplog):
    bridge = _bridge(tls=True)
    with caplog.at_level(logging.WARNING, logger=mqtt.log.name):
        bridge._make_client()
    assert "clear text" not in caplog.text
