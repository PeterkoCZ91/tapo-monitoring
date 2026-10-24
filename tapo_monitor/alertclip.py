"""Opt-in alert clip: a short video of the event, sent as a reply to its alert photo.

The photo says *someone was here*; the clip shows what they did. With ``clip_video: true``
every incident whose photo reached Telegram gets one card read of the window
``[start - clip_pre_roll, start + clip_seconds]``, transcoded to H.264 at ``clip_height``
lines (any phone plays it, a 30 s clip is a few MB) and sent with ``sendVideo`` as a reply
to the photo.

The clip is never the alert. It is due once the window has cleared pytapo's freshness
guard (about 100 s after the event with the defaults), runs on the camera's SD worker
thread after any SD follow-up of that camera (they share the card, and an alert must not
wait behind a clip), and is tried at most twice. The queue lives in memory: a restart
loses the clips not yet sent, never an alert.

Only the card read holds the camera's card thread. The transcode (about 27 s on a Pi 4)
and the upload run on the queue's own sender thread, so an SD follow-up that comes due
meanwhile waits for a download at most, never for an encode; the loop spends neither.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import notify, sdclip, sdworker

log = logging.getLogger(__name__)

# Seconds past the window's end on top of the freshness guard, as the card follow-up uses.
DUE_MARGIN = 9
# An entry older than this (from the event start) is dropped: the card has long moved on
# and a clip that late answers nobody's question.
MAX_AGE = 900
# A failed read or send is tried once more after this pause.
RETRY_DELAY = 60
# Telegram's bot upload limit is 50 MB; stay well under it.
MAX_BYTES = 45 * 1024 * 1024
# Transcode budget: 27 s for a real 30 s 1440p card clip on a Pi 4 (measured 2026-10-24).
TRANSCODE_TIMEOUT = 180
# Incidents remembered so a later photo of the same incident does not queue a second clip.
SEEN_LIMIT = 500


def start_from_incident(incident):
    """Event start from an incident id ``<camera>-<start>``, or None. Pure."""
    if not incident or "-" not in str(incident):
        return None
    tail = str(incident).rsplit("-", 1)[1]
    return int(tail) if tail.isdigit() else None


def due_at(cfg, start):
    """When the clip window has cleared the card's freshness guard. Pure."""
    return start + cfg.clip_seconds + cfg.sd_fresh_guard + DUE_MARGIN


def caption(photo_caption):
    """The clip's caption: the photo's, marked as video. Pure."""
    return f"🎬 {photo_caption}" if photo_caption else "🎬"


def transcode_args(src, dst, *, start_offset, seconds, height):
    """ffmpeg argv: ``seconds`` from ``start_offset`` of ``src`` as H.264 ``height``p. Pure.

    The first video stream only (a two-lens card file carries both lenses), audio when the
    file has any. x264 veryfast at CRF 28 kept a 30 s 720p clip near 3 MB on a Pi 4, smaller
    and barely slower than the hardware encoder, whose input still decodes in software.
    """
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file",
            "-ss", str(max(int(start_offset), 0)), "-i", src, "-t", str(int(seconds)),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:{int(height)}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k",
            "-movflags", "+faststart", dst]


def _run_ffmpeg(argv, timeout):  # pragma: no cover - real process
    subprocess.run(["nice", "-n", "19", *argv], check=True, timeout=timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _fetch_card(cfg, start, out_dir, pre_roll, seconds, guard):  # pragma: no cover - I/O
    return sdclip.fetch_sd_clip_subprocess(cfg, start, out_dir, pre_roll=pre_roll,
                                           seconds=seconds, guard=guard)


@dataclass
class ClipEntry:
    camera: str
    incident: str
    start: int
    reply_to: int
    caption: str
    due_at: float
    tries: int = 0
    submitted: bool = False


@dataclass
class ClipRead:
    """A card read waiting for its encode: the raw file, where it begins, its temp dir."""
    raw: str
    window_start: int | None
    job_dir: str


@dataclass
class ClipJob:
    """Worker context marking a finished job as a clip, not an SD follow-up."""
    entry: ClipEntry
    cfg: object = None
    secrets: dict | None = None


def read_clip(cfg, entry, *, work_dir=None, fetch=_fetch_card):
    """Read the clip window from the card. A :class:`ClipRead` or None. Card thread."""
    job_dir = tempfile.mkdtemp(prefix=sdworker.JOB_DIR_PREFIX + f"{os.getpid()}_clip_",
                               dir=work_dir)
    try:
        raw, window_start = fetch(cfg, entry.start, job_dir, cfg.clip_pre_roll,
                                  cfg.clip_seconds, cfg.sd_fresh_guard)
    except Exception as exc:  # noqa: BLE001 - a failed read must not end the worker
        log.warning("clip %s: card read failed for %s: %s", cfg.name, entry.incident, exc)
        raw, window_start = None, None
    if not raw:
        log.warning("clip %s: no card read for %s", cfg.name, entry.incident)
        shutil.rmtree(job_dir, ignore_errors=True)
        return None
    return ClipRead(raw, window_start, job_dir)


def deliver_clip(cfg, entry, read, secrets, *, transcode=_run_ffmpeg, send=None):
    """Transcode a read and send it as a reply. True when Telegram took it. Sender thread.

    The read's temp dir is removed whatever happens.
    """
    send = send or notify.send_video
    length = cfg.clip_pre_roll + cfg.clip_seconds
    try:
        out = os.path.join(read.job_dir, "clip.mp4")
        offset = (entry.start - cfg.clip_pre_roll) - (read.window_start or entry.start)
        try:
            transcode(transcode_args(read.raw, out, start_offset=offset, seconds=length,
                                     height=cfg.clip_height), timeout=TRANSCODE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a broken clip must never end the sender
            log.warning("clip %s: transcode failed for %s: %s", cfg.name, entry.incident, exc)
            return False
        size = os.path.getsize(out) if os.path.exists(out) else 0
        if not 0 < size <= MAX_BYTES:
            log.warning("clip %s: %d bytes for %s, not sent", cfg.name, size, entry.incident)
            return False
        ok = bool(send(secrets["telegram_token"], secrets["telegram_chat"], out,
                       caption(entry.caption), reply_to=entry.reply_to, duration=length))
        log.info("clip %s: %s %s (%d bytes)", cfg.name,
                 "sent" if ok else "not delivered", entry.incident, size)
        return ok
    finally:
        shutil.rmtree(read.job_dir, ignore_errors=True)


def run_job(cfg, entry, secrets, *, work_dir=None, fetch=_fetch_card,
            transcode=_run_ffmpeg, send=None):
    """Read, transcode and send one clip on the calling thread. True when sent."""
    read = read_clip(cfg, entry, work_dir=work_dir, fetch=fetch)
    if read is None:
        return False
    return deliver_clip(cfg, entry, read, secrets, transcode=transcode, send=send)


def _background(fn):
    _sender().submit(fn)


_executor = None
_executor_lock = threading.Lock()


def _sender():  # pragma: no cover - see _background
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-sender")
        return _executor


@dataclass
class ClipQueue:
    """Clips waiting for their card window, one per incident. Loop thread only.

    ``submit_send`` runs a delivery off the loop (one sender thread); its outcome comes
    back through a thread-safe list read on the next :meth:`drain`.
    """
    seen_limit: int = SEEN_LIMIT
    entries: list = field(default_factory=list)
    seen: OrderedDict = field(default_factory=OrderedDict)
    submit_send: object = _background
    _sent: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def note_alert(self, cfg, incident, *, message_id, caption, now):
        """Queue the clip of an incident whose photo was delivered. True when queued."""
        start = start_from_incident(incident)
        if not cfg.clip_video or start is None or not message_id or incident in self.seen:
            return False
        if now - start > MAX_AGE:
            return False
        self.seen[incident] = True
        while len(self.seen) > self.seen_limit:
            self.seen.popitem(last=False)
        self.entries.append(ClipEntry(cfg.name, incident, start, message_id, caption,
                                      due_at(cfg, start)))
        return True

    def drain(self, app, worker, *, now, secrets, read=read_clip):
        """Collect sends; submit due card reads to idle cameras; drop stale entries."""
        with self._lock:
            sent, self._sent = self._sent, []
        for entry, ok, error in sent:
            self._outcome(entry, ok, error, now)
        cameras = {c.name: c for c in app.cameras}
        kept = []
        for entry in self.entries:
            cfg = cameras.get(entry.camera)
            if not entry.submitted and (cfg is None or now - entry.start > MAX_AGE):
                log.info("clip %s: dropped %s (%s)", entry.camera, entry.incident,
                         "camera gone" if cfg is None else "too old")
                continue
            kept.append(entry)
            if entry.submitted or now < entry.due_at or worker.busy(entry.camera):
                continue
            entry.submitted = True
            worker.submit(entry.camera, ("clip", entry.incident),
                          lambda cfg=cfg, entry=entry: read(cfg, entry),
                          context=ClipJob(entry, cfg, secrets))
        self.entries = kept

    def finish(self, job, *, value, error, now, deliver=None):
        """Collect a card read: hand it to the sender, or count a failed try."""
        deliver = deliver or deliver_clip
        if isinstance(value, ClipRead):
            entry, cfg, secrets = job.entry, job.cfg, job.secrets

            def send():
                try:
                    ok, err = deliver(cfg, entry, value, secrets), None
                except Exception as exc:  # noqa: BLE001 - reported back, never raised
                    ok, err = False, exc
                with self._lock:
                    self._sent.append((entry, ok, err))

            self.submit_send(send)
            return
        self._outcome(job.entry, False, error, now)

    def _outcome(self, entry, ok, error, now):
        entry.submitted = False
        if ok:
            self._remove(entry)
            return
        entry.tries += 1
        if entry.tries >= 2:
            log.warning("clip %s: giving up on %s%s", entry.camera, entry.incident,
                        f" ({error})" if error else "")
            self._remove(entry)
            return
        entry.due_at = now + RETRY_DELAY

    def _remove(self, entry):
        self.entries = [e for e in self.entries if e is not entry]
