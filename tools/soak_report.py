#!/usr/bin/env python3
"""Parsing, statistics and baseline comparison for tools/soak_report.sh.

The shell script collects one raw probe per host over ssh (read-only) and hands the
files to this module, which does all the arithmetic: counts, median/p90 of the first
alert, per-camera breakdowns, resource figures and the comparison with an earlier run.
Keeping that here means it is testable on fixtures without a host, a journal or ssh.

Every check is written so that "the thing does not exist" cannot read as "all clear":
a unit that is not loaded, a probe that never reached its end marker, an unreadable or
empty journal and an accounting-less unit each produce an explicit unknown/finding, never
a zero.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tapo_monitor.audit import parse_audit_line  # noqa: E402

BASELINE_VERSION = 1
DEFAULT_TOLERANCE = 0.30
# Below this many alerts of difference a "+300 %" is two alerts, not a regression.
MIN_ABS_ALERT_DELTA = 5
UINT64_MAX = 2**64 - 1

ERROR_RE = re.compile(r"Traceback|ERROR|CRITICAL")
START_RE = re.compile(r"systemd\[\d+\]: Started ")
FAILED_RE = re.compile(r"systemd\[\d+\]: (Failed with result|.*Main process exited)")
HUB_ERR_RE = re.compile(r"\bhub\b.*(failed|refused|error_code)", re.IGNORECASE)
OUTBOX_PROBLEM_RE = re.compile(r"could not|unreadable|failed|bookkeeping|undeliverable|unset")
JOURNAL_DENIED_RE = re.compile(r"No journal files were opened|insufficient permissions")


# ── statistics ────────────────────────────────────────────────────────────────────────

def percentile(values, fraction):
    """Linear-interpolated percentile of a list, None for an empty one."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def median(values):
    return percentile(values, 0.5)


def pct_change(base, cur):
    """Relative change, None when there is no base to compare against."""
    if not base:
        return None
    return (cur - base) / base


# ── raw probe parsing ─────────────────────────────────────────────────────────────────

def split_sections(text):
    """Split the probe output at `@@name [rest]` markers. Returns ({name: [lines]}, complete)."""
    sections: dict[str, list[str]] = {}
    current = None
    complete = False
    for line in text.splitlines():
        if line.startswith("@@"):
            name, _, rest = line[2:].partition(" ")
            if name == "end":
                complete = True
                current = None
                continue
            current = name
            sections.setdefault(name, [])
            if rest:
                sections[name].append(rest)
            continue
        if current is not None:
            sections[current].append(line)
    return sections, complete


def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    # systemd prints UINT64_MAX / "[not set]" for a counter whose accounting is off.
    return None if number >= UINT64_MAX else number


def parse_show(lines):
    props = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def unit_verdict(props):
    """(ok, text). LoadState is the key a missing unit cannot fake: a unit that does not
    exist still answers `systemctl show` with Result=success and ActiveState=inactive."""
    load = props.get("LoadState")
    active = props.get("ActiveState")
    sub = props.get("SubState")
    if load is None:
        return False, "systemctl returned no LoadState (not a systemd host or wrong scope?)"
    if load != "loaded":
        return False, f"unit not loaded (LoadState={load})"
    if active == "active" and sub == "running":
        return True, "active/running"
    # activating/auto-restart is the crash loop is-active reports as healthy.
    return False, f"ActiveState={active} SubState={sub} Result={props.get('Result', '?')}"


def analyze(text, since_hours):
    """Turn one raw probe into a flat, JSON-serialisable record with its own findings."""
    sections, complete = split_sections(text)
    findings: list[str] = []
    out: dict = {"reachable": True, "complete": complete, "since_hours": since_hours}
    if not sections:
        out["reachable"] = False
        out["findings"] = ["no probe output"]
        return out
    if not complete:
        findings.append("probe output truncated (no end marker); numbers below are partial")

    props = parse_show(sections.get("show", []))
    ok, verdict = unit_verdict(props)
    nrestarts = _int_or_none(props.get("NRestarts"))
    out["unit"] = {"ok": ok, "verdict": verdict, "load": props.get("LoadState"),
                   "active": props.get("ActiveState"), "sub": props.get("SubState"),
                   "nrestarts": nrestarts}
    if not ok:
        findings.append(f"service: {verdict}")

    version = {"fingerprint": None, "version": None}
    for line in sections.get("version", []):
        if line.startswith("package "):
            version["fingerprint"] = line.split()[1] if len(line.split()) > 1 else None
        elif line.startswith("tapo-monitor "):
            version["version"] = line.split(None, 1)[1]
    link = [x for x in sections.get("link", []) if x.strip()]
    version["release"] = Path(link[0].strip()).name if link else None
    out["package"] = version
    if version["fingerprint"] is None:
        findings.append("package fingerprint unknown (version command gave no `package` line)")

    journal = sections.get("journal", [])
    total = None
    for line in sections.get("total", []):
        total = _int_or_none(line.strip())
    denied = any(JOURNAL_DENIED_RE.search(line) for line in journal)
    out["journal"] = {"lines": total, "readable": not denied and total is not None}
    journal_usable = out["journal"]["readable"] and bool(total)
    out["journal"]["usable"] = journal_usable
    if denied:
        findings.append("journal unreadable (insufficient permissions)")
    elif total is None:
        findings.append("journal line count missing")
    elif total == 0:
        findings.append(f"journal has no lines in {since_hours:g} h; counts below prove nothing")

    stats = _journal_stats(journal)
    if not journal_usable:
        # No journal evidence: report unknown instead of a comforting zero.
        for key in ("errors", "starts", "failures", "hub_errors", "outbox_lines",
                    "outbox_problems", "host_switch", "audit_errors"):
            stats[key] = None
        stats["error_signatures"] = []
    else:
        if stats["errors"]:
            findings.append(f"{stats['errors']} Traceback/ERROR/CRITICAL line(s)")
        if stats["failures"]:
            findings.append(f"{stats['failures']} systemd failure/exit line(s)")
        if stats["outbox_problems"]:
            findings.append(f"{stats['outbox_problems']} outbox problem line(s)")
        if stats["audit_errors"]:
            findings.append(f"{stats['audit_errors']} audit action=error line(s)")
    out.update(stats)

    resources = _resources(props, sections)
    out["resources"] = resources
    out["findings"] = findings
    return out


def _journal_stats(lines):
    errors = 0
    signatures: Counter = Counter()
    starts = failures = hub_errors = outbox_lines = outbox_problems = 0
    audit_errors = host_switch = 0
    sent: dict = {}
    first_age: dict = {}
    gates: dict = {}
    would: dict = {}
    for line in lines:
        rec = parse_audit_line(line) if "audit " in line else None
        if rec is not None:
            camera = str(rec.get("camera") or "unknown")
            action = str(rec.get("action") or "unknown")
            if action == "send":
                path = str(rec.get("path") or "unknown")
                bucket = sent.setdefault(camera, {})
                cell = bucket.setdefault(path, {"ok": 0, "failed": 0, "unknown": 0})
                if rec.get("telegram") is True:
                    cell["ok"] += 1
                    age = rec.get("event_age_s")
                    start = rec.get("start")
                    if isinstance(age, (int, float)) and not isinstance(age, bool) \
                            and math.isfinite(age) and age >= 0:
                        key = (camera, str(rec.get("etype")), start)
                        if key not in first_age or age < first_age[key]:
                            first_age[key] = float(age)
                elif rec.get("telegram") is False:
                    cell["failed"] += 1
                else:
                    cell["unknown"] += 1
            elif action in ("drop", "hold", "cooldown"):
                gates.setdefault(camera, Counter())[action] += 1
            elif action.startswith("would_"):
                would.setdefault(camera, Counter())[action] += 1
            elif action == "host_switch":
                host_switch += 1
            elif action == "error":
                audit_errors += 1
            continue
        if ERROR_RE.search(line):
            errors += 1
            signatures[_signature(line)] += 1
        if START_RE.search(line):
            starts += 1
        if FAILED_RE.search(line):
            failures += 1
        if "outbox" in line:
            outbox_lines += 1
            if OUTBOX_PROBLEM_RE.search(line):
                outbox_problems += 1
        if HUB_ERR_RE.search(line):
            hub_errors += 1

    ages_by_camera: dict = {}
    for (camera, _etype, _start), age in first_age.items():
        ages_by_camera.setdefault(camera, []).append(age)
    all_ages = [age for ages in ages_by_camera.values() for age in ages]
    first_alert = {"n": len(all_ages), "median_s": median(all_ages),
                   "p90_s": percentile(all_ages, 0.9),
                   "by_camera": {c: {"n": len(a), "median_s": median(a), "p90_s": percentile(a, 0.9)}
                                 for c, a in sorted(ages_by_camera.items())}}
    sent_ok = sum(c["ok"] for paths in sent.values() for c in paths.values())
    sent_failed = sum(c["failed"] for paths in sent.values() for c in paths.values())
    return {
        "errors": errors,
        "error_signatures": [{"count": n, "text": s} for s, n in signatures.most_common(3)],
        "starts": starts,
        "failures": failures,
        "hub_errors": hub_errors,
        "outbox_lines": outbox_lines,
        "outbox_problems": outbox_problems,
        "host_switch": host_switch,
        "audit_errors": audit_errors,
        "alerts": {"sent_ok": sent_ok, "sent_failed": sent_failed, "by_camera": sent},
        "first_alert": first_alert,
        "gates": {c: dict(v) for c, v in sorted(gates.items())},
        "would": {c: dict(v) for c, v in sorted(would.items())},
    }


def _signature(line):
    """A stable shape of an error line: timestamp and pid stripped, digits folded."""
    body = line.split("]: ", 1)[-1] if "]: " in line else line
    return re.sub(r"\d+", "N", body.strip())[:160]


def _resources(props, sections):
    mem = _int_or_none(props.get("MemoryCurrent"))
    cpu_ns = _int_or_none(props.get("CPUUsageNSec"))
    uptime = None
    for line in sections.get("uptime", []):
        try:
            uptime = float(line.split()[0])
        except (IndexError, ValueError):
            pass
    entered = _int_or_none(props.get("ActiveEnterTimestampMonotonic"))
    age = None
    if uptime is not None and entered:
        age = uptime - entered / 1e6
        if age <= 0:
            age = None
    cpu_s = cpu_ns / 1e9 if cpu_ns is not None else None
    cpu_pct = (cpu_s / age * 100) if cpu_s is not None and age else None
    return {"memory_bytes": mem, "cpu_s": cpu_s, "service_age_s": age, "cpu_pct": cpu_pct}


# ── baseline ──────────────────────────────────────────────────────────────────────────

def _rate(record, key_total):
    hours = record.get("since_hours") or 0
    return key_total / hours if hours else None


def compare(base, cur, tolerance=DEFAULT_TOLERANCE):
    """Deviations of one host's record from its baseline record, as readable strings."""
    out = []
    if not base.get("reachable", True) or not cur.get("reachable", True):
        return out
    bpkg = (base.get("package") or {}).get("fingerprint")
    cpkg = (cur.get("package") or {}).get("fingerprint")
    if bpkg and cpkg and bpkg != cpkg:
        out.append(f"package changed {bpkg} -> {cpkg}")
    if (base.get("unit") or {}).get("ok") and not (cur.get("unit") or {}).get("ok"):
        out.append("service was healthy in the baseline, is not now")

    ba = (base.get("alerts") or {}).get("sent_ok") if (base.get("journal") or {}).get("usable") else None
    ca = (cur.get("alerts") or {}).get("sent_ok") if (cur.get("journal") or {}).get("usable") else None
    brate = _rate(base, ba) if ba is not None else None
    crate = _rate(cur, ca) if ca is not None else None
    if brate is not None and crate is not None:
        window = cur.get("since_hours") or 0
        delta = (crate - brate) * window
        change = pct_change(brate, crate)
        if change is None:
            if crate * window >= MIN_ABS_ALERT_DELTA:
                out.append(f"alerts: none in the baseline, {ca} now")
        elif abs(change) > tolerance and abs(delta) >= MIN_ABS_ALERT_DELTA:
            out.append(f"alert volume {change:+.0%} ({brate:.2f}/h -> {crate:.2f}/h)")

    for key, label in (("errors", "Traceback/ERROR/CRITICAL lines"), ("starts", "starts"),
                       ("failures", "systemd failures"), ("hub_errors", "hub errors"),
                       ("outbox_problems", "outbox problems"), ("host_switch", "host_switch"),
                       ("audit_errors", "audit errors")):
        b, c = base.get(key), cur.get(key)
        if b is None or c is None:
            continue
        brate_k, crate_k = _rate(base, b), _rate(cur, c)
        if brate_k is None or crate_k is None:
            continue
        # Compare as rates so a baseline taken over another window stays comparable;
        # anything that appears where there was none, or grows, is a deviation.
        if crate_k > brate_k + 1e-9 and c > 0:
            out.append(f"{label}: {b} in {base['since_hours']:g} h -> {c} in {cur['since_hours']:g} h")
    return out


def load_baseline(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SystemExit(f"soak_report: cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise SystemExit(f"soak_report: {path} is not a version {BASELINE_VERSION} baseline")
    return data


def build_report(records, tolerance=DEFAULT_TOLERANCE, baseline=None):
    hosts = {}
    for name, record in records.items():
        entry = dict(record)
        entry["deviations"] = []
        if baseline is not None:
            base = (baseline.get("hosts") or {}).get(name)
            if base is None:
                entry["deviations"] = ["host not in the baseline"]
            else:
                entry["deviations"] = compare(base, record, tolerance)
        hosts[name] = entry
    return {"version": BASELINE_VERSION,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hosts": hosts}


def host_status(entry):
    if not entry.get("reachable", True):
        return "UNREACHABLE"
    if entry.get("findings") or entry.get("deviations"):
        return "ATTENTION"
    return "OK"


# ── rendering ─────────────────────────────────────────────────────────────────────────

def _fmt(value, spec="{:.0f}", suffix=""):
    return "n/a" if value is None else spec.format(value) + suffix


def _fmt_mib(value):
    return "n/a" if value is None else f"{value / 1048576:.1f} MiB"


def _count(value):
    return "unknown" if value is None else str(value)


def render_host(name, entry):
    lines = [f"== {name}  [{host_status(entry)}]"]
    if not entry.get("reachable", True):
        lines += [f"   {f}" for f in entry.get("findings", [])]
        return "\n".join(lines)
    unit, pkg = entry["unit"], entry["package"]
    lines.append(f"   service    {unit['verdict']}, NRestarts={_count(unit['nrestarts'])} (cumulative), "
                 f"starts in window {_count(entry['starts'])}")
    lines.append(f"   package    {pkg['fingerprint'] or 'unknown'}  release {pkg['release'] or 'unknown'}  "
                 f"version {pkg['version'] or 'unknown'}")
    lines.append(f"   journal    {_count(entry['journal']['lines'])} lines in {entry['since_hours']:g} h")
    lines.append(f"   errors     Traceback|ERROR|CRITICAL {_count(entry['errors'])}, "
                 f"systemd failures {_count(entry['failures'])}")
    for sig in entry["error_signatures"]:
        lines.append(f"              {sig['count']:>4}x {sig['text']}")
    if not entry["journal"]["usable"]:
        lines.append("   alerts     unknown (no usable journal)")
        res = entry["resources"]
        lines.append(f"   resources  memory {_fmt_mib(res['memory_bytes'])}, cpu {_fmt(res['cpu_pct'], '{:.2f}', ' %')}")
        lines += [f"   ! {f}" for f in entry["findings"]]
        lines += [f"   ~ vs baseline: {d}" for d in entry["deviations"]]
        return "\n".join(lines)
    alerts = entry["alerts"]
    lines.append(f"   alerts     sent {alerts['sent_ok']}, failed {alerts['sent_failed']}")
    for camera, paths in sorted(alerts["by_camera"].items()):
        cells = ", ".join(f"{p} {c['ok']}" + (f" (+{c['failed']} failed)" if c["failed"] else "")
                          for p, c in sorted(paths.items()))
        lines.append(f"              {camera}: {cells}")
    fa = entry["first_alert"]
    lines.append(f"   first      median {_fmt(fa['median_s'], suffix=' s')} p90 {_fmt(fa['p90_s'], suffix=' s')} "
                 f"(n={fa['n']}, first delivered send per event)")
    gate_total: Counter = Counter()
    for counts in entry["gates"].values():
        gate_total.update(counts)
    lines.append(f"   gates      drop {gate_total['drop']}, hold {gate_total['hold']}, "
                 f"cooldown {gate_total['cooldown']}")
    for camera, counts in entry["would"].items():
        lines.append(f"   observe    {camera}: " + ", ".join(f"{a} {n}" for a, n in sorted(counts.items())))
    if not entry["would"]:
        lines.append("   observe    no would_* rows")
    lines.append(f"   events     host_switch {_count(entry['host_switch'])}, outbox lines "
                 f"{_count(entry['outbox_lines'])} (problems {_count(entry['outbox_problems'])}), "
                 f"hub errors {_count(entry['hub_errors'])}")
    res = entry["resources"]
    cpu = (f"{_fmt(res['cpu_pct'], '{:.2f}', ' %')} ({_fmt(res['cpu_s'], '{:.0f}', ' s')} over "
           f"{_fmt(None if res['service_age_s'] is None else res['service_age_s'] / 3600, '{:.1f}', ' h')})")
    lines.append(f"   resources  memory {_fmt_mib(res['memory_bytes'])}, cpu {cpu}")
    for finding in entry["findings"]:
        lines.append(f"   ! {finding}")
    for deviation in entry["deviations"]:
        lines.append(f"   ~ vs baseline: {deviation}")
    return "\n".join(lines)


def render(report):
    blocks = [render_host(name, entry) for name, entry in report["hosts"].items()]
    bad = [n for n, e in report["hosts"].items() if host_status(e) != "OK"]
    summary = ("soak report: all hosts OK" if not bad
               else f"soak report: attention needed on {', '.join(bad)}")
    return "\n\n".join(blocks) + "\n\n" + summary


# ── CLI ───────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarise raw soak probes (see soak_report.sh)")
    parser.add_argument("probes", nargs="+", metavar="NAME=FILE")
    parser.add_argument("--since", type=float, default=24.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--baseline")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE * 100,
                        help="alert volume deviation in percent (default 30)")
    args = parser.parse_args(argv)

    records = {}
    for spec in args.probes:
        name, sep, path = spec.partition("=")
        if not sep:
            parser.error(f"expected NAME=FILE, got {spec}")
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            text = ""
        records[name] = analyze(text, args.since)

    baseline = load_baseline(args.baseline) if args.baseline else None
    report = build_report(records, args.tolerance / 100, baseline)
    notes = []
    if args.baseline:
        if baseline is None:
            notes.append(f"baseline {args.baseline} did not exist; saved this run as the baseline")
        elif args.update_baseline:
            notes.append(f"baseline {args.baseline} updated")
        if baseline is None or args.update_baseline:
            slim = {"version": BASELINE_VERSION, "created": report["created"],
                    "hosts": {n: {k: v for k, v in e.items() if k != "deviations"}
                              for n, e in report["hosts"].items()}}
            Path(args.baseline).write_text(json.dumps(slim, indent=2, sort_keys=True) + "\n")
    if args.json:
        report["notes"] = notes
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
        for note in notes:
            print(note)
    return 1 if any(host_status(e) != "OK" for e in report["hosts"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
