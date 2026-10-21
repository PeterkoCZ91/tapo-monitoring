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
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def source_id_for_camera(camera_name):
    """Return a stable pseudonymous source identifier for scorer telemetry."""
    value = f"tapo-camera:{camera_name}".encode()
    return hashlib.sha256(value).hexdigest()[:16]


def resolve_token(token_env, env=None):
    """The shared scorer token named by ``token_env`` (an env var NAME), or None.

    ``<token_env>_FILE``, when set, names a file holding the token and wins over the value
    itself (a unit can then load it as a credential). Never raises and never logs the
    token: a missing or unreadable source reads as "no token", and the service's 401 says so.
    """
    if not token_env:
        return None
    env = os.environ if env is None else env
    path = (env.get(f"{token_env}_FILE") or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            log.warning("scorer: cannot read the token file named by %s_FILE", token_env)
            return None
    return (env.get(token_env) or "").strip() or None


def keep_auth_off_redirects(req):
    """Make ``req``'s ``Authorization`` header private to the original URL. Mutates ``req``.

    urllib's redirect handler copies ``req.headers`` to the redirect target, so a bearer
    token set the normal way would be replayed to whatever host a (compromised or
    misconfigured) scorer redirects to. Headers added with ``add_unredirected_header`` are
    sent on the first request only and never copied by the handler.
    """
    for name in list(req.headers):
        if name.lower() == "authorization":
            req.add_unredirected_header("Authorization", req.headers.pop(name))
    return req


def auth_headers(token):
    """``{"Authorization": "Bearer <token>"}`` for a token, else ``{}``."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def score_image(url, image_path, timeout=10, tiles=1, *, source_id=None, ignore_zones=(),
                alert_zones=(), alert_zones_mode="enforce", token=None, max_person_box=None,
                ignore_boxes=(), ignore_boxes_mode="enforce", faces=False):
    """POST a JPEG to the scoring service; dict on success, None on ANY failure.

    ``tiles > 1`` asks the service to also score a tiles×tiles grid (rescues distant
    subjects) and to return a ``box`` for the winning person — see
    :func:`subject_box`. ``ignore_zones`` removes the people found inside them
    (:func:`apply_ignore_zones`) before anyone reads the answer; ``alert_zones`` then keeps
    only the people standing in them (:func:`apply_alert_zones`). With ``alert_zones_mode="observe"`` nothing is filtered:
    the answer only gains ``alert_zone_observe`` when a zone would have dropped a person
    (:func:`observe_alert_zones`). ``max_person_box`` (a fraction, None = off) drops a
    person whose box exceeds it in both dimensions (:func:`apply_max_person_box`), after the
    ignore zones and before the alert zones. ``ignore_boxes`` drops a person whose box
    matches one of the given rects in size AND place (:func:`apply_ignore_boxes`), after the
    ignore zones; with ``ignore_boxes_mode="observe"`` nothing is dropped and the answer
    gains ``ignore_box_observe`` (:func:`observe_ignore_boxes`).

    ``token`` (the shared secret of a service that requires one) goes out as a bearer
    header and is never logged.

    ``faces=True`` asks the service for face boxes (``faces`` in the answer, see
    :func:`frame_faces`); a service without a face model simply leaves them out.

    Callers that have a camera's ``ScorerConfig`` use :func:`score`, which adds failover
    and reads the token named by ``scorer.token_env``.
    """
    return _score_once(url, image_path, timeout, tiles, source_id=source_id,
                       ignore_zones=ignore_zones, alert_zones=alert_zones,
                       alert_zones_mode=alert_zones_mode, token=token,
                       max_person_box=max_person_box, ignore_boxes=ignore_boxes,
                       ignore_boxes_mode=ignore_boxes_mode, faces=faces)[0]


_warned_unauthorized: set = set()


def _unauthorized(exc):
    """True when the service answered 401: a configuration error, not an outage."""
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 401


def _warn_unauthorized(url, sent_token):
    """Say once per endpoint that the service wants a token we lack or it refused."""
    key = urllib.parse.urlsplit(url)._replace(path="", query="").geturl()
    if key in _warned_unauthorized:
        return
    _warned_unauthorized.add(key)
    log.error("scorer %s rejected the token (HTTP 401): %s; set scorer.token_env to the "
              "variable holding the service's shared token", key,
              "the configured token is wrong" if sent_token else "no token is configured")


def _transport_failure(exc):
    """True when ``exc`` says the service could not be reached (not that it refused a frame).

    A timeout or connection error counts; so does a gateway answer (502/503/504) from a
    proxy in front of a dead service. Any other HTTP status — the 4xx a bad image earns —
    and any parse error mean the service is there.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (502, 503, 504)
    return isinstance(exc, (OSError, TimeoutError))


# One retry of a request that hung or that the service answered 400/500 (a truncated
# upload, a one-off inference error). The whole call may use RETRY_BUDGET_FACTOR x the
# request timeout; the retry runs only when at least RETRY_MIN_SECONDS of that remain.
RETRY_BUDGET_FACTOR = 2.0
RETRY_BACKOFF_SECONDS = 0.5
RETRY_MIN_SECONDS = 3.0
_sleep = time.sleep


def _retryable(exc):
    """True for a timeout/hang or a 400/500 answer: worth one more try, unlike a refusal."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (400, 500)
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TimeoutError)
    return isinstance(exc, TimeoutError)


def _score_once(url, image_path, timeout, tiles, **kwargs):
    """``(result, transport_failed)`` of a scoring request, retried once when it makes sense.

    See :func:`_score_attempt`; the retry covers timeouts and 400/500 answers only (not
    refused connections or gateway errors, which failover handles) and only while the
    time budget allows.
    """
    started = time.monotonic()
    result, failed, exc = _score_attempt(url, image_path, timeout, tiles, **kwargs)
    if result is not None or exc is None or not _retryable(exc):
        return result, failed
    remaining = timeout * RETRY_BUDGET_FACTOR - (time.monotonic() - started)
    remaining -= RETRY_BACKOFF_SECONDS
    if remaining < RETRY_MIN_SECONDS:
        return result, failed
    log.info("scorer: retrying once after %s", type(exc).__name__)
    _sleep(RETRY_BACKOFF_SECONDS)
    result, failed, _exc = _score_attempt(url, image_path, min(timeout, remaining), tiles,
                                          **kwargs)
    return result, failed


def _score_attempt(url, image_path, timeout, tiles, *, source_id=None, ignore_zones=(),
                   alert_zones=(), alert_zones_mode="enforce", token=None, max_person_box=None,
                   ignore_boxes=(), ignore_boxes_mode="enforce", faces=False):
    """``(result, transport_failed, exc)`` of one request; result None on any failure.

    ``exc`` is the request exception when the request itself failed, else None.
    """
    try:
        with open(image_path, "rb") as f:
            body = f.read()
    except OSError:
        log.warning("scorer: cannot read %s", image_path)
        return None, False, None
    if tiles and int(tiles) > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}tiles={int(tiles)}"
    if faces:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}faces=1"
    headers = {"Content-Type": "image/jpeg"}
    if isinstance(source_id, str) and len(source_id) in range(16, 65):
        normalized_source_id = source_id.lower()
        if all(character in "0123456789abcdef" for character in normalized_source_id):
            headers["X-Tapo-Source-ID"] = normalized_source_id
    headers.update(auth_headers(token))
    req = urllib.request.Request(url, data=body, headers=headers)
    keep_auth_off_redirects(req)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            result = json.load(resp)
            if not isinstance(result, dict):
                log.warning("scorer returned a non-object JSON response")
                return None, False, None
            if ignore_zones:
                result = apply_ignore_zones(result, ignore_zones)
            if ignore_boxes and ignore_boxes_mode == "observe":
                result = observe_ignore_boxes(result, ignore_boxes)
            elif ignore_boxes:
                result = apply_ignore_boxes(result, ignore_boxes)
            if max_person_box:
                result = apply_max_person_box(result, max_person_box)
            if alert_zones and alert_zones_mode == "observe":
                result = observe_alert_zones(result, alert_zones)
            elif alert_zones:
                result = apply_vehicle_alert_zones(apply_alert_zones(result, alert_zones),
                                                   alert_zones)
            return result, False, None
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - any transport/parse failure means "unavailable"
        if _unauthorized(e):
            _warn_unauthorized(url, bool(token))
            return None, False, None
        log.warning("scorer request failed: %s", e)
        return None, _transport_failure(e), e


# -- failover between interchangeable scoring services ----------------------------------
#
# ``scorer.url`` may be a list of candidates. Unlike cameras, scorers are interchangeable,
# so the monitor keeps ONE active URL per candidate list (every camera listing the same
# candidates shares it), moves on after FAIL_THRESHOLD consecutive transport failures to the
# first candidate whose /health answers, and goes back to the first (primary) candidate as
# soon as its /health answers again, checked at most every PRIMARY_RECHECK_SECONDS. All of
# it happens inside :func:`score`. That path is reactive: the first requests after an outage
# each wait out their timeout. :func:`start_probes` adds an opt-in background probe that
# checks every candidate's /health outside of scoring, so a dead candidate is skipped
# before a real frame has to find out.

FAIL_THRESHOLD = 3
PRIMARY_RECHECK_SECONDS = 300
HEALTH_TIMEOUT = 2.0
PROBE_INTERVAL = 30.0       # seconds between background /health rounds
PROBE_FAILS = 2             # consecutive failed rounds before a candidate counts as down

_clock = time.monotonic
_pools: dict[tuple, "ScorerPool"] = {}
_pools_lock = threading.Lock()
_listeners: list = []
_health_listeners: list = []
_probe_stop = threading.Event()


def health_url(url):
    """The ``/health`` address of the service that serves scoring ``url``."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path
    path = (path[:-len("/score")] if path.endswith("/score") else path.rstrip("/")) + "/health"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _health_ok(url, timeout=HEALTH_TIMEOUT):
    """True when the service behind scoring ``url`` answers GET /health with 2xx."""
    try:
        with urllib.request.urlopen(health_url(url), timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001 - any failure means "not alive"
        return False


class ScorerPool:
    """The active URL of one candidate list and the streak that decides when to move."""

    def __init__(self, urls):
        self.urls = tuple(urls)
        self.index = 0
        self.fails = 0
        self.next_primary_check = 0.0
        self.lock = threading.Lock()
        self.alive = {url: True for url in self.urls}
        self.probe_fails = {url: 0 for url in self.urls}
        self.probe_thread = None

    @property
    def active(self):
        return self.urls[self.index]

    def record(self, url, transport_failed):
        """Count one request's outcome made against ``url`` (stale answers are ignored)."""
        with self.lock:
            if url != self.active:
                return
            if not transport_failed:
                self.fails = 0
                return
            self.fails += 1
            if self.fails < FAIL_THRESHOLD:
                return
            self.fails = 0        # a dead fleet is re-probed after N more failures, not per call
            index = self.index
            others = [(step, self.urls[(index + step) % len(self.urls)])
                      for step in range(1, len(self.urls))]
        # Health probes block on the network: never hold the lock (and every other
        # request's record()) while they run.
        for step, candidate_url in others:
            if _health_ok(candidate_url):
                with self.lock:
                    if self.index == index:      # nobody switched while we probed
                        self._switch((index + step) % len(self.urls), "unreachable")
                return

    def maybe_return_to_primary(self):
        """Back to the first candidate once its /health answers; checked every 5 minutes."""
        if self.index == 0 or not self.lock.acquire(blocking=False):
            return       # nothing to recover, or another request is already checking
        try:
            now = _clock()
            if self.index == 0 or now < self.next_primary_check:
                return
            self.next_primary_check = now + PRIMARY_RECHECK_SECONDS
            if _health_ok(self.urls[0]):
                self._switch(0, "primary recovered")
        finally:
            self.lock.release()

    def probe_once(self):
        """One /health round over every candidate; moves to the first live one.

        A candidate needs PROBE_FAILS failed rounds in a row to count as down, one
        answer to count as up again. Down/up transitions go to the health listeners.
        When none answers the active URL is left alone: there is nothing better to use.
        """
        results = {url: _health_ok(url) for url in self.urls}
        changed = []
        with self.lock:
            for url, ok in results.items():
                if ok:
                    self.probe_fails[url] = 0
                    if not self.alive[url]:
                        self.alive[url] = True
                        changed.append((url, True))
                else:
                    self.probe_fails[url] += 1
                    if self.alive[url] and self.probe_fails[url] >= PROBE_FAILS:
                        self.alive[url] = False
                        changed.append((url, False))
            target = next((i for i, url in enumerate(self.urls) if self.alive[url]), None)
            if target is not None and target != self.index:
                self._switch(target, "health probe")
        for url, healthy in changed:
            log.warning("scorer candidate %s: %s", health_url(url),
                        "answers again" if healthy else "not answering /health")
            log.info("audit path=scorer action=scorer_health etype=system start=%d "
                     "reason=%s", int(time.time()), "up" if healthy else "down")
            for listener in list(_health_listeners):
                try:
                    listener(url, healthy)
                except Exception:  # noqa: BLE001 - a notice must never break probing
                    log.warning("scorer health listener failed", exc_info=True)

    def _probe_loop(self, interval):
        while not _probe_stop.is_set():
            try:
                self.probe_once()
            except Exception:  # noqa: BLE001 - probing is best effort
                log.warning("scorer probe failed", exc_info=True)
            if _probe_stop.wait(interval):
                return

    def start_probe(self, interval=PROBE_INTERVAL):
        with self.lock:
            if self.probe_thread is not None and self.probe_thread.is_alive():
                return
            self.probe_thread = threading.Thread(
                target=self._probe_loop, args=(interval,), daemon=True, name="scorer-probe")
            self.probe_thread.start()

    def _switch(self, index, reason):
        old, self.index, self.fails = self.index, index, 0
        self.next_primary_check = _clock() + PRIMARY_RECHECK_SECONDS
        log.warning("scorer switch: %s -> %s (%s)", self.urls[old], self.urls[index], reason)
        log.info("audit path=scorer action=scorer_switch etype=system start=%d reason=%s "
                 "scorer_index=%d", int(time.time()), reason.replace(" ", "_"), index)
        for listener in list(_listeners):
            try:
                listener(self.urls[0], index)
            except Exception:  # noqa: BLE001 - persistence must never break scoring
                log.warning("scorer switch listener failed", exc_info=True)


def pool_for(scorer_cfg):
    """The shared :class:`ScorerPool` of a config with several candidates, else None."""
    urls = tuple(getattr(scorer_cfg, "urls", ()) or ())
    if len(urls) < 2:
        return None
    with _pools_lock:
        pool = _pools.get(urls)
        if pool is None:
            pool = _pools[urls] = ScorerPool(urls)
        return pool


def active_url(scorer_cfg):
    """The one scoring URL to use for this config right now (None when unconfigured).

    The single read point for ``scorer.url``: ``cfg.url`` is the configured primary, a
    config whose ``url`` is a list may have failed over to another candidate at runtime.
    """
    pool = pool_for(scorer_cfg)
    return pool.active if pool else scorer_cfg.url


def score(scorer_cfg, image_path, **kwargs):
    """:func:`score_image` against the config's active URL, with failover. None on failure.

    ``kwargs`` are those of :func:`score_image` (``timeout`` defaults to the config's).
    """
    kwargs.setdefault("timeout", scorer_cfg.timeout)
    kwargs.setdefault("tiles", 1)
    kwargs.setdefault("token", resolve_token(getattr(scorer_cfg, "token_env", None)))
    pool = pool_for(scorer_cfg)
    if pool is None:
        return score_image(scorer_cfg.url, image_path, **kwargs)
    pool.maybe_return_to_primary()
    url = pool.active
    result, transport_failed = _score_once(url, image_path, **kwargs)
    pool.record(url, transport_failed)
    return result


def start_probes(scorer_cfgs, interval=PROBE_INTERVAL):
    """Probe every candidate list that has alternatives in the background (opt-in)."""
    _probe_stop.clear()
    for scorer_cfg in scorer_cfgs:
        pool = pool_for(scorer_cfg)
        if pool is not None:
            pool.start_probe(interval)


def add_health_listener(callback):
    """Call ``callback(url, healthy)`` when a candidate goes down or comes back."""
    _health_listeners.append(callback)


def add_switch_listener(callback):
    """Call ``callback(primary_url, index)`` after every switch (to persist it)."""
    _listeners.append(callback)


def restore_active(scorer_cfgs, active):
    """Re-apply persisted choices ``{primary_url: index}`` that are still valid candidates."""
    for scorer_cfg in scorer_cfgs:
        pool = pool_for(scorer_cfg)
        index = active.get(pool.urls[0]) if pool else None
        if isinstance(index, int) and 0 <= index < len(pool.urls):
            pool.index = index


def reset_pools():
    """Forget every pool and listener (tests; a fresh process starts on the primaries)."""
    _probe_stop.set()
    with _pools_lock:
        _pools.clear()
    _listeners.clear()
    _health_listeners.clear()


# A person counts as inside an ignore zone when at least this share of its box is.
IGNORE_ZONE_OVERLAP = 0.8
_warned_no_boxes = False
_warned_no_boxes_max = False
_warned_no_boxes_ignore = False
# A person is "that static box" when its box overlaps an ignore box with at least this IoU.
IGNORE_BOX_IOU = 0.75


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


def zones_for_lens(scorer_cfg, lens):
    """``ignore_zones`` plus the rects of ``lens`` from ``lens_ignore_zones``. Pure.

    A frame without a known lens (SD card, recording) gets the global zones only.
    """
    zones = scorer_cfg.ignore_zones
    per_lens = getattr(scorer_cfg, "lens_ignore_zones", None)
    if lens and per_lens:
        extra = per_lens.get(str(lens).replace("/", "_"))
        if extra:
            return tuple(zones) + tuple(extra)
    return zones


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
    return _without_persons(result, kept, len(pairs) - len(kept), "ignored_persons")


def _without_persons(result, kept, dropped, counter):
    """Copy of ``result`` that lists only ``kept`` ``[(score, box)]``, ``dropped`` counted. Pure."""
    out = dict(result)
    out[counter] = dropped
    out["person_scores"] = [sc for sc, _ in kept]
    out["person_boxes"] = [bx for _, bx in kept]
    if kept:
        best, best_box = kept[0]
        if best < float(result.get("person", 0.0)):
            out["person"], out["box"] = best, best_box
    else:
        # Everything the service listed was dropped; anything left is below its floor.
        out["person"], out["box"] = 0.0, None
    if isinstance(out.get("classes"), dict) and "person" in out["classes"]:
        out["classes"] = {**out["classes"], "person": out["person"]}
    return out


def apply_max_person_box(result, fraction):
    """Drop the people whose box is larger than ``fraction`` of the frame both ways. Pure-ish.

    The model now and then boxes a whole frame (width and height >= ~95 %, score ~0.5) or
    a huge static object as a person. Same mechanism as :func:`apply_ignore_zones`: the
    dropped ones are counted in ``oversized_persons`` and ``person`` becomes the best one
    left. A box must exceed ``fraction`` in width AND height; a wide-but-short or
    tall-but-narrow box stays. Needs ``person_boxes``: an older service is left as it was
    (logged once).
    """
    global _warned_no_boxes_max
    scores, boxes = result.get("person_scores"), result.get("person_boxes")
    w, h = result.get("w"), result.get("h")
    if (not isinstance(scores, list) or not isinstance(boxes, list)
            or len(scores) != len(boxes) or not w or not h):
        if not _warned_no_boxes_max:
            log.warning("scorer: max_person_box needs a scorer that returns person_boxes; "
                        "update the scoring service")
            _warned_no_boxes_max = True
        return result
    try:
        pairs = [(float(sc), [float(v) for v in bx]) for sc, bx in zip(scores, boxes, strict=True)]
        w, h, fraction = float(w), float(h), float(fraction)
        kept = [(sc, bx) for sc, bx in pairs
                if not ((bx[2] - bx[0]) / w > fraction and (bx[3] - bx[1]) / h > fraction)]
    except (TypeError, ValueError, IndexError):
        return result
    if len(kept) == len(pairs):
        return result
    return _without_persons(result, kept, len(pairs) - len(kept), "oversized_persons")


def _iou(box, rect, w, h):
    """Intersection over union of a pixel ``box`` and a ``rect`` in frame fractions. Pure."""
    x1, y1, x2, y2 = box
    rx1, ry1, rx2, ry2 = rect[0] * w, rect[1] * h, rect[2] * w, rect[3] * h
    ix = max(min(x2, rx2) - max(x1, rx1), 0.0)
    iy = max(min(y2, ry2) - max(y1, ry1), 0.0)
    inter = ix * iy
    union = max(x2 - x1, 0.0) * max(y2 - y1, 0.0) + (rx2 - rx1) * (ry2 - ry1) - inter
    return inter / union if union > 0 else 0.0


def apply_ignore_boxes(result, rects, iou=IGNORE_BOX_IOU):
    """Drop the people whose box IS one of ``rects`` (size and place). Pure-ish.

    For a static object the model boxes as one big person (a swing roof, 0.44 x 0.43 of the
    frame) where an ignore zone would also hide a real person standing in front of it: that
    person's box is small and lies inside the zone. A rect here removes only a box that
    matches it with an IoU of at least ``iou``, so a person in front, or anywhere else,
    still counts. Counted in ``ignored_boxes``; ``person`` becomes the best one left. Needs
    ``person_boxes``: an older service is left as it was (logged once).
    """
    global _warned_no_boxes_ignore
    scores, boxes = result.get("person_scores"), result.get("person_boxes")
    w, h = result.get("w"), result.get("h")
    if (not isinstance(scores, list) or not isinstance(boxes, list)
            or len(scores) != len(boxes) or not w or not h):
        if not _warned_no_boxes_ignore:
            log.warning("scorer: ignore_boxes needs a scorer that returns person_boxes; "
                        "update the scoring service")
            _warned_no_boxes_ignore = True
        return result
    try:
        pairs = [(float(sc), [float(v) for v in bx]) for sc, bx in zip(scores, boxes, strict=True)]
        w, h = float(w), float(h)
        kept = [(sc, bx) for sc, bx in pairs if not any(_iou(bx, r, w, h) >= iou for r in rects)]
    except (TypeError, ValueError, IndexError):
        return result
    if len(kept) == len(pairs):
        return result
    return _without_persons(result, kept, len(pairs) - len(kept), "ignored_boxes")


def observe_ignore_boxes(result, rects):
    """Report what :func:`apply_ignore_boxes` would do without doing it. Pure-ish.

    The answer is returned unfiltered; when a box would be dropped it gains
    ``ignore_box_observe`` = ``{"dropped", "person"}`` (boxes that would go, and the best
    person score left after the filter).
    """
    filtered = apply_ignore_boxes(result, rects)
    if filtered is result:
        return result
    out = dict(result)
    out["ignore_box_observe"] = {"dropped": filtered["ignored_boxes"],
                                 "person": filtered["person"]}
    return out


def point_in_polygon(x, y, polygon):
    """Even-odd ray casting; a point on an edge or vertex counts as inside. Pure."""
    n = len(polygon)
    inside = False
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if (abs(cross) < 1e-12 and min(x1, x2) - 1e-12 <= x <= max(x1, x2) + 1e-12
                and min(y1, y2) - 1e-12 <= y <= max(y1, y2) + 1e-12):
            return True
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _foot(box, w, h):
    """Bottom centre of a pixel box as frame fractions, clamped to the frame. Pure.

    The model can place a box edge a little past the frame (a person cut off at the bottom
    edge); a zone cannot reach beyond 1.0, so the foot is clamped instead of falling outside.
    """
    return (min(max((box[0] + box[2]) / 2 / w, 0.0), 1.0), min(max(box[3] / h, 0.0), 1.0))


def apply_alert_zones(result, zones):
    """Keep only the people standing in an alert zone. Pure-ish (warns once).

    The opposite of :func:`apply_ignore_zones`: a zone is a polygon (``[(x, y), ...]``
    as fractions of the frame) and a person counts only when the bottom centre of its
    box (the feet) lies in at least one zone. Run it after the ignore zones. The
    result is reshaped the same way (``person`` the best remaining one, ``box``,
    ``person_scores``, ``person_boxes``; ``outside_alert_zones`` counts the dropped).
    An older service without ``person_boxes`` is left as it was.
    """
    global _warned_no_boxes
    scores, boxes = result.get("person_scores"), result.get("person_boxes")
    w, h = result.get("w"), result.get("h")
    if (not isinstance(scores, list) or not isinstance(boxes, list)
            or len(scores) != len(boxes) or not w or not h):
        if not _warned_no_boxes:
            log.warning("scorer: zones need a scorer that returns person_boxes; "
                        "update the scoring service")
            _warned_no_boxes = True
        return result
    try:
        pairs = [(float(sc), [float(v) for v in bx]) for sc, bx in zip(scores, boxes, strict=True)]
        w, h = float(w), float(h)
        feet = [_foot(bx, w, h) for _, bx in pairs]
    except (TypeError, ValueError, IndexError):
        return result
    kept = [pair for pair, (fx, fy) in zip(pairs, feet, strict=True)
            if any(point_in_polygon(fx, fy, z) for z in zones)]
    if len(kept) == len(pairs):
        return result
    out = dict(result)
    out["outside_alert_zones"] = len(pairs) - len(kept)
    out["person_scores"] = [sc for sc, _ in kept]
    out["person_boxes"] = [bx for _, bx in kept]
    if kept:
        out["person"], out["box"] = kept[0]
    else:
        out["person"], out["box"] = 0.0, None
    if isinstance(out.get("classes"), dict) and "person" in out["classes"]:
        out["classes"] = {**out["classes"], "person": out["person"]}
    return out


def observe_alert_zones(result, zones):
    """Report what :func:`apply_alert_zones` would do without doing it. Pure-ish.

    The answer is returned unfiltered. When the zones would remove at least one person it
    gains ``alert_zone_observe`` = ``{"inside", "outside", "person"}`` (people kept and
    dropped, and the best person score left after the filter). Vehicles are not looked at.
    """
    filtered = apply_alert_zones(result, zones)
    if filtered is result:
        return result
    out = dict(result)
    out["alert_zone_observe"] = {"inside": len(filtered["person_scores"]),
                                 "outside": filtered["outside_alert_zones"],
                                 "person": filtered["person"]}
    return out


def apply_vehicle_alert_zones(result, zones):
    """Keep only the vehicles standing in an alert zone. Pure.

    Same rule as :func:`apply_alert_zones`, for vehicles: one counts when the bottom centre
    of its box lies in at least one zone. ``vehicle`` becomes the best remaining score
    (0.0 when none), ``vehicle_scores``/``vehicle_boxes`` the remaining ones and
    ``outside_alert_zones_vehicles`` counts the dropped. A vehicle alert is opt-in, so a
    vehicle that cannot be placed (a service without ``vehicle_boxes``, malformed boxes)
    scores 0 here rather than passing unchecked. A frame without vehicles is returned as is.
    """
    if vehicle_score(result) <= 0.0:
        return result
    scores, boxes = result.get("vehicle_scores"), result.get("vehicle_boxes")
    w, h = result.get("w"), result.get("h")
    kept = None
    if (isinstance(scores, list) and isinstance(boxes, list) and len(scores) == len(boxes)
            and w and h):
        try:
            pairs = [(float(sc), [float(v) for v in bx])
                     for sc, bx in zip(scores, boxes, strict=True)]
            w, h = float(w), float(h)
            kept = [(sc, bx) for sc, bx in pairs
                    if any(point_in_polygon(*_foot(bx, w, h), z) for z in zones)]
        except (TypeError, ValueError, IndexError):
            kept = None
    if kept is not None and len(kept) == len(pairs):
        return result
    out = dict(result)
    if kept is None:
        out["vehicle"], out["vehicle_scores"], out["vehicle_boxes"] = 0.0, [], []
        return out
    out["outside_alert_zones_vehicles"] = len(pairs) - len(kept)
    out["vehicle_scores"] = [sc for sc, _ in kept]
    out["vehicle_boxes"] = [bx for _, bx in kept]
    out["vehicle"] = max(out["vehicle_scores"], default=0.0)
    return out


class SubjectScore(float):
    """Person score with animal confidence retained for audit telemetry.

    ``persons`` is how many people the frame holds at or above the caller's threshold
    (see :func:`person_count`), or None when the scorer did not report it; ``vehicle`` is
    the best vehicle confidence (0.0 from an older service).
    """

    def __new__(cls, person, animal, persons=None, vehicle=0.0, zone_observe=None,
                box_observe=None):
        value = float.__new__(cls, person)
        value.person = person
        value.animal = animal
        value.persons = persons
        value.vehicle = vehicle      # best vehicle confidence (0.0 = none / older service)
        # ``(inside, outside)`` people when observe-mode alert zones would have dropped
        # this frame's person below the threshold, else None.
        value.zone_observe = zone_observe
        # Number of boxes observe-mode ``ignore_boxes`` would have dropped from a frame whose
        # person that leaves under the threshold, else None.
        value.box_observe = box_observe
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


def vehicle_score(result):
    """Vehicle confidence from a response, 0.0 when absent or malformed. Pure.

    Older scoring services send no ``vehicle`` field; that must read as "no vehicle",
    never as an error, and a bad value never raises.
    """
    try:
        score = float(result.get("vehicle", 0.0)) if isinstance(result, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) and 0.0 <= score <= 1.0 else 0.0


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
    return SubjectScore(*values, persons=person_count(result, threshold),
                        vehicle=vehicle_score(result),
                        zone_observe=_zone_observe(result, values[0], threshold),
                        box_observe=_box_observe(result, values[0], threshold))


def _box_observe(result, person, threshold):
    """Count of boxes observe ``ignore_boxes`` would drop, if that pushes the frame under."""
    info = result.get("ignore_box_observe")
    if threshold is None or not isinstance(info, dict) or person < threshold:
        return None
    try:
        dropped, kept = int(info["dropped"]), float(info["person"])
    except (KeyError, TypeError, ValueError):
        return None
    return dropped if kept < threshold and dropped > 0 else None


def _zone_observe(result, person, threshold):
    """``(inside, outside)`` when observe zones would push a person frame under ``threshold``."""
    info = result.get("alert_zone_observe")
    if threshold is None or not isinstance(info, dict) or person < threshold:
        return None
    try:
        inside, outside, kept = int(info["inside"]), int(info["outside"]), float(info["person"])
    except (KeyError, TypeError, ValueError):
        return None
    return (inside, outside) if kept < threshold else None


def frame_faces(result):
    """Face boxes ``[[x1, y1, x2, y2, score], ...]`` from an answer, or None when unknown.

    None (not asked, no face model on the service, or the detector failed) is different
    from ``[]`` (looked, found none): only a known answer may steer the frame pick. Pure.
    """
    faces = result.get("faces") if isinstance(result, dict) else None
    if not isinstance(faces, list):
        return None
    return [f for f in faces if isinstance(f, (list, tuple)) and len(f) >= 4]


def subject_box(result):
    """Winning person's ``[x1, y1, x2, y2]`` (original px) from a response, or None. Pure."""
    box = result.get("box") if isinstance(result, dict) else None
    if not box or len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except (TypeError, ValueError):
        return None
