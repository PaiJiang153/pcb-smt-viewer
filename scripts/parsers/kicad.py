#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KiCad backend:  .kicad_pcb  ->  canonical pcb_data (see parsers/common.py)

* S-expression file, coordinates in mm, Y axis DOWN  -> output Y up.
* Components come from `(footprint ...)` blocks (reference/value from the
  `Reference`/`Value` properties or the legacy `fp_text`).
* Pads are relative to their footprint, so they are transformed here.
* Board outline is assembled from all `Edge.Cuts` graphics by chaining their
  endpoints into closed loops.
"""
import math
import os
import sys

from . import common
from .common import (COPPER_TOP, COPPER_BOTTOM, SILK_TOP, SILK_BOTTOM,
                     rnd, add_warning)

FORMAT = "kicad"
LABEL = "KiCad（.kicad_pcb / 工程 .zip）"

# --------------------------------------------------------------------------
# tiny S-expression reader
# --------------------------------------------------------------------------
_TOKEN_RE = None


def _tokenize(text):
    """Yield tokens: '(' ')' atoms strings (strings are unquoted)."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "()":
            yield c
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            yield ("S", "".join(buf))
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n()"':
            j += 1
        yield text[i:j]
        i = j


def sexpr_parse(text):
    """Parse a whole file into nested lists of strings."""
    stack = [[]]
    for tok in _tokenize(text):
        if tok == "(":
            new = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            if isinstance(tok, tuple):
                stack[-1].append(tok[1])
            else:
                stack[-1].append(tok)
    return stack[0]


def children(node, name):
    """All direct children lists whose head atom == name."""
    if not isinstance(node, list):
        return []
    out = []
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            out.append(c)
    return out


def child(node, name):
    got = children(node, name)
    return got[0] if got else None


def atoms(node):
    return [c for c in node if not isinstance(c, list)] if isinstance(node, list) else []


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def xy_of(node, xkey="start", ykey=None):
    """(start x y) / (center x y) / (at x y ...) -> (x, y)"""
    if not isinstance(node, list) or len(node) < 3:
        return (0.0, 0.0)
    return (num(node[1]), num(node[2]))


def _pt_up(p):
    """KiCad (Y down) -> canonical (Y up)."""
    return [p[0], -p[1]]


# --------------------------------------------------------------------------
# arcs: 3 points -> center / radius / angles (Y up, CCW sweeps through mid)
# --------------------------------------------------------------------------
def arc_from_3pt(p0, pm, p1):
    """Return (cx, cy, r, a0, a1) such that the CCW sweep a0->a1 passes pm."""
    (x0, y0), (xm, ym), (x1, y1) = p0, pm, p1
    d = 2 * (x0 * (ym - y1) + xm * (y1 - y0) + x1 * (y0 - ym))
    if abs(d) < 1e-12:                       # collinear -> straight
        return None
    ux = ((x0 * x0 + y0 * y0) * (ym - y1) + (xm * xm + ym * ym) * (y1 - y0)
          + (x1 * x1 + y1 * y1) * (y0 - ym)) / d
    uy = ((x0 * x0 + y0 * y0) * (x1 - xm) + (xm * xm + ym * ym) * (x0 - x1)
          + (x1 * x1 + y1 * y1) * (xm - x0)) / d
    r = math.hypot(x0 - ux, y0 - uy)
    a0 = math.degrees(math.atan2(y0 - uy, x0 - ux))
    am = math.degrees(math.atan2(ym - uy, xm - ux))
    a1 = math.degrees(math.atan2(y1 - uy, x1 - ux))
    # choose the sweep that contains the mid angle
    def ccw_delta(a, b):
        d = (b - a) % 360.0
        return d
    if ccw_delta(a0, am) > ccw_delta(a0, a1):     # mid not on the CCW path
        a0, a1 = a1, a0
    return (ux, uy, r, a0, a1)


def _arc_geometry(node):
    """KiCad arc (start X Y) (mid ..) (end ..) or legacy (start)(end)(angle)."""
    s = child(node, "start")
    m = child(node, "mid")
    e = child(node, "end")
    if s is None or e is None:
        return None
    p0, p1 = _pt_up(xy_of(s)), _pt_up(xy_of(e))
    if m is not None:
        pm = _pt_up(xy_of(m))
    else:
        ang = child(node, "angle")
        if ang is None:
            return None
        # legacy: start, end, angle (deg, CCW in KiCad space)
        cx = (p0[0] + p1[0]) / 2.0
        cy = (p0[1] + p1[1]) / 2.0
        r = math.hypot(p0[0] - cx, p0[1] - cy)
        a0 = math.degrees(math.atan2(p0[1] - cy, p0[0] - cx))
        pm = [cx + r * math.cos(math.radians(a0 + num(ang[1]) / 2)),
              cy + r * math.sin(math.radians(a0 + num(ang[1]) / 2))]
    return arc_from_3pt(p0, pm, p1)


# --------------------------------------------------------------------------
# chaining edge segments into closed contours
# --------------------------------------------------------------------------
def chain_loops(pieces, tol=0.05):
    """pieces: list of polylines ([[x,y], ...]).  Returns closed contours."""
    pieces = [p for p in pieces if len(p) >= 2]
    loops = []
    used = [False] * len(pieces)
    for i in range(len(pieces)):
        if used[i]:
            continue
        used[i] = True
        loop = list(pieces[i])
        extended = True
        while extended:
            extended = False
            for j in range(len(pieces)):
                if used[j]:
                    continue
                cand = pieces[j]
                for rev in (False, True):
                    pts = cand[::-1] if rev else cand
                    if common._dist(loop[-1], pts[0]) <= tol:
                        loop.extend(pts[1:])
                    elif common._dist(loop[-1], pts[-1]) <= tol:
                        loop.extend(pts[-2::-1])
                    elif common._dist(loop[0], pts[-1]) <= tol:
                        loop[:0] = pts[:-1]
                    elif common._dist(loop[0], pts[0]) <= tol:
                        loop[:0] = pts[:0:-1]
                    else:
                        continue
                    used[j] = True
                    extended = True
                    break
                if extended:
                    break
        if len(loop) >= 3 and common._dist(loop[0], loop[-1]) <= max(tol * 4, 0.2):
            if common._dist(loop[0], loop[-1]) > 1e-9:
                loop.append(loop[0])
            loops.append(loop)
    return loops


# --------------------------------------------------------------------------
# layer helpers
# --------------------------------------------------------------------------
def copper_layer_id(name):
    if name == "F.Cu":
        return COPPER_TOP
    if name == "B.Cu":
        return COPPER_BOTTOM
    if name.startswith("In") and name.endswith(".Cu"):
        try:
            return 1 + int(name[2:-3])       # In1 -> 2 (mid1), In2 -> 3 ...
        except ValueError:
            return None
    return None


def silk_layer_id(name):
    if name == "F.SilkS":
        return SILK_TOP
    if name == "B.SilkS":
        return SILK_BOTTOM
    return None


def side_of_layer(name):
    if name in ("B.Cu", "B.SilkS", "B.Fab", "B.Mask"):
        return "bottom"
    return "top"


# --------------------------------------------------------------------------
# graphics helpers
# --------------------------------------------------------------------------
def _graphic_pieces(node, width_default=0.15):
    """Turn one KiCad graphic item into (lines, arcs) in canonical coords."""
    head = node[0]
    w = child(node, "width")
    width = num(w[1]) if w is not None else width_default
    lines, arcs = [], []
    if head in ("gr_line", "fp_line"):
        s, e = child(node, "start"), child(node, "end")
        if s is not None and e is not None:
            lines.append([_pt_up(xy_of(s)), _pt_up(xy_of(e))])
    elif head in ("gr_arc", "fp_arc"):
        g = _arc_geometry(node)
        if g:
            cx, cy, r, a0, a1 = g
            arcs.append([cx, cy, r, a0, a1])
            lines.append(common.arc_polyline(cx, cy, r, a0, a1))
    elif head in ("gr_rect", "fp_rect"):
        s, e = child(node, "start"), child(node, "end")
        if s is not None and e is not None:
            x0, y0 = xy_of(s)
            x1, y1 = xy_of(e)
            pts = [_pt_up((x0, y0)), _pt_up((x1, y0)), _pt_up((x1, y1)),
                   _pt_up((x0, y1)), _pt_up((x0, y0))]
            lines.append(pts)
    elif head in ("gr_circle", "fp_circle"):
        c, e = child(node, "center"), child(node, "end")
        if c is not None and e is not None:
            cx, cy = xy_of(c)
            ex, ey = xy_of(e)
            r = math.hypot(ex - cx, ey - cy)
            arcs.append([cx, -cy, r, 0.0, 360.0])
            lines.append(common.arc_polyline(cx, -cy, r, 0, 360))
    elif head in ("gr_poly", "fp_poly"):
        pts = child(node, "pts")
        if pts is not None:
            poly = [_pt_up((num(p[1]), num(p[2]))) for p in children(pts, "xy")]
            if len(poly) >= 2:
                lines.append(poly + [poly[0]])
    return lines, arcs, width


def _layer_names(node):
    ln = child(node, "layer")
    if ln is not None and len(ln) > 1:
        return [ln[1]]
    lns = child(node, "layers")
    if lns is not None:
        return [a for a in lns[1:]]
    return []


# --------------------------------------------------------------------------
def sniff(root):
    """True when `root` is / contains a KiCad PCB file."""
    if os.path.isfile(root):
        if root.lower().endswith(".kicad_pcb"):
            return True
        try:
            with open(root, encoding="utf-8", errors="replace") as f:
                head = f.read(400)
            return "kicad_pcb" in head
        except OSError:
            return False
    return common.pick_by_ext(root, (".kicad_pcb",)) is not None


def parse(root, out_dir=None, title=None, **_kw):
    path = common.pick_by_ext(root, (".kicad_pcb",), prefer="")
    if path is None:
        raise SystemExit("没找到 .kicad_pcb（KiCad 6/7/8 PCB 文件）")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    tree = sexpr_parse(text)
    if not tree or not isinstance(tree[0], list) or tree[0][0] != "kicad_pcb":
        raise SystemExit("不是有效的 .kicad_pcb（缺少 kicad_pcb 根节点）")
    root_node = tree[0]
    return _KiCadParser(root_node, title or os.path.splitext(os.path.basename(path))[0]).run()


class _KiCadParser:
    def __init__(self, root_node, title):
        self.root = root_node
        self.data = common.new_data(title)
        self.warnings = self.data["meta"]["warnings"]
        self.edge_pieces = []
        self.comps = []
        self.silk = self.data["silk"]
        self.silk_arcs = self.data["silkarcs"]

    # -- help ---------------------------------------------------------
    def _emit_silk(self, lines, arcs, width, comp, comp_idx):
        for seg in lines:
            for a, b in zip(seg, seg[1:]):
                self.data["silk"].append([rnd(a[0]), rnd(a[1]), rnd(b[0]), rnd(b[1]),
                                          rnd(width), comp, comp_idx])

    def run(self):
        data = self.data
        # ---- footprints -> components ---------------------------------
        for block in children(self.root, "footprint") + children(self.root, "module"):
            idx = len(self.comps)
            comp, pads, silks, sarcs = self._footprint(block)
            if comp is None:
                continue
            self.comps.append(comp)
            data["components"] = self.comps
            data["pads"].extend(pads)
            for seg, width in silks:
                for a, b in zip(seg, seg[1:]):
                    data["silk"].append([rnd(a[0]), rnd(a[1]), rnd(b[0]), rnd(b[1]),
                                         rnd(width), SILK_TOP if comp["side"] == "top" else SILK_BOTTOM, idx])
            for cx, cy, r, a0, a1, width in sarcs:
                data["silkarcs"].append([rnd(cx), rnd(cy), rnd(r), round(a0, 2), round(a1, 2),
                                         rnd(width), SILK_TOP if comp["side"] == "top" else SILK_BOTTOM, idx])

        # ---- board level graphics -------------------------------------
        for g in self.root:
            if not isinstance(g, list) or not g:
                continue
            head = g[0]
            if head in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
                layers = _layer_names(g)
                if "Edge.Cuts" in layers:
                    lines, arcs, _w = _graphic_pieces(g)
                    self.edge_pieces.extend(lines)
                    continue
                lines, arcs, width = _graphic_pieces(g)
                for lname in layers:
                    cid = copper_layer_id(lname)
                    sid = silk_layer_id(lname)
                    if cid is not None:
                        for seg in lines:
                            for a, b in zip(seg, seg[1:]):
                                self.data["tracks"].append([rnd(a[0]), rnd(a[1]), rnd(b[0]),
                                                            rnd(b[1]), rnd(width), cid])
                        for cx, cy, r, a0, a1 in arcs:
                            self.data["arcs"].append([rnd(cx), rnd(cy), rnd(r), round(a0, 2),
                                                      round(a1, 2), rnd(width), cid])
                        if head in ("gr_rect", "gr_poly", "gr_circle"):
                            poly = lines[0] if lines else []
                            if len(poly) >= 3:
                                self.data["polygons"].append({
                                    "layer": "TOP" if cid == COPPER_TOP else "BOTTOM",
                                    "contours": [[[rnd(p[0]), rnd(p[1])] for p in poly]]})
                    elif sid is not None:
                        for seg in lines:
                            for a, b in zip(seg, seg[1:]):
                                self.data["silk"].append([rnd(a[0]), rnd(a[1]), rnd(b[0]),
                                                          rnd(b[1]), rnd(width), sid, None])
            elif head == "segment":
                ln = copper_layer_id((_layer_names(g) or [""])[0])
                if ln is None:
                    continue
                s, e = child(g, "start"), child(g, "end")
                w = child(g, "width")
                if s is None or e is None:
                    continue
                a, b = _pt_up(xy_of(s)), _pt_up(xy_of(e))
                data["tracks"].append([rnd(a[0]), rnd(a[1]), rnd(b[0]), rnd(b[1]),
                                       rnd(num(w[1]) if w else 0.25), ln])
            elif head == "arc":
                ln = copper_layer_id((_layer_names(g) or [""])[0])
                if ln is None:
                    continue
                g3 = _arc_geometry(g)
                w = child(g, "width")
                if g3 is None:
                    continue
                cx, cy, r, a0, a1 = g3
                data["arcs"].append([rnd(cx), rnd(cy), rnd(r), round(a0, 2), round(a1, 2),
                                     rnd(num(w[1]) if w else 0.25), ln])
            elif head == "via":
                at = child(g, "at")
                size = child(g, "size")
                drill = child(g, "drill")
                if at is None:
                    continue
                x, y = xy_of(at)
                data["vias"].append([rnd(x), rnd(-y),
                                     rnd(num(size[1]) if size else 0.6),
                                     rnd(num(drill[1]) if drill else 0.3)])
            elif head == "zone":
                self._zone(g)

        # ---- board outline --------------------------------------------
        loops = chain_loops(self.edge_pieces)
        if loops:
            data["outline"] = [[[rnd(p[0]), rnd(p[1])] for p in loop] for loop in loops]
            data["outline"], n = common.trim_outline_slivers(data["outline"], near_tol=0.6)
            if n:
                add_warning(data, "板框里去掉了 %d 个细长毛刺顶点" % n)
        else:
            add_warning(data, "Edge.Cuts 层没有闭合板框，将用整板包围盒代替")

        if not data["polygons"]:
            add_warning(data, "没有解析到覆铜区（zone），板子上大片铜皮不会显示")
        return data

    # -- footprint -----------------------------------------------------
    def _footprint(self, block):
        layer = (_layer_names(block) or ["F.Cu"])[0]
        at = child(block, "at")
        if at is None:
            return None, [], [], []
        fx, fy = num(at[1]), num(at[2])
        fangle = num(at[3]) if len(at) > 3 else 0.0
        side = side_of_layer(layer)
        ref = val = mpn = ""
        for prop in children(block, "property"):
            if len(prop) >= 3:
                key, value = str(prop[1]), str(prop[2])
                if key == "Reference":
                    ref = value
                elif key == "Value":
                    val = value
                elif key in ("MPN", "Manufacturer Part Number", "LCSC", "LCSC Part"):
                    mpn = value
        for txt in children(block, "fp_text"):
            if len(txt) >= 3 and str(txt[1]).lower() == "reference":
                ref = ref or str(txt[2])
            elif len(txt) >= 3 and str(txt[1]).lower() == "value":
                val = val or str(txt[2])
        fp_name = block[1] if len(block) > 1 and not isinstance(block[1], list) else ""
        idx = len(self.comps)
        a = math.radians(fangle)
        ca, sa = math.cos(a), math.sin(a)

        def tf(px, py):
            rx = px * ca + py * sa
            ry = -px * sa + py * ca
            return fx + rx, -(fy + ry)

        comp = {
            "d": ref or "?",
            "fp": fp_name,
            "side": side,
            "x": rnd(fx), "y": rnd(-fy),
            "rot": round(fangle % 360, 2),
            "val": val, "comment": val,
        }
        if mpn:
            comp["mpn"] = mpn

        # pads
        pads = []
        for pad in children(block, "pad"):
            if len(pad) < 4:
                continue
            pname = str(pad[1])
            ptype = str(pad[2])
            pshape = str(pad[3])
            pat = child(pad, "at")
            px = num(pat[1]) if pat is not None else 0.0
            py = num(pat[2]) if pat is not None else 0.0
            pang = num(pat[3]) if pat is not None and len(pat) > 3 else 0.0
            ax, ay = tf(px, py)
            size = child(pad, "size")
            w = num(size[1]) if size is not None else 0.5
            h = num(size[2]) if size is not None and len(size) > 2 else w
            drill = child(pad, "drill")
            hole = 0.0
            if drill is not None:
                for atom in drill[1:]:
                    try:
                        hole = float(atom)
                        break
                    except (TypeError, ValueError):
                        continue
            players = _layer_names(pad)
            if ptype.startswith("thru") or "*.Cu" in players:
                pl = "MULTILAYER"
            elif "B.Cu" in players and "F.Cu" not in players:
                pl = "BOTTOM"
            elif players:
                pl = "TOP"
            else:
                pl = "TOP" if side == "top" else "BOTTOM"
            shape = {"circle": 1, "oval": 4, "roundrect": 2, "rect": 2,
                     "trapezoid": 2, "custom": 2}.get(pshape, 2)
            if pshape in ("trapezoid", "custom"):
                add_warning(self.data, "焊盘形状 %s 按矩形近似" % pshape)
            pads.append({
                "x": rnd(ax), "y": rnd(ay), "w": rnd(w), "h": rnd(h),
                "s": shape, "r": round(-(fangle + pang) % 360, 2),
                "l": pl, "c": idx, "p": pname, "hole": rnd(hole),
            })

        # silk graphics (local coords -> board coords)
        silks, sarcs = [], []
        for g in block:
            if not isinstance(g, list) or not g:
                continue
            if g[0] not in ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly"):
                continue
            sid = None
            for lname in _layer_names(g):
                sid = silk_layer_id(lname)
                if sid:
                    break
            if sid is None:
                continue
            lines, arcs, width = _graphic_pieces(g)
            for seg in lines:
                silks.append(([list(tf(p[0], -p[1])) for p in seg], width))
            for cx, cy, r, a0, a1 in arcs:
                # local arc -> board: transform three points
                pts = common.arc_polyline(cx, -cy if False else cy, r, a0, a1)
                g2 = arc_from_3pt(*[list(tf(p[0], -p[1])) for p in (pts[0], pts[len(pts) // 2], pts[-1])])
                if g2:
                    sarcs.append((g2[0], g2[1], g2[2], g2[3], g2[4], width))
        return comp, pads, silks, sarcs

    # -- zone ----------------------------------------------------------
    def _zone(self, zone):
        for fp in children(zone, "filled_polygon"):
            layer = (_layer_names(fp) or [""])[0]
            cid = copper_layer_id(layer)
            if cid is None:
                continue
            pts = child(fp, "pts")
            if pts is None:
                continue
            poly = [_pt_up((num(p[1]), num(p[2]))) for p in children(pts, "xy")]
            if len(poly) >= 3:
                self.data["polygons"].append({
                    "layer": "TOP" if cid == COPPER_TOP else "BOTTOM",
                    "contours": [[[rnd(p[0]), rnd(p[1])] for p in poly]]})
        if children(zone, "filled_polygon"):
            return
        layer = (_layer_names(zone) or [""])[0]
        cid = copper_layer_id(layer)
        if cid is None:
            return
        poly_node = child(zone, "polygon")
        if poly_node is None:
            return
        pts = child(poly_node, "pts")
        if pts is None:
            return
        poly = [_pt_up((num(p[1]), num(p[2]))) for p in children(pts, "xy")]
        if len(poly) >= 3:
            self.data["polygons"].append({
                "layer": "TOP" if cid == COPPER_TOP else "BOTTOM",
                "contours": [[[rnd(p[0]), rnd(p[1])] for p in poly]]})


def main():
    import argparse
    ap = argparse.ArgumentParser(description="KiCad .kicad_pcb -> pcb_data.json")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default="pcb_data.json")
    ap.add_argument("--title")
    args = ap.parse_args()
    from . import common as c
    data = c.finalize(parse(args.input, ".", title=args.title))
    import json
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(data["meta"]["stats"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
