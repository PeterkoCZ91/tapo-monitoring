"""Client for the local object-detection scoring service.

The scorer replaces Groq as the *arbiter* of whether a frame shows a subject: a tiny
YOLO model behind :mod:`tapo_monitor.scorer_service` returns person/animal confidence
and the caller compares it to a config threshold. Every failure returns ``None`` so
callers can degrade to raw passthrough (send unfiltered) — the scorer must never turn
into a silent drop.
"""

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def score_image(url, image_path, timeout=10):
    """POST a JPEG to the scoring service; dict on success, None on ANY failure."""
    try:
        with open(image_path, "rb") as f:
            body = f.read()
    except OSError:
        log.warning("scorer: cannot read %s", image_path)
        return None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "image/jpeg"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return json.load(resp)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - any transport/parse failure means "unavailable"
        log.warning("scorer request failed: %s", e)
        return None


def subject_score(result):
    """Max subject confidence from a service response. Pure."""
    return max(float(result.get("person", 0.0)), float(result.get("animal", 0.0)))
