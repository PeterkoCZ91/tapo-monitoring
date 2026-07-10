"""Frame enrichment: AI scene descriptions and frame selection.

Pure helpers (``has_person``, ``pick_sharpest``) are tested here. The Groq call is a
thin urllib layer; frame capture (ffmpeg/RTSP, SD clips) and face cropping (OpenCV) are
I/O-bound and stay with the daemon for now — only the selection logic is extracted.
"""

import base64
import json
import urllib.request

VISION_PROMPT = (
    "You are a security camera. The attached images are consecutive frames of one "
    "event, oldest first. Reply with a single JSON object: "
    '{"person": <bool>, "animal": <animal name or null>, "description": <string>}. '
    "If any frame shows a person, set person=true and describe clothing, behaviour "
    "and direction of movement across the frames in at most 15 words. Else if any "
    "frame shows an animal (cat, dog, fox, etc.), name it in animal and say what it "
    "does. For ANYTHING ELSE — vehicles, trees, shadows, lighting changes, empty "
    'frames — reply {"person": false, "animal": null, "description": ""}.'
)

DEFAULT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# The marker callers (notify.is_empty_scene, the SD arbiter loop) test for; predates
# JSON mode and stays the return contract for "nothing of interest".
EMPTY_SCENE = "empty scene"

# Groq vision accepts at most 5 images per request; longer sequences are thinned.
MAX_IMAGES = 5

# Groq's API sits behind Cloudflare, which rejects the default ``Python-urllib``
# agent with HTTP 403 (error 1010). Send a real client UA or every call fails and
# we silently lose all descriptions.
USER_AGENT = "groq-python/0.13.0"

_PERSON_WORDS = (
    "person", "people", "man", "woman", "men", "women", "child", "kid",
    "boy", "girl", "pedestrian", "figure", "someone", "human",
)


def has_person(description):
    """True if an AI description mentions a person (English vocabulary)."""
    low = (description or "").lower()
    return any(w in low for w in _PERSON_WORDS)


def parse_face_names(raw):
    """Parse a ``"face_id:name,face_id:name"`` env string into ``{int_id: name}``.

    Tolerant of blanks and malformed pairs (skipped), so a typo never crashes the daemon.
    """
    names = {}
    for pair in (raw or "").split(","):
        if ":" not in pair:
            continue
        fid, name = pair.split(":", 1)
        try:
            names[int(fid.strip())] = name.strip()
        except ValueError:
            continue
    return names


def face_label(face_ids, names=None):
    """Caption fragment for recognized faces, or '' when there are none.

    Known IDs are named; unrecognized (but stable) IDs collapse to 'unknown face'.
    """
    if not face_ids:
        return ""
    names = names or {}
    known = [names[f] for f in face_ids if names.get(f)]
    unknown = sum(1 for f in face_ids if not names.get(f))
    parts = []
    if known:
        parts.append(", ".join(known))
    if unknown == 1:
        parts.append("unknown face")
    elif unknown > 1:
        parts.append(f"{unknown} unknown faces")
    return " + ".join(parts)


def pick_sharpest(scored):
    """Given [(path, sharpness_score), ...] return the sharpest path, or None."""
    if not scored:
        return None
    return max(scored, key=lambda item: item[1])[0]


def select_frames(paths, keep=None, limit=MAX_IMAGES):
    """Thin a chronological frame list to <= ``limit``, keeping order.

    The selection spans the whole sequence (first and last frame always survive) so a
    caption built from it can describe movement across the event. ``keep`` — typically
    the frame chosen for sending — is swapped in for its nearest neighbour when the
    even spacing would drop it.
    """
    paths = list(paths)
    if len(paths) <= limit:
        return paths
    step = (len(paths) - 1) / (limit - 1)
    idx = sorted({round(i * step) for i in range(limit)})
    if keep in paths:
        ki = paths.index(keep)
        if ki not in idx:
            nearest = min(range(len(idx)), key=lambda j: abs(idx[j] - ki))
            idx[nearest] = ki
            idx = sorted(set(idx))
    return [paths[i] for i in idx]


def _content_to_description(content):
    """Map the model's JSON reply onto the caller contract (see ``groq_describe``)."""
    data = json.loads(content)
    desc = (data.get("description") or "").strip()
    animal = data.get("animal")
    animal = animal.strip() if isinstance(animal, str) else ""
    if data.get("person"):
        return desc or "person"          # blank desc must not read as "empty scene"
    if animal:
        return desc or animal
    return EMPTY_SCENE


def groq_describe(api_key, images, prompt=VISION_PROMPT, model=DEFAULT_MODEL,
                  max_tokens=160, timeout=20):
    """Describe one frame or a chronological frame sequence via Groq vision.

    ``images`` is a path or a list of paths (thinned to ``MAX_IMAGES``). The request
    uses JSON mode and the reply is mapped onto the historical caller contract:
    description text when a person/animal is present, ``'empty scene'`` when not,
    ``''`` on any failure (timeout, HTTP error, unparseable reply) — callers treat ''
    as "no evidence", never as a confirmed empty frame.

    ``timeout`` is generous (20 s) because a slow Pi Zero spends real time base64-
    encoding and uploading the JPEGs; at 10 s the request timed out, returned '', and
    the caller — for whom '' is not an "empty scene" — sent a blank/false alert instead
    of dropping it. The fast path (Pi 4) still returns in ~1-2 s, so the higher ceiling
    only rescues the slow uploads.
    """
    if isinstance(images, (str, bytes)) or not hasattr(images, "__iter__"):
        images = [images]
    try:
        content = [{"type": "text", "text": prompt}]
        for image_path in select_frames(images):
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }).encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)["choices"][0]["message"]["content"]
        return _content_to_description(reply)
    except Exception:
        return ""
