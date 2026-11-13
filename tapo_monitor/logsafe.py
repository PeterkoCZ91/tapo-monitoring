"""Log-safe descriptions of failures from camera calls.

A pytapo, requests or ffmpeg failure text can carry the camera's address, a session URL
(``/stok=...``), RTSP credentials or part of the camera's reply. None of that belongs in a
journal that is pasted into bug reports. This module turns a failure into what is useful
and harmless: the exception class, a fixed category, and where it was raised, and masks
URLs, credentials and addresses in free text such as an ffmpeg stderr tail.

Pure and dependency free, so any module (including the SD download subprocess entry) can
use it.
"""

import os
import re
import subprocess
import traceback

_TIMEOUT_NAMES = frozenset({"ReadTimeout", "ConnectTimeout", "Timeout", "TimeoutError",
                            "TimeoutExpired", "ReadTimeoutError", "ConnectTimeoutError"})

_URL = re.compile(r"""\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'"<>)\]]+""")
_SECRET = re.compile(
    r"(?i)\b(stok|token|password|passwd|passcode|pwd|authorization|api[_-]?key)"
    r"(\s*[=:]\s*|\s+)[^\s'\"&/,;)]+")
_USERINFO = re.compile(r"[^\s/@:'\"]+:[^\s/@'\"]+@")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_timeout(exc):
    """True for any timeout: builtin, ``requests`` (not a ``TimeoutError``) or subprocess."""
    for item in _chain(exc):
        if isinstance(item, (TimeoutError, subprocess.TimeoutExpired)):
            return True
        if any(cls.__name__ in _TIMEOUT_NAMES for cls in type(item).__mro__):
            return True
    return False


def failure_category(exc):
    """A fixed, text-free diagnosis of ``exc``: never a slice of its message."""
    names = {cls.__name__ for item in _chain(exc) for cls in type(item).__mro__}
    text = " ".join(str(item) for item in _chain(exc)).lower()
    if "ConnectTimeout" in names or "connecttimeout" in text or "connect timeout" in text:
        return "connect_timeout"
    if "ReadTimeout" in names or "readtimeout" in text or "read timed out" in text:
        return "read_timeout"
    if is_timeout(exc):
        return "timeout"
    if "invalid stok" in text or "unauthorized" in text or "authentication" in text:
        return "authentication_failure"
    if "connection refused" in text:
        return "connection_refused"
    return "other"


def describe(exc):
    """``class=<ExceptionClass> reason=<category>`` (+ ``code=<n>`` when numeric) for a log line."""
    if exc is None:
        return "class=none reason=no_result"
    text = f"class={type(exc).__name__} reason={failure_category(exc)}"
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        text += f" code={code}"          # pytapo's numeric error code: a number, no text
    return text


def where(exc):
    """``file.py:line:function`` of the innermost frame the exception passed through."""
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return "unknown"
    last = traceback.extract_tb(tb)[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno}:{last.name}"


def mask_text(text):
    """``text`` with URLs, credentials, session tokens and IPv4 addresses masked."""
    text = _URL.sub("<url>", str(text))
    text = _SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _USERINFO.sub("<cred>@", text)
    return _IPV4.sub("<ip>", text)


def masked_traceback(exc):
    """The full traceback text of ``exc`` with ``mask_text`` applied: for loops whose
    unexpected failures need their stack, but must not echo camera URLs or credentials."""
    return mask_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
