#!/usr/bin/env python3
"""Replay the SD follow-up's frame grid and pickers against a 1-second "oracle".

Step 3 of "Evaluating the follow-up frame pick" (docs/operations.md). Reads the features
JSON of ``tools/frame_pick_eval.py`` (frames at 1 s density) and, per event, keeps only
the frames a candidate grid would have extracted, then lets a picker choose the alert
photo among those with ``person >= threshold``:

  grids    ``all`` (every frame), ``grid6`` (``sdclip.frame_offsets(36, 6)``), ``dense``
           (the same plus every 2 s over the first 12 s), ``dense24`` (... first 24 s),
           ``every3`` (every 3 s)
  pickers  ``sharpest``, ``largest``, ``tallest`` (``recclip.select_*``)
  guard    with ``+guard`` the smear guard (``recclip.smeared_frames``) drops frames of
           a swinging camera first, as the daemon does

A variant is named ``<grid>+<picker>[+guard]``. The oracle of an event is the tallest
subject among all frames that pass the threshold, are not smeared and keep the box clear
of the frame edge. Per variant the summary shows the mean of chosen height / oracle height
(1.0 = as good as the clip allows, capped at 1.5 per event), how many picks were smeared
frames and how many events got no candidate at all (missed). Events without any usable
frame are left out. The event start is the event directory's name, a frame's time the
``_at<epoch>`` stamp in its file name::

    .venv/bin/python tools/frame_pick_sim.py features.json --threshold 0.3
    .venv/bin/python tools/frame_pick_sim.py features.json --gallery out/gallery --sheets out/sheets

``--gallery`` writes a self-contained before/after page (``index.html`` + ``img/``)
comparing two variants, ``--sheets`` one contact sheet per event. Both copy private
camera frames: keep the output out of the repository.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys

from tapo_monitor import recclip, sdclip

GRID_SPAN = 36
GRIDS = {
    "all": None,
    "grid6": (6, None),
    "dense": (6, (12, 2)),
    "dense24": (6, (24, 2)),
    "every3": (3, None),
}
PICKERS = ("sharpest", "largest", "tallest")
RATIO_CAP = 1.5
DEFAULT_SIZE = (1280, 720)
DEFAULT_BEFORE = "grid6+sharpest"
DEFAULT_AFTER = "dense+tallest+guard"


# ── pure core ────────────────────────────────────────────────────────────────

def parse_variant(name):
    """``"dense+tallest+guard"`` -> ``("dense", "tallest", True)``; ValueError if unknown."""
    parts = name.split("+")
    guard = parts[-1] == "guard"
    if guard:
        parts = parts[:-1]
    if len(parts) != 2 or parts[0] not in GRIDS or parts[1] not in PICKERS:
        raise ValueError(f"unknown variant {name!r} (grid+picker[+guard])")
    return parts[0], parts[1], guard


def variant_name(grid, picker, guard):
    return f"{grid}+{picker}" + ("+guard" if guard else "")


def default_variants(grids=("grid6", "dense", "dense24", "every3"), pickers=PICKERS):
    return [variant_name(g, p, guard) for g in grids for p in pickers for guard in (False, True)]


def frame_time(rec):
    """Capture epoch (int) from the frame's ``_at<epoch>`` name, or None."""
    t = sdclip.frame_capture_time(rec["frame"])
    return int(t) if t is not None else None


def grid_offsets(grid):
    """Offsets (s from the event start) the grid extracts, or None for every frame."""
    spec = GRIDS[grid]
    if spec is None:
        return None
    every, dense = spec
    return sdclip.frame_offsets(GRID_SPAN, every, dense=dense)


def grid_frames(frames, start, grid):
    """The frames of one event a ``grid`` would have extracted, in grid order."""
    offsets = grid_offsets(grid)
    if offsets is None:
        return list(frames)
    by_time = {frame_time(f): f for f in frames}
    return [by_time[start + o] for o in offsets if start + o in by_time]


def is_candidate(rec, threshold):
    return (rec.get("person") or 0) >= threshold and bool(rec.get("box"))


def smeared(frames):
    """Frame paths the smear guard marks among ``frames`` (one clip or a grid of it)."""
    detail = {f["frame"]: f.get("frame_var") for f in frames}
    return recclip.smeared_frames([f["frame"] for f in frames], detail=detail.get)


def frame_size(frames, default=DEFAULT_SIZE):
    """``(width, height)`` from the first record that carries ``size``, else ``default``."""
    for f in frames:
        if f.get("size"):
            return tuple(f["size"])
    return tuple(default)


def pick(frames, picker, guard, threshold, size=DEFAULT_SIZE):
    """The record the daemon's ``picker`` would send among ``frames``, or None (no candidate).

    Candidates are ordered by person score like the daemon's; with ``guard`` the smeared
    frames of ``frames`` are dropped first unless that would leave none.
    """
    above = sorted((f for f in frames if is_candidate(f, threshold)),
                   key=lambda f: f["person"], reverse=True)
    if not above:
        return None
    if guard and len(above) > 1:
        marked = smeared(frames)
        above = [f for f in above if f["frame"] not in marked] or above
    cands = [(f["frame"], f.get("blur"), f["box"]) for f in above]
    if picker == "sharpest":
        chosen = recclip.select_sharpest([(a, b) for a, b, _ in cands])
    elif picker == "largest":
        chosen = recclip.select_largest(cands)
    else:
        chosen = recclip.select_tallest(cands, frame_size=size)
    return next(f for f in above if f["frame"] == chosen)


def oracle(frames, threshold, size=DEFAULT_SIZE, marked=None):
    """Tallest steady, edge-clear candidate of the whole clip, or None."""
    marked = smeared(frames) if marked is None else marked
    good = [f for f in frames if is_candidate(f, threshold) and f["frame"] not in marked
            and not recclip._touches_edge(f["box"], size)]
    return max(good, key=lambda f: recclip.box_height(f["box"]), default=None)


def evaluate(feats, variants, threshold, default_size=DEFAULT_SIZE):
    """Replay ``variants`` over ``{event_start: [records]}``.

    Returns ``(events, summary)``: ``events`` is a list of ``{"event", "oracle",
    "picks": {variant: {"frame", "ratio", "smeared"} | None}}`` for events with an
    oracle; ``summary`` maps each variant to ``{"mean_ratio", "picked", "smeared",
    "missed"}``. Events whose name is not an epoch are skipped.
    """
    parsed = [(v, *parse_variant(v)) for v in variants]
    totals = {v: [0.0, 0, 0, 0] for v in variants}  # sum ratio, picked, smeared, missed
    events = []
    for ev, frames in feats.items():
        try:
            start = int(ev)
        except ValueError:
            continue
        size = frame_size(frames, default_size)
        marked = smeared(frames)
        best = oracle(frames, threshold, size, marked)
        if best is None:
            continue
        top = recclip.box_height(best["box"])
        row = {"event": ev, "oracle": best, "picks": {}}
        for name, grid, picker, guard in parsed:
            chosen = pick(grid_frames(frames, start, grid), picker, guard, threshold, size)
            t = totals[name]
            if chosen is None:
                t[3] += 1
                row["picks"][name] = None
                continue
            ratio = recclip.box_height(chosen["box"]) / top
            is_smeared = chosen["frame"] in marked
            t[0] += min(ratio, RATIO_CAP)
            t[1] += 1
            t[2] += is_smeared
            row["picks"][name] = {"frame": chosen, "ratio": ratio, "smeared": is_smeared}
        events.append(row)
    summary = {v: {"mean_ratio": s / n if n else None, "picked": n, "smeared": sm, "missed": miss}
               for v, (s, n, sm, miss) in totals.items()}
    return events, summary


def format_pick(p):
    if p is None:
        return "miss"
    return f"{p['ratio']:.2f}" + ("*" if p["smeared"] else "")


def format_summary(events, summary):
    """The summary table as text lines."""
    width = max((len(v) for v in summary), default=7)
    lines = [f"events with a subject: {len(events)}",
             f"{'variant':{width}}  height ratio  smeared picks  missed"]
    for v, s in summary.items():
        mean = f"{s['mean_ratio']:.2f}" if s["mean_ratio"] is not None else "-"
        lines.append(f"{v:{width}}  {mean:>12}  {s['smeared']:>13}  {s['missed']:>6}")
    return lines


# ── output: gallery and contact sheets (read the frames) ────────────────────

def _boxed_copy(rec, dest, color="lime", width=640):
    """Copy a frame, scaled to ``width`` px, with its subject box drawn."""
    from PIL import Image, ImageDraw
    with Image.open(rec["frame"]) as im:
        im = im.convert("RGB")
        scale = width / im.width
        im = im.resize((width, round(im.height * scale)))
    if rec.get("box"):
        x1, y1, x2, y2 = (v * scale for v in rec["box"])
        ImageDraw.Draw(im).rectangle([x1, y1, x2, y2], outline=color, width=2)
    im.save(dest, quality=85)


def _cell(pick_, src):
    if pick_ is None:
        return '<td class="miss">no candidate</td>'
    f = pick_["frame"]
    note = f"height {pick_['ratio']:.2f} of oracle · person {f['person']:.2f}"
    if pick_["smeared"]:
        note += ' · <b class="bad">smeared</b>'
    return f'<td><img src="{html.escape(src)}" loading="lazy" alt=""><div>{note}</div></td>'


def write_gallery(events, summary, out, before, after, copy=_boxed_copy):
    """``out/index.html`` + ``out/img/``: the two variants' picks side by side per event."""
    img = os.path.join(out, "img")
    os.makedirs(img, exist_ok=True)
    rows = []
    for row in events:
        cells = []
        for tag, v in (("a", before), ("b", after)):
            p = row["picks"].get(v)
            rel = f"img/{row['event']}_{tag}.jpg"
            if p is not None:
                copy(p["frame"], os.path.join(out, rel))
            cells.append(_cell(p, rel))
        changed = (row["picks"].get(before) or {}).get("frame") != \
            (row["picks"].get(after) or {}).get("frame")
        rows.append(f'<tr class="{"diff" if changed else "same"}"><th>{html.escape(row["event"])}'
                    f'</th>{"".join(cells)}</tr>')

    def stat(v):
        s = summary[v]
        mean = f"{s['mean_ratio']:.2f}" if s["mean_ratio"] is not None else "-"
        return (f"<li><b>{html.escape(v)}</b>: mean height ratio {mean}, "
                f"smeared picks {s['smeared']}, missed {s['missed']}</li>")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Frame pick comparison</title>
<style>
:root {{ --bg: #fff; --fg: #111; --muted: #666; --line: #ddd; --bad: #c00; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #111; --fg: #eee; --muted: #999; --line: #333; --bad: #f66; }} }}
body {{ background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif;
       margin: 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-top: 1px solid var(--line); padding: 6px; vertical-align: top; text-align: left; }}
td img {{ width: 100%; max-width: 640px; display: block; }}
td div, .miss {{ color: var(--muted); }}
.bad {{ color: var(--bad); }}
label {{ display: block; margin: 8px 0; }}
body.only-diff tr.same {{ display: none; }}
</style></head><body>
<h1>Frame pick: {html.escape(before)} vs {html.escape(after)}</h1>
<ul>{stat(before)}{stat(after)}</ul>
<label><input type="checkbox" onchange="document.body.classList.toggle('only-diff', this.checked)">
only events where the pick differs</label>
<table><thead><tr><th>event</th><th>{html.escape(before)}</th><th>{html.escape(after)}</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></body></html>
"""
    path = os.path.join(out, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def write_sheets(feats, out, threshold, default_size=DEFAULT_SIZE, cols=6, tile=(320, 180)):
    """One contact sheet per event; marks the S(harpest)/L(argest)/T(allest) pick, ~ = smeared."""
    from PIL import Image, ImageDraw
    os.makedirs(out, exist_ok=True)
    tw, th = tile
    for ev, frames in feats.items():
        size = frame_size(frames, default_size)
        marked = smeared(frames)
        picks = {k[0].upper(): (pick(frames, k, False, threshold, size) or {}).get("frame")
                 for k in PICKERS}
        rows = (len(frames) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, max(rows, 1) * (th + 16)), "black")
        draw = ImageDraw.Draw(sheet)
        for i, f in enumerate(frames):
            x, y = (i % cols) * tw, (i // cols) * (th + 16)
            try:
                with Image.open(f["frame"]) as im:
                    sx, sy = tw / im.width, th / im.height
                    sheet.paste(im.convert("RGB").resize((tw, th)), (x, y))
            except OSError:
                continue
            if f.get("box"):
                b = f["box"]
                color = "lime" if is_candidate(f, threshold) else "gray"
                draw.rectangle([x + b[0] * sx, y + b[1] * sy, x + b[2] * sx, y + b[3] * sy],
                               outline=color)
            mark = "".join(k for k, v in picks.items() if v == f["frame"])
            if f["frame"] in marked:
                mark += "~"
            fv = f.get("frame_var")
            draw.text((x + 2, y + th + 2),
                      f"{i} p={f.get('person') or 0:.2f} bl={f.get('blur') or 0:.1f} "
                      f"fv={fv if fv is None else round(fv)} {mark}",
                      fill="yellow" if mark.strip("~") else "white")
        sheet.save(os.path.join(out, f"{ev}.jpg"), quality=80)
    return len(feats)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("features", help="JSON written by tools/frame_pick_eval.py")
    ap.add_argument("--threshold", type=float, default=0.3, help="person threshold (0.3)")
    ap.add_argument("--variants", default=None,
                    help="comma-separated grid+picker[+guard] (default: all grids x pickers)")
    ap.add_argument("--frame-size", default="1280x720",
                    help="WxH for records without a size (1280x720)")
    ap.add_argument("--per-event", action="store_true", help="also print one line per event")
    ap.add_argument("--gallery", metavar="DIR", help="write a before/after HTML gallery")
    ap.add_argument("--before", default=DEFAULT_BEFORE, help=f"gallery left ({DEFAULT_BEFORE})")
    ap.add_argument("--after", default=DEFAULT_AFTER, help=f"gallery right ({DEFAULT_AFTER})")
    ap.add_argument("--sheets", metavar="DIR", help="write one contact sheet per event")
    args = ap.parse_args(argv)

    with open(args.features, encoding="utf-8") as f:
        feats = json.load(f)
    size = tuple(int(v) for v in args.frame_size.lower().split("x"))
    variants = args.variants.split(",") if args.variants else default_variants()
    if args.gallery:
        variants += [v for v in (args.before, args.after) if v not in variants]
    try:
        events, summary = evaluate(feats, variants, args.threshold, size)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.per_event:
        for row in events:
            print(row["event"], " ".join(format_pick(row["picks"][v]) for v in variants))
    print("\n".join(format_summary(events, summary)))
    if args.gallery:
        print("gallery:", write_gallery(events, summary, args.gallery, args.before, args.after))
    if args.sheets:
        print("sheets:", write_sheets(feats, args.sheets, args.threshold, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
