"""Client for the local object-detection scoring service.

The scorer replaces Groq as the *arbiter* of whether a frame shows a subject: a tiny
YOLO model behind :mod:`tapo_monitor.scorer_service` returns person/animal confidence
and the caller compares it to a config threshold. Every failure returns ``None`` so
callers can degrade to raw passthrough (send unfiltered) — the scorer must never turn
into a silent drop.
"""

import json
import logging
import math
import urllib.request

log = logging.getLogger(__name__)


def score_image(url, image_path, timeout=10, tiles=1):
    """POST a JPEG to the scoring service; dict on success, None on ANY failure.

    ``tiles > 1`` asks the service to also score a tiles×tiles grid (rescues distant
    subjects) and to return a ``box`` for the winning person — see
    :func:`subject_box`.
    """
    try:
        with open(image_path, "rb") as f:
            body = f.read()
    except OSError:
        log.warning("scorer: cannot read %s", image_path)
        return None
    if tiles and int(tiles) > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}tiles={int(tiles)}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "image/jpeg"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            result = json.load(resp)
            if not isinstance(result, dict):
                log.warning("scorer returned a non-object JSON response")
                return None
            return result
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - any transport/parse failure means "unavailable"
        log.warning("scorer request failed: %s", e)
        return None


class SubjectScore(float):
    """Person score with animal confidence retained for audit telemetry."""

    def __new__(cls, person, animal):
        value = float.__new__(cls, person)
        value.person = person
        value.animal = animal
        return value


def subject_scores(result):
    """Return validated ``(person, animal)`` confidences, or None when malformed."""
    if not isinstance(result, dict):
        return None
    scores = {}
    for key in ("person", "animal"):
        try:
            score = float(result.get(key, 0.0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            return None
        scores[key] = score
    return scores["person"], scores["animal"]


def subject_score(result):
    """Use person confidence for alert gating; preserve animal confidence for audit."""
    values = subject_scores(result)
    return None if values is None else SubjectScore(*values)


def subject_box(result):
    """Winning person's ``[x1, y1, x2, y2]`` (original px) from a response, or None. Pure."""
    box = result.get("box") if isinstance(result, dict) else None
    if not box or len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except (TypeError, ValueError):
        return None
