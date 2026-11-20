#!/usr/bin/env python3
"""A scorer-compatible shadow target backed by an OpenAI-compatible vision model.

The scorer service can mirror every frame it scores to up to four "shadow" URLs
(``TAPO_SCORER_SHADOW*_URL``, see docs/operations.md) and log how a second model
would have judged it. This tool is such a target for a *vision-language model*
(llama.cpp / vLLM / Ollama / LM Studio ...): it answers the same ``POST /score``
with a JPEG body, asks the model whether the frame shows a person or an animal, and
answers ``{"person": 0.9 | 0.0, "animal": 0.9 | 0.0, "description": ...}``. The score is
a verdict, not a calibrated probability, so it only compares at thresholds.

It never takes part in an alert: it exists to measure the model next to the production
scorer. Next to the scorer's own shadow log it writes its own JSONL (verdict,
description, latency, a hash of the frame) so descriptions can be reviewed too.

    tools/vision_shadow.py --base-url http://127.0.0.1:11437/v1 --model local-vision \
        --log /srv/tapo-scorer/shadow/vision-shadow.jsonl

Binds to loopback by default; stdlib plus the repository's ``tapo_monitor.enrich``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import enrich  # noqa: E402

MAX_BODY = 8 * 1024 * 1024
VERDICT_SCORE = 0.9
LOG_MAX_BYTES = 200 * 1024 * 1024


class VisionShadow:
    """Answers one frame at a time (a single model inference is the bottleneck)."""

    def __init__(self, base_url, model, *, api_key=None, timeout=60.0, json_mode=True,
                 prompt=enrich.VISION_PROMPT, max_tokens=160, log_path=None,
                 log_max_bytes=LOG_MAX_BYTES, ask=enrich.vision_reply,
                 clock=time.time):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.json_mode = json_mode
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.log_path = log_path
        self.log_max_bytes = log_max_bytes
        self._ask = ask
        self._clock = clock
        self._lock = threading.Lock()          # one inference at a time
        self._log_lock = threading.Lock()
        self.counts = {"scored": 0, "errors": 0, "persons": 0}

    def score(self, jpeg):
        """``(status, payload)`` for one JPEG: 200 with a verdict, or 502 on a bad reply."""
        started = self._clock()
        digest = hashlib.sha256(jpeg).hexdigest()[:16]
        error = None
        verdict = None
        with self._lock:
            fd, path = tempfile.mkstemp(suffix=".jpg", prefix="visionshadow-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(jpeg)
                reply = self._ask(self.api_key, [path], prompt=self.prompt,
                                  model=self.model, max_tokens=self.max_tokens,
                                  timeout=self.timeout, base_url=self.base_url,
                                  json_mode=self.json_mode)
                verdict = enrich.parse_verdict(reply)
                if verdict["person"] is None:
                    error = "bad_reply"
            except Exception as exc:  # noqa: BLE001 - reported as a class word, never raised
                error = type(exc).__name__
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        latency = round(self._clock() - started, 3)
        row = {"ts": round(started, 3), "frame": digest, "model": self.model,
               "latency": latency, "error": error}
        if error is None:
            person = bool(verdict["person"])
            animal = bool(verdict["animal"]) and not person
            row.update(person=VERDICT_SCORE if person else 0.0,
                       animal=VERDICT_SCORE if animal else 0.0,
                       animal_name=verdict["animal"] or None,
                       description=verdict["description"])
            self.counts["scored"] += 1
            self.counts["persons"] += int(person)
            payload = {"person": row["person"], "animal": row["animal"],
                       "description": row["description"], "model": self.model,
                       "latency": latency}
            status = 200
        else:
            self.counts["errors"] += 1
            payload = {"error": error}
            status = 502
        self._write_log(row)
        return status, payload

    def _write_log(self, row):
        if not self.log_path:
            return
        with self._log_lock:
            try:
                if (os.path.exists(self.log_path)
                        and os.path.getsize(self.log_path) > self.log_max_bytes):
                    os.replace(self.log_path, self.log_path + ".1")
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass        # logging must never fail a request


def make_handler(shadow):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vision-shadow"

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] == "/health":
                self._send(200, {"ok": True, "model": shadow.model, **shadow.counts})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/score":
                self._send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._send(413 if length > MAX_BODY else 400, {"error": "bad_body"})
                return
            status, payload = shadow.score(self.rfile.read(length))
            self._send(status, payload)

        def log_message(self, *args):  # silence the per-request stderr line
            pass

    return Handler


def serve(shadow, host="127.0.0.1", port=8774):
    server = ThreadingHTTPServer((host, port), make_handler(shadow))
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True,
                        help="OpenAI-compatible server root, e.g. http://127.0.0.1:11437/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", help="name of the environment variable with the key")
    parser.add_argument("--listen", default="127.0.0.1:8774", help="host:port (default loopback)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--no-json-mode", action="store_true",
                        help="do not send response_format (servers without JSON mode)")
    parser.add_argument("--log", help="JSONL file for verdicts and descriptions")
    args = parser.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    key = os.environ.get(args.api_key_env) if args.api_key_env else None
    shadow = VisionShadow(args.base_url, args.model, api_key=key, timeout=args.timeout,
                          json_mode=not args.no_json_mode, log_path=args.log)
    server = serve(shadow, host or "127.0.0.1", int(port))
    print(f"vision shadow on {args.listen} -> {args.model}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
