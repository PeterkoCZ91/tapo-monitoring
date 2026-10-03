"""``scorer.lens_ignore_zones``: ignore zones that belong to one lens of a dual-lens camera."""

import pytest

from tapo_monitor import config as cfg_mod
from tapo_monitor import daemon, snapshot

WIDE_ZONE = [0.0, 0.5, 0.1, 0.7]
PT_ZONE = [0.6, 0.1, 0.9, 0.4]
GLOBAL_ZONE = [0.4, 0.4, 0.5, 0.5]


def _camera(**scorer_kw):
    scorer_kw = {"url": "http://x", **scorer_kw}
    return cfg_mod.load_config_from_dict({"cameras": [{
        "name": "front", "host": "192.0.2.20", "event_profile": "c545d",
        "enrich": {"groq": False}, "scorer": scorer_kw}]}).cameras[0]


def test_lens_ignore_zones_parse_per_lens():
    camera = _camera(lens_ignore_zones={"wide": [WIDE_ZONE], "pan_tilt": [PT_ZONE]})
    assert camera.scorer.lens_ignore_zones == {"wide": (tuple(WIDE_ZONE),),
                                               "pan_tilt": (tuple(PT_ZONE),)}


def test_lens_ignore_zones_default_empty():
    assert _camera().scorer.lens_ignore_zones == {}


@pytest.mark.parametrize("value", [{"fisheye": [WIDE_ZONE]}, {"wide": [[0.5, 0, 0.4, 1]]},
                                   {"wide": "x"}, [WIDE_ZONE]])
def test_lens_ignore_zones_reject_bad_values(value):
    with pytest.raises(cfg_mod.ConfigError, match="lens_ignore_zones"):
        _camera(lens_ignore_zones=value)


def test_lens_ignore_zones_need_a_pan_tilt_profile():
    with pytest.raises(cfg_mod.ConfigError, match="lens_ignore_zones"):
        cfg_mod.load_config_from_dict({"cameras": [{
            "name": "a", "host": "192.0.2.10",
            "scorer": {"url": "http://x", "lens_ignore_zones": {"wide": [WIDE_ZONE]}}}]})


def _zones_seen(monkeypatch, camera, image):
    seen = []

    def fake(url, path, **kw):
        seen.append(kw.get("ignore_zones"))
        return {"w": 10, "h": 10, "person": 0.0}

    monkeypatch.setattr(daemon.scorer, "score_image", fake)
    daemon.score_for(camera)(image)
    return seen[0]


def _tagged(tmp_path, lens):
    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    return snapshot.with_lens(str(path), lens)


def test_score_uses_global_plus_the_frames_lens_zones(monkeypatch, tmp_path):
    camera = _camera(ignore_zones=[GLOBAL_ZONE],
                     lens_ignore_zones={"wide": [WIDE_ZONE], "pan_tilt": [PT_ZONE]})
    assert (_zones_seen(monkeypatch, camera, _tagged(tmp_path, "wide"))
            == (tuple(GLOBAL_ZONE), tuple(WIDE_ZONE)))
    assert (_zones_seen(monkeypatch, camera, _tagged(tmp_path, "pan/tilt"))
            == (tuple(GLOBAL_ZONE), tuple(PT_ZONE)))


def test_score_of_an_untagged_frame_uses_only_global_zones(monkeypatch, tmp_path):
    camera = _camera(ignore_zones=[GLOBAL_ZONE], lens_ignore_zones={"wide": [WIDE_ZONE]})
    assert _zones_seen(monkeypatch, camera, _tagged(tmp_path, None)) == (tuple(GLOBAL_ZONE),)


def test_empty_lens_zones_pass_the_global_zones_unchanged(monkeypatch, tmp_path):
    camera = _camera(ignore_zones=[GLOBAL_ZONE])
    assert _zones_seen(monkeypatch, camera, _tagged(tmp_path, "wide")) == (tuple(GLOBAL_ZONE),)


def test_score_passes_max_person_box_through(monkeypatch, tmp_path):
    camera = _camera(max_person_box=0.9)
    seen = []
    monkeypatch.setattr(daemon.scorer, "score_image",
                        lambda url, path, **kw: seen.append(kw.get("max_person_box")) or None)
    daemon.score_for(camera)(_tagged(tmp_path, "wide"))
    assert seen and set(seen) == {0.9}
