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
