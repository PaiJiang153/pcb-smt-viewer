#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared contract + helpers for all format backends.

Every backend module in this package must provide:

    FORMAT = "altium"                 # short id (used on the CLI: --format altium)
    LABEL  = "Altium PcbDoc"          # human readable name
    def sniff(root) -> bool           # root: file or directory; cheap, must not raise
    def parse(root, out_dir=None, **opts) -> dict

`parse()` returns a RAW dict in the canonical schema below; `finalize()` fills the
derived fields (bbox, per-component pad boxes, stats, warnings) so that backends
can stay small.  Everything is in **millimetres**, **Y axis up**; the origin is
arbitrary (the viewer normalises by the board bbox).

Canonical schema
----------------
title      : str
outline    : [ [[x,y], ...], ... ]                     board outline contour(s)
components : [ {d, fp, side, x, y, rot, val, comment, value, mpn, np} ]
pads       : [ {x, y, w, h, s, r, l, c, p, hole} ]
             s: 1 round, 2 rect, 3 octagon, 4 oblong ; l: TOP|BOTTOM|MULTILAYER
             c: index into components (or 65535)     ; p: pad name
tracks     : [ [x1,y1,x2,y2,width,layer] ]             layer: 1 top, 2 mid1, 3 mid2, 32 bottom
filltracks : same as tracks (Altium pour scan lines; may be empty)
arcs       : [ [cx,cy,r,start_deg,end_deg,width,layer] ]
silk       : [ [x1,y1,x2,y2,width,layer,comp] ]        layer: 33 top silk, 34 bottom silk
silkarcs   : [ [cx,cy,r,start_deg,end_deg,width,layer,comp] ]
vias       : [ [x,y,outer_dia,drill_dia] ]
polygons   : [ {layer, contours:[[[x,y],...]]} ]       copper areas / pours

`finalize()` adds: bbox, component bb/np/pin1, meta.stats, meta.warnings.
"""
import os
import shutil
import struct
import zipfile

# ---------------------------------------------------------------- constants
IU_PER_MIL = 10000.0          # Altium internal units per mil (1/10000 mil)
MM_PER_MIL = 0.0254

COPPER_TOP = 1
COPPER_MID1 = 2
COPPER_MID2 = 3
COPPER_BOTTOM = 32
SILK_TOP = 33
SILK_BOTTOM = 34
NO_COMPONENT = 65535

PAD_ROUND, PAD_RECT, PAD_OCT, PAD_OBLONG = 1, 2, 3, 4

# how many digits of mm we keep in the JSON
PREC = 4


def rnd(v):
    try:
        return round(float(v), PREC)
    except (TypeError, ValueError):
        return 0.0


def new_data(title):
    return {
        "title": title,
        "outline": [],
        "components": [],
        "pads": [],
        "tracks": [],
        "filltracks": [],
        "arcs": [],
        "silk": [],
        "silkarcs": [],
        "vias": [],
        "polygons": [],
        "meta": {"warnings": []},
    }


def add_warning(data, msg):
    data.setdefault("meta", {}).setdefault("warnings", [])
    if msg not in data["meta"]["warnings"]:
        data["meta"]["warnings"].append(msg)


# ------------------------------------------------------------------- input
def iter_files(root):
    """Yield every regular file under `root` (or `root` itself)."""
    if os.path.isfile(root):
        yield root
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            yield os.path.join(dirpath, f)


def pick_by_ext(root, exts, prefer=None):
    """First file whose name ends with one of `exts` (case insensitive).
    `prefer` may be a substring that wins if present.  Largest wins on ties."""
    exts = tuple(e.lower() for e in exts)
    hits = [p for p in iter_files(root)
            if p.lower().endswith(exts) and not os.path.basename(p).startswith(".")]
    if prefer:
        pref = [p for p in hits if prefer.lower() in os.path.basename(p).lower()]
        if pref:
            hits = pref
    hits.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return hits[0] if hits else None


def zip_member_name(info):
    """Decode a zip member name; Windows/GBK zips lack the UTF-8 flag and
    Python decodes them as cp437 mojibake."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def extract_zip(path, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            member = zip_member_name(info)
            target = os.path.normpath(os.path.join(dest, member))
            if not target.startswith(os.path.abspath(dest)):
                continue                      # never escape the extract dir
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest


def materialize(path, out_dir):
    """Return a filesystem root the backends can sniff.

    A .zip is extracted to ``<out_dir>/source/<name>/`` (kept so the user can
    inspect the original project); anything else is returned unchanged.
    """
    path = os.path.abspath(path)
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        dest = os.path.join(out_dir, "source", os.path.splitext(os.path.basename(path))[0])
        return extract_zip(path, dest)
    return path


# ------------------------------------------------------------------ geometry
def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _dist_point_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L == 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / L
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def arc_polyline(cx, cy, r, a0_deg, a1_deg, step_deg=3.0):
    """Flatten a circular arc into a list of [x, y] (degrees CCW, Y up)."""
    import math
    if not (r > 0):
        return [[cx, cy]]
    a0 = math.radians(a0_deg)
    a1 = math.radians(a1_deg)
    sweep = a1 - a0
    while sweep > math.pi:
        sweep -= 2 * math.pi
    while sweep < -math.pi:
        sweep += 2 * math.pi
    steps = max(1, int(abs(math.degrees(sweep)) / max(step_deg, 0.1)))
    return [[cx + r * math.cos(a0 + sweep * k / steps),
             cy + r * math.sin(a0 + sweep * k / steps)] for k in range(steps + 1)]


def pts_bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def point_in_contours(px, py, contours):
    inside = False
    for c in contours:
        n = len(c)
        if n < 3:
            continue
        j = n - 1
        for i in range(n):
            xi, yi = c[i]
            xj, yj = c[j]
            if ((yi > py) != (yj > py)) and \
                    (px < (xj - xi) * (py - yi) / (yj - yi + 1e-30) + xi):
                inside = not inside
            j = i
    return inside


def trim_outline_slivers(contours, near_tol=0.6, min_detour=3.0, max_iter=200):
    """Remove hairline 'cut line' artefacts from a board silhouette.

    Some CAD tools leave sub-1 mm slivers in the board shape (a long out-and-back
    excursion of the boundary).  This finds the excursion that starts and ends
    within `near_tol` mm of itself while being at least `min_detour` mm long and
    cuts it out.  Real features at least `near_tol` wide survive.
    Units: mm (input/output).  Returns (contours, removed_vertex_count).
    """
    out = []
    removed = 0
    for pts0 in contours:
        pts = [[float(x), float(y)] for x, y in pts0]
        before = len(pts)
        guard = 0
        while guard < max_iter:
            guard += 1
            n = len(pts)
            if n < 8:
                break
            max_gap = max(4, int(n * 0.5))
            best = None
            for i in range(n):
                for gap in range(2, max_gap):
                    j = (i + gap) % n
                    if _dist(pts[i], pts[j]) > near_tol:
                        continue
                    run = 0.0
                    for k in range(gap):
                        run += _dist(pts[(i + k) % n], pts[(i + k + 1) % n])
                    if run < min_detour:
                        continue
                    if best is None or run > best[0]:
                        best = (run, i, gap)
            if best is None:
                break
            _, i, gap = best
            if i + gap <= n - 1:
                del pts[i + 1:i + gap]
            else:
                pts = pts[i:] + pts[:i]
                del pts[1:gap]
        clean = []
        for q in pts:
            if not clean or _dist(clean[-1], q) > 0.02:
                clean.append(q)
        removed += max(0, before - len(clean))
        if len(clean) > 2:
            out.append([[rnd(x), rnd(y)] for x, y in clean])
    return out, removed


# ------------------------------------------------------------------ finalize
def finalize(data):
    """Fill the derived fields every backend would otherwise duplicate.

    Idempotent: values already present are kept, warnings are merged.
    """
    data.setdefault("title", "PCB")
    for key in ("outline", "components", "pads", "tracks", "filltracks",
                "arcs", "silk", "silkarcs", "vias", "polygons"):
        if not isinstance(data.get(key), list):
            data[key] = []
    meta = data.setdefault("meta", {})
    warnings = meta.setdefault("warnings", [])
    comps = data["components"]
    pads = data["pads"]

    # ---- per component: pad count / pad box / pin 1 --------------------
    by_comp = {}
    for p in pads:
        by_comp.setdefault(p.get("c", NO_COMPONENT), []).append(p)
    out_of_board = []
    for i, c in enumerate(comps):
        c.setdefault("d", "?")
        c.setdefault("side", "top")
        c.setdefault("fp", "")
        c.setdefault("rot", 0.0)
        ps = by_comp.get(i, [])
        if "np" not in c:
            c["np"] = len(ps)
        if ps and "bb" not in c:
            c["bb"] = [rnd(min(p["x"] - abs(p["w"]) / 2 for p in ps)),
                       rnd(min(p["y"] - abs(p["h"]) / 2 for p in ps)),
                       rnd(max(p["x"] + abs(p["w"]) / 2 for p in ps)),
                       rnd(max(p["y"] + abs(p["h"]) / 2 for p in ps))]
        if ps and "pin1" not in c:
            p1 = [p for p in ps if str(p.get("p", "")).strip() in ("1", "A1")]
            if p1:
                c["pin1"] = [rnd(p1[0]["x"]), rnd(p1[0]["y"])]

    # ---- bbox ----------------------------------------------------------
    if not data.get("bbox"):
        pts = [p for c in data["outline"] for p in c]
        if not pts:
            for p in pads:
                pts += [[p["x"] - abs(p["w"]) / 2, p["y"] - abs(p["h"]) / 2],
                        [p["x"] + abs(p["w"]) / 2, p["y"] + abs(p["h"]) / 2]]
        if not pts:
            for t in data["tracks"] + data["silk"]:
                pts += [[t[0], t[1]], [t[2], t[3]]]
        if not pts:
            for c in comps:
                if "bb" in c:
                    pts += [[c["bb"][0], c["bb"][1]], [c["bb"][2], c["bb"][3]]]
        data["bbox"] = [rnd(v) for v in pts_bbox(pts)] if pts else [0.0, 0.0, 10.0, 10.0]
    bx0, by0, bx1, by1 = data["bbox"]
    if bx1 - bx0 < 0.2:
        bx1 = bx0 + 0.2
    if by1 - by0 < 0.2:
        by1 = by0 + 0.2
    data["bbox"] = [rnd(bx0), rnd(by0), rnd(bx1), rnd(by1)]

    # ---- no outline at all: draw the bbox so the viewer has a board -----
    if not data["outline"]:
        m = 0.3
        data["outline"] = [[[rnd(bx0 - m), rnd(by0 - m)], [rnd(bx1 + m), rnd(by0 - m)],
                            [rnd(bx1 + m), rnd(by1 + m)], [rnd(bx0 - m), rnd(by1 + m)]]]
        add_warning(data, "没有解析到板框，用整板包围盒代替（形状不准，但坐标是对的）")

    # ---- sanity checks --------------------------------------------------
    seen = {}
    for c in comps:
        seen[c["d"]] = seen.get(c["d"], 0) + 1
    dup = sorted(d for d, n in seen.items() if n > 1)
    if dup:
        add_warning(data, "位号重复：%s" % ", ".join(dup[:10]))
    attributed = any(p.get("c", NO_COMPONENT) != NO_COMPONENT for p in pads)
    no_pads = [c["d"] for c in comps if not c.get("np")]
    if no_pads and pads and attributed:
        add_warning(data, "%d 个元件没有焊盘（只画了位置）：%s"
                    % (len(no_pads), ", ".join(no_pads[:10])))
    for c in comps:
        bb = c.get("bb")
        if not bb:
            continue
        if bb[0] < bx0 - 0.5 or bb[1] < by0 - 0.5 or bb[2] > bx1 + 0.5 or bb[3] > by1 + 0.5:
            out_of_board.append(c["d"])
    if out_of_board:
        add_warning(data, "%d 个元件在板框外：%s"
                    % (len(out_of_board), ", ".join(out_of_board[:10])))
    if not comps:
        add_warning(data, "没有解析到任何元件（这个文件可能只有板图，没有坐标/器件信息）")
    if not comps and not data["pads"] and not data["tracks"]:
        add_warning(data, "图形数据也是空的：这个文件可能不是 PCB（原理图/封装库？）")

    # ---- stats ----------------------------------------------------------
    copper_layers = sorted({t[5] for t in data["tracks"] if isinstance(t[5], int)} |
                           {a[6] for a in data["arcs"] if isinstance(a[6], int)})
    meta["stats"] = {
        "components": len(comps),
        "top": sum(1 for c in comps if c.get("side") == "top"),
        "bottom": sum(1 for c in comps if c.get("side") == "bottom"),
        "pads": len(pads),
        "routes": len(data["tracks"]),
        "pour_lines": len(data["filltracks"]),
        "arcs": len(data["arcs"]),
        "vias": len(data["vias"]),
        "silk": len(data["silk"]) + len(data["silkarcs"]),
        "pours": len(data["polygons"]),
        "board_mm": [rnd(data["bbox"][2] - data["bbox"][0]),
                     rnd(data["bbox"][3] - data["bbox"][1])],
        "outline_points": sum(len(c) for c in data["outline"]),
        "copper_layers": [int(x) for x in copper_layers],
    }
    return data
