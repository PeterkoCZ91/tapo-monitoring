import os

os.environ.setdefault("TAPO_IP", "192.168.1.1")
os.environ.setdefault("TAPO_EMAIL", "test@example.com")
os.environ.setdefault("TAPO_PASSWORD", "testpass")
os.environ.setdefault("TELEGRAM_TOKEN", "test:token123")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

import camera_automation


def test_write_state_vrati_true_pri_uspechu(tmp_path):
    original = camera_automation.STATE_FILE
    try:
        state_file = str(tmp_path / "state")
        camera_automation.STATE_FILE = state_file
        result = camera_automation.write_state("night")
        assert result is True
        with open(state_file) as f:
            assert f.read() == "night"
    finally:
        camera_automation.STATE_FILE = original


def test_write_state_vrati_false_pri_chybe():
    original = camera_automation.STATE_FILE
    try:
        camera_automation.STATE_FILE = "/nonexistent_dir_xyz/state_file"
        result = camera_automation.write_state("night")
        assert result is False
    finally:
        camera_automation.STATE_FILE = original


def test_day_preset_nacten_z_env():
    assert hasattr(camera_automation, "DAY_PRESET")
    assert camera_automation.DAY_PRESET == os.getenv("DAY_PRESET", "2")


def test_night_preset_nacten_z_env():
    assert hasattr(camera_automation, "NIGHT_PRESET")
    assert camera_automation.NIGHT_PRESET == os.getenv("NIGHT_PRESET", "")


# ---- decide_motion_sensitivity (déšť -> nižší citlivost na pohyb) ----

def test_motion_sensitivity_dest_snizena():
    assert camera_automation.decide_motion_sensitivity(True, normal="60", rain="20") == "20"

def test_motion_sensitivity_sucho_normalni():
    assert camera_automation.decide_motion_sensitivity(False, normal="60", rain="20") == "60"


class _FakeTapo:
    def __init__(self):
        self.sensitivity = None
    def setMotionDetection(self, enabled=None, sensitivity=False):
        self.sensitivity = sensitivity


def test_apply_rain_sensitivity_posila_int_ne_string():
    # pytapo: string "60" se přemapuje na label "high" (digital 80!) — musí jít int,
    # aby kamera dostala přesně digital 60 (medium), ne 80.
    fake = _FakeTapo()
    camera_automation.apply_rain_sensitivity(fake, rain_active=False)
    assert fake.sensitivity == 60
    assert isinstance(fake.sensitivity, int)


def test_apply_rain_sensitivity_dest_nizsi_int():
    fake = _FakeTapo()
    camera_automation.apply_rain_sensitivity(fake, rain_active=True)
    assert fake.sensitivity == 20
