import os

# Nastavit env proměnné PŘED importem — modul je parsuje při importu
os.environ.setdefault("TAPO_IP", "192.168.1.1")
os.environ.setdefault("ONVIF_USER", "admin")
os.environ.setdefault("ONVIF_PASS", "testpass")
os.environ.setdefault("TELEGRAM_TOKEN", "test:token123")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("GROQ_API_KEY", "gsk_testkey")

import person_monitor

# ---- outage_alert_due ----

def test_outage_no_failure():
    assert person_monitor.outage_alert_due(None, 1000.0, False, threshold=900) is False

def test_outage_below_threshold():
    assert person_monitor.outage_alert_due(1000.0, 1100.0, False, threshold=900) is False

def test_outage_above_threshold():
    assert person_monitor.outage_alert_due(1000.0, 1901.0, False, threshold=900) is True

def test_outage_already_alerted_suppressed():
    assert person_monitor.outage_alert_due(1000.0, 5000.0, True, threshold=900) is False


# ---- autotrack_reassert_due ----

def test_reassert_first_run_due_immediately():
    # last_assert=0 -> hned po startu re-assert
    assert person_monitor.autotrack_reassert_due(0.0, 1000.0, interval=300) is True

def test_reassert_not_due_within_interval():
    assert person_monitor.autotrack_reassert_due(1000.0, 1200.0, interval=300) is False

def test_reassert_due_after_interval():
    assert person_monitor.autotrack_reassert_due(1000.0, 1301.0, interval=300) is True


# ---- should_reassert_autotrack (gate na astral noc) ----
# Bug: person_monitor bezi do MONITOR_END (05:30), ale camera_automation prepne
# na den uz v astral sunrise+30 (~05:09). V okne 05:09-05:30 reassert znovu zapnul
# autotrack, ktery camera_automation prave vypnul -> kamera pres den honila auta.
# Fix: reassert jen kdyz je SKUTECNA astral noc (is_night()).

def test_should_reassert_skip_kdyz_neni_noc():
    # i kdyz je reassert "due", mimo astral noc se NEVOLA
    assert person_monitor.should_reassert_autotrack(False, 0.0, 1000.0, interval=300) is False

def test_should_reassert_ano_v_noci_kdyz_due():
    assert person_monitor.should_reassert_autotrack(True, 0.0, 1000.0, interval=300) is True

def test_should_reassert_ne_v_noci_kdyz_neni_due():
    assert person_monitor.should_reassert_autotrack(True, 1000.0, 1200.0, interval=300) is False


# ---- is_monitoring_time deleguje na astral is_night ----
# Sjednoceni: person_monitor hlida lidi presne behem astral noci (jediny zdroj
# pravdy), aby handoff s groq_watch a autotrackem byl ve stejny okamzik.

def test_monitoring_time_je_astral_noc(monkeypatch):
    monkeypatch.setattr(person_monitor, "is_night", lambda: True)
    assert person_monitor.is_monitoring_time() is True

def test_monitoring_time_mimo_noc_je_false(monkeypatch):
    monkeypatch.setattr(person_monitor, "is_night", lambda: False)
    assert person_monitor.is_monitoring_time() is False


# ---- is_time_in_window ----

def test_same_day_window_inside():
    # 09:00–17:00, cur=12:00 → uvnitř
    assert person_monitor.is_time_in_window(540, 1020, 720) is True

def test_same_day_window_before_start():
    # 09:00–17:00, cur=05:00 → před oknem
    assert person_monitor.is_time_in_window(540, 1020, 300) is False

def test_same_day_window_after_end():
    # 09:00–17:00, cur=20:00 → za oknem
    assert person_monitor.is_time_in_window(540, 1020, 1200) is False

def test_same_day_window_at_start_inclusive():
    # 09:00–17:00, cur=09:00 → přesně start (inclusive)
    assert person_monitor.is_time_in_window(540, 1020, 540) is True

def test_same_day_window_at_end_exclusive():
    # 09:00–17:00, cur=17:00 → přesně konec (exclusive)
    assert person_monitor.is_time_in_window(540, 1020, 1020) is False

def test_midnight_crossing_inside_before_midnight():
    # 22:30–05:30, cur=23:00 → před půlnocí, uvnitř
    assert person_monitor.is_time_in_window(1350, 330, 1380) is True

def test_midnight_crossing_inside_after_midnight():
    # 22:30–05:30, cur=02:00 → po půlnoci, uvnitř
    assert person_monitor.is_time_in_window(1350, 330, 120) is True

def test_midnight_crossing_outside_midday():
    # 22:30–05:30, cur=12:00 → mimo okno
    assert person_monitor.is_time_in_window(1350, 330, 720) is False

def test_midnight_crossing_at_start():
    # 22:30–05:30, cur=22:30 → přesně start (inclusive)
    assert person_monitor.is_time_in_window(1350, 330, 1350) is True

def test_midnight_crossing_at_end_exclusive():
    # 22:30–05:30, cur=05:30 → přesně konec (exclusive)
    assert person_monitor.is_time_in_window(1350, 330, 330) is False

def test_same_start_and_end_returns_false():
    # start == end → nulové okno, nikdy aktivní
    assert person_monitor.is_time_in_window(540, 540, 540) is False
    assert person_monitor.is_time_in_window(540, 540, 720) is False
    assert person_monitor.is_time_in_window(0, 0, 0) is False


# ---- handle_detection cooldown ----

import time
from unittest.mock import patch


def _reset_monitor_state():
    person_monitor.last_alert_time = 0.0
    person_monitor.last_person_event_time = 0.0
    person_monitor.returned_to_home = True
    person_monitor.daily_count = 0
    person_monitor.daily_date = ""


def test_cooldown_aktivovan_po_uspesnem_telegramu():
    _reset_monitor_state()
    before = time.time()
    with patch.object(person_monitor, "grab_snapshot_full", return_value="/tmp/x_full.jpg"), \
         patch.object(person_monitor, "process_frame", return_value=("/tmp/x_wide.jpg", None)), \
         patch.object(person_monitor, "groq_describe", return_value="clovek v cernem"), \
         patch.object(person_monitor, "telegram_photo", return_value=True), \
         patch.object(person_monitor, "remove_quiet"):
        person_monitor.handle_detection("person")
    assert person_monitor.last_alert_time >= before, \
        "last_alert_time musí být nastaven po úspěšném Telegramu"


def test_cooldown_neni_nastaven_kdyz_telegram_selze():
    _reset_monitor_state()
    with patch.object(person_monitor, "grab_snapshot_full", return_value="/tmp/x_full.jpg"), \
         patch.object(person_monitor, "process_frame", return_value=("/tmp/x_wide.jpg", None)), \
         patch.object(person_monitor, "groq_describe", return_value="clovek v cernem"), \
         patch.object(person_monitor, "telegram_photo", return_value=False), \
         patch.object(person_monitor, "remove_quiet"):
        person_monitor.handle_detection("person")
    assert person_monitor.last_alert_time == 0.0, \
        "last_alert_time nesmí být nastaven když Telegram selže"


def test_zoom_neodeslan_kdyz_wide_selze():
    _reset_monitor_state()
    telegram_calls = []

    def fake_telegram(path, caption):
        telegram_calls.append(path)
        return False  # wide foto selže

    with patch.object(person_monitor, "grab_snapshot_full", return_value="/tmp/x_full.jpg"), \
         patch.object(person_monitor, "process_frame", return_value=("/tmp/x_wide.jpg", "/tmp/x_face.jpg")), \
         patch.object(person_monitor, "groq_describe", return_value="clovek"), \
         patch.object(person_monitor, "telegram_photo", side_effect=fake_telegram), \
         patch.object(person_monitor, "remove_quiet"):
        person_monitor.handle_detection("person")

    assert len(telegram_calls) == 1, "Při selhání wide foto se nesmí volat zoom"
    assert "/tmp/x_face.jpg" not in telegram_calls, "Zoom foto nesmí být odesláno pokud wide selhalo"


def test_aktivni_cooldown_preskoci_snapshot():
    person_monitor.last_alert_time = time.time()  # právě teď → cooldown aktivní
    person_monitor.last_person_event_time = 0.0
    person_monitor.returned_to_home = True
    with patch.object(person_monitor, "grab_snapshot_full") as mock_snap:
        person_monitor.handle_detection("person")
        mock_snap.assert_not_called()


# ---- mask_secrets ----

def test_mask_secrets_odstraní_heslo():
    # ONVIF_PASS byl nastaven na "testpass" v setUp tohoto souboru
    text = "rtsp://admin:testpass@192.168.1.1:554/stream1"
    result = person_monitor.mask_secrets(text)
    assert "testpass" not in result
    assert "192.168.1.1" in result  # host zůstane


def test_mask_secrets_prazdny_text():
    assert person_monitor.mask_secrets("") == ""


def test_mask_secrets_bez_credentials():
    text = "ffmpeg: Connection refused"
    assert person_monitor.mask_secrets(text) == text


def test_mask_secrets_url_enkodovane_heslo():
    # Heslo se spec. znaky — ffmpeg může logovat URL-enkódovanou verzi
    import urllib.parse
    encoded_pass = urllib.parse.quote("testpass", safe="")
    text = f"rtsp://admin:{encoded_pass}@192.168.1.1:554"
    result = person_monitor.mask_secrets(text)
    assert encoded_pass not in result
    assert "192.168.1.1" in result


# ---- reconcile_get_events baseline ----

from unittest.mock import MagicMock


def test_prvni_volani_getevents_nespusti_alert():
    person_monitor.last_seen_event_time = 0
    person_monitor._getevents_initialized = False
    person_monitor.last_alert_time = 0.0

    fake_events = [
        {"start_time": 1000, "event_type": "person"},
        {"start_time": 1100, "event_type": "person"},
    ]

    with patch.object(person_monitor, "get_tapo_client") as mock_client, \
         patch.object(person_monitor, "handle_detection") as mock_handle:
        mock_tapo = MagicMock()
        mock_tapo.getEvents.return_value = fake_events
        mock_client.return_value = mock_tapo
        person_monitor.reconcile_get_events()

    mock_handle.assert_not_called()
    assert person_monitor.last_seen_event_time == 1100
    assert person_monitor._getevents_initialized is True


def test_druhe_volani_getevents_spusti_alert():
    person_monitor.last_seen_event_time = 1100
    person_monitor._getevents_initialized = True
    person_monitor.last_alert_time = 0.0

    fake_events = [
        {"start_time": 1100, "event_type": "person"},  # starý, ignorovat
        {"start_time": 2000, "event_type": "person"},  # nový
    ]

    with patch.object(person_monitor, "get_tapo_client") as mock_client, \
         patch.object(person_monitor, "handle_detection") as mock_handle:
        mock_tapo = MagicMock()
        mock_tapo.getEvents.return_value = fake_events
        mock_client.return_value = mock_tapo
        person_monitor.reconcile_get_events()

    mock_handle.assert_called_once()
