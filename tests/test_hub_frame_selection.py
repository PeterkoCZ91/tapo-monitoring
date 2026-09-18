"""Exercise selection through the real hub download-to-alert path."""

from pathlib import Path

import pytest

from tapo_monitor import daemon
from tests.test_daemon import _clip, _FakeHub, _frames, _hub_app, _hub_for, _hub_secrets


@pytest.mark.parametrize("scores,blurs,expected", [
    ([0.9, 0.8, 0.1], [12, 2, 1], 1),  # sharp background must not beat a person
    ([0.2, 0.6, 0.1], [12, 2, 1], 1),  # subject enters after the first frame
    ([0.6, 0.9, 0.1], [None, None, None], 1),
    ([0.7, None, 0.9], [2, 1, 1], 0),  # outage cannot replace confirmed subject
    ([None, 0.9, 0.8], [None, None, None], 0),  # original fail-open behavior
])
def test_hub_selects_subject_before_sharpness(monkeypatch, tmp_path, scores, blurs, expected):
    app = _hub_app(scorer={"url": "http://scorer/score", "threshold": 0.5})
    state = daemon.MonitorState()
    state.hub_cursor["gate"] = 1000
    hub = _FakeHub(clips=[[_clip(1100, 1120)]])
    extracted, downloads, sent = [], [], []

    def download(*args):
        downloads.append(1)
        Path(args[6]).write_bytes(b"clip")
        return args[6]

    def extract(*args, **kwargs):
        i = len(extracted)
        if i >= len(scores):
            return None
        path = tmp_path / f"candidate-{i}.jpg"
        path.write_text(str(i))
        extracted.append(str(path))
        return str(path)

    def send(cfg, secrets, image, caption, **kwargs):
        sent.append(int(Path(image).read_text()))
        return True

    monkeypatch.setattr(daemon, "resolve_hub_credentials", lambda cfg: ("user", "password"))
    monkeypatch.setattr(daemon.hubclient, "download_clip", download)
    monkeypatch.setattr(daemon.snapshot, "frame_from_clip", extract)
    monkeypatch.setattr(daemon.recclip, "blur_score", lambda f: blurs[int(Path(f).read_text())])
    monkeypatch.setattr(daemon, "send_alert_photo", send)
    live = _frames(tmp_path)
    daemon.run_hubpoll_pass(
        app, {}, state, now=1200, secrets=_hub_secrets(), hub_for=_hub_for(hub),
        frame_for=live,
        score_for=lambda cfg: lambda f: scores[int(Path(f).read_text())],
    )
    assert sent == [expected]
    assert downloads == [1]
    assert live.calls == []
    assert all(not Path(f).exists() for f in extracted)
