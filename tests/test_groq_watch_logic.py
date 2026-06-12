import os

os.environ.setdefault("TAPO_IP", "192.168.1.1")
os.environ.setdefault("TAPO_EMAIL", "test@example.com")
os.environ.setdefault("TAPO_PASSWORD", "testpass")
os.environ.setdefault("TELEGRAM_TOKEN", "test:token123")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("GROQ_API_KEY", "gsk_testkey")

import groq_watch

# ---- _parse_hhmm ----

def test_parse_hhmm_valid():
    assert groq_watch._parse_hhmm("05:30", (6, 0)) == (5, 30)

def test_parse_hhmm_empty_uses_default():
    assert groq_watch._parse_hhmm("", (5, 30)) == (5, 30)

def test_parse_hhmm_garbage_uses_default():
    assert groq_watch._parse_hhmm("xx:yy", (6, 0)) == (6, 0)

def test_parse_hhmm_out_of_range_uses_default():
    assert groq_watch._parse_hhmm("25:00", (6, 0)) == (6, 0)


# ---- outage_alert_due ----

def test_outage_no_failure():
    assert groq_watch.outage_alert_due(None, 1000.0, False, threshold=900) is False

def test_outage_below_threshold():
    # vypadek zacal pred 100s, prah 900s -> jeste ne
    assert groq_watch.outage_alert_due(1000.0, 1100.0, False, threshold=900) is False

def test_outage_above_threshold():
    assert groq_watch.outage_alert_due(1000.0, 1901.0, False, threshold=900) is True

def test_outage_already_alerted_suppressed():
    # i kdyz prah prekrocen, uz jsme alertovali -> nespamuj
    assert groq_watch.outage_alert_due(1000.0, 5000.0, True, threshold=900) is False


# ---- is_monitoring_time = den (not astral noc) ----
# Sjednoceni: groq_watch (denni Telegram) bezi presne kdyz NENI astral noc.

def test_monitoring_time_den_kdyz_neni_noc(monkeypatch):
    monkeypatch.setattr(groq_watch, "is_night", lambda: False)
    assert groq_watch.is_monitoring_time() is True

def test_monitoring_time_noc_je_false(monkeypatch):
    monkeypatch.setattr(groq_watch, "is_night", lambda: True)
    assert groq_watch.is_monitoring_time() is False
