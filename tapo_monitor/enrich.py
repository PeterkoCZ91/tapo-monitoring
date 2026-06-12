"""Frame enrichment: AI scene descriptions and frame selection.

Pure helpers (``has_person``, ``pick_sharpest``) are tested here. The Groq call is a
thin urllib layer; frame capture (ffmpeg/RTSP, SD clips) and face cropping (OpenCV) are
I/O-bound and stay with the daemon for now — only the selection logic is extracted.
"""

import base64
import json
import urllib.request

VISION_PROMPT = (
    "You are a night security camera. If you see a person (one or more): describe "
    "clothing, behaviour and direction of movement in at most 15 words. If you see only "
    "a vehicle with no person, or an empty frame: reply exactly 'empty scene'. "
    "Description only, nothing else."
)

DEFAULT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

_PERSON_WORDS = (
    "person", "people", "man", "woman", "men", "women", "child", "kid",
    "boy", "girl", "pedestrian", "figure", "someone", "human",
)


def has_person(description):
    """True if an AI description mentions a person (English vocabulary)."""
    low = (description or "").lower()
    return any(w in low for w in _PERSON_WORDS)


def pick_sharpest(scored):
    """Given [(path, sharpness_score), ...] return the sharpest path, or None."""
    if not scored:
        return None
    return max(scored, key=lambda item: item[1])[0]


def groq_describe(api_key, image_path, prompt=VISION_PROMPT, model=DEFAULT_MODEL,
                  max_tokens=60, timeout=10):
    """Send an image to Groq vision and return the description text (or '')."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""
