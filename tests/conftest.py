"""Shared test doubles.

Kept here rather than copied per module: three files had grown their own byte-identical
fake HTTP response, so a change to what ``notify`` reads off a response meant three edits.
"""


class FakeResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` returns."""

    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_lamp_spans(monkeypatch):
    """Give every test its own record of when a lamp was seen lit (module-level state)."""
    from tapo_monitor import camera
    monkeypatch.setattr(camera, "_lamp_spans", {})


@pytest.fixture(autouse=True)
def _no_random_drop_sample(monkeypatch):
    """Keep the review log deterministic: the random drop sample is off unless a test asks.

    Tests that exercise it set ``TAPO_REVIEW_DROP_SAMPLE`` (or pass ``env``) and an rng;
    the hourly cap starts empty for every test.
    """
    from tapo_monitor import sentlog
    monkeypatch.setenv(sentlog.ENV_DROP_SAMPLE, "0")
    monkeypatch.setattr(sentlog, "_drop_cap", sentlog.DropSampleCap())
