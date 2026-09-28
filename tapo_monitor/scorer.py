"""Client for the local object-detection scoring service.

The scorer replaces Groq as the *arbiter* of whether a frame shows a subject: a tiny
YOLO model behind :mod:`tapo_monitor.scorer_service` returns person/animal confidence
and the caller compares it to a config threshold. Every failure returns ``None`` so
callers can degrade to raw passthrough (send unfiltered) — the scorer must never turn
into a silent drop.
"""

import hashlib
import json
import logging
import math
import urllib.request

log = logging.getLogger(__name__)


def source_id_for_camera(camera_name):
    """Return a stable pseudonymous source identifier for scorer telemetry."""
    value = f"tapo-camera:{camera_name}".encode()
    return hashlib.sha256(value).hexdigest()[:16]


def score_image(url, image_path, timeout=10, tiles=1, *, source_id=None, ignore_zones=()):
    """POST a JPEG to the scoring service; dict on success, None on ANY failure.

    ``tiles > 1`` asks the service to also score a tiles×tiles grid (rescues distant
    subjects) and to return a ``box`` for the winning person — see
    :func:`subject_box`. ``ignore_zones`` removes the people found inside them
    (:func:`apply_ignore_zones`) before anyone reads the answer.
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
    headers = {"Content-Type": "image/jpeg"}
    if isinstance(source_id, str) and len(source_id) in range(16, 65):
        normalized_source_id = source_id.lower()
        if all(character in "0123456789abcdef" for character in normalized_source_id):
            headers["X-Tapo-Source-ID"] = normalized_source_id
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            result = json.load(resp)
            if not isinstance(result, dict):
                log.warning("scorer returned a non-object JSON response")
                return None
            return apply_ignore_zones(result, ignore_zones) if ignore_zones else result
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - any transport/parse failure means "unavailable"
        log.warning("scorer request failed: %s", e)
        return None


# A person counts as inside an ignore zone when at least this share of its box is.
IGNORE_ZONE_OVERLAP = 0.8
_warned_no_boxes = False


def _inside_share(box, zone, w, h):
    """Share of ``box`` (pixels) covered by ``zone`` (fractions of the frame). Pure."""
    x1, y1, x2, y2 = box
    area = max(x2 - x1, 0.0) * max(y2 - y1, 0.0)
    if area <= 0:
        return 0.0
    zx1, zy1, zx2, zy2 = zone[0] * w, zone[1] * h, zone[2] * w, zone[3] * h
    ix = max(min(x2, zx2) - max(x1, zx1), 0.0)
    iy = max(min(y2, zy2) - max(y1, zy1), 0.0)
    return ix * iy / area


def apply_ignore_zones(result, zones, overlap=IGNORE_ZONE_OVERLAP):
    """Drop the people whose box lies inside an ignore zone from a response. Pure-ish.

    A static person-shaped object (sacks, a coat on a hook) scores as a person on every
    frame, so any event the camera raises nearby — a bird, a shadow — "confirms" it. A
    zone (``[x1, y1, x2, y2]`` as fractions of the frame) removes the people found at
    least ``overlap`` inside it; ``person`` becomes the best remaining one, ``box`` its
    box and ``person_scores`` the rest, so a real person elsewhere, or one standing in
    front of the object with a larger box, still counts. Needs the service's
    ``person_boxes``: an older service is left as it was (logged once).
    """
    global _warned_no_boxes
    scores, boxes = result.get("person_scores"), result.get("person_boxes")
    w, h = result.get("w"), result.get("h")
    if (not isinstance(scores, list) or not isinstance(boxes, list)
            or len(scores) != len(boxes) or not w or not h):
        if not _warned_no_boxes:
            log.warning("scorer: ignore zones need a scorer that returns person_boxes; "
                        "update the scoring service")
            _warned_no_boxes = True
        return result
    try:
        pairs = [(float(sc), [float(v) for v in bx]) for sc, bx in zip(scores, boxes, strict=True)]
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return result
    kept = [(sc, bx) for sc, bx in pairs
            if not any(_inside_share(bx, z, w, h) >= overlap for z in zones)]
    if len(kept) == len(pairs):
        return result
    out = dict(result)
    out["ignored_persons"] = len(pairs) - len(kept)
    out["person_scores"] = [sc for sc, _ in kept]
    out["person_boxes"] = [bx for _, bx in kept]
    if kept:
        best, best_box = kept[0]
        if best < float(result.get("person", 0.0)):
            out["person"], out["box"] = best, best_box
    else:
        # Everything the service listed was ignored; anything left is below its floor.
        out["person"], out["box"] = 0.0, None
    if isinstance(out.get("classes"), dict) and "person" in out["classes"]:
        out["classes"] = {**out["classes"], "person": out["person"]}
    return out


class SubjectScore(float):
    """Person score with animal confidence retained for audit telemetry.

    ``persons`` is how many people the frame holds at or above the caller's threshold
    (see :func:`person_count`), or None when the scorer did not report it.
    """

    def __new__(cls, person, animal, persons=None):
        value = float.__new__(cls, person)
        value.person = person
        value.animal = animal
        value.persons = persons
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


def person_count(result, threshold):
    """People at or above ``threshold`` in a response, or None when it cannot say. Pure.

    The service lists every separate person it found (``person_scores``, after
    non-maximum suppression, full frame only). An older service sends no such list, and a
    malformed one is ignored: either way the answer is None, never a guessed count.
    """
    values = result.get("person_scores") if isinstance(result, dict) else None
    if not isinstance(values, list) or threshold is None:
        return None
    try:
        scores = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in scores):
        return None
    return sum(1 for v in scores if v >= threshold)


def subject_score(result, threshold=None):
    """Use person confidence for alert gating; preserve animal confidence for audit.

    With ``threshold`` the score also carries the person count at it (``persons``).
    """
    values = subject_scores(result)
    if values is None:
        return None
    return SubjectScore(*values, persons=person_count(result, threshold))


def subject_box(result):
    """Winning person's ``[x1, y1, x2, y2]`` (original px) from a response, or None. Pure."""
    box = result.get("box") if isinstance(result, dict) else None
    if not box or len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except (TypeError, ValueError):
        return None
