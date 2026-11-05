#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Altium .PcbDoc (+ optional .SchDoc)  ->  pcb_data.json

Output feeds build_html.py, which packs it into a single-file HTML "SMT 贴片定位图"
(board picture + component lookup + checklist).

Input may be:
  * a .PcbDoc file
  * a project directory (the .PcbDoc/.SchDoc inside are auto-detected)
  * a .zip exported from Altium / WeChat / email (auto-extracted to <outdir>/source/)

Binary layout reference: KiCad pcbnew/pcb_io/altium/altium_parser.ksy
Coordinate system: Altium internal unit = 1/10000 mil, Y axis up -> output in mm, Y still up.
"""
import argparse
import json
import math
import os
import shutil
import struct
import sys
import zipfile

try:
    import olefile
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "需要 olefile 才能读取 Altium 的 OLE 复合文档。\n"
        "  python3 -m pip install --user olefile\n"
        "  （或者：apt install python3-olefile / pip3 install olefile）\n")
    raise

from . import common   # noqa: E402  (shared contract + helpers)
from .common import (IU_PER_MIL, MM_PER_MIL, rnd, add_warning,     # noqa: F401
                     trim_outline_slivers)

LAYER_NAMES = {
    1: "TOP", 32: "BOTTOM", 74: "MULTILAYER",
    33: "TOPOVERLAY", 34: "BOTTOMOVERLAY",
    35: "TOPPASTE", 36: "BOTTOMPASTE",
    37: "TOPSOLDER", 38: "BOTTOMSOLDER",
    41: "KEEPOUT",
}
for _i in range(2, 32):
    LAYER_NAMES[_i] = "MID%d" % (_i - 1)

REC = {0x01: "ARC", 0x02: "PAD", 0x03: "VIA", 0x04: "TRACK",
       0x05: "TEXT", 0x06: "FILL", 0x0B: "REGION", 0x0C: "BODY"}


# ---------------------------------------------------------------- utilities
def mm(v_iu):
    return round(v_iu * MM_PER_MIL / IU_PER_MIL, 4)


def mm_mil(v_mil):
    return round(v_mil * MM_PER_MIL, 4)


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def remaining(self):
        return len(self.d) - self.p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def skip(self, n):
        self.p += n

    def xy(self):
        return (self.i32(), self.i32())

    def sub(self):
        """length-prefixed subrecord -> independent Reader"""
        n = self.u32()
        r = Reader(self.d, self.p)
        r.limit = self.p + n
        self.p += n
        return r, n


def decode_text(raw):
    """Altium text fields are ASCII or (for Chinese) GBK; fall back to latin-1."""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def text_records(data, stream_name=""):
    """PcbDoc text streams: u32 length + '|k=v|k=v...' records."""
    out = []
    p = 0
    n = len(data)
    while p + 4 <= n:
        ln = struct.unpack_from("<I", data, p)[0]
        if ln == 0 or p + 4 + ln > n:
            break
        payload = data[p + 4:p + 4 + ln]
        if not payload.startswith(b"|"):
            break
        out.append(decode_text(payload))
        p += 4 + ln
    return out


def props(rec):
    d = {}
    for f in rec.split("|"):
        if "=" in f:
            k, v = f.split("=", 1)
            d[k] = v
    return d


def p_mil(d, key):
    v = d.get(key, "0mil").strip()
    try:
        if v.endswith("mil"):
            return float(v[:-3])
        if v.endswith("mm"):
            return float(v[:-2]) / MM_PER_MIL
        return float(v)
    except ValueError:
        return 0.0


def p_float(d, key, default=0.0):
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------- region / polygon vertices
def flatten_vertices(verts, max_step=2.0):
    """Altium closed contour: KIND==0 straight vertex, KIND!=0 arc that starts
    at the vertex and ends at the endpoint derived from C/R/SA/EA
    (mirrors KiCad's HelperShapeLineChainFromAltiumVertices)."""
    n = len(verts)
    if n == 0:
        return []
    start = 0
    for i in range(n):
        if not verts[i]["kind"]:
            start = i
            break
    order = list(range(start, n)) + list(range(0, start))
    pts = []

    def add(v):
        if not pts or abs(pts[-1][0] - v[0]) > 1e-7 or abs(pts[-1][1] - v[1]) > 1e-7:
            pts.append(list(v))

    for i in order:
        v = verts[i]
        if not v["kind"]:
            add((v["x"], v["y"]))
            continue
        cx, cy, r = v["cx"], v["cy"], v["r"]
        sa = math.radians(v["sa"])
        ea = math.radians(v["ea"])
        start_pt = (cx + r * math.cos(sa), cy + r * math.sin(sa))
        end_pt = (cx + r * math.cos(ea), cy + r * math.sin(ea))
        vp = (v["x"], v["y"])
        d_sa = math.hypot(start_pt[0] - vp[0], start_pt[1] - vp[1])
        d_ea = math.hypot(end_pt[0] - vp[0], end_pt[1] - vp[1])
        angle = math.radians((v["ea"] - v["sa"]) % 360.0)
        if d_sa <= d_ea:
            sweep, end = angle, end_pt
        else:
            sweep, end = -angle, start_pt
        add(vp)
        steps = max(1, int(abs(math.degrees(sweep)) / max_step))
        a0 = math.atan2(vp[1] - cy, vp[0] - cx)
        for k in range(1, steps):
            a = a0 + sweep * k / steps
            add((cx + r * math.cos(a), cy + r * math.sin(a)))
        add(end)
    if len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1e-6 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-6:
        pts.pop()
    return [pts] if len(pts) > 2 else []


def read_vertices(p):
    verts = []
    i = 0
    while ("VX%d" % i) in p:
        verts.append({
            "kind": int(p.get("KIND%d" % i, "0") or 0),
            "x": p_mil(p, "VX%d" % i), "y": p_mil(p, "VY%d" % i),
            "cx": p_mil(p, "CX%d" % i), "cy": p_mil(p, "CY%d" % i),
            "sa": p_float(p, "SA%d" % i), "ea": p_float(p, "EA%d" % i),
            "r": p_mil(p, "R%d" % i),
        })
        i += 1
    return verts


# -------------------------------------------------------- board shape stream
def parse_board_regions(data):
    """BoardRegions/Data -> list of contours ([x,y] in mil)."""
    contours = []
    p = 0
    while p + 5 <= len(data):
        marker = data[p]
        if marker != 0x0B:
            raise ValueError("unexpected BoardRegions marker 0x%02x" % marker)
        sub_len = struct.unpack_from("<I", data, p + 1)[0]
        s = data[p + 5:p + 5 + sub_len]
        if len(s) < sub_len:
            break
        hole_count = struct.unpack_from("<H", s, 14)[0]
        props_len = struct.unpack_from("<I", s, 18)[0]
        off = 22 + props_len
        parts = []
        for _ in range(hole_count + 1):
            n = struct.unpack_from("<I", s, off)[0]
            off += 4
            part = []
            for _ in range(n):
                x, y = struct.unpack_from("<dd", s, off)
                off += 16
                part.append([x / IU_PER_MIL, y / IU_PER_MIL])
            parts.append(part)
        if parts and len(parts[0]) > 2:
            contours.append(parts[0])
        contours.extend(q for q in parts[1:] if len(q) > 2)
        p = p + 5 + sub_len
    return contours


# --------------------------------------------------------------- schematic
def _sch_props(rec):
    d = {}
    for f in rec.split("|"):
        if "=" in f:
            k, v = f.split("=", 1)
            d[k] = v
    return d


def read_schdoc_params(path):
    """designator -> {Comment, Value, ManufacturerPartNumber, ...}"""
    ole = olefile.OleFileIO(path)
    data = ole.openstream("FileHeader").read()
    recs = []
    p = 0
    while p + 4 <= len(data):
        ln = struct.unpack_from("<I", data, p)[0]
        if ln == 0 or p + 4 + ln > len(data):
            break
        recs.append(decode_text(data[p + 4:p + 4 + ln]))
        p += 4 + ln
    desig = {}
    params = {}
    for rec in recs:
        o = _sch_props(rec)
        kind = o.get("RECORD")
        if kind == "34" and o.get("Name") == "Designator":
            desig[o.get("OwnerIndex")] = o.get("Text", "")
        elif kind == "41":
            params.setdefault(o.get("OwnerIndex"), {})[o.get("Name", "")] = o.get("Text", "")
    return {des: params.get(owner, {}) for owner, des in desig.items()}


# ---------------------------------------------------------- input discovery
def zip_member_name(info):
    """Decode a zip member name.  Windows/Altium zips are often GBK encoded
    without the UTF-8 flag, which Python decodes as cp437 mojibake."""
    name = info.filename
    if info.flag_bits & 0x800:              # already UTF-8
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


PCB_EXTS = (".pcbdoc", ".pcb")
SCH_EXTS = (".schdoc", ".sch")


def _ole_streams(path):
    try:
        ole = olefile.OleFileIO(path)
        names = {"%s" % "/".join(e) for e in ole.listdir()}
        ole.close()
        return names
    except Exception:
        return set()


def looks_like_pcb(path, names=None):
    """Altium PCB?  (OLE compound file with the PcbDoc record streams)"""
    try:
        if os.path.getsize(path) < 4096:
            return False
    except OSError:
        return False
    names = names if names is not None else _ole_streams(path)
    if not names:
        return False
    return any(n.split("/")[0] in ("Board6", "Components6", "Pads6", "Polygons6")
               for n in names)


def looks_like_pcblib(path, names=None):
    names = names if names is not None else _ole_streams(path)
    return "Library/Data" in names or "Library" in names


def find_inputs(path, out_dir, pcb_hint=None):
    """Resolve any input form to (pcb_path, sch_path, extra_pcbs, source_dir)."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SystemExit("输入不存在: %s" % path)

    source_dir = None
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        source_dir = os.path.join(out_dir, "source",
                                  os.path.splitext(os.path.basename(path))[0])
        os.makedirs(source_dir, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                member = zip_member_name(info)
                target = os.path.normpath(os.path.join(source_dir, member))
                if not target.startswith(os.path.abspath(source_dir)):
                    continue                    # never escape the extract dir
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    elif os.path.isdir(path):
        source_dir = path
    elif os.path.isfile(path):
        source_dir = os.path.dirname(path)
        if not looks_like_pcb(path):
            if looks_like_pcblib(path):
                raise SystemExit("这是封装库(.PcbLib)，不是 PCB 工程：%s" % path)
            raise SystemExit("不是 Altium PCB 文件（应为 .PcbDoc / 导出的 .zip）：%s" % path)
    else:
        raise SystemExit("无法识别的输入（需要 .zip / 目录 / .PcbDoc）: %s" % path)

    # ---- collect candidates ------------------------------------------------
    pcbs, schs, others = [], [], []
    for root, _dirs, files in os.walk(source_dir):
        for f in files:
            full = os.path.join(root, f)
            if os.path.getsize(full) > 400 * 1024 * 1024:
                continue
            low = f.lower()
            if low.endswith(PCB_EXTS):
                pcbs.append(full)
            elif low.endswith(SCH_EXTS):
                schs.append(full)
            elif not low.endswith((".zip", ".rar", ".7z", ".txt", ".pdf", ".md")) \
                    and not low.startswith("."):
                others.append(full)

    # files renamed to an unknown extension: sniff the OLE content
    if not pcbs:
        for full in others:
            if looks_like_pcb(full):
                pcbs.append(full)
    else:
        for full in [p for p in pcbs]:
            if not looks_like_pcb(full):
                pcbs.remove(full)

    if not pcbs:
        for full in others:
            if looks_like_pcblib(full):
                raise SystemExit("只找到封装库(.PcbLib)，没有 .PcbDoc：%s" % full)
        raise SystemExit("在 %s 里没找到 .PcbDoc（Altium PCB 文件）" % source_dir)

    if pcb_hint:
        pcbs = [p for p in pcbs if pcb_hint.lower() in os.path.basename(p).lower()] or pcbs
    pcbs.sort(key=lambda p: os.path.getsize(p), reverse=True)
    pcb_path = pcbs[0]

    # ---- matching schematic -------------------------------------------------
    sch_path = None
    if schs:
        stem = os.path.splitext(os.path.basename(pcb_path))[0].lower()
        same = [s for s in schs
                if os.path.splitext(os.path.basename(s))[0].lower() == stem]
        sch_path = (same or sorted(schs, key=os.path.getsize, reverse=True))[0]
    return pcb_path, sch_path, pcbs[1:], source_dir


# ------------------------------------------------------------------- main
def build_data(pcb_path, sch_path, title=None, spur_trim=True, verbose=True):
    warnings = []
    ole = olefile.OleFileIO(pcb_path)

    def stream(name):
        try:
            return ole.openstream(name).read()
        except Exception:
            return b""

    data = {}
    stats = {}

    # ---- board outline -------------------------------------------------
    outline_mil = None
    br = stream("BoardRegions/Data")
    if br:
        try:
            outline_mil = parse_board_regions(br)
        except Exception as exc:
            warnings.append("BoardRegions 解析失败(%s)，回退 Board6" % exc)
            outline_mil = None
    if outline_mil and spur_trim:
        mm_contours = [[[x * MM_PER_MIL, y * MM_PER_MIL] for x, y in c]
                       for c in outline_mil]
        trimmed, n_removed = trim_outline_slivers(mm_contours, near_tol=1.0)
        if n_removed:
            outline_mil = [[[x / MM_PER_MIL, y / MM_PER_MIL] for x, y in c]
                           for c in trimmed]
            warnings.append("板框里去掉了 %d 个细长毛刺顶点（<1mm 宽的贴边缺口/切割线）"
                            % n_removed)
    if not outline_mil:
        brecs = text_records(stream("Board6/Data"))
        bp = props(brecs[0]) if brecs else {}
        outline_mil = flatten_vertices(read_vertices(bp))
        if outline_mil:
            warnings.append("使用 Board6 顶点重建板框（无 BoardRegions 或解析失败）")
    if not outline_mil:
        warnings.append("没有解析到板框，改用元件/铜箔包围盒")
    outline = [[[round(x * MM_PER_MIL, 4), round(y * MM_PER_MIL, 4)]
                for x, y in c] for c in outline_mil]
    data["outline"] = outline

    # ---- components ----------------------------------------------------
    comps = []
    for rec in text_records(stream("Components6/Data")):
        d = props(rec)
        des = (d.get("SOURCEDESIGNATOR") or d.get("NAME") or "").strip()
        comps.append({
            "d": des or "?",
            "fp": d.get("PATTERN", ""),
            "val": (d.get("SOURCELIBREFERENCE") or d.get("COMMENT") or "").strip(),
            "x": mm_mil(p_mil(d, "X")),
            "y": mm_mil(p_mil(d, "Y")),
            "rot": round(p_float(d, "ROTATION"), 4),
            "side": "bottom" if d.get("LAYER", "TOP").upper().startswith("BOTTOM") else "top",
        })
    data["components"] = comps

    # ---- schematic parameters (optional) -------------------------------
    if sch_path and os.path.exists(sch_path):
        try:
            params = read_schdoc_params(sch_path)
            by_des = {c["d"]: c for c in comps}
            hit = 0
            for des, prm in params.items():
                c = by_des.get(des)
                if not c:
                    continue
                c["comment"] = prm.get("Comment", "")
                c["value"] = prm.get("Value", "")
                c["mpn"] = prm.get("ManufacturerPartNumber", "")
                hit += 1
            stats["sch_matched"] = hit
            if hit < len(comps):
                warnings.append("%d 个元件没有在原理图里匹配到参数" % (len(comps) - hit))
        except Exception as exc:
            warnings.append("SchDoc 解析失败：%s" % exc)
    else:
        warnings.append("没有原理图(.SchDoc)，型号/值列将为空")

    # ---- pads ----------------------------------------------------------
    pads = []
    r = Reader(stream("Pads6/Data"))
    while r.remaining() > 0:
        marker = r.u8()
        if marker != 0x02:
            raise ValueError("Pads6 未知记录标记 0x%02x @%d" % (marker, r.p - 1))
        s, n = r.sub()
        name = ""
        if n >= 1:
            ln = s.u8()
            name = decode_text(s.d[s.p:s.p + ln])
        for _ in range(3):
            r.sub()
        s5, _n5 = r.sub()
        layer_id = s5.u8()
        s5.skip(2)
        net = s5.u16()
        s5.skip(2)
        comp = s5.u16()
        s5.skip(4)
        pos = s5.xy()
        top = s5.xy()
        s5.xy()
        s5.xy()
        hole = s5.i32()
        shape = s5.u8()
        s5.u8()
        s5.u8()
        direction = s5.f64()
        r.sub()                      # sub6 (size/shape per layer)
        pads.append({
            "x": mm(pos[0]), "y": mm(pos[1]),
            "w": mm(top[0]), "h": mm(top[1]),
            "s": shape, "r": round(direction, 2),
            "l": LAYER_NAMES.get(layer_id, str(layer_id)),
            "c": comp, "p": name, "n": net,
            "hole": mm(hole),
        })
    data["pads"] = pads

    # ---- copper tracks + silkscreen tracks -----------------------------
    tracks, silk = [], []
    r = Reader(stream("Tracks6/Data"))
    while r.remaining() > 0:
        marker = r.u8()
        if marker != 0x04:
            raise ValueError("Tracks6 未知记录标记 0x%02x @%d" % (marker, r.p - 1))
        s, _n = r.sub()
        layer_id = s.u8()
        s.skip(2)
        s.skip(2)                    # net
        s.skip(2)                    # polygon
        comp = s.u16()
        s.skip(4)
        a = s.xy()
        b = s.xy()
        width = s.i32()
        if layer_id in (1, 2, 3, 32) or 1 <= layer_id <= 32:
            tracks.append([mm(a[0]), mm(a[1]), mm(b[0]), mm(b[1]), mm(width), layer_id])
        elif layer_id in (33, 34):
            c = None if comp == 0xFFFF or comp >= len(comps) else comp
            silk.append([mm(a[0]), mm(a[1]), mm(b[0]), mm(b[1]), mm(width), layer_id, c])

    # ---- copper arcs + silk arcs ---------------------------------------
    arcs, silk_arcs = [], []
    r = Reader(stream("Arcs6/Data"))
    while r.remaining() > 0:
        marker = r.u8()
        if marker != 0x01:
            raise ValueError("Arcs6 未知记录标记 0x%02x @%d" % (marker, r.p - 1))
        s, _n = r.sub()
        layer_id = s.u8()
        s.skip(2)
        s.skip(2)
        s.skip(2)
        comp = s.u16()
        s.skip(4)
        center = s.xy()
        radius = s.i32()
        sa = s.f64()
        ea = s.f64()
        width = s.i32()
        item = [mm(center[0]), mm(center[1]), mm(radius),
                round(sa, 2), round(ea, 2), mm(width), layer_id]
        if 1 <= layer_id <= 32:
            arcs.append(item)
        elif layer_id in (33, 34):
            c = None if comp == 0xFFFF or comp >= len(comps) else comp
            silk_arcs.append(item + [c])

    # ---- vias ----------------------------------------------------------
    vias = []
    r = Reader(stream("Vias6/Data"))
    while r.remaining() > 0:
        marker = r.u8()
        if marker != 0x03:
            raise ValueError("Vias6 未知记录标记 0x%02x @%d" % (marker, r.p - 1))
        s, _n = r.sub()
        s.skip(3)
        s.skip(2)
        s.skip(2)
        s.skip(2)
        s.skip(4)
        pos = s.xy()
        dia = s.i32()
        hole = s.i32()
        vias.append([mm(pos[0]), mm(pos[1]), mm(dia), mm(hole)])

    # ---- copper polygons / pours ---------------------------------------
    polys = []
    for rec in text_records(stream("Polygons6/Data")):
        d = props(rec)
        verts = read_vertices(d)
        if not verts:
            continue
        contours = [[[mm_mil(x), mm_mil(y)] for x, y in c]
                    for c in flatten_vertices(verts)]
        if not contours:
            continue
        polys.append({"layer": d.get("LAYER", "TOP"), "contours": contours})

    # ---- separate pour scan-line copper from real routing --------------
    pour_by_layer = {}
    for pg in polys:
        pour_by_layer.setdefault((pg.get("layer") or "").upper(), []).extend(pg["contours"])

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

    layer_key = {1: "TOP", 32: "BOTTOM", 2: "MID1", 3: "MID2"}
    fill_tracks, route_tracks = [], []
    for t in tracks:
        x1, y1, x2, y2, w, lid = t
        cs = pour_by_layer.get(layer_key.get(lid, ""), [])
        if abs(y1 - y2) < 1e-6 and cs and point_in_contours((x1 + x2) / 2.0, (y1 + y2) / 2.0, cs):
            fill_tracks.append(t)
        else:
            route_tracks.append(t)
    data["tracks"] = route_tracks
    data["filltracks"] = fill_tracks
    data["arcs"] = arcs
    data["silk"] = silk
    data["silkarcs"] = silk_arcs
    data["vias"] = vias
    data["polygons"] = polys

    # ---- per component boxes / pin-1 -----------------------------------
    by_comp = {}
    for p in pads:
        by_comp.setdefault(p["c"], []).append(p)
    for i, c in enumerate(comps):
        ps = by_comp.get(i, [])
        if not ps:
            continue
        c["bb"] = [round(min(p["x"] - p["w"] / 2 for p in ps), 3),
                   round(min(p["y"] - p["h"] / 2 for p in ps), 3),
                   round(max(p["x"] + p["w"] / 2 for p in ps), 3),
                   round(max(p["y"] + p["h"] / 2 for p in ps), 3)]
        c["np"] = len(ps)
        p1 = [p for p in ps if p["p"].strip() in ("1", "A1")]
        if p1:
            c["pin1"] = [p1[0]["x"], p1[0]["y"]]

    # ---- bounding box ---------------------------------------------------
    xs, ys = [], []
    for c in outline:
        for x, y in c:
            xs.append(x)
            ys.append(y)
    if not xs:
        for p in pads:
            xs += [p["x"] - p["w"] / 2, p["x"] + p["w"] / 2]
            ys += [p["y"] - p["h"] / 2, p["y"] + p["h"] / 2]
    data["bbox"] = [round(min(xs), 3), round(min(ys), 3),
                    round(max(xs), 3), round(max(ys), 3)] if xs else [0, 0, 1, 1]
    data["title"] = title or os.path.splitext(os.path.basename(pcb_path))[0]

    # ---- sanity checks --------------------------------------------------
    if not comps:
        warnings.append("没有解析到任何元件（Components6 为空？）")
    des_seen = {}
    for c in comps:
        des_seen[c["d"]] = des_seen.get(c["d"], 0) + 1
    dup = [d for d, n in des_seen.items() if n > 1]
    if dup:
        warnings.append("位号重复：%s" % ", ".join(sorted(dup)[:10]))
    no_pads = [c["d"] for c in comps if "bb" not in c]
    if no_pads:
        warnings.append("%d 个元件没有焊盘：%s" % (len(no_pads), ", ".join(no_pads[:10])))
    bx0, by0, bx1, by1 = data["bbox"]
    outside = []
    for c in comps:
        if "bb" not in c:
            continue
        if c["bb"][0] < bx0 - 0.5 or c["bb"][1] < by0 - 0.5 or \
                c["bb"][2] > bx1 + 0.5 or c["bb"][3] > by1 + 0.5:
            outside.append(c["d"])
    if outside:
        warnings.append("%d 个元件在板框外：%s" % (len(outside), ", ".join(outside[:10])))

    copper_layers = sorted({t[5] for t in data["tracks"]} |
                           {a[6] for a in arcs} |
                           {pads[i]["l"] for i in range(len(pads)) if pads[i]["l"].startswith("MID")})
    stats.update({
        "components": len(comps),
        "top": sum(1 for c in comps if c["side"] == "top"),
        "bottom": sum(1 for c in comps if c["side"] == "bottom"),
        "pads": len(pads),
        "routes": len(route_tracks),
        "pour_lines": len(fill_tracks),
        "arcs": len(arcs),
        "vias": len(vias),
        "silk": len(silk) + len(silk_arcs),
        "pours": len(polys),
        "board_mm": [round(data["bbox"][2] - data["bbox"][0], 2),
                     round(data["bbox"][3] - data["bbox"][1], 2)],
        "outline_points": sum(len(c) for c in outline),
        "copper_layers": copper_layers,
    })
    data["meta"] = {"stats": stats, "warnings": warnings}
    return data


# --------------------------------------------------------------------------
# backend interface (see parsers/common.py)
# --------------------------------------------------------------------------
FORMAT = "altium"
LABEL = "Altium PcbDoc（.PcbDoc / .SchDoc / 工程 .zip）"


def sniff(root):
    """True when `root` contains an Altium PCB file (or is one)."""
    import os as _os
    if _os.path.isfile(root):
        if root.lower().endswith(PCB_EXTS):
            return True
        return looks_like_pcb(root)
    for p in _os.walk(root):
        for f in p[2]:
            low = f.lower()
            if low.endswith(PCB_EXTS):
                return True
    return False


def parse(root, out_dir=None, title=None, pcb_hint=None, spur_trim=True, **_kw):
    """Parse an Altium project into the canonical schema (see common.py)."""
    out_dir = out_dir or "."
    pcb_path, sch_path, extra, source_dir = find_inputs(root, out_dir, pcb_hint)
    data = build_data(pcb_path, sch_path, title, spur_trim=spur_trim)
    data.setdefault("meta", {})["source"] = {
        "format": FORMAT,
        "pcb": pcb_path,
        "sch": sch_path or "",
        "extracted_to": source_dir or "",
        "other_pcbs": extra,
    }
    return data


def main():
    ap = argparse.ArgumentParser(
        description="Altium PcbDoc -> pcb_data.json（供 build_html.py 生成贴片定位网页）")
    ap.add_argument("input", help=".PcbDoc 文件 / 工程目录 / 导出的 .zip")
    ap.add_argument("output", nargs="?", default="pcb_data.json", help="输出 json（默认 pcb_data.json）")
    ap.add_argument("--title", help="网页标题（默认取 .PcbDoc 文件名）")
    ap.add_argument("--pcb", help="zip/目录里有多个 PcbDoc 时，用文件名包含此关键词的那个")
    ap.add_argument("--no-spur-trim", action="store_true",
                    help="不剔除板框里的细长毛刺（默认剔除 <1mm 宽的贴边缺口/切割线）")
    args = ap.parse_args()

    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."
    os.makedirs(out_dir, exist_ok=True)
    pcb_path, sch_path, extra, source_dir = find_inputs(args.input, out_dir, args.pcb)
    if extra:
        sys.stderr.write("提示：找到多个 PcbDoc，使用 %s；其余：%s（可用 --pcb 指定）\n"
                         % (os.path.basename(pcb_path),
                            ", ".join(os.path.basename(p) for p in extra)))
    print("PCB   :", pcb_path)
    print("原理图:", sch_path or "(无，型号/值列会留空)")
    if source_dir:
        print("源目录:", source_dir)

    data = build_data(pcb_path, sch_path, args.title, spur_trim=not args.no_spur_trim)
    data["meta"]["source"] = {
        "pcb": pcb_path,
        "sch": sch_path or "",
        "extracted_to": source_dir or "",
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    st = data["meta"]["stats"]
    print("\n=== 解析结果 ===")
    print("标题      :", data["title"])
    print("板尺寸    : %.2f x %.2f mm" % tuple(st["board_mm"]))
    print("元件      : %d（顶层 %d / 底层 %d）" % (st["components"], st["top"], st["bottom"]))
    print("焊盘      : %d   走线 %d   覆铜线 %d   过孔 %d"
          % (st["pads"], st["routes"], st["pour_lines"], st["vias"]))
    print("丝印/铜皮 : %d 条 / %d 块   铜层 %s"
          % (st["silk"], st["pours"], st["copper_layers"]))
    for w in data["meta"]["warnings"]:
        print("⚠️ ", w)
    print("\n写入:", args.output, os.path.getsize(args.output), "bytes")


if __name__ == "__main__":
    main()
