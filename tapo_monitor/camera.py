"""Camera connection wrapper around pytapo.

pytapo is a thin API client; this adds the operational glue around it:

* **Cheap reachability probe** — ping the device before creating an authenticated session,
  so a disconnected camera is distinct from an API/authentication failure.
* **Lockout-aware connect** — the C560WS locks out a source IP for ~30 min after failed
  logins, and the first login after a reconnect often fails with "Invalid authentication
  data" before a retry succeeds. ``connect`` retries a few times with a delay so callers
  don't rediscover this; ``sleep`` is injectable for tests.
* **getEvents helpers** — pure filtering of new events since a watermark.

The pytapo import is lazy (inside ``tapo_factory``) so the rest of the package — and the
tests — import without the dependency present.
"""

import subprocess
import time as _time


def ping_reachable(host, timeout=1, run=subprocess.run):
    """True when the camera answers one ICMP echo request.

    The command uses an argv list (never a shell), suppresses output and has both ping's
    own deadline and a subprocess deadline. It performs no camera login.
    """
    try:
        result = run(
            ["ping", "-n", "-c", "1", "-W", str(max(1, int(timeout))), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def tapo_factory(host, user, password, cloud_password=None):
    """Return a zero-arg callable that builds a pytapo client (import kept lazy)."""
    def make():
        from pytapo import Tapo
        return Tapo(host, user, password, cloud_password or password)
    return make


def connect(factory, retries=3, sleep=_time.sleep, delay=5):
    """Try ``factory()`` up to ``retries`` times. Returns (client, last_error).

    On success returns (client, None); on exhaustion (None, last_error).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return factory(), None
        except Exception as e:  # noqa: BLE001 - surface the last error to the caller
            last_err = e
            if attempt < retries:
                sleep(delay)
    return None, last_err


def new_events(events, last_seen):
    """Events whose start_time is strictly newer than the watermark, oldest first."""
    fresh = [e for e in (events or []) if e.get("start_time", 0) > last_seen]
    return sorted(fresh, key=lambda e: e.get("start_time", 0))


def newest_start(events):
    """Largest start_time across events, or None when empty."""
    starts = [e.get("start_time", 0) for e in (events or [])]
    return max(starts) if starts else None
