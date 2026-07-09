"""Parse tapo-monitor audit log lines for threshold calibration.

The daemon emits compact ``audit`` records for camera detections, scorer decisions and
Telegram sends. This module keeps the parser small and dependency-free so operators can
run it against ``journalctl`` output on the target host.
"""

from __future__ import annotations

import argparse
import math
import shlex
import sys
from dataclasses import dataclass, field


def _parse_value(value: str):
    if value == "none":
        return None
    if value in ("true", "false"):
        return value == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_audit_line(line: str) -> dict | None:
    """Return key/value audit fields from one log line, or None when absent."""
    marker = "audit "
    if marker not in line:
        return None
    text = line.split(marker, 1)[1].strip()
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    out = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key] = _parse_value(value)
    return out if out else None


def _pct(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * percentile
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class CameraSummary:
    detections: int = 0
    telegram_ok: int = 0
    telegram_failed: int = 0
    deferred: int = 0
    cooldown: int = 0
    dropped_below_threshold: int = 0
    scorer_unavailable: int = 0
    snapshot_failed: int = 0
    sent_scores: list[float] = field(default_factory=list)
    dropped_scores: list[float] = field(default_factory=list)
    thresholds: list[float] = field(default_factory=list)

    def observe(self, rec: dict) -> None:
        action = rec.get("action")
        if action == "detect":
            self.detections += 1
        elif action == "send":
            if rec.get("telegram") is False:
                self.telegram_failed += 1
            else:
                self.telegram_ok += 1
            if isinstance(rec.get("score"), (int, float)):
                self.sent_scores.append(float(rec["score"]))
        elif action == "defer":
            self.deferred += 1
        elif action == "cooldown":
            self.cooldown += 1
        elif action == "drop":
            if rec.get("reason") == "below_threshold":
                self.dropped_below_threshold += 1
            if isinstance(rec.get("score"), (int, float)):
                self.dropped_scores.append(float(rec["score"]))
        elif action == "scorer_unavailable":
            self.scorer_unavailable += 1
        elif action == "snapshot_failed":
            self.snapshot_failed += 1
        if isinstance(rec.get("threshold"), (int, float)):
            self.thresholds.append(float(rec["threshold"]))


def summarize(lines) -> dict[str, CameraSummary]:
    """Build per-camera summaries from iterable log lines."""
    summaries: dict[str, CameraSummary] = {}
    for line in lines:
        rec = parse_audit_line(line)
        if not rec:
            continue
        camera = str(rec.get("camera") or "unknown")
        summaries.setdefault(camera, CameraSummary()).observe(rec)
    return summaries


def _fmt_num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _threshold(summary: CameraSummary) -> float | None:
    return _pct(summary.thresholds, 0.5)


def format_summary(summaries: dict[str, CameraSummary]) -> str:
    """Human-readable report for CLI output."""
    if not summaries:
        return "No audit lines found."
    lines = []
    for camera in sorted(summaries):
        s = summaries[camera]
        considered = s.telegram_ok + s.telegram_failed + s.deferred + s.dropped_below_threshold
        send_rate = (s.telegram_ok / s.detections * 100.0) if s.detections else 0.0
        lines.append(f"{camera}:")
        lines.append(
            f"  detections={s.detections} considered={considered} "
            f"telegram_ok={s.telegram_ok} telegram_failed={s.telegram_failed} "
            f"send_rate={send_rate:.1f}%"
        )
        lines.append(
            f"  deferred={s.deferred} cooldown={s.cooldown} "
            f"dropped_below_threshold={s.dropped_below_threshold} "
            f"scorer_unavailable={s.scorer_unavailable} snapshot_failed={s.snapshot_failed}"
        )
        lines.append(
            "  scores: "
            f"sent min/p50/max={_fmt_num(_pct(s.sent_scores, 0.0))}/"
            f"{_fmt_num(_pct(s.sent_scores, 0.5))}/"
            f"{_fmt_num(_pct(s.sent_scores, 1.0))}; "
            f"dropped max={_fmt_num(_pct(s.dropped_scores, 1.0))}; "
            f"threshold~{_fmt_num(_threshold(s))}"
        )
        if s.detections and s.telegram_ok == 0 and s.dropped_below_threshold:
            lines.append("  hint: threshold may be too high, or camera events are mostly false positives.")
        elif s.telegram_ok and s.dropped_below_threshold:
            lines.append(
                "  hint: compare dropped frames near threshold against real footage before changing it."
            )
        elif s.scorer_unavailable:
            lines.append("  hint: scorer outages make calibration noisy; fix availability before tuning.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Summarize tapo-monitor audit log lines")
    parser.add_argument("path", nargs="?", help="log file path; stdin when omitted or '-'")
    args = parser.parse_args(argv)
    if not args.path or args.path == "-":
        print(format_summary(summarize(sys.stdin)))
        return 0
    with open(args.path, encoding="utf-8", errors="replace") as fh:
        print(format_summary(summarize(fh)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
