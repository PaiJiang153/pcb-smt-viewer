#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嘉立创EDA 专业版 / EasyEDA Pro  ->  pcb_data.json

Output feeds build_html.py, which packs it into a single-file HTML "SMT 贴片定位图"
(board picture + component lookup + checklist).

Accepted inputs
---------------
  * ``.epro`` / ``.epro2`` / project ``.zip``  -- project archive.  Two generations
    exist on disk and both are supported:
      v2 (Pro 2.x) : ``project.json`` + ``PCB/<uuid>.epcb`` + ``FOOTPRINT/*.efoo``
      v3 (Pro 3.x) : ``project2.json`` + ``*.epru`` (unified document container)
  * ``.eprj3`` folder project                  -- ``pcb/*.epcb2`` + ``FOOTPRINT/*`` +
    the project index file (``*.eprj3``)
  * a single ``.epcb2`` / ``.epcb`` / ``.epru`` / ``.efoo`` file
  * legacy SQLite project ``.eprj`` / ``.eprj2``

Record model
------------
v3 (3.x) documents are JSON Lines, one object per line::

    {"type":"PAD","ticket":132,"id":"51e563e7"}||{"centerX":500,...}|

``||`` separates the envelope from the payload, the line ends with ``|`` + LF.
Documents are delimited by ``DOCHEAD`` rows.  Storage is append-only, so the same
id appears many times: the row with the highest ``ticket`` wins and an empty
payload is the deletion tombstone.

v2 (2.x) documents are JSON Lines of *positional arrays*::

    ["LINE", "e31615", 0, "$1N1378", 1, 667.96, -1225, 667.96, -1235, 10, 0]

Both are normalised to the same ``Row(type, id, ticket, payload-dict)`` shape, so
all the semantic code below is shared.

Coordinates
-----------
Raw units are **mil**, Y axis **down** (screen convention, both generations).
Everything is converted to **mm, Y up** in :func:`to_out`; the geometry helpers
therefore run directly in the output frame (this mirrors KiCad's EasyEDA Pro
importer, which is the de-facto reference implementation).

Rotation angles are stored as *visual* counter-clockwise degrees.  After the Y
flip the same number is CCW in the output frame, so it is emitted unchanged.
"""
import json
import math
import os
import sqlite3
import zipfile

from . import common  # noqa: E402
from .common import (  # noqa: F401
    COPPER_TOP, COPPER_MID1, COPPER_MID2, COPPER_BOTTOM, SILK_TOP, SILK_BOTTOM,
    NO_COMPONENT, PAD_ROUND, PAD_RECT, PAD_OBLONG,
    rnd, add_warning, iter_files,
)

MM_PER_MIL = 0.0254

# record-bearing file extensions (v3 first, then legacy v2)
RECORD_EXTS = (".epru", ".epcb2", ".esch2", ".epan2", ".elibu",
               ".epcb", ".efoo", ".esym", ".esch")
# files that can hold PCB / FOOTPRINT documents
PCB_DOC_EXTS = (".epru", ".epcb2", ".epcb")
FP_DOC_EXTS = (".epru", ".elibu", ".efoo")
ARCHIVE_EXTS = (".epro", ".epro2", ".zip", ".elibz", ".elibz2")
SQLITE_EXTS = (".eprj", ".eprj2", ".epro.db")

MAX_RECORD_BYTES = 96 * 1024 * 1024

# ---------------------------------------------------------------------------
# EasyEDA Pro layer ids  ->  canonical layer ids (see common.py)
#   1 top copper, 2 bottom copper, 3 top silk, 4 bottom silk,
#   5/6 solder mask, 7/8 paste, 9/10 assembly, 11 outline, 12 multi,
#   13 document, 14 mechanical, 15..46 inner 1..32, 47 hole
# ---------------------------------------------------------------------------
EE_LAYER_NAMES = {
    1: "TOP", 2: "BOTTOM", 3: "TOP_SILK", 4: "BOT_SILK",
    5: "TOP_SOLDER_MASK", 6: "BOT_SOLDER_MASK", 7: "TOP_PASTE_MASK",
    8: "BOT_PASTE_MASK", 9: "TOP_ASSEMBLY", 10: "BOT_ASSEMBLY",
    11: "OUTLINE", 12: "MULTI", 13: "DOCUMENT", 14: "MECHANICAL",
    47: "HOLE", 48: "COMPONENT_SHAPE", 49: "COMPONENT_MARKING",
    50: "PIN_SOLDERING", 51: "PIN_FLOATING", 52: "COMPONENT_MODEL",
    53: "3D_SHELL_OUTLINE", 54: "3D_SHELL_TOP", 55: "3D_SHELL_BOTTOM",
    56: "DRILL_DRAWING",
}
for _i in range(15, 47):
    EE_LAYER_NAMES[_i] = "INNER%d" % (_i - 14)

OUTLINE_LAYERS = (11,)

# inner copper 15..46 maps onto the two mid layers the viewer understands
def _map_inner(ee_layer):
    if ee_layer <= 15:
        return COPPER_MID1
    if ee_layer == 16:
        return COPPER_MID2
    return COPPER_MID1


def copper_layer(ee_layer):
    """EasyEDA layer id -> canonical copper layer id, or None if not copper."""
    try:
        ee_layer = int(ee_layer)
    except (TypeError, ValueError):
        return None
    if ee_layer == 1:
        return COPPER_TOP
    if ee_layer == 2:
        return COPPER_BOTTOM
    if 15 <= ee_layer <= 46:
        return _map_inner(ee_layer)
    return None


def silk_layer(ee_layer):
    """EasyEDA layer id -> canonical silk layer id, or None."""
    try:
        ee_layer = int(ee_layer)
    except (TypeError, ValueError):
        return None
    if ee_layer == 3:
        return SILK_TOP
    if ee_layer == 4:
        return SILK_BOTTOM
    return None


def pad_layer_name(ee_layer, bottom_component=False):
    """PAD `layerId` is restricted to top / bottom / multi.

    Footprint pads are authored on the top layer; when the component sits on the
    bottom the pad follows it (this is what the editor and KiCad both do).
    """
    try:
        ee_layer = int(ee_layer)
    except (TypeError, ValueError):
        ee_layer = 1
    if ee_layer == 12:
        return "MULTILAYER"
    if ee_layer == 2:
        return "BOTTOM"
    if ee_layer == 1:
        return "BOTTOM" if bottom_component else "TOP"
    # odd layer: treat like top/bottom by parity
    return "BOTTOM" if bottom_component else "TOP"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _f(v, default=0.0):
    try:
        if isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        if isinstance(v, bool):
            return int(v)
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _s(v, default=""):
    if v is None:
        return default
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return ("%g" % v)
    return str(v)


def to_out(x, y):
    """EasyEDA raw (mil, Y down) -> canonical (mm, Y up)."""
    return rnd(_f(x) * MM_PER_MIL), rnd(-_f(y) * MM_PER_MIL)


def sz(v):
    """EasyEDA raw length (mil) -> mm."""
    return rnd(_f(v) * MM_PER_MIL)


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _read_text(path):
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_RECORD_BYTES)
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("latin-1", "replace")


def _head_bytes(path, n=64):
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def is_zip_file(path):
    return _head_bytes(path, 4) == b"PK\x03\x04"


def is_sqlite_file(path):
    return _head_bytes(path, 16) == b"SQLite format 3\x00"


# ---------------------------------------------------------------------------
# document / row model
# ---------------------------------------------------------------------------
class Row(object):
    __slots__ = ("type", "id", "ticket", "inner")

    def __init__(self, type_, id_="", ticket=0, inner=None):
        self.type = type_ or ""
        self.id = id_ or ""
        self.ticket = ticket
        self.inner = inner

    def get(self, key, default=None):
        if isinstance(self.inner, dict):
            v = self.inner.get(key, default)
            return default if v is None else v
        return default

    def gs(self, key, default=""):
        return _s(self.get(key, default), default)


class Doc(object):
    __slots__ = ("doc_type", "uuid", "title", "rows", "source", "v2")

    def __init__(self, doc_type="", uuid="", rows=None, source="", v2=False):
        self.doc_type = (doc_type or "").upper()
        self.uuid = uuid or ""
        self.title = ""
        self.rows = rows if rows is not None else []
        self.source = source
        self.v2 = bool(v2)


# ---------------------------------------------------------------------------
# v3 reader:  {"type":...}||{...}|   with DOCHEAD-delimited documents
# ---------------------------------------------------------------------------
def parse_v3_text(text, source=""):
    """Split an EasyEDA Pro v3 JSON-Lines blob into documents.

    Mirrors the reference decoder: envelope/payload split on the first ``||``,
    one trailing ``|`` removed, documents delimited by ``DOCHEAD``, append-only
    log de-duplicated by id keeping the highest ticket, empty payload = deletion.
    """
    docs = []
    cur = None
    order = []          # ids in first-seen order (stable output)
    by_id = {}
    skipped = 0

    def flush():
        if cur is None:
            return
        rows = []
        for rid in order:
            row = by_id[rid]
            if row.inner is None:
                continue            # deletion tombstone
            rows.append(row)
        cur.rows = rows
        if cur.doc_type or rows:
            docs.append(cur)

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        sep = line.find("||")
        if sep < 0:
            # tolerate a missing/inner-empty payload written without the
            # trailing record terminator
            if line.endswith("|"):
                line = line[:-1]
                sep = line.find("||")
        if sep < 0:
            skipped += 1
            continue
        outer_s = line[:sep].strip()
        inner_s = line[sep + 2:]
        if inner_s.endswith("|"):
            inner_s = inner_s[:-1]
        inner_s = inner_s.strip()
        try:
            outer = json.loads(outer_s) if outer_s else {}
        except ValueError:
            skipped += 1
            continue
        if not isinstance(outer, dict):
            skipped += 1
            continue
        rtype = _s(outer.get("type")).upper()
        try:
            inner = json.loads(inner_s) if inner_s else None
        except ValueError:
            inner = None
        if rtype == "DOCHEAD":
            flush()
            cur = Doc(uuid=_s(inner.get("uuid")) if isinstance(inner, dict) else "",
                      source=source)
            if isinstance(inner, dict):
                cur.doc_type = _s(inner.get("docType")).upper()
            order = []
            by_id = {}
            continue
        if cur is None:
            cur = Doc(source=source)
            order = []
            by_id = {}
        rid = _s(outer.get("id"))
        if not rid:
            rid = "__line_%d" % lineno
        ticket = _i(outer.get("ticket"), 0)
        row = Row(rtype, rid, ticket, inner)
        prev = by_id.get(rid)
        if prev is None:
            by_id[rid] = row
            order.append(rid)
        elif ticket >= prev.ticket:
            by_id[rid] = row
    flush()
    return docs, skipped


# ---------------------------------------------------------------------------
# v2 reader:  ["TYPE", positional, ...]
# ---------------------------------------------------------------------------
def _v2_pad(arr):
    hole = arr[9] if len(arr) > 9 else None
    shape = arr[10] if len(arr) > 10 else None
    inner = {
        "netName": _s(arr[3] if len(arr) > 3 else ""),
        "layerId": _i(arr[4] if len(arr) > 4 else 1, 1),
        "num": _s(arr[5] if len(arr) > 5 else ""),
        "centerX": _f(arr[6] if len(arr) > 6 else 0),
        "centerY": _f(arr[7] if len(arr) > 7 else 0),
        "padAngle": _f(arr[8] if len(arr) > 8 else 0),
        "hole": None,
        "defaultPad": None,
    }
    if isinstance(hole, list) and len(hole) >= 3:
        ht = _s(hole[0]).upper()
        if ht in ("ROUND", "SLOT", "RECT") and (_f(hole[1]) > 0 or _f(hole[2]) > 0):
            inner["hole"] = {"holeType": "SLOT" if ht == "SLOT" else ht,
                             "width": _f(hole[1]), "height": _f(hole[2])}
    if isinstance(shape, list) and shape:
        st = _s(shape[0]).upper()
        if st == "RECT":
            inner["defaultPad"] = {"padType": "RECT",
                                   "width": _f(shape[1] if len(shape) > 1 else 0),
                                   "height": _f(shape[2] if len(shape) > 2 else 0),
                                   "radius": _f(shape[3] if len(shape) > 3 else 0)}
        elif st in ("ELLIPSE", "OVAL"):
            inner["defaultPad"] = {"padType": st,
                                   "width": _f(shape[1] if len(shape) > 1 else 0),
                                   "height": _f(shape[2] if len(shape) > 2 else 0)}
        elif st in ("POLY", "POLYGON") and len(shape) > 1:
            inner["defaultPad"] = {"padType": "POLY", "path": shape[1],
                                   "width": 0, "height": 0}
    if inner["defaultPad"] is None:
        inner["defaultPad"] = {"padType": "RECT", "width": 0, "height": 0}
    if len(arr) > 15 and _isnum(arr[15]):
        inner["plated"] = _i(arr[15]) != 0
    return inner


def _v2_inner(rtype, arr):
    """Convert one positional v2 record into a v3-shaped payload dict."""
    if rtype == "LINE":
        return {"netName": _s(arr[3] if len(arr) > 3 else ""),
                "layerId": _i(arr[4] if len(arr) > 4 else 1, 1),
                "startX": _f(arr[5] if len(arr) > 5 else 0),
                "startY": _f(arr[6] if len(arr) > 6 else 0),
                "endX": _f(arr[7] if len(arr) > 7 else 0),
                "endY": _f(arr[8] if len(arr) > 8 else 0),
                "width": _f(arr[9] if len(arr) > 9 else 0)}
    if rtype == "ARC":
        return {"netName": _s(arr[3] if len(arr) > 3 else ""),
                "layerId": _i(arr[4] if len(arr) > 4 else 1, 1),
                "startX": _f(arr[5] if len(arr) > 5 else 0),
                "startY": _f(arr[6] if len(arr) > 6 else 0),
                "endX": _f(arr[7] if len(arr) > 7 else 0),
                "endY": _f(arr[8] if len(arr) > 8 else 0),
                "angle": _f(arr[9] if len(arr) > 9 else 0),
                "width": _f(arr[10] if len(arr) > 10 else 0)}
    if rtype == "VIA":
        return {"netName": _s(arr[3] if len(arr) > 3 else ""),
                "centerX": _f(arr[5] if len(arr) > 5 else 0),
                "centerY": _f(arr[6] if len(arr) > 6 else 0),
                "holeDiameter": _f(arr[7] if len(arr) > 7 else 0),
                "viaDiameter": _f(arr[8] if len(arr) > 8 else 0)}
    if rtype == "PAD":
        return _v2_pad(arr)
    if rtype in ("POLY", "FILL", "POUR", "REGION", "IMAGE", "TEARDROP"):
        inner = {"layerId": _i(arr[4] if len(arr) > 4 else 1, 1),
                 "netName": _s(arr[3] if len(arr) > 3 else "")}
        if rtype == "POLY":
            # v2 has no `polyType`; the board outline is identified by layer 11.
            inner["width"] = _f(arr[5] if len(arr) > 5 else 0)
            inner["path"] = arr[6] if len(arr) > 6 else []
            inner["polyType"] = "NORMAL"
        elif rtype == "FILL":
            inner["width"] = _f(arr[5] if len(arr) > 5 else 0)
            inner["path"] = arr[7] if len(arr) > 7 else []
        elif rtype == "POUR":
            inner["width"] = _f(arr[5] if len(arr) > 5 else 0)
            inner["name"] = _s(arr[6] if len(arr) > 6 else "")
            inner["order"] = _i(arr[7] if len(arr) > 7 else 0)
            inner["path"] = arr[8] if len(arr) > 8 else []
        else:                                    # REGION
            inner["layerId"] = _i(arr[3] if len(arr) > 3 else 1, 1)
            inner["width"] = _f(arr[4] if len(arr) > 4 else 0)
            inner["path"] = arr[6] if len(arr) > 6 else []
        return inner
    if rtype == "COMPONENT":
        return {"layerId": _i(arr[3] if len(arr) > 3 else 1, 1),
                "x": _f(arr[4] if len(arr) > 4 else 0),
                "y": _f(arr[5] if len(arr) > 5 else 0),
                "angle": _f(arr[6] if len(arr) > 6 else 0),
                "attrs": arr[7] if len(arr) > 7 and isinstance(arr[7], dict) else {}}
    if rtype == "ATTR":
        return {"parentId": _s(arr[3] if len(arr) > 3 else ""),
                "layerId": _i(arr[4] if len(arr) > 4 else 3, 3),
                "x": arr[5] if len(arr) > 5 else None,
                "y": arr[6] if len(arr) > 6 else None,
                "key": _s(arr[7] if len(arr) > 7 else ""),
                "value": _s(arr[8] if len(arr) > 8 else "")}
    if rtype == "STRING":
        return {"layerId": _i(arr[3] if len(arr) > 3 else 3, 3),
                "x": _f(arr[4] if len(arr) > 4 else 0),
                "y": _f(arr[5] if len(arr) > 5 else 0),
                "text": _s(arr[6] if len(arr) > 6 else "")}
    if rtype == "PAD_NET":
        return {"compId": _s(arr[1] if len(arr) > 1 else ""),
                "padNumber": _s(arr[2] if len(arr) > 2 else ""),
                "padNet": _s(arr[3] if len(arr) > 3 else "")}
    return None


def parse_v2_text(text, uuid="", source=""):
    """Parse a v2 JSON-Lines document (positional arrays)."""
    doc_type = ""
    rows = []
    skipped = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            # an empty line separates footprint data from PCB data in .efoo
            if doc_type == "FOOTPRINT" and rows:
                break
            continue
        try:
            arr = json.loads(s)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(arr, list):
            skipped += 1
            continue
        if not arr:                       # `[]` separator
            if doc_type == "FOOTPRINT" and rows:
                break
            continue
        rtype = _s(arr[0]).upper()
        if rtype == "DOCTYPE":
            doc_type = _s(arr[1]).upper() if len(arr) > 1 else doc_type
            continue
        inner = _v2_inner(rtype, arr)
        if inner is None:
            continue
        rid = _s(arr[1]) if len(arr) > 1 else ""
        rows.append(Row(rtype, rid, 0, inner))
    return Doc(doc_type, uuid, rows, source, v2=True), skipped


# ---------------------------------------------------------------------------
# geometry: EasyEDA Pro polygon path  ->  contours in the OUTPUT frame (mm, Y up)
# ---------------------------------------------------------------------------
def _circle_pts(cx, cy, r, step_deg=5.0):
    if r <= 0:
        return []
    steps = max(8, int(360.0 / step_deg))
    return [[cx + r * math.cos(2 * math.pi * k / steps),
             cy + r * math.sin(2 * math.pi * k / steps)] for k in range(steps)]


def _rect_pts(sx, sy, ex, ey, angle_deg=0.0, rotation_centre=None):
    """Axis-aligned rectangle in output coords, then optional rotation (CCW)."""
    pts = [[sx, sy], [ex, sy], [ex, ey], [sx, ey]]
    if abs(angle_deg) > 1e-9:
        cx, cy = rotation_centre if rotation_centre else (sx, sy)
        a = math.radians(angle_deg)
        ca, sa = math.cos(a), math.sin(a)
        out = []
        for x, y in pts:
            dx, dy = x - cx, y - cy
            out.append([cx + dx * ca - dy * sa, cy + dx * sa + dy * ca])
        pts = out
    return pts


def _arc_points(px, py, ex, ey, angle_deg, step_deg=4.0):
    """Points along the arc from (px,py) to (ex,ey) with included angle
    `angle_deg` (positive = counter-clockwise), computed in the output frame."""
    if abs(angle_deg) < 1e-6 or abs(angle_deg) >= 359.999:
        return [[ex, ey]]
    dx, dy = ex - px, ey - py
    chord = math.hypot(dx, dy)
    if chord < 1e-9:
        return [[ex, ey]]
    half = math.radians(angle_deg) / 2.0
    cdist = (chord / 2.0) / math.tan(half)
    mx, my = (px + ex) / 2.0, (py + ey) / 2.0
    # +90 deg perpendicular of the chord
    nx, ny = -dy / chord, dx / chord
    cx, cy = mx + nx * cdist, my + ny * cdist
    r = math.hypot(px - cx, py - cy)
    if r < 1e-9:
        return [[ex, ey]]
    a0 = math.atan2(py - cy, px - cx)
    sweep = math.radians(angle_deg)
    steps = max(2, int(abs(angle_deg) / step_deg) + 1)
    out = []
    for k in range(1, steps + 1):
        a = a0 + sweep * k / steps
        out.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    out[-1] = [ex, ey]
    return out


def _bezier_points(p0, p1, p2, p3, steps=12):
    out = []
    for k in range(1, steps + 1):
        t = float(k) / steps
        mt = 1.0 - t
        x = (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0]
             + 3 * mt * t * t * p2[0] + t ** 3 * p3[0])
        y = (mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1]
             + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])
        out.append([x, y])
    return out


def decode_path(path, fill_angle_scale=1.0):
    """Decode one EasyEDA Pro single-polygon path into contours.

    Returns ``[[[x,y], ...], ...]`` in the **output** frame (mm, Y up).  A path
    normally yields a single contour; the standalone ``CIRCLE`` / ``R`` forms and
    degenerate inputs are handled too.
    """
    if not isinstance(path, list) or not path:
        return []
    if isinstance(path[0], str):
        head = path[0].upper()
        if head == "CIRCLE" and len(path) >= 4:
            cx, cy = to_out(path[1], path[2])
            return [_circle_pts(cx, cy, sz(path[3]))]
        if head == "R" and len(path) >= 5:
            x, y = _f(path[1]), _f(path[2])
            w, h = _f(path[3]), _f(path[4])
            rot = _f(path[5]) if len(path) > 5 else 0.0
            sx, sy = to_out(x, y)
            ex, ey = to_out(x + w, y - h)     # R's y is the top edge
            return [_rect_pts(sx, sy, ex, ey, rot, (sx, sy))]
        return []

    pts = []
    i = 0
    n = len(path)
    prev = None
    if n >= 2 and _isnum(path[0]) and _isnum(path[1]):
        prev = to_out(path[0], path[1])
        pts.append(prev)
        i = 2
    while i < n:
        tok = path[i]
        if _isnum(tok):
            # stray coordinate pair: treat as a line-to
            if i + 1 < n and _isnum(path[i + 1]):
                prev = to_out(tok, path[i + 1])
                pts.append(prev)
                i += 2
            else:
                i += 1
            continue
        cmd = _s(tok).upper()
        if cmd == "L":
            i += 1
            while i + 1 < n and _isnum(path[i]) and _isnum(path[i + 1]):
                prev = to_out(path[i], path[i + 1])
                pts.append(prev)
                i += 2
            continue
        if cmd in ("ARC", "CARC"):
            if i + 3 < n:
                ang = _f(path[i + 1]) / (fill_angle_scale or 1.0)
                ex, ey = _f(path[i + 2]), _f(path[i + 3])
                if prev is not None:
                    exo, eyo = to_out(ex, ey)
                    pts.extend(_arc_points(prev[0], prev[1], exo, eyo, ang))
                    prev = pts[-1] if pts else prev
                i += 4
            else:
                i += 1
            continue
        if cmd == "C":
            if i + 6 < n and prev is not None:
                c1 = to_out(path[i + 1], path[i + 2])
                c2 = to_out(path[i + 3], path[i + 4])
                p3 = to_out(path[i + 5], path[i + 6])
                pts.extend(_bezier_points(prev, c1, c2, p3))
                prev = p3
                i += 7
            else:
                i += 1
            continue
        i += 1
    # drop duplicate closing point
    if len(pts) > 2:
        if abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][1] - pts[-1][1]) < 1e-6:
            pts.pop()
    # two points is a legitimate (open) segment -- board outlines are often
    # drawn as separate line pieces
    return [pts] if len(pts) >= 2 else []


def decode_complex_polygon(path, fill_angle_scale=1.0):
    """``BOARD`` / ``POUR`` / ``REGION`` style path: array of single polygons."""
    if not isinstance(path, list) or not path:
        return []
    if isinstance(path[0], str):
        return decode_path(path, fill_angle_scale)
    contours = []
    for sub in path:
        contours.extend(decode_path(sub, fill_angle_scale))
    return contours


def decode_path_list(path, fill_angle_scale=1.0):
    """``FILL`` style path: the sub-paths are independent polygons."""
    if not isinstance(path, list) or not path:
        return []
    if isinstance(path[0], str):
        return decode_path(path, fill_angle_scale)
    contours = []
    for sub in path:
        if isinstance(sub, list) and sub and isinstance(sub[0], str):
            contours.extend(decode_path(sub, fill_angle_scale))
        elif isinstance(sub, list) and sub and _isnum(sub[0]):
            contours.extend(decode_path(sub, fill_angle_scale))
    return contours


def _contour_bbox(contours):
    xs, ys = [], []
    for c in contours:
        for x, y in c:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _contour_area(contours):
    total = 0.0
    for c in contours:
        n = len(c)
        if n < 3:
            continue
        s = 0.0
        for i in range(n):
            x1, y1 = c[i]
            x2, y2 = c[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        total += abs(s) / 2.0
    return total


# ---------------------------------------------------------------------------
# input discovery
# ---------------------------------------------------------------------------
class Inputs(object):
    __slots__ = ("kind", "source_dir", "docs", "project", "skipped", "notes")

    def __init__(self):
        self.kind = ""
        self.source_dir = ""
        self.docs = []
        self.project = {}
        self.skipped = 0
        self.notes = []


def _load_json_file(path):
    txt = _read_text(path)
    if not txt.strip():
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def _looks_like_record_file(path):
    """Cheap content sniff: does this file hold EasyEDA Pro records?"""
    head = _head_bytes(path, 4096).decode("latin-1", "replace")
    if '||{"' in head or '||{\"' in head:
        return True
    stripped = head.lstrip()
    if stripped.startswith('["DOCTYPE"') or stripped.startswith('["PAD"') \
            or stripped.startswith('["LINE"'):
        return True
    if stripped.startswith('{"type"'):
        return True
    return False


def _collect_record_files(root):
    """Return (pcb_files, fp_files, other_files) of record-bearing files."""
    pcb, fp, other = [], [], []
    for path in iter_files(root):
        name = os.path.basename(path).lower()
        if name.startswith("."):
            continue
        try:
            if os.path.getsize(path) > MAX_RECORD_BYTES:
                continue
        except OSError:
            continue
        low = name
        if low.endswith(PCB_DOC_EXTS):
            pcb.append(path)
        elif low.endswith(FP_DOC_EXTS):
            fp.append(path)
        elif low.endswith((".epcb2", ".esch2", ".epan2", ".esym", ".esch")):
            other.append(path)
        elif "." not in low and _looks_like_record_file(path):
            other.append(path)          # extension-less dump (rare)
    return pcb, fp, other


def _guess_doc_type(path, rows):
    """Infer the document kind when a file carries no DOCHEAD."""
    if not rows:
        return ""
    name = os.path.basename(path).lower()
    types = {r.type for r in rows}
    if name.endswith((".efoo", ".elibu")):
        return "FOOTPRINT"
    if "COMPONENT" in types or "BOARD" in types or "VIA" in types or "LINE" in types:
        return "PCB"
    if "PAD" in types or "FILL" in types or "POLY" in types:
        return "FOOTPRINT" if name.endswith((".efoo", ".elibu")) else "PCB"
    return ""


def _read_doc_file(path):
    """Read one record file into documents (auto-detects the v2/v3 dialect)."""
    text = _read_text(path)
    if not text.strip():
        return [], 0
    stripped = text.lstrip()
    head = stripped[:400]
    uuid = os.path.splitext(os.path.basename(path))[0]

    if head.startswith("["):
        doc, skipped = parse_v2_text(text, uuid=uuid, source=path)
        if not doc.rows and not doc.doc_type:
            return [], skipped
        if not doc.doc_type:
            doc.doc_type = _guess_doc_type(path, doc.rows)
        return [doc], skipped

    docs, skipped = parse_v3_text(text, source=path)
    if not docs:
        doc, sk = parse_v2_text(text, uuid=uuid, source=path)
        if doc.rows:
            if not doc.doc_type:
                doc.doc_type = _guess_doc_type(path, doc.rows)
            return [doc], skipped + sk
        return [], skipped
    for doc in docs:
        if not doc.uuid:
            doc.uuid = uuid
        if not doc.doc_type:
            doc.doc_type = _guess_doc_type(path, doc.rows)
    return docs, skipped


def _load_project_index(root):
    """Find project.json / project2.json / *.eprj3 index anywhere under root."""
    cands = []
    for path in iter_files(root):
        low = os.path.basename(path).lower()
        if low in ("project2.json", "project.json"):
            cands.append(path)
        elif low.endswith(".eprj3") or low.endswith(".eprj2.json"):
            cands.append(path)
    # project2.json (v3) first, then project.json (v2)
    cands.sort(key=lambda p: (0 if "project2" in os.path.basename(p).lower() else
                              1 if os.path.basename(p).lower() == "project.json" else 2))
    for path in cands:
        data = _load_json_file(path)
        if isinstance(data, dict) and any(
                k in data for k in ("pcbs", "boards", "footprints", "devices")):
            return data
    return {}


def _sqlite_docs(path):
    """Read a legacy ``.eprj`` / ``.eprj2`` SQLite project.

    The schema is not part of the (renamed) documented format, so this is done
    defensively: find every TEXT/BLOB column that contains EasyEDA Pro records
    and parse those.  Returns (docs, project_index, note).
    """
    docs = []
    notes = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
    except sqlite3.Error as exc:
        return [], {}, ["无法打开 SQLite 工程：%s" % exc]
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall() if r and r[0]]
        project = {}
        for table in tables:
            try:
                cur.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
            except sqlite3.Error:
                continue
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                for col, value in zip(cols, row):
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                    if not isinstance(value, str) or len(value) < 8:
                        continue
                    head = value.lstrip()[:400]
                    is_v3 = "||{" in head or '"type"' in head
                    is_v2 = head.startswith('["')
                    if not (is_v3 or is_v2):
                        continue
                    if is_v3:
                        found, _sk = parse_v3_text(value, source="%s:%s" % (table, col))
                    else:
                        doc, _sk = parse_v2_text(value, source="%s:%s" % (table, col))
                        found = [doc] if doc.rows else []
                    for d in found:
                        docs.append(d)
        # try to find a project index blob
        for table in tables:
            try:
                cur.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
            except sqlite3.Error:
                continue
            for row in cur.fetchall():
                for value in row:
                    if isinstance(value, bytes):
                        continue
                    if not isinstance(value, str) or '"pcbs"' not in value:
                        continue
                    data = _load_json_file_str(value)
                    if isinstance(data, dict) and "pcbs" in data:
                        project = data
                        break
        if docs:
            notes.append("从 SQLite 工程读出 %d 个文档" % len(docs))
        else:
            notes.append("这个 SQLite 工程里没找到 EasyEDA Pro 记录（表：%s）"
                         % ", ".join(tables[:8]))
    except sqlite3.Error as exc:
        notes.append("SQLite 读取失败：%s" % exc)
    finally:
        try:
            con.close()
        except Exception:
            pass
    return docs, project, notes


def _load_json_file_str(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def find_inputs(root, out_dir=None, pcb_hint=None):
    """Resolve any accepted input form into an :class:`Inputs`."""
    root = os.path.abspath(root)
    if not os.path.exists(root):
        raise SystemExit("输入不存在: %s" % root)
    inp = Inputs()

    # ---- 1. archive / sqlite / single record file --------------------------
    if os.path.isfile(root):
        low = root.lower()
        if is_zip_file(root):
            base_dir = os.path.abspath(out_dir or ".")
            dest = os.path.join(base_dir, "source",
                                os.path.splitext(os.path.basename(root))[0])
            try:
                common.extract_zip(root, dest)
                inp.source_dir = dest
                inp.kind = "archive"
            except (zipfile.BadZipFile, OSError) as exc:
                raise SystemExit("解压 EasyEDA Pro 工程失败：%s" % exc)
        elif is_sqlite_file(root):
            docs, project, notes = _sqlite_docs(root)
            inp.kind = "sqlite"
            inp.docs = docs
            inp.project = project
            inp.notes.extend(notes)
            return inp
        elif low.endswith(".eprj3") and not is_zip_file(root):
            # the folder project's index file was pointed at: use the folder
            parent = os.path.dirname(root)
            inp.source_dir = parent
            inp.kind = "folder"
            sub = find_inputs(parent, out_dir, pcb_hint)
            inp.docs = sub.docs
            inp.project = sub.project or _load_project_index(parent)
            inp.skipped += sub.skipped
            return inp
        elif low.endswith(RECORD_EXTS) or _looks_like_record_file(root):
            docs, skipped = _read_doc_file(root)
            inp.kind = "document"
            inp.docs = docs
            inp.skipped += skipped
            inp.source_dir = os.path.dirname(root)
            return inp
        else:
            raise SystemExit(
                "认不出这个 EasyEDA Pro 工程：%s\n"
                "  需要 .epro / .epro2 / 工程 .zip / .eprj3 文件夹 / .eprj(.eprj2) "
                "SQLite / .epcb2 单文件" % root)

    # ---- 2. directory ------------------------------------------------------
    elif os.path.isdir(root):
        inp.source_dir = root
        low = root.lower()
        inp.kind = "folder" if not low.endswith((".zip", "_unzipped")) else "archive"
        # a bare .eprj / .eprj2 sitting in a folder
        for path in iter_files(root):
            if os.path.basename(path).lower().endswith(SQLITE_EXTS) and is_sqlite_file(path):
                docs, project, notes = _sqlite_docs(path)
                inp.kind = "sqlite"
                inp.docs = docs
                inp.project = project
                inp.notes.extend(notes)
                return inp
    else:
        raise SystemExit("无法识别的输入: %s" % root)

    inp.project = _load_project_index(inp.source_dir)

    pcb_files, fp_files, other = _collect_record_files(inp.source_dir)
    # .epru containers hold everything; make sure they are read for both roles
    files = []
    for path in pcb_files + fp_files + other:
        if path not in files:
            files.append(path)
    # prefer PCB-sized docs first so a huge library does not dominate
    files.sort(key=lambda p: (0 if p.lower().endswith(PCB_DOC_EXTS) else 1,
                              os.path.basename(p).lower()))

    for path in files:
        docs, skipped = _read_doc_file(path)
        inp.skipped += skipped
        inp.docs.extend(docs)

    if not inp.docs:
        raise SystemExit(
            "在 %s 里没找到 EasyEDA Pro（嘉立创EDA专业版）工程数据。\n"
            "  期望：project.json/project2.json + PCB/*.epcb2|*.epcb，或 *.epru" % root)
    return inp


# ---------------------------------------------------------------------------
# board extraction
# ---------------------------------------------------------------------------
def _pick_pcb_doc(docs, pcb_hint=None):
    pcbs = [d for d in docs if d.doc_type == "PCB"]
    if not pcbs:
        return None
    if pcb_hint:
        hit = [d for d in pcbs if pcb_hint.lower() in (d.uuid + d.title).lower()]
        if hit:
            pcbs = hit
    # the PCB with the most geometry wins
    def weight(d):
        n = 0
        for row in d.rows:
            if row.type in ("LINE", "PAD", "COMPONENT", "VIA", "POLY",
                            "FILL", "POUR", "REGION", "BOARD"):
                n += 1
        return n
    pcbs.sort(key=weight, reverse=True)
    return pcbs[0]


def _footprints_by_uuid(docs):
    """Index footprint documents by uuid and by their own package name.

    The name comes from the document's own ``Footprint`` attribute; older
    containers (e.g. SQLite projects) do not always carry a uuid for the
    document row, so the name is the only reliable key there.
    """
    out = {}
    for d in docs:
        if d.doc_type != "FOOTPRINT":
            continue
        if d.uuid:
            out[d.uuid] = d
        for row in d.rows:
            if row.type == "ATTR" and row.gs("key") == "Footprint":
                name = row.gs("value")
                if name:
                    out.setdefault(name, d)
                    d.title = name
                break
    return out


def _fp_lookup(fpmap, fp_uuid, notes=None):
    """Resolve a component's footprint reference."""
    if not fp_uuid:
        return None
    if fp_uuid in fpmap:
        return fpmap[fp_uuid]
    # a project carrying exactly one footprint document: unambiguous fallback,
    # but make it visible -- pads could belong to a different package
    uniq = []
    for doc in fpmap.values():
        if not any(doc is u for u in uniq):
            uniq.append(doc)
    if len(uniq) == 1:
        if notes is not None:
            msg = ("有元件的封装引用在工程里找不到（%s），已退回唯一的封装文件"
                   % fp_uuid[:12])
            if msg not in notes:
                notes.append(msg)
        return uniq[0]
    return None


def _outline_chains(pcb_doc):
    """All board-outline geometry as open point chains (output frame)."""
    chains = []
    for row in pcb_doc.rows:
        t = row.type
        if t == "POLY" and _i(row.get("layerId"), 0) in OUTLINE_LAYERS:
            for cont in decode_path(row.get("path")):
                if len(cont) >= 2:
                    chains.append([[p[0], p[1]] for p in cont])
        elif t == "LINE" and _i(row.get("layerId"), 0) in OUTLINE_LAYERS:
            chains.append([list(to_out(row.get("startX"), row.get("startY"))),
                           list(to_out(row.get("endX"), row.get("endY")))])
        elif t == "ARC" and _i(row.get("layerId"), 0) in OUTLINE_LAYERS:
            ang = _f(row.get("angle"))
            p1 = to_out(row.get("startX"), row.get("startY"))
            p2 = to_out(row.get("endX"), row.get("endY"))
            if abs(ang) < 1e-6 or abs(ang) >= 359.999:
                chains.append([list(p1), list(p2)])
            else:
                chains.append([list(p1)] + _arc_points(p1[0], p1[1], p2[0], p2[1], ang))
        elif t in ("REGION", "FILL") and _i(row.get("layerId"), 0) in OUTLINE_LAYERS:
            conts = (decode_path_list(row.get("path")) if t == "FILL"
                     else decode_complex_polygon(row.get("path")))
            for cont in conts:
                if len(cont) >= 2:
                    chains.append([[p[0], p[1]] for p in cont])
    return chains


def _chain_near(a, b, tol):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _stitch_chains(chains, tol=0.15):
    """Join touching segments into closed loops (board outlines are often drawn
    as dozens of separate polylines / arcs / circles)."""
    pool = [c for c in chains if len(c) >= 2]
    loops = []
    while pool:
        cur = pool.pop(0)
        progressed = True
        while progressed and len(pool):
            progressed = False
            if _chain_near(cur[0], cur[-1], tol):
                break
            for idx, other in enumerate(pool):
                if _chain_near(cur[-1], other[0], tol):
                    cur = cur + other[1:]
                elif _chain_near(cur[-1], other[-1], tol):
                    cur = cur + list(reversed(other))[1:]
                elif _chain_near(cur[0], other[-1], tol):
                    cur = other[:-1] + cur
                elif _chain_near(cur[0], other[0], tol):
                    cur = list(reversed(other))[:-1] + cur
                else:
                    continue
                pool.pop(idx)
                progressed = True
                break
        if len(cur) > 3 and _chain_near(cur[0], cur[-1], tol):
            cur = cur[:-1]
        if len(cur) >= 3:
            loops.append(cur)
    loops.sort(key=lambda c: _contour_area([c]), reverse=True)
    return loops


def _centroid(contour):
    n = len(contour)
    if not n:
        return 0.0, 0.0
    return (sum(p[0] for p in contour) / n, sum(p[1] for p in contour) / n)


def point_in_contours(px, py, contours):
    return common.point_in_contours(px, py, contours)


def _outline_from_doc(pcb_doc):
    """Return (contours, source-label) in output coords, or ([], "")."""
    # 1. the dedicated BOARD singleton (v3) is authoritative when present
    for row in pcb_doc.rows:
        if row.type == "BOARD":
            cont = decode_complex_polygon(row.get("path"))
            cont = [c for c in cont if len(c) > 2]
            if cont:
                return cont, "BOARD"

    # 2. v3 POLY explicitly flagged as the board outline
    explicit = []
    for row in pcb_doc.rows:
        if row.type == "POLY" and row.gs("polyType").upper() == "BOARD_OUTLINE":
            explicit.extend(decode_path(row.get("path")))
    explicit = [c for c in explicit if len(c) > 2]
    if explicit:
        return explicit, "POLY:BOARD_OUTLINE"

    # 3. everything on the outline layer, stitched into loops: keep the outer
    #    boundary and the loops nested inside it (cut-outs / mounting holes),
    #    drop stray marks that would inflate the board box
    chains = _outline_chains(pcb_doc)
    loops = _stitch_chains(chains)
    if loops:
        outer = loops[0]
        keep = [outer]
        for loop in loops[1:]:
            cx, cy = _centroid(loop)
            if point_in_contours(cx, cy, [outer]):
                keep.append(loop)
        return keep, "POLY-stitched"

    # 4. last resort: a board-level REGION / FILL on the multi layer
    for row in pcb_doc.rows:
        if row.type in ("REGION", "FILL") and _i(row.get("layerId"), 0) in (11, 12):
            cont = decode_path_list(row.get("path"))
            cont = [c for c in cont if len(c) > 2]
            if cont:
                return cont, row.type
    return [], ""


# ---------------------------------------------------------------------------
# component / pad expansion
# ---------------------------------------------------------------------------
def _device_value(project, device_uuid):
    devices = project.get("devices") if isinstance(project, dict) else None
    if not isinstance(devices, dict):
        return {}
    dev = devices.get(device_uuid)
    if not isinstance(dev, dict):
        return {}
    attrs = dev.get("attributes")
    return {"title": _s(dev.get("title")), "attributes": attrs if isinstance(attrs, dict) else {}}


_TEMPLATE_RE = None


def _resolve_template(text, attrs):
    """Resolve EasyEDA ``={Key}`` value templates against the attribute map."""
    global _TEMPLATE_RE
    if not text or "={" not in text:
        return text
    import re
    if _TEMPLATE_RE is None:
        _TEMPLATE_RE = re.compile(r"=\{([^}]+)\}")
    return _TEMPLATE_RE.sub(lambda m: _s(attrs.get(m.group(1).strip(), "")), text)


def _device_fields(project, device_uuid, local_attrs=None):
    """Return dict of display fields for a component."""
    info = _device_value(project, device_uuid)
    attrs = dict(info.get("attributes") or {})
    if local_attrs:
        for k, v in local_attrs.items():
            attrs.setdefault(k, v)
    name = _resolve_template(_s(attrs.get("Name")), attrs)
    value = _resolve_template(_s(attrs.get("Value")), attrs) or name
    mfr = _resolve_template(_s(attrs.get("Manufacturer Part")), attrs)
    lcsc = _resolve_template(_s(attrs.get("Supplier Part")), attrs)
    title = info.get("title") or ""
    return {
        "val": title,
        "value": value,
        "comment": mfr or lcsc or title,
        "mpn": mfr or lcsc,
        "lcsc": lcsc,
    }


def _fp_title(project, fp_uuid, fp_doc):
    footprints = project.get("footprints") if isinstance(project, dict) else None
    if isinstance(footprints, dict):
        fp = footprints.get(fp_uuid)
        if isinstance(fp, dict):
            t = _s(fp.get("title"))
            if t:
                return t
        elif isinstance(fp, str) and fp:
            return fp
    if fp_doc is not None:
        for row in fp_doc.rows:
            if row.type == "ATTR" and row.gs("key") == "Footprint":
                v = row.gs("value")
                if v:
                    return v
    return fp_uuid[:8] if fp_uuid else ""


def _pad_wh_from_path(path):
    cont = decode_path_list(path)
    bb = _contour_bbox(cont)
    if not bb:
        return 0.0, 0.0
    return bb[2] - bb[0], bb[3] - bb[1]


def _pad_entry(row, comp_index, bottom, transform):
    """Build one canonical pad dict from a PAD row.

    ``transform`` maps footprint-local output-frame coordinates to board-global
    coordinates; ``None`` means the row is already board-global (v3 board-level
    pads) and only the slot alignment is applied.
    """
    lx, ly = to_out(row.get("centerX"), row.get("centerY"))
    rot = _f(row.get("padAngle"))
    ee_layer = _i(row.get("layerId"), 1)

    pad_def = row.get("defaultPad")
    if not isinstance(pad_def, dict):
        pad_def = {}
    ptype = _s(pad_def.get("padType"), "RECT").upper()
    poly_path = pad_def.get("path")
    if ptype in ("POLY", "POLYGON"):
        w, h = _pad_wh_from_path(poly_path)
        shape = PAD_RECT
        if w <= 0 or h <= 0:
            w = h = max(w, h, 0.3)
    elif ptype == "ELLIPSE":
        w = sz(pad_def.get("width"))
        h = sz(pad_def.get("height"))
        shape = PAD_ROUND if abs(w - h) < 0.02 else PAD_OBLONG
    elif ptype == "OVAL":
        w = sz(pad_def.get("width"))
        h = sz(pad_def.get("height"))
        shape = PAD_OBLONG
    else:                                   # RECT
        w = sz(pad_def.get("width"))
        h = sz(pad_def.get("height"))
        shape = PAD_RECT

    hole = row.get("hole")
    hole_d = 0.0
    if isinstance(hole, dict) and hole:
        hw, hh = sz(hole.get("width")), sz(hole.get("height"))
        if hw > 0 or hh > 0:
            hole_d = min(hw, hh) if (hw > 0 and hh > 0) else max(hw, hh)

    if transform is None:
        px, py = lx, ly
    else:
        px, py = transform((lx, ly), rot, bottom, hole_d > 0)

    return {
        "x": rnd(px), "y": rnd(py),
        "w": rnd(w), "h": rnd(h),
        "s": shape,
        "r": rnd((-rot) % 360.0),
        "l": pad_layer_name(ee_layer, bottom),
        "c": comp_index,
        "p": row.gs("num"),
        "hole": rnd(hole_d),
    }


class _ComponentTransform(object):
    """Places a footprint-local point on the board.

    Mirrors the reference implementation: convert to the output frame, mirror
    vertically when the part is on the bottom, rotate counter-clockwise by the
    component angle and translate to the component origin.
    """

    __slots__ = ("ox", "oy", "angle", "bottom", "cos", "sin")

    def __init__(self, ox, oy, angle, bottom):
        self.ox, self.oy = ox, oy
        self.angle = angle
        self.bottom = bottom
        a = math.radians(angle)
        self.cos, self.sin = math.cos(a), math.sin(a)

    def __call__(self, local_out, pad_rot=0.0, bottom=None, thru=False):
        x, y = local_out
        if self.bottom:
            y = -y
        rx = x * self.cos - y * self.sin
        ry = x * self.sin + y * self.cos
        return self.ox + rx, self.oy + ry


def _silk_segments_from_path(path, width, layer, comp, out, fill_angle_scale=1.0):
    """Flatten a POLY path into silk line segments."""
    for contour in decode_path(path, fill_angle_scale):
        if len(contour) < 2:
            continue
        pts = list(contour) + [contour[0]]
        for k in range(len(pts) - 1):
            x1, y1 = pts[k]
            x2, y2 = pts[k + 1]
            if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
                continue
            out.append([rnd(x1), rnd(y1), rnd(x2), rnd(y2), width, layer, comp])


# ---------------------------------------------------------------------------
# main assembly
# ---------------------------------------------------------------------------
def build_data(inp, title=None, pcb_hint=None):
    data = common.new_data("PCB")
    warnings = data["meta"]["warnings"]
    project = inp.project or {}
    docs = inp.docs

    pcb_doc = _pick_pcb_doc(docs, pcb_hint)
    if pcb_doc is None:
        raise SystemExit("没有在工程里找到 PCB 文档（只有原理图/符号？）")
    fpmap = _footprints_by_uuid(docs)

    # v2 documents store polygon arc angles x10 inside FILL data (see the
    # reference importer); v3 stores them verbatim.
    fill_angle_scale = 10.0 if getattr(pcb_doc, "v2", False) else 1.0

    # ---- title ---------------------------------------------------------
    pcbs = project.get("pcbs") if isinstance(project, dict) else None
    boards = project.get("boards") if isinstance(project, dict) else None
    auto_title = ""
    if isinstance(boards, dict) and boards:
        for bname, bval in boards.items():
            if isinstance(bval, dict) and _s(bval.get("pcb")) == pcb_doc.uuid:
                auto_title = _s(bname)
                break
        if not auto_title:
            auto_title = _s(next(iter(boards.keys())))
    if not auto_title and isinstance(pcbs, dict):
        entry = pcbs.get(pcb_doc.uuid)
        if isinstance(entry, dict):
            auto_title = _s(entry.get("title"))
        elif isinstance(entry, str):
            auto_title = entry
    if not auto_title:
        for row in pcb_doc.rows:
            if row.type == "META":
                auto_title = row.gs("title")
                if auto_title:
                    break
    if not auto_title:
        auto_title = os.path.splitext(os.path.basename(pcb_doc.source))[0] or "PCB"
    data["title"] = title or auto_title

    # ---- board outline -------------------------------------------------
    outline, how = _outline_from_doc(pcb_doc)
    if outline:
        data["outline"] = [[[rnd(x), rnd(y)] for x, y in c] for c in outline]
    else:
        add_warning(data, "没有解析到板框（BOARD / 板框层 POLY），改用整板包围盒")

    # ---- components ----------------------------------------------------
    comp_rows = [r for r in pcb_doc.rows if r.type == "COMPONENT"]
    attr_by_parent = {}
    for row in pcb_doc.rows:
        if row.type != "ATTR":
            continue
        parent = row.gs("parentId")
        if parent:
            attr_by_parent.setdefault(parent, []).append(row)

    comps = []
    transforms = []
    comp_fp_docs = []          # footprint Doc per component (or None)
    for order, row in enumerate(comp_rows):
        attrs = {}
        for a in attr_by_parent.get(row.id, []):
            key = a.gs("key")
            if key:
                attrs[key] = a.gs("value")
        local_attrs = row.get("attrs")
        if not isinstance(local_attrs, dict):
            local_attrs = {}
        for k, v in local_attrs.items():
            if k and k not in attrs:
                attrs[k] = _s(v)

        device_uuid = _s(attrs.get("Device"))
        fp_uuid = _s(attrs.get("Footprint"))
        if not fp_uuid:
            dev_attrs = _device_value(project, device_uuid).get("attributes") or {}
            fp_uuid = _s(dev_attrs.get("Footprint"))
        if not fp_uuid:
            fp_uuid = _s(local_attrs.get("Footprint"))
        fp_doc = _fp_lookup(fpmap, fp_uuid, warnings)

        fp_name = _fp_title(project, fp_uuid, fp_doc) or _s(local_attrs.get("Origin Footprint"))
        if not fp_name:
            fp_name = fp_uuid[:8] if fp_uuid else ""

        fields = _device_fields(project, device_uuid, local_attrs)

        ee_layer = _i(row.get("layerId"), 1)
        bottom = (ee_layer == 2)
        ox, oy = to_out(row.get("x"), row.get("y"))
        angle = _f(row.get("angle"))
        # EasyEDA stores the *screen* rotation; the output frame is Y-up, i.e.
        # mirrored, so the geometric (counter-clockwise) angle is -angle.
        # Verified against track/pad net coincidence on real boards.
        geom_angle = -angle
        designator = _s(attrs.get("Designator")).strip()

        comp = {
            "d": designator or ("U%d" % (order + 1)),
            "fp": fp_name,
            "side": "bottom" if bottom else "top",
            "x": ox, "y": oy,
            "rot": rnd(geom_angle % 360.0),
            "val": fields["val"],
            "comment": fields["comment"],
            "value": fields["value"],
            "mpn": fields["mpn"],
        }
        if fields.get("lcsc") and fields["lcsc"] != fields["mpn"]:
            comp["lcsc"] = fields["lcsc"]
        comps.append(comp)
        transforms.append(_ComponentTransform(ox, oy, geom_angle, bottom))
        comp_fp_docs.append(fp_doc)
    data["components"] = comps

    if comp_rows and not any(comp_fp_docs):
        add_warning(data, "找到了 %d 个元件，但没有对应的封装库文件（FOOTPRINT/*.efoo），"
                          "所以没有焊盘可画" % len(comp_rows))

    # ---- pads ----------------------------------------------------------
    pads = []
    for idx, fp_doc in enumerate(comp_fp_docs):
        if fp_doc is None:
            continue
        bottom = comps[idx]["side"] == "bottom"
        tr = transforms[idx]
        for fprow in fp_doc.rows:
            if fprow.type != "PAD":
                continue
            try:
                pads.append(_pad_entry(fprow, idx, bottom, tr))
            except Exception:
                continue
    # board-level pads (absolute coordinates, no component)
    for row in pcb_doc.rows:
        if row.type == "PAD":
            try:
                pads.append(_pad_entry(row, NO_COMPONENT, False, None))
            except Exception:
                continue
    data["pads"] = pads


    # ---- copper tracks / arcs / silk ----------------------------------
    tracks, arcs = [], []
    silk, silkarcs = [], []
    for row in pcb_doc.rows:
        t = row.type
        if t == "LINE":
            ee_layer = _i(row.get("layerId"), 0)
            cu = copper_layer(ee_layer)
            x1, y1 = to_out(row.get("startX"), row.get("startY"))
            x2, y2 = to_out(row.get("endX"), row.get("endY"))
            w = sz(row.get("width"))
            if cu is not None:
                tracks.append([x1, y1, x2, y2, w, cu])
            else:
                sl = silk_layer(ee_layer)
                if sl is not None:
                    silk.append([x1, y1, x2, y2, w, sl, NO_COMPONENT])
        elif t == "ARC":
            ee_layer = _i(row.get("layerId"), 0)
            ang = _f(row.get("angle"))
            w = sz(row.get("width"))
            if abs(ang) < 1e-6 or abs(ang) >= 359.999:
                # degenerate: straight line
                x1, y1 = to_out(row.get("startX"), row.get("startY"))
                x2, y2 = to_out(row.get("endX"), row.get("endY"))
                if copper_layer(ee_layer) is not None:
                    tracks.append([x1, y1, x2, y2, w, copper_layer(ee_layer)])
                elif silk_layer(ee_layer) is not None:
                    silk.append([x1, y1, x2, y2, w, silk_layer(ee_layer), NO_COMPONENT])
                continue
            entry = _arc_entry(row, ang, w)
            if entry is None:
                continue
            cu = copper_layer(ee_layer)
            sl = silk_layer(ee_layer)
            if cu is not None:
                arcs.append(entry[:6] + [cu])
            elif sl is not None:
                silkarcs.append(entry + [sl, NO_COMPONENT])
        elif t == "POLY":
            ee_layer = _i(row.get("layerId"), 0)
            sl = silk_layer(ee_layer)
            if sl is not None:
                _silk_segments_from_path(row.get("path"), sz(row.get("width")),
                                         sl, NO_COMPONENT, silk)
    data["tracks"] = tracks
    data["arcs"] = arcs
    data["silk"] = silk
    data["silkarcs"] = silkarcs

    # ---- footprint-level silk (nice part outlines on the board) --------
    fp_silk = []
    for idx, fp_doc in enumerate(comp_fp_docs):
        if fp_doc is None:
            continue
        tr = transforms[idx]
        for fprow in fp_doc.rows:
            if fprow.type not in ("POLY", "LINE", "ARC"):
                continue
            ee_layer = _i(fprow.get("layerId"), 0)
            sl = silk_layer(ee_layer)
            if sl is None:
                continue
            w = sz(fprow.get("width"))
            segments = []
            if fprow.type == "POLY":
                for contour in decode_path(fprow.get("path")):
                    if len(contour) < 2:
                        continue
                    pts = list(contour) + [contour[0]]
                    for k in range(len(pts) - 1):
                        segments.append((pts[k], pts[k + 1]))
            elif fprow.type == "LINE":
                segments.append((to_out(fprow.get("startX"), fprow.get("startY")),
                                 to_out(fprow.get("endX"), fprow.get("endY"))))
            else:                                   # ARC
                ang = _f(fprow.get("angle"))
                p1 = to_out(fprow.get("startX"), fprow.get("startY"))
                p2 = to_out(fprow.get("endX"), fprow.get("endY"))
                if abs(ang) < 1e-6 or abs(ang) >= 359.999:
                    segments.append((p1, p2))
                else:
                    pts = [p1] + _arc_points(p1[0], p1[1], p2[0], p2[1], ang)
                    for k in range(len(pts) - 1):
                        segments.append((pts[k], pts[k + 1]))
            for p1, p2 in segments:
                a1 = tr(p1)
                a2 = tr(p2)
                if abs(a1[0] - a2[0]) < 1e-6 and abs(a1[1] - a2[1]) < 1e-6:
                    continue
                fp_silk.append([rnd(a1[0]), rnd(a1[1]), rnd(a2[0]), rnd(a2[1]),
                                w, sl, idx])
    data["silk"].extend(fp_silk)

    # ---- vias ----------------------------------------------------------
    vias = []
    for row in pcb_doc.rows:
        if row.type != "VIA":
            continue
        x, y = to_out(row.get("centerX"), row.get("centerY"))
        vias.append([x, y, sz(row.get("viaDiameter")), sz(row.get("holeDiameter"))])
    data["vias"] = vias

    # ---- copper pours (outline only; POURED results are skipped) -------
    polygons = []
    for row in pcb_doc.rows:
        if row.type not in ("FILL", "POUR", "REGION"):
            continue
        ee_layer = _i(row.get("layerId"), 0)
        cu = copper_layer(ee_layer)
        if cu is None or cu not in (COPPER_TOP, COPPER_BOTTOM):
            continue
        path = row.get("path")
        if row.type == "FILL":
            cont = decode_path_list(path, fill_angle_scale)
        else:
            cont = decode_complex_polygon(path, fill_angle_scale)
        if not cont:
            continue
        cont = [[[rnd(x), rnd(y)] for x, y in c] for c in cont if len(c) > 2]
        if not cont:
            continue
        polygons.append({"layer": "TOP" if cu == COPPER_TOP else "BOTTOM",
                         "contours": cont})
    data["polygons"] = polygons

    if any(r.type == "POURED" for r in pcb_doc.rows):
        add_warning(data, "覆铜结果（POURED）已跳过，只画了覆铜边框（POUR/FILL）")
    if inp.skipped:
        add_warning(data, "有 %d 行记录无法解析，已跳过" % inp.skipped)

    # ---- source meta ---------------------------------------------------
    data["meta"]["source"] = {
        "format": FORMAT,
        "kind": inp.kind,
        "pcb_doc": pcb_doc.uuid or pcb_doc.source,
        "source_dir": inp.source_dir,
        "outline_from": how,
        "documents": len(docs),
        "footprints": len(fpmap),
    }
    for note in inp.notes:
        add_warning(data, note)
    return data


def _arc_entry(row, angle_deg, width_mm):
    """Canonical arc entry ``[cx, cy, r, a0, a1, w]`` in the output frame."""
    sx, sy = to_out(row.get("startX"), row.get("startY"))
    ex, ey = to_out(row.get("endX"), row.get("endY"))
    dx, dy = ex - sx, ey - sy
    chord = math.hypot(dx, dy)
    if chord < 1e-9:
        return None
    half = math.radians(angle_deg) / 2.0
    if abs(math.tan(half)) < 1e-12:
        return None
    cdist = (chord / 2.0) / math.tan(half)
    # the stored angle is visual CCW, so the centre sits on the +90 deg side
    nx, ny = -dy / chord, dx / chord
    cx = (sx + ex) / 2.0 + nx * cdist
    cy = (sy + ey) / 2.0 + ny * cdist
    r = math.hypot(sx - cx, sy - cy)
    if r < 1e-9:
        return None
    a0 = math.degrees(math.atan2(sy - cy, sx - cx))
    a1 = math.degrees(math.atan2(ey - cy, ex - cx))
    sweep_ccw = (a1 - a0) % 360.0
    target = abs(angle_deg) % 360.0
    if abs(sweep_ccw - target) > abs((360.0 - sweep_ccw) - target):
        a0, a1 = a1, a0
        sweep_ccw = (a1 - a0) % 360.0
    a1 = a0 + sweep_ccw
    return [rnd(cx), rnd(cy), rnd(r), rnd(a0), rnd(a1), rnd(width_mm)]


# ---------------------------------------------------------------------------
# backend interface (see parsers/common.py)
# ---------------------------------------------------------------------------
FORMAT = "easyeda_pro"
LABEL = "嘉立创EDA 专业版 / EasyEDA Pro (.epro/.eprj3/.eprj)"


def sniff(root):
    """Cheap, non-throwing claim test for an EasyEDA Pro project."""
    try:
        root = os.path.abspath(root)
    except Exception:
        return False
    try:
        if os.path.isfile(root):
            if is_sqlite_file(root):
                return _sniff_sqlite(root)
            if is_zip_file(root):
                try:
                    with zipfile.ZipFile(root) as z:
                        names = z.namelist()
                except (zipfile.BadZipFile, OSError):
                    return False
                for name in names:
                    low = os.path.basename(name).lower()
                    if low in ("project.json", "project2.json"):
                        return True
                    if low.endswith((".epcb", ".epcb2", ".epru")):
                        return True
                return False
            low = root.lower()
            if low.endswith(".eprj3"):
                return sniff(os.path.dirname(root))
            if low.endswith(PCB_DOC_EXTS):
                return True
            if low.endswith((".efoo", ".elibu", ".esym", ".esch", ".esch2", ".epan2")):
                return False            # library / schematic only, not a board
            return _looks_like_record_file(root)
        if os.path.isdir(root):
            seen = 0
            for path in iter_files(root):
                seen += 1
                if seen > 3000:          # cheap: never walk a whole home dir
                    return False
                low = os.path.basename(path).lower()
                if low.startswith("."):
                    continue
                if low in ("project.json", "project2.json"):
                    return True
                if low.endswith(".eprj3") or low.endswith(".epcb2"):
                    return True
                if low.endswith((".epcb", ".epru")):
                    return True
                if low.endswith(SQLITE_EXTS) and is_sqlite_file(path):
                    return True
            return False
    except Exception:
        return False
    return False


def _sniff_sqlite(path):
    """legacy .eprj / .eprj2 project database?"""
    try:
        if path.lower().endswith(SQLITE_EXTS):
            return True
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [(_s(r[0]) if r else "") for r in cur.fetchall()]
            if not tables:
                return False
            cur.execute('SELECT * FROM "%s" LIMIT 5' % tables[0].replace('"', '""'))
            for row in cur.fetchall():
                for value in row:
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-8", "replace")
                        except Exception:
                            continue
                    if not isinstance(value, str) or len(value) < 16:
                        continue
                    if "||{" in value[:400] or '["DOCTYPE"' in value[:400]:
                        return True
        finally:
            con.close()
    except sqlite3.Error:
        return False
    except Exception:
        return False
    return False


def parse(root, out_dir=None, title=None, pcb_hint=None, **_kw):
    """Parse an EasyEDA Pro project into the canonical schema (see common.py)."""
    out_dir = out_dir or "."
    inp = find_inputs(root, out_dir, pcb_hint)
    data = build_data(inp, title=title, pcb_hint=pcb_hint)
    return data


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="嘉立创EDA 专业版 / EasyEDA Pro -> pcb_data.json")
    ap.add_argument("input", help=".epro / .epro2 / 工程 .zip / .eprj3 文件夹 / .eprj / .epcb2")
    ap.add_argument("output", nargs="?", default="pcb_data.json")
    ap.add_argument("--title", help="网页标题（默认取工程名）")
    ap.add_argument("--pcb", help="有多个板时按名称关键词选择")
    args = ap.parse_args()

    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."
    os.makedirs(out_dir, exist_ok=True)
    data = parse(args.input, out_dir, title=args.title, pcb_hint=args.pcb)
    data = common.finalize(data)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    st = data["meta"]["stats"]
    print("板尺寸 : %.2f x %.2f mm" % tuple(st["board_mm"]))
    print("元件   : %d（顶层 %d / 底层 %d）"
          % (st["components"], st["top"], st["bottom"]))
    print("焊盘   : %d  过孔 %d  走线 %d  丝印 %d" % (st["pads"], st["vias"],
                                                st["routes"], st["silk"]))
    for w in data["meta"].get("warnings", []):
        print("⚠️ ", w)
    print("数据   :", os.path.abspath(args.output))


if __name__ == "__main__":
    main()
