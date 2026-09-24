"""Local frame-labeling tool: ground truth for the alert frames the fleet kept.

The sent log keeps what went out and the review log what corroboration held back
(:mod:`tapo_monitor.sentlog`), but neither says whether a person was really in the
frame. ``tapo-monitor label <dataset_dir>`` serves one page that shows those frames
one at a time and records a human verdict; ``tapo-monitor label-stats`` turns the
verdicts into a false-alarm rate, a miss count and the threshold that would have
separated the labeled frames best.

The dataset is whatever was copied off the hosts: every ``index.jsonl`` under
``dataset_dir`` (searched recursively) is read, a record with a ``verdict`` is a review
frame, anything else a sent frame. Images are only ever read. Verdicts are appended to
``<dataset_dir>/labels.jsonl`` keyed by the image's SHA-256, so the same frame copied
twice or re-collected later keeps its label; the latest line per hash wins, and undo is
one more line, never an edit.

The server binds ``127.0.0.1`` by default: it has no authentication, so serving it
beyond the host is the operator's explicit choice, as with the status endpoint. Only
frames listed in an index and resolving inside ``dataset_dir`` are ever served.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import random
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

INDEX_NAME = "index.jsonl"
LABELS_NAME = "labels.jsonl"
# Teacher-model person scores written by tapo_monitor.autolabel, keyed by image hash.
TEACHER_NAME = "teacher.jsonl"
LABELS = ("person", "no_person", "unsure")
UNLABELED = "unlabeled"
GRAY_LOW = 0.30
GRAY_HIGH = 0.65
BANDS = ("gray", "low", "high", "none")
DEFAULT_PORT = 8791
DEFAULT_BIND = "127.0.0.1"


@dataclass(frozen=True)
class Frame:
    path: str            # relative to dataset_dir, '/'-separated
    sha256: str
    source: str          # "sent" or "review"
    camera: str | None
    ts: float | None
    score: float | None  # person score, when the record carried one


def band(score):
    """Score band the queue and the stats are ordered by. Pure."""
    if score is None:
        return "none"
    if score < GRAY_LOW:
        return "low"
    if score > GRAY_HIGH:
        return "high"
    return "gray"


def _inside(root_real, path):
    real = os.path.realpath(path)
    return real != root_real and real.startswith(root_real + os.sep)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_frames(dataset_dir):
    """Every indexed JPEG under ``dataset_dir``, one per image hash. Read-only.

    Records whose file is missing, unreadable, not a JPEG name or resolving outside
    the dataset (``..``, absolute names, symlinks) are skipped silently: a collected
    dataset is expected to have gaps where retention pruned a frame.
    """
    root_real = os.path.realpath(dataset_dir)
    frames = []
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames.sort()
        if INDEX_NAME not in filenames:
            continue
        for record in _read_jsonl(os.path.join(dirpath, INDEX_NAME)):
            name = record.get("file")
            if not isinstance(name, str) or not name.lower().endswith((".jpg", ".jpeg")):
                continue
            full = os.path.join(dirpath, name)
            if not _inside(root_real, full) or not os.path.isfile(full):
                continue
            try:
                sha = _sha256(full)
            except OSError:
                continue
            if sha in seen:
                continue
            seen.add(sha)
            rel = os.path.relpath(os.path.realpath(full), root_real).replace(os.sep, "/")
            camera = record.get("camera")
            frames.append(Frame(
                path=rel, sha256=sha,
                source="review" if "verdict" in record else "sent",
                camera=str(camera) if camera else None,
                ts=_number(record.get("ts")),
                score=_number(record.get("person")),
            ))
    return frames


def latest_labels(dataset_dir):
    """``{sha256: latest label record}``; an undo back to unlabeled drops the hash."""
    latest = {}
    for record in _read_jsonl(os.path.join(dataset_dir, LABELS_NAME)):
        sha = record.get("sha256")
        if isinstance(sha, str):
            latest[sha] = record
    return {sha: rec for sha, rec in latest.items() if rec.get("label") in LABELS}


def teacher_scores(dataset_dir):
    """``{sha256: teacher person score}`` from ``teacher.jsonl``; latest line wins."""
    scores = {}
    for record in _read_jsonl(os.path.join(dataset_dir, TEACHER_NAME)):
        sha, person = record.get("sha256"), _number(record.get("person"))
        if isinstance(sha, str) and person is not None:
            scores[sha] = person
    return scores


def build_queue(frames, labeled, *, seed=None, low_sample=None, teacher=None):
    """Unlabeled frames in review order. Pure apart from the seeded shuffle.

    Frames a teacher model has scored come first, biggest disagreement with the
    production score first: after :mod:`tapo_monitor.autolabel` has labeled every
    frame both models agree on, what is left is exactly where one of them is wrong.
    Then the gray zone (the threshold's own uncertainty), a random sample of low scores
    (possible misses), high scores (possible false alarms, nearest the gray zone first)
    and frames without a score.
    """
    rng = random.Random(seed)
    teacher = teacher or {}
    disputed = []
    by_band = {name: [] for name in BANDS}
    for frame in frames:
        if frame.sha256 in labeled:
            continue
        if frame.sha256 in teacher:
            disputed.append(frame)
        else:
            by_band[band(frame.score)].append(frame)
    disputed.sort(key=lambda f: (-abs(teacher[f.sha256] - (0.5 if f.score is None
                                                           else f.score)), f.path))
    by_band["gray"].sort(key=lambda f: (f.ts or 0.0, f.path))
    low = sorted(by_band["low"], key=lambda f: f.path)
    rng.shuffle(low)
    if low_sample is not None:
        low = low[:max(0, int(low_sample))]
    by_band["high"].sort(key=lambda f: (f.score, f.path))
    by_band["none"].sort(key=lambda f: (f.ts or 0.0, f.path))
    return disputed + by_band["gray"] + low + by_band["high"] + by_band["none"]


class LabelSession:
    """One labeling run over a dataset: the queue, the label file, the undo stack.

    Thread-safe: the HTTP server is threaded and every mutation holds one lock.
    """

    def __init__(self, dataset_dir, *, seed=None, low_sample=None):
        self.dataset_dir = dataset_dir
        self.root_real = os.path.realpath(dataset_dir)
        self.frames = {f.path: f for f in load_frames(dataset_dir)}
        self._labeled = latest_labels(dataset_dir)
        self._teacher = teacher_scores(dataset_dir)
        self._queue = build_queue(self.frames.values(), self._labeled,
                                  seed=seed, low_sample=low_sample, teacher=self._teacher)
        self._history: list[tuple] = []
        self._lock = threading.Lock()

    def pending(self):
        with self._lock:
            return list(self._queue)

    def next_frame(self):
        with self._lock:
            return self._queue[0] if self._queue else None

    def _frame(self, path):
        frame = self.frames.get(path)
        if frame is None:
            raise KeyError(path)
        return frame

    def _append(self, frame, label, now, *, undo=False):
        record = {"path": frame.path, "sha256": frame.sha256, "label": label,
                  "labeled_at": float(time.time() if now is None else now),
                  "score": frame.score, "camera": frame.camera, "source": frame.source}
        if undo:
            record["undo"] = True
        with open(os.path.join(self.dataset_dir, LABELS_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def label(self, path, label, *, now=None):
        """Append one verdict for ``path`` and drop it from the queue."""
        if label not in LABELS:
            raise ValueError(f"unknown label {label!r}")
        with self._lock:
            frame = self._frame(path)
            record = self._append(frame, label, now)
            previous = self._labeled.get(frame.sha256)
            self._labeled[frame.sha256] = record
            position = next((i for i, f in enumerate(self._queue) if f.path == path), None)
            if position is not None:
                del self._queue[position]
            self._history.append(("label", frame, previous, position))
            return record

    def skip(self, path):
        """Move ``path`` to the back of the queue; nothing is written."""
        with self._lock:
            frame = self._frame(path)
            position = next((i for i, f in enumerate(self._queue) if f.path == path), None)
            if position is None:
                return
            del self._queue[position]
            self._queue.append(frame)
            self._history.append(("skip", frame, None, position))

    def undo(self, *, now=None):
        """Revert the last label or skip. A label undo is a new line, never an edit."""
        with self._lock:
            if not self._history:
                return None
            kind, frame, previous, position = self._history.pop()
            if kind == "skip":
                self._queue.remove(frame)
                self._queue.insert(position, frame)
                return frame
            restored = previous.get("label") if previous else UNLABELED
            record = self._append(frame, restored, now, undo=True)
            if previous:
                self._labeled[frame.sha256] = record
            else:
                self._labeled.pop(frame.sha256, None)
                # Back to the front: the operator pressed undo to look at it again.
                self._queue.insert(0, frame)
            return frame

    def state(self):
        with self._lock:
            frame = self._queue[0] if self._queue else None
            view = _frame_view(frame)
            if view is not None:
                view["teacher"] = self._teacher.get(frame.sha256)
            return {"frame": view, "remaining": len(self._queue),
                    "labeled": len(self._labeled), "can_undo": bool(self._history)}

    def image_path(self, rel):
        """Absolute path of an indexed frame inside the dataset, or None."""
        frame = self.frames.get(rel)
        if frame is None:
            return None
        full = os.path.join(self.root_real, *frame.path.split("/"))
        return full if _inside(self.root_real, full) and os.path.isfile(full) else None


def _frame_view(frame):
    if frame is None:
        return None
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frame.ts)) if frame.ts else None
    return {"path": frame.path, "camera": frame.camera, "ts": frame.ts, "time": when,
            "source": frame.source, "score": frame.score, "band": band(frame.score)}


# ── stats ────────────────────────────────────────────────────────────────────

def _rate(part, whole):
    return part / whole if whole else None


def _summarize(records):
    counts = {name: 0 for name in LABELS}
    fa_no = fa_decided = miss_yes = miss_decided = 0
    for rec in records:
        label = rec["label"]
        counts[label] += 1
        if label == "unsure":
            continue
        if rec.get("source") == "review":
            miss_decided += 1
            miss_yes += label == "person"
        else:
            fa_decided += 1
            fa_no += label == "no_person"
    counts["total"] = len(records)
    return {
        "counts": counts,
        "false_alarms": {"no_person": fa_no, "decided": fa_decided,
                         "rate": _rate(fa_no, fa_decided)},
        "misses": {"person": miss_yes, "decided": miss_decided,
                   "rate": _rate(miss_yes, miss_decided)},
    }


def best_threshold(pairs):
    """Threshold on ``score >= t`` with the fewest errors over ``(score, is_person)``.

    Candidates are the labeled scores themselves. Ties go to the lower threshold, i.e.
    fewer misses: a missed person costs more than one more photo to glance at.
    """
    pairs = [(float(s), bool(p)) for s, p in pairs]
    result = {"threshold": None, "errors": None, "false_alarms": None, "misses": None,
              "accuracy": None, "n": len(pairs)}
    if not pairs:
        return result
    best = None
    for t in sorted({s for s, _ in pairs}):
        fp = sum(1 for s, p in pairs if s >= t and not p)
        fn = sum(1 for s, p in pairs if s < t and p)
        if best is None or fp + fn < best[1] + best[2]:
            best = (t, fp, fn)
    assert best is not None
    t, fp, fn = best
    result.update(threshold=t, errors=fp + fn, false_alarms=fp, misses=fn,
                  accuracy=1 - (fp + fn) / len(pairs))
    return result


def compute_stats(dataset_dir):
    """Label counts, false-alarm and miss estimates, per band/camera, best threshold."""
    records = list(latest_labels(dataset_dir).values())
    for rec in records:
        rec["score"] = _number(rec.get("score"))
    stats = _summarize(records)
    stats["bands"] = {name: _summarize([r for r in records if band(r["score"]) == name])
                      for name in BANDS}
    cameras = sorted({str(r.get("camera") or "unknown") for r in records})
    stats["cameras"] = {cam: _summarize([r for r in records
                                         if str(r.get("camera") or "unknown") == cam])
                        for cam in cameras}
    auto = sum(1 for r in records if str(r.get("by") or "").startswith("auto"))
    stats["by"] = {"auto": auto, "human": len(records) - auto}
    stats["threshold"] = best_threshold(
        (r["score"], r["label"] == "person") for r in records
        if r["score"] is not None and r["label"] != "unsure")
    return stats


def _pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _stats_rows(stats):
    """``(name, labeled, person, no_person, unsure, false alarms, misses)`` rows."""
    rows = []
    groups = [("all", stats)]
    groups += [(f"band {name}", stats["bands"][name]) for name in BANDS]
    groups += [(f"camera {name}", block) for name, block in stats["cameras"].items()]
    for name, block in groups:
        c, fa, miss = block["counts"], block["false_alarms"], block["misses"]
        rows.append((name, str(c["total"]), str(c["person"]), str(c["no_person"]),
                     str(c["unsure"]),
                     f"{fa['no_person']}/{fa['decided']} ({_pct(fa['rate'])})",
                     f"{miss['person']}/{miss['decided']} ({_pct(miss['rate'])})"))
    return rows


STATS_HEADERS = ("group", "labeled", "person", "no_person", "unsure",
                 "false alarms (sent)", "misses (review)")


def _threshold_line(best):
    if best["threshold"] is None:
        return "best threshold: n/a (no decided frames with a score)"
    return (f"best threshold: person >= {best['threshold']:.2f} -> "
            f"{best['errors']} errors of {best['n']} ({best['false_alarms']} false alarms, "
            f"{best['misses']} misses, accuracy {_pct(best['accuracy'])})")


def format_stats(stats):
    rows = _stats_rows(stats)
    widths = [max(len(STATS_HEADERS[i]), *(len(r[i]) for r in rows))
              for i in range(len(STATS_HEADERS))]

    def line(values):
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

    out = [line(STATS_HEADERS), line(tuple("-" * w for w in widths))]
    out += [line(r) for r in rows]
    by = stats.get("by") or {}
    out += ["", f"labels: {by.get('human', 0)} by a person, {by.get('auto', 0)} automatic "
                "(both models agreed)",
            _threshold_line(stats["threshold"]),
            "false alarms = sent frames labeled no_person; misses = review (held) frames "
            "labeled person; unsure excluded"]
    return "\n".join(out)


# ── HTTP ─────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Frame labeling</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--muted:#9a9a94;
--card:#222220;--line:#333}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:8px 16px 16px}
#img{display:block;width:100%;max-height:72vh;object-fit:contain;background:#000;
border-radius:6px}
#meta{display:flex;flex-wrap:wrap;gap:4px 16px;margin:8px 0;color:var(--muted)}
#meta b{color:var(--fg)}
#buttons{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
#buttons2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
button{font:inherit;font-size:18px;padding:14px 8px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
button:disabled{opacity:.4}
kbd{font-size:12px;color:var(--muted)}
header{display:flex;justify-content:space-between;align-items:center;padding:8px 16px;
max-width:1100px;margin:0 auto}
a{color:inherit}
#done{display:none;padding:48px 0;text-align:center}
</style></head><body>
<header><span id="progress">loading...</span><a href="/stats">stats</a></header>
<main>
<div id="work">
<img id="img" alt="frame">
<div id="meta"><span>camera <b id="camera"></b></span><span id="time"></span>
<span>source <b id="source"></b></span><span>person <b id="score"></b> <span id="band">
</span></span><span>teacher <b id="teacher"></b></span></div>
<div id="buttons">
<button data-label="person">&#9989; person <kbd>1</kbd></button>
<button data-label="no_person">&#10060; no person <kbd>2</kbd></button>
<button data-label="unsure">&#10067; unsure <kbd>3</kbd></button>
</div>
<div id="buttons2">
<button id="skip">skip <kbd>space</kbd></button>
<button id="undo">undo <kbd>u</kbd></button>
</div>
</div>
<div id="done">Nothing left to label. <button id="undo2">undo last</button></div>
</main>
<script>
let current = null, busy = false;
const $ = id => document.getElementById(id);
function show(s) {
  current = s.frame;
  $("progress").textContent = s.remaining + " left, " + s.labeled + " labeled";
  $("undo").disabled = $("undo2").disabled = !s.can_undo;
  $("work").style.display = current ? "" : "none";
  $("done").style.display = current ? "none" : "block";
  if (!current) return;
  $("img").src = "/image/" + current.path.split("/").map(encodeURIComponent).join("/");
  $("camera").textContent = current.camera || "unknown";
  $("time").textContent = current.time || "";
  $("source").textContent = current.source;
  $("score").textContent = current.score == null ? "n/a" : current.score.toFixed(2);
  $("teacher").textContent = current.teacher == null ? "n/a" : current.teacher.toFixed(2);
  $("band").textContent = "(" + current.band + ")";
}
async function call(url, body) {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch(url, body === undefined ? {} :
      {method: "POST", headers: {"Content-Type": "application/json"},
       body: JSON.stringify(body)});
    if (r.ok) show(await r.json());
  } finally { busy = false; }
}
function label(l) { if (current) call("/api/label", {path: current.path, label: l}); }
function skip() { if (current) call("/api/skip", {path: current.path}); }
function undo() { call("/api/undo", {}); }
document.querySelectorAll("[data-label]").forEach(b =>
  b.addEventListener("click", () => label(b.dataset.label)));
$("skip").onclick = skip; $("undo").onclick = undo; $("undo2").onclick = undo;
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const map = {"1": "person", "2": "no_person", "3": "unsure"};
  if (map[e.key]) { e.preventDefault(); label(map[e.key]); }
  else if (e.key === " ") { e.preventDefault(); skip(); }
  else if (e.key === "u" || e.key === "Backspace") { e.preventDefault(); undo(); }
});
call("/api/next");
</script></body></html>
"""


def stats_page(stats):
    """The ``/stats`` HTML: the same table and threshold line the CLI prints."""
    head = "".join(f"<th>{html.escape(h)}</th>" for h in STATS_HEADERS)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row) + "</tr>"
                   for row in _stats_rows(stats))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Label stats</title>
<style>
:root{{--bg:#f6f6f4;--fg:#1d1d1b;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecece8;--line:#333}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:16px}}
.wrap{{overflow-x:auto}}
table{{border-collapse:collapse}}
td,th{{padding:4px 10px;border-bottom:1px solid var(--line);text-align:left;
white-space:nowrap}}
a{{color:inherit}}
</style></head><body><main>
<p><a href="/">&larr; back to labeling</a></p>
<h1>Label stats</h1>
<div class="wrap"><table><tr>{head}</tr>{body}</table></div>
<p>{html.escape(_threshold_line(stats["threshold"]))}</p>
<p>False alarms = sent frames labeled no_person. Misses = review (held) frames labeled
person. Unsure is excluded from both rates.</p>
</main></body></html>
"""


def make_server(session, port=0, bind=DEFAULT_BIND):
    """HTTP server for one :class:`LabelSession`. Stdlib only, threaded."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type):
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.debug("client closed the connection before the reply")

        def _json(self, code, payload):
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _html(self, text):
            self._send(200, text.encode(), "text/html; charset=utf-8")

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self._html(PAGE)
            elif path == "/api/next":
                self._json(200, session.state())
            elif path == "/stats":
                self._html(stats_page(compute_stats(session.dataset_dir)))
            elif path == "/api/stats":
                self._json(200, compute_stats(session.dataset_dir))
            elif path.startswith("/image/"):
                self._image(urllib.parse.unquote(path[len("/image/"):]))
            else:
                self._json(404, {"error": "not found"})

        def _image(self, rel):
            full = session.image_path(rel)
            if full is None:
                self._json(404, {"error": "not found"})
                return
            try:
                with open(full, "rb") as f:
                    body = f.read()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, body, "image/jpeg")

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                if path == "/api/label":
                    session.label(payload.get("path"), payload.get("label"))
                elif path == "/api/skip":
                    session.skip(payload.get("path"))
                elif path == "/api/undo":
                    session.undo()
                else:
                    self._json(404, {"error": "not found"})
                    return
            except (KeyError, ValueError, TypeError) as exc:
                self._json(400, {"error": type(exc).__name__})
                return
            except OSError as exc:
                log.warning("label write failed: %s", exc)
                self._json(500, {"error": "label not written"})
                return
            self._json(200, session.state())

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def serve(session, port, bind):
    """Serve until Ctrl-C. Returns the process exit code."""
    try:
        server = make_server(session, port=port, bind=bind)
    except OSError as exc:
        print(f"cannot bind {bind}:{port}: {exc}", file=sys.stderr)
        return 1
    print(f"Labeling {len(session.frames)} frames ({len(session.pending())} unlabeled); "
          f"open http://{bind}:{server.server_address[1]}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _dataset_ok(prog, value):
    if os.path.isdir(value):
        return True
    print(f"{prog}: not a directory: {value}", file=sys.stderr)
    return False


def label_main(argv):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor label",
        description="Label collected sent/review-log frames in a local web page")
    parser.add_argument("dataset_dir")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help="address to bind (default 127.0.0.1; the page has no auth)")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed for the low-score sample order")
    parser.add_argument("--low-sample", type=int, default=None,
                        help="queue at most N low-score frames (default: all, shuffled)")
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    session = LabelSession(args.dataset_dir, seed=args.seed, low_sample=args.low_sample)
    return serve(session, args.port, args.bind)


def stats_main(argv):
    parser = argparse.ArgumentParser(
        prog="tapo-monitor label-stats",
        description="Summarize labels.jsonl: false alarms, misses, best threshold")
    parser.add_argument("dataset_dir")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if not _dataset_ok(parser.prog, args.dataset_dir):
        return 2
    stats = compute_stats(args.dataset_dir)
    if args.json_output:
        print(json.dumps(stats, sort_keys=True))
    else:
        print(format_stats(stats))
    return 0
