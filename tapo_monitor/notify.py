"""Telegram notifications and the camera-down watchdog.

Transport (``send_text`` / ``send_photo``) is a thin urllib layer. The decision and
formatting helpers (``build_caption``, ``is_empty_scene``, ``outage_alert_due``) are
pure and tested.
"""

import json
import time
import urllib.parse
import urllib.request

from . import sentlog

_API = "https://api.telegram.org"

# A transient Telegram/network failure would otherwise lose a real alert; retry once.
TELEGRAM_RETRY_DELAY = 1.0

# Marker the vision model returns for a frame with nothing of interest.
EMPTY_MARKER = "empty"


def is_empty_scene(description, marker=EMPTY_MARKER):
    """True if the AI description marks an empty scene, or is blank.

    A blank/whitespace description means the vision model returned nothing (timeout or
    failure), so we can't confirm a subject — treat it as empty rather than letting a
    content-free alert through. Without this a Groq timeout sent a captionless blank: a
    bare-motion event that should have dropped, or an SD follow-up frame with no subject.
    """
    text = (description or "").strip()
    if not text:
        return True
    return marker in text.lower()


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


def format_duration(seconds):
    """Return a compact human-readable duration using at most two units."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    units = []
    if days:
        units.append(f"{days}d")
    if hours:
        units.append(f"{hours}h")
    if minutes:
        units.append(f"{minutes}m")
    if secs or not units:
        units.append(f"{secs}s")
    return " ".join(units[:2])


def should_send_alert(last_alert_ts, now, cooldown):
    """True when enough time has passed since the last detection alert.

    Rate-limits bursts of detections on one camera: the first alert (no prior
    timestamp) always passes; subsequent ones only after ``cooldown`` seconds.
    """
    if last_alert_ts is None:
        return True
    return now - last_alert_ts >= cooldown


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


def _post_photo(token, chat_id, image, caption):
    """POST one photo to Telegram. Returns True on success, False on any failure."""
    try:
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


def _archive_bytes(archive_path, sent_image):
    """Frame to keep in the sent log: the pre-crop one when readable, else what we sent."""
    if not archive_path:
        return sent_image
    try:
        with open(archive_path, "rb") as f:
            return f.read()
    except OSError:
        return sent_image


def send_photo(token, chat_id, image_path, caption, archive_path=None):
    """Send a photo with a caption via multipart/form-data. Returns True on success.

    ``archive_path`` names a different frame to keep in the sent log: ``crop_to_subject``
    cameras push a zoom, and only the uncropped scene shows whether the alert was a false
    positive. Unreadable? The sent frame is archived instead — never nothing.
    """
    try:
        with open(image_path, "rb") as f:
            image = f.read()
    except OSError:
        return False
    ok = _post_photo(token, chat_id, image, caption)
    if not ok:
        # A transient Telegram/network failure would otherwise lose a real alert.
        time.sleep(TELEGRAM_RETRY_DELAY)
        ok = _post_photo(token, chat_id, image, caption)
    # Best-effort diagnostic copy of the frame (opt-in via env).
    sentlog.archive_if_configured(_archive_bytes(archive_path, image), caption, delivered=ok)
    return ok


# Re-export for callers that just want JSON building (kept tiny on purpose).
def _payload(chat_id, text):  # pragma: no cover - trivial helper
    return json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
