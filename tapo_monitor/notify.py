"""Telegram notifications and the camera-down watchdog.

Transport (``send_text`` / ``send_photo``) is a thin urllib layer. The decision and
formatting helpers (``build_caption``, ``is_empty_scene``, ``outage_alert_due``) are
pure and tested.
"""

import json
import urllib.parse
import urllib.request

_API = "https://api.telegram.org"

# Marker the vision model returns for a frame with nothing of interest.
EMPTY_MARKER = "empty"


def is_empty_scene(description, marker=EMPTY_MARKER):
    """True if the AI description marks an empty scene (nothing to alert on)."""
    return marker in (description or "").lower()


def build_caption(emoji, time_str, description=None, detail=None, count=None,
                  minutes_since_last=None):
    """Assemble an alert caption. Pure — no I/O."""
    headline = f"{emoji} {detail} {time_str}".strip() if detail else f"{emoji} {time_str}"
    lines = [headline]
    if description:
        lines.append(f'"{description}"')
    if count:
        suffix = f" · last {minutes_since_last} min ago" if minutes_since_last else ""
        lines.append(f"📊 detection #{count} today{suffix}")
    return "\n".join(lines)


def outage_alert_due(fail_since, now, already_alerted, threshold):
    """True when a continuous outage has lasted >= threshold and we have not alerted yet.

    Keeps the once-a-minute timer from spamming: alert once per outage, not per tick.
    """
    if fail_since is None or already_alerted:
        return False
    return now - fail_since >= threshold


def send_text(token, chat_id, text):
    """Send a plain (HTML) Telegram message. Returns True on success."""
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"{_API}/bot{token}/sendMessage", data=data),
            timeout=10,
        )
        return True
    except Exception:
        return False


def send_photo(token, chat_id, image_path, caption):
    """Send a photo with a caption via multipart/form-data. Returns True on success."""
    try:
        with open(image_path, "rb") as f:
            image = f.read()
        boundary = "tapoMonitorBoundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.jpg\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{_API}/bot{token}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status < 300 and '"ok":true' in resp.read().decode(errors="replace")
    except Exception:
        return False


# Re-export for callers that just want JSON building (kept tiny on purpose).
def _payload(chat_id, text):  # pragma: no cover - trivial helper
    return json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
