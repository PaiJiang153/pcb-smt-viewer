#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嘉立创EDA / EasyEDA 标准版（Standard / LCEDA Std / JLCEDA Std）PCB  `.json` -> canonical data.

文件格式要点（见 docs.easyeda.com/cn/DocumentFormat/EasyEDA-Format-Standard/，
以及 KiCad 的 pcbnew/pcb_io/easyeda 解析器）:

* 整个文档是一个 JSON 对象；图元是 ``shape`` 数组里的 ``~`` 分隔字符串。
* ``head.docType`` = 3 表示 PCB（14 = PCB module，4 = 封装库，1/2/5 = 原理图相关）。
  老版本 ``head`` 是字符串 ``"3~1.11.3~"``（第一段即 docType）。
* 坐标单位是 **10 mil = 0.254 mm**（KiCad: ``round((v*254000)/100)*100`` nm），
  坐标系是 SVG 画布（Y 向下）-> 这里统一换算成 mm 并把 Y 取反（向上）。
* 元件是 ``LIB`` 记录，内部子图元用 ``#@$`` 拼接；子图元坐标已经是
  **文档绝对坐标（旋转/翻转已应用）**，直接使用，不能再按 LIB 的 rotation 转一次。
* 位号/值藏在 LIB 子图元 ``TEXT`` 里：``TEXT~P`` = 位号（reference），
  ``TEXT~N`` = 值（value）。老文件也可能把 ``value`` 写在 LIB 的 ``params`` 里。
* 板框是层 10 (BoardOutLine) 上的 TRACK / ARC / CIRCLE / RECT，需要串成闭合轮廓。
* 层号：1 顶层铜 / 2 底层铜 / 3,4 丝印 / 5,6 锡膏 / 7,8 阻焊 / 10 板框 /
  11 多层(通孔) / 21.. 内层铜；99/100/101 是封装辅助层（不渲染）。
"""
import html
import json
import math
import os
import re
import tempfile

try:                                     # 既支持 `parsers.easyeda_std`，也支持直接跑
    from . import common
except ImportError:                      # pragma: no cover
    import common                        # type: ignore

FORMAT = "easyeda_std"
LABEL = "嘉立创EDA/EasyEDA 标准版 (.json)"

# EasyEDA PCB 单位 = 10 mil = 0.254 mm
UNIT = 0.254

DOC_TYPE_PCB = 3
DOC_TYPE_PCB_MODULE = 14
DOC_TYPE_FOOTPRINT = 4

# layer ids ---------------------------------------------------------------
LAYER_TOP = "1"
LAYER_BOTTOM = "2"
LAYER_SILK_TOP = "3"
LAYER_SILK_BOTTOM = "4"
LAYER_OUTLINE = "10"
LAYER_MULTI = "11"

# EasyEDA copper layer -> canonical copper layer id
COPPER_MAP = {
    1: common.COPPER_TOP,        # 1  -> 1  top
    2: common.COPPER_BOTTOM,     # 2  -> 32 bottom
    21: common.COPPER_MID1,      # 21 -> 2  mid1 (inner1)
    22: common.COPPER_MID2,      # 22 -> 3  mid2 (inner2)
}
SILK_MAP = {
    3: common.SILK_TOP,          # 3  -> 33
    4: common.SILK_BOTTOM,       # 4  -> 34
}

# shape 之间用 #@$ 拼接（LIB 的子图元）
SUB_DELIM = "#@$"

_TAG_RE = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------ helpers
def _f(parts, idx, default=""):
    return parts[idx] if 0 <= idx < len(parts) else default


def _num(parts, idx, default=0.0):
    try:
        v = parts[idx]
    except IndexError:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _xy(x, y):
    """EasyEDA units (Y down) -> canonical mm (Y up)."""
    return (common.rnd(float(x) * UNIT), common.rnd(-float(y) * UNIT))


def _clean_text(s):
    if not s:
        return ""
    s = s.replace("\\n", " ")
    s = html.unescape(s)
    s = _TAG_RE.sub("", s)
    return s.strip()


def _kv_params(field):
    """``package`R0603`Contributor`LCEDA_Lib`...`` -> dict."""
    out = {}
    if not field:
        return out
    chunks = field.split("`")
    for i in range(0, len(chunks) - 1, 2):
        key = chunks[i].strip()
        if key:
            out[key] = chunks[i + 1].strip()
    return out


def _doc_type_of(doc):
    """docType may live at top level, inside head (int/str), or in a head string."""
    dt = doc.get("docType")
    head = doc.get("head")
    if dt is None and isinstance(head, dict):
        dt = head.get("docType")
    if dt is None and isinstance(head, str):
        first = head.split("~")[0].strip()
        dt = first
    if isinstance(dt, str):
        dt = dt.strip().strip('"')
        m = re.match(r"^(\d+)", dt)
        dt = m.group(1) if m else ""
    try:
        return int(dt)
    except (TypeError, ValueError):
        return None


def _editor_version(doc):
    head = doc.get("head")
    if isinstance(head, dict):
        return str(head.get("editorVersion") or "")
    if isinstance(head, str):
        chunks = head.split("~")
        return chunks[1] if len(chunks) > 1 else ""
    return ""


def _peek_doc_type(path):
    """Cheap sniff: read the first few KB and look for docType / old head string."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"(?:docType)"\s*:\s*"?(?:\\?"?)(\d+)', head)
    if m:
        return int(m.group(1))
    m = re.search(r'"head"\s*:\s*"(\d+)~', head)
    if m:
        return int(m.group(1))
    return None


def _peek_legacy_pcb(path):
    """2020 年前的「文件对象」dict 格式没有 docType，靠这几个 key 认出来。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8192).decode("utf-8", "replace")
    except OSError:
        return False
    if '"docType"' in head:
        return False
    return ('"TRACK"' in head and '"PAD"' in head) or '"itemOrder"' in head


def _candidate_files(root):
    files = []
    for p in common.iter_files(root):
        low = p.lower()
        if not (low.endswith(".json") or low.endswith(".json5")):
            continue
        if os.path.basename(p).startswith("."):
            continue
        try:
            if os.path.getsize(p) < 100:
                continue
        except OSError:
            continue
        files.append(p)
    return files


def _pick_doc(root, pcb_hint=None):
    """Pick the most likely EasyEDA Standard PCB document under `root`."""
    files = _candidate_files(root)
    if not files:
        raise SystemExit("在 %s 里没找到 .json 文件（EasyEDA 标准版 PCB 应为 .json）" % root)

    scored = []
    for p in files:
        dt = _peek_doc_type(p)
        legacy = dt is None and _peek_legacy_pcb(p)
        if dt == DOC_TYPE_PCB:
            score = 100
        elif dt == DOC_TYPE_PCB_MODULE:
            score = 90
        elif dt == DOC_TYPE_FOOTPRINT:
            score = 40
        elif legacy:
            score = 50
        elif dt is not None:
            score = 0            # schematic / project / unknown JSON
        else:
            score = 5            # might be a PCB saved without a readable head
        if pcb_hint and pcb_hint.lower() in os.path.basename(p).lower():
            score += 1000
        scored.append((score, os.path.getsize(p), p))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = scored[0]
    if (best[0] % 1000) < 10:
        # 没有任何 PCB/封装文档
        names = ", ".join(os.path.basename(s[2]) for s in scored[:5])
        raise SystemExit("找到的 .json 都不是嘉立创EDA/EasyEDA 标准版 PCB（docType=3）：%s" % names)
    return best[2]


# ------------------------------------------------------------- geometry ---
def _arc_from_path(path):
    """Parse ``M x y A rx ry rot far cw ex ey`` -> (sx, sy, ex, ey, r, far, cw).

    EasyEDA ARC 记录就是一段 SVG 弧。返回 None 表示解析失败。
    """
    if not path:
        return None
    m = re.search(
        r"M\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)\s*"
        r"A\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)[\s,]+"
        r"([01])[\s,]+([01])[\s,]+(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)",
        path)
    if not m:
        return None
    g = [float(v) for v in m.groups()]
    sx, sy, rx, ry, _rot, far, cw, ex, ey = g
    r = (abs(rx) + abs(ry)) / 2.0            # 圆/近似圆；椭圆弧按平均半径近似
    return sx, sy, ex, ey, r, bool(far), bool(cw)


def _arc_geometry(arc):
    """SVG 弧 -> (cx, cy, r, a0, sweep)（EasyEDA 原始坐标，Y 向下；sweep 有符号）。"""
    sx, sy, ex, ey, r, far, cw = arc
    dx, dy = ex - sx, ey - sy
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return None
    if d > 2.0 * r:                          # 参数不合法时退化处理
        r = d / 2.0
    h = math.sqrt(max(0.0, r * r - d * d / 4.0))
    ux, uy = -dy / d, dx / d                 # KiCad VECTOR2::Perpendicular
    sign = 1.0 if (far ^ cw) else -1.0
    cx = sx + dx / 2.0 + ux * h * sign
    cy = sy + dy / 2.0 + uy * h * sign

    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    diff = a1 - a0
    while diff > math.pi:
        diff -= 2.0 * math.pi
    while diff <= -math.pi:
        diff += 2.0 * math.pi
    if cw:                                   # SVG sweep-flag=1 = 角度增大方向
        sweep = diff if diff > 0 else diff + 2.0 * math.pi
    else:
        sweep = diff if diff < 0 else diff - 2.0 * math.pi
    return cx, cy, r, a0, sweep


def _arc_canonical(arc):
    """SVG 弧 -> (cx, cy, r, start_deg, end_deg) in mm / Y-up.

    canonical 约定：从 start_deg 逆时针扫到 end_deg（Y 向上坐标系）。
    Y 取反会翻转旋向，所以用扫过方向重新定 start/end。
    """
    geo = _arc_geometry(arc)
    if not geo:
        return None
    cx, cy, r, a0, sweep = geo
    if sweep > 0:                            # 原始坐标里角度增大 -> Y-up 里角度减小(顺时针)
        start_deg = math.degrees(-(a0 + sweep))
        end_deg = math.degrees(-a0)
    else:
        start_deg = math.degrees(-a0)
        end_deg = math.degrees(-(a0 + sweep))
    return (common.rnd(cx * UNIT), common.rnd(-cy * UNIT), common.rnd(r * UNIT),
            round(start_deg, 3), round(end_deg, 3))


def _arc_polyline_raw(arc, step_deg=3.0):
    """同 _arc_polyline，但返回 EasyEDA 原始坐标（不换算 mm）。"""
    geo = _arc_geometry(arc)
    if not geo:
        return []
    cx, cy, r, a0, sweep = geo
    n = max(2, int(abs(math.degrees(sweep)) / max(step_deg, 0.2)) + 1)
    return [(cx + r * math.cos(a0 + sweep * k / n),
             cy + r * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def _circle_polyline(cx, cy, r, step_deg=4.0):
    n = max(12, int(360.0 / max(step_deg, 0.5)))
    pts = []
    for k in range(n):
        a = 2.0 * math.pi * k / n
        pts.append(_xy(cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _rect_points(x, y, w, h):
    """EasyEDA RECT (x,y = 左上角, Y 向下) -> 4 个角点（mm/Y-up，逆时针无所谓）。"""
    return [_xy(x, y), _xy(x + w, y), _xy(x + w, y + h), _xy(x, y + h)]


def _chain_outline(segments, tol=0.5):
    """把板框线段串成闭合轮廓。segments: [((x1,y1),(x2,y2)), ...] 原始单位。"""
    n = len(segments)
    used = [False] * n
    loops = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        pts = [segments[i][0], segments[i][1]]
        closed = False
        while True:
            cur = pts[-1]
            best = None
            for j in range(n):
                if used[j]:
                    continue
                a, b = segments[j]
                da = math.hypot(cur[0] - a[0], cur[1] - a[1])
                db = math.hypot(cur[0] - b[0], cur[1] - b[1])
                if da <= tol or db <= tol:
                    best = (j, b if da <= db else a)
                    break
            if best is None:
                break
            used[best[0]] = True
            pts.append(best[1])
            if len(pts) > 3 and math.hypot(pts[-1][0] - pts[0][0],
                                           pts[-1][1] - pts[0][1]) <= tol:
                closed = True
                pts.pop()
                break
        loops.append((pts, closed))
    return loops


def _polygon_area(points):
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _split_polyline_points(pts):
    """[p0, p1, p2, ...] -> [[p0,p1], [p1,p2], ...]"""
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


# --------------------------------------------------------------- doc load --
def _load_doc(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8-sig", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 少数导出会在末尾多逗号；再给一次宽松尝试
        text2 = re.sub(r",\s*([\]}])", r"\1", text)
        try:
            return json.loads(text2)
        except json.JSONDecodeError as exc:
            raise SystemExit("%s 不是有效的 JSON（%s）" % (path, exc))


# ------------------------------------------------------------------- parse --
class _Builder:
    def __init__(self, data, doc, path):
        self.data = data
        self.doc = doc
        self.path = path
        self.warn = data["meta"]["warnings"]
        # 板框：segments 存 EasyEDA 原始单位（Y 向下），loops 存已换算好的 mm/Y-up 闭合轮廓
        self.outline_segments = []
        self.outline_loops = []
        self.polys = data["polygons"]
        self.skipped_layers = {}
        self.unknown = {}
        self.polygon_pad_saw = False
        self.inner_saw = False
        self.slot_saw = False
        self.cutout_saw = 0
        self.svg_skipped = 0
        self.doc_graphics_skipped = 0

    # -- warnings ---------------------------------------------------------
    def _warn(self, msg):
        if msg not in self.warn:
            self.warn.append(msg)

    def _skip_layer(self, layer):
        self.skipped_layers[layer] = self.skipped_layers.get(layer, 0) + 1

    # -- copper/silk classification --------------------------------------
    @staticmethod
    def _copper(layer):
        try:
            lid = int(float(layer))
        except (TypeError, ValueError):
            return None
        if lid in COPPER_MAP:
            return COPPER_MAP[lid]
        if 21 <= lid <= 52:
            return -1                       # 更深的内层：只做提示，不完整渲染
        return None

    @staticmethod
    def _silk(layer):
        try:
            lid = int(float(layer))
        except (TypeError, ValueError):
            return None
        return SILK_MAP.get(lid)

    def _inner_note(self, layer):
        if not self.inner_saw:
            self.inner_saw = True
            self._warn("发现内层铜（层 %s）：只映射 4 层（内1→MID1、内2→MID2），"
                       "更深的层合并到 MID2；内层覆铜/走线不完整渲染" % layer)

    # -- outline ----------------------------------------------------------
    def _outline_track(self, pts):
        for seg in _split_polyline_points(pts):
            self.outline_segments.append((seg[0], seg[1]))

    def _outline_rect(self, x, y, w, h):
        raw = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        for k in range(4):
            self.outline_segments.append((raw[k], raw[(k + 1) % 4]))

    def _outline_arc(self, arc):
        pts = _arc_polyline_raw(arc)
        for seg in _split_polyline_points(pts):
            self.outline_segments.append((seg[0], seg[1]))

    def _outline_circle(self, cx, cy, r):
        self.outline_loops.append(_circle_polyline(cx, cy, r))

    # -- simple graphics on an arbitrary layer ----------------------------
    def _segments_on(self, layer_id, pts, width):
        """把一条折线写到 tracks / silk。pts 是 mm/Y-up。"""
        cu = self._copper(layer_id)
        silk = self._silk(layer_id)
        if cu == -1:
            self._inner_note(layer_id)
            return True
        if cu is not None and cu in (common.COPPER_TOP, common.COPPER_BOTTOM,
                                     common.COPPER_MID1, common.COPPER_MID2):
            for k in range(len(pts) - 1):
                self.data["tracks"].append([pts[k][0], pts[k][1],
                                            pts[k + 1][0], pts[k + 1][1],
                                            common.rnd(width), cu])
            return True
        if silk is not None:
            for k in range(len(pts) - 1):
                self.data["silk"].append([pts[k][0], pts[k][1],
                                          pts[k + 1][0], pts[k + 1][1],
                                          common.rnd(width), silk, None])
            return True
        return False

    # -- one shape record -------------------------------------------------
    def handle(self, rec, comp_index=None, in_footprint=False):
        """处理一条 ~ 分隔的记录；comp_index 非 None 表示在 LIB 内部。"""
        parts = rec.split("~")
        kind = parts[0]
        try:
            getattr(self, "_shape_" + kind.lower())(parts, comp_index, in_footprint)
        except AttributeError:
            self.unknown[kind] = self.unknown.get(kind, 0) + 1
        except (ValueError, TypeError, IndexError, ZeroDivisionError) as exc:
            self._warn("图元 %s 解析失败：%s" % (kind, exc))
        except Exception as exc:                       # 防御：任何怪数据都不能崩
            self._warn("图元 %s 解析异常：%s" % (kind, exc))

    # -- shape handlers ---------------------------------------------------
    def _shape_lib(self, parts, comp_index, in_footprint):
        # 顶层 LIB 由 parse() 直接走 parse_lib()；嵌套 LIB 不存在，忽略
        return

    def _shape_track(self, parts, comp_index, in_footprint):
        width = _num(parts, 1) * UNIT
        layer_id = _f(parts, 2)
        coords = _f(parts, 4).replace(",", " ").split()
        vals = []
        for v in coords:
            try:
                vals.append(float(v))
            except ValueError:
                pass
        pts = [_xy(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        if len(pts) < 2:
            return
        if layer_id == LAYER_OUTLINE and not in_footprint:
            raw = [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
            self._outline_track(raw)
            return
        if self._segments_on(layer_id, pts, width):
            return
        if layer_id == LAYER_OUTLINE and in_footprint:
            # 封装里的板框线通常是接插件开槽，属于元件图形，不并入板框
            self.doc_graphics_skipped += 1
            return
        self._skip_layer(layer_id)

    def _shape_arc(self, parts, comp_index, in_footprint):
        width = _num(parts, 1) * UNIT
        layer_id = _f(parts, 2)
        arc = _arc_from_path(_f(parts, 4))
        if not arc:
            self._warn("圆弧路径无法解析：%s" % (_f(parts, 4)[:40],))
            return
        if layer_id == LAYER_OUTLINE and not in_footprint:
            self._outline_arc(arc)
            return
        canon = _arc_canonical(arc)
        if not canon:
            return
        cx, cy, r, a0, a1 = canon
        cu = self._copper(layer_id)
        silk = self._silk(layer_id)
        if silk is not None:
            self.data["silkarcs"].append([cx, cy, r, a0, a1,
                                          common.rnd(width), silk,
                                          comp_index if comp_index is not None else None])
            return
        if cu == -1:
            self._inner_note(layer_id)
            self.data["arcs"].append([cx, cy, r, a0, a1, common.rnd(width),
                                      common.COPPER_MID2])
            return
        if cu is not None:
            self.data["arcs"].append([cx, cy, r, a0, a1, common.rnd(width), cu])
            return
        self._skip_layer(layer_id)

    def _shape_circle(self, parts, comp_index, in_footprint):
        cx = _num(parts, 1)
        cy = _num(parts, 2)
        r = _num(parts, 3)
        width = _num(parts, 4) * UNIT
        layer_id = _f(parts, 5)
        if not (r > 0):
            return
        if layer_id == LAYER_OUTLINE and not in_footprint:
            self._outline_circle(cx, cy, r)
            return
        cu = self._copper(layer_id)
        silk = self._silk(layer_id)
        if silk is not None:
            self.data["silkarcs"].append([common.rnd(cx * UNIT), common.rnd(-cy * UNIT),
                                          common.rnd(r * UNIT), 0.0, 180.0,
                                          common.rnd(width), silk,
                                          comp_index if comp_index is not None else None])
            self.data["silkarcs"].append([common.rnd(cx * UNIT), common.rnd(-cy * UNIT),
                                          common.rnd(r * UNIT), 180.0, 360.0,
                                          common.rnd(width), silk,
                                          comp_index if comp_index is not None else None])
            return
        if cu == -1:
            self._inner_note(layer_id)
            return
        if cu is not None:
            for a0, a1 in ((0.0, 180.0), (180.0, 360.0)):
                self.data["arcs"].append([common.rnd(cx * UNIT), common.rnd(-cy * UNIT),
                                          common.rnd(r * UNIT), a0, a1,
                                          common.rnd(width), cu])
            return
        self._skip_layer(layer_id)

    def _shape_rect(self, parts, comp_index, in_footprint):
        x = _num(parts, 1)
        y = _num(parts, 2)
        w = _num(parts, 3)
        h = _num(parts, 4)
        layer_id = _f(parts, 5)
        width = _num(parts, 8) * UNIT
        if w == 0 or h == 0:
            return
        if layer_id == LAYER_OUTLINE and not in_footprint:
            self._outline_rect(x, y, w, h)
            return
        pts = _rect_points(x, y, w, h)
        cu = self._copper(layer_id)
        silk = self._silk(layer_id)
        if cu == -1:
            self._inner_note(layer_id)
            return
        if cu is not None:
            # 铜层矩形：按实心多边形处理（例如铺铜小块 / 天线）
            self.polys.append({"layer": "TOP" if cu == common.COPPER_TOP else "BOTTOM",
                               "contours": [pts]})
            return
        if silk is not None:
            for k in range(4):
                self.data["silk"].append([pts[k][0], pts[k][1],
                                          pts[(k + 1) % 4][0], pts[(k + 1) % 4][1],
                                          common.rnd(width), silk,
                                          comp_index if comp_index is not None else None])
            return
        self._skip_layer(layer_id)

    def _shape_pad(self, parts, comp_index, in_footprint):
        shape = _f(parts, 1).upper()
        x = _num(parts, 2)
        y = _num(parts, 3)
        w = abs(_num(parts, 4))
        h = abs(_num(parts, 5))
        layer_id = _f(parts, 6)
        number = _f(parts, 8)
        hole_r = _num(parts, 9)
        rot = _num(parts, 11)
        hole_len = _num(parts, 13)

        if shape == "POLYGON":
            poly = _f(parts, 10).replace(",", " ").split()
            pxs, pys = [], []
            for i in range(0, len(poly) - 1, 2):
                try:
                    pxs.append(float(poly[i]))
                    pys.append(float(poly[i + 1]))
                except ValueError:
                    pass
            if pxs and pys:
                # 自定义多边形焊盘：用点集包围盒近似成矩形
                w = max(pxs) - min(pxs)
                h = max(pys) - min(pys)
            if not self.polygon_pad_saw:
                self.polygon_pad_saw = True
                self._warn("自定义多边形焊盘(POLYGON)按矩形近似绘制（位置/层/编号是准的）")

        if layer_id == LAYER_TOP:
            pl = "TOP"
        elif layer_id == LAYER_BOTTOM:
            pl = "BOTTOM"
        elif layer_id == LAYER_MULTI:
            pl = "MULTILAYER"
        else:
            try:
                lid = int(float(layer_id))
            except (TypeError, ValueError):
                lid = None
            if lid is not None and 21 <= lid <= 52:
                pl = "MULTILAYER"
                self._inner_note(layer_id)
            else:
                pl = "TOP"
                self._skip_layer(layer_id)

        if shape == "ELLIPSE":
            s = common.PAD_ROUND if abs(w - h) < 1e-6 else common.PAD_OBLONG
        elif shape == "OVAL":
            s = common.PAD_ROUND if abs(w - h) < 1e-6 else common.PAD_OBLONG
        elif shape == "RECT":
            s = common.PAD_RECT
        elif shape == "POLYGON":
            s = common.PAD_RECT
        elif shape in ("ROUNDRECT", "ROUND_RECT"):
            s = common.PAD_RECT
            self._warn("圆角矩形焊盘(ROUNDRECT)按直角矩形绘制")
        else:
            s = common.PAD_RECT
            self._warn("未知焊盘形状 %r，按矩形绘制" % shape)

        if hole_len > 0 and not self.slot_saw:
            self.slot_saw = True
            self._warn("槽孔(holeLength>0)按圆孔近似绘制")

        hole = common.rnd(abs(hole_r) * 2.0 * UNIT) if hole_r else 0.0
        px, py = _xy(x, y)
        self.data["pads"].append({
            "x": px, "y": py,
            "w": common.rnd(w * UNIT), "h": common.rnd(h * UNIT),
            "s": s, "r": round(rot, 3), "l": pl,
            "c": comp_index if comp_index is not None else common.NO_COMPONENT,
            "p": _clean_text(number),
            "hole": hole,
        })

    def _shape_via(self, parts, comp_index, in_footprint):
        x = _num(parts, 1)
        y = _num(parts, 2)
        dia = abs(_num(parts, 3))
        drill_r = _num(parts, 5)
        px, py = _xy(x, y)
        if in_footprint:
            # 封装里的 VIA 在 EasyEDA 里是通孔焊盘（PTH）
            self.data["pads"].append({
                "x": px, "y": py,
                "w": common.rnd(dia * UNIT), "h": common.rnd(dia * UNIT),
                "s": common.PAD_ROUND, "r": 0.0, "l": "MULTILAYER",
                "c": comp_index if comp_index is not None else common.NO_COMPONENT,
                "p": "", "hole": common.rnd(abs(drill_r) * 2.0 * UNIT),
            })
            return
        self.data["vias"].append([px, py,
                                  common.rnd(dia * UNIT),
                                  common.rnd(abs(drill_r) * 2.0 * UNIT)])

    def _shape_hole(self, parts, comp_index, in_footprint):
        # 非金属化通孔（安装孔）：按过孔画（外径=孔径）
        x = _num(parts, 1)
        y = _num(parts, 2)
        r = abs(_num(parts, 3))
        px, py = _xy(x, y)
        d = common.rnd(r * 2.0 * UNIT)
        self.data["vias"].append([px, py, d, d])

    def _shape_solidregion(self, parts, comp_index, in_footprint):
        layer_id = _f(parts, 1)
        path = _f(parts, 3)
        rtype = _f(parts, 4)
        contours = _parse_path_contours(path)
        if not contours:
            return
        if layer_id == LAYER_MULTI:
            self.cutout_saw += 1
            return
        cu = self._copper(layer_id)
        silk = self._silk(layer_id)
        if cu == -1:
            self._inner_note(layer_id)
            return
        if cu is not None:
            if cu == common.COPPER_TOP:
                self.polys.append({"layer": "TOP", "contours": contours})
            else:
                self.polys.append({"layer": "BOTTOM", "contours": contours})
            if rtype.lower() == "cutout":
                self._warn("SOLIDREGION 的 cutout（挖空/禁布）按实心铜绘制，未挖孔")
            return
        if silk is not None:
            for c in contours:
                if len(c) < 2:
                    continue
                for k in range(len(c)):
                    a = c[k]
                    b = c[(k + 1) % len(c)]
                    self.data["silk"].append([a[0], a[1], b[0], b[1], 0.12, silk,
                                              comp_index if comp_index is not None else None])
            return
        self._skip_layer(layer_id)

    def _shape_copperarea(self, parts, comp_index, in_footprint):
        layer_id = _f(parts, 2)
        path = _f(parts, 4)
        fill_data = _f(parts, 10)
        contours = []
        if fill_data:
            try:
                arr = json.loads(fill_data)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, list):
                            for pstr in item:
                                if isinstance(pstr, str):
                                    contours.extend(_parse_path_contours(pstr))
            except (json.JSONDecodeError, TypeError, ValueError):
                contours = []
        if not contours:
            contours = _parse_path_contours(path)
        if not contours:
            return
        cu = self._copper(layer_id)
        if cu == -1:
            self._inner_note(layer_id)
        elif cu == common.COPPER_TOP:
            self.polys.append({"layer": "TOP", "contours": contours})
        elif cu is not None:
            self.polys.append({"layer": "BOTTOM", "contours": contours})
        else:
            self._skip_layer(layer_id)

    def _shape_text(self, parts, comp_index, in_footprint):
        # 文字图形本身不渲染；位号/值在 LIB 里单独取（见 parse_lib）
        return

    def _shape_dimension(self, parts, comp_index, in_footprint):
        self.doc_graphics_skipped += 1

    def _shape_protractor(self, parts, comp_index, in_footprint):
        self.doc_graphics_skipped += 1

    def _shape_svgnode(self, parts, comp_index, in_footprint):
        self.svg_skipped += 1

    def _shape_image(self, parts, comp_index, in_footprint):
        self.doc_graphics_skipped += 1

    def _shape_pimage(self, parts, comp_index, in_footprint):
        self.doc_graphics_skipped += 1

    def _shape_planezone(self, parts, comp_index, in_footprint):
        self._warn("内电层 PLANEZONE 暂不解析（按铺铜缺失处理）")

    # -- LIB --------------------------------------------------------------
    def parse_lib(self, rec):
        chunks = rec.split(SUB_DELIM)
        head = chunks[0].split("~")
        x = _num(head, 1)
        y = _num(head, 2)
        params = _kv_params(_f(head, 3))
        rot = _num(head, 4)
        layer_id = _f(head, 7)
        side = "bottom" if layer_id == LAYER_BOTTOM else "top"

        comp = {
            "d": "",
            "fp": params.get("package", "") or "",
            "side": side,
            "x": common.rnd(x * UNIT),
            "y": common.rnd(-y * UNIT),
            "rot": round(rot, 3),
            "val": "",
            "comment": "",
            "value": "",
            "mpn": "",
        }
        idx = len(self.data["components"])
        self.data["components"].append(comp)

        designator = ""
        value = ""
        for child in chunks[1:]:
            if not child:
                continue
            f = child.split("~")
            if f[0] == "TEXT":
                ttype = _f(f, 1)
                text = _clean_text(_f(f, 10))
                if ttype == "P" and not designator:
                    designator = text
                elif ttype == "N" and not value:
                    value = text
                continue
            self.handle(child, comp_index=idx, in_footprint=True)

        # 老格式：值可能写在 params 里
        if not value:
            value = params.get("value", "") or ""
        if not designator:
            designator = params.get("pre", "") or params.get("designator", "")
        comp["d"] = designator
        comp["value"] = value
        comp["val"] = value
        comp["comment"] = (params.get("Manufacturer Part", "")
                           or params.get("BOM_Manufacturer Part", ""))
        comp["mpn"] = (params.get("Supplier Part", "")
                       or params.get("BOM_Supplier Part", ""))

    # -- finish ------------------------------------------------------------
    def finish_outline(self):
        outline = []
        # 先串线段（板框 TRACK/ARC/RECT）
        if self.outline_segments:
            loops = _chain_outline(self.outline_segments, tol=0.5)
            loops.sort(key=lambda item: _polygon_area(item[0]), reverse=True)
            for pts, _closed in loops:
                if len(pts) < 3:
                    continue
                if _polygon_area(pts) < 1.0:       # 原始单位²，约 0.065 mm² 的碎片忽略
                    continue
                outline.append([list(_xy(px, py)) for px, py in pts])
        # 再补整圆板框
        for pts in self.outline_loops:
            if len(pts) > 2:
                outline.append([list(p) for p in pts])

        if not outline:
            # 文件自带 BBox 兜底
            bb = self.doc.get("BBox") or {}
            try:
                bx = float(bb.get("x"))
                by = float(bb.get("y"))
                bw = float(bb.get("width"))
                bh = float(bb.get("height"))
                if bw > 0 and bh > 0:
                    outline = [[list(_xy(bx, by)), list(_xy(bx + bw, by)),
                                list(_xy(bx + bw, by + bh)), list(_xy(bx, by + bh))]]
                    self._warn("板框层(BoardOutLine)没有可用的闭合图形，用文件自带 BBox 代替")
            except (TypeError, ValueError, AttributeError):
                outline = []
        # 去毛刺（与其他后端保持一致）
        if outline and self.spur_trim:
            try:
                outline, removed = common.trim_outline_slivers(outline)
                if removed:
                    self._warn("板框里去掉了 %d 个细长毛刺顶点" % removed)
            except Exception as exc:
                self._warn("板框毛刺清理失败：%s" % exc)
        self.data["outline"] = outline

    def finish_warnings(self):
        if self.skipped_layers:
            layers = ", ".join("%s(×%d)" % (k, v)
                               for k, v in sorted(self.skipped_layers.items(),
                                                  key=lambda kv: str(kv[0])))
            self._warn("忽略了非铜/非丝印/非板框层的图元：%s" % layers)
        if self.unknown:
            items = ", ".join("%s(×%d)" % (k, v) for k, v in sorted(self.unknown.items()))
            self._warn("暂不支持的图元类型：%s" % items)
        if self.svg_skipped:
            self._warn("忽略了 %d 个 SVGNODE 图形（3D 模型 / 矢量图形，不影响贴片定位）"
                       % self.svg_skipped)
        if self.cutout_saw:
            self._warn("%d 个 SOLIDREGION 板挖槽(Multi-Layer 11)未渲染" % self.cutout_saw)
        if self.doc_graphics_skipped:
            self._warn("忽略了 %d 个文档层/尺寸/装配层图形" % self.doc_graphics_skipped)


def _parse_path_contours(path):
    """SVG 风格路径（M/L/A/C/Z，坐标可为空格/逗号分隔）-> [[ [x,y], ... ], ...] mm。"""
    if not path or not isinstance(path, str):
        return []
    toks = re.findall(r"[MLAZCmlazc]|-?\d+(?:\.\d+)?|[-+]?\d*\.\d+", path)
    if not toks:
        return []
    contours = []
    chain = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    i = 0
    cmd = None

    def flush():
        nonlocal chain
        if len(chain) > 2:
            contours.append([list(_xy(px, py)) for px, py in chain])
        chain = []

    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t.upper()
            i += 1
            if cmd == "Z":
                if chain and len(chain) > 2 and abs(chain[0][0] - cur[0]) + \
                        abs(chain[0][1] - cur[1]) > 1e-9:
                    chain.append(chain[0])       # 显式闭合
                flush()
                cur = start
                continue
            if cmd not in ("M", "L", "A", "C"):
                continue
            # 针对 M/L/C/A 读取参数
            continue
        # 数字：按当前命令消费
        try:
            if cmd in ("M", "L"):
                x = float(toks[i]); y = float(toks[i + 1]); i += 2
                pt = (x, y)
                if cmd == "M":
                    if chain:
                        flush()
                    start = pt
                    chain = [pt]
                    cmd = "L"                      # 后续无命令坐标视作 L
                else:
                    if not chain:
                        chain = [cur]
                    chain.append(pt)
                cur = pt
                continue
            if cmd == "A":
                # rx ry rot far cw ex ey
                rx = float(toks[i]); ry = float(toks[i + 1])
                far = int(float(toks[i + 3])); cw = int(float(toks[i + 4]))
                ex = float(toks[i + 5]); ey = float(toks[i + 6]); i += 7
                arc = (cur[0], cur[1], ex, ey, (abs(rx) + abs(ry)) / 2.0,
                       bool(far), bool(cw))
                if not chain:
                    chain = [cur]
                chain.extend(_arc_polyline_raw(arc)[1:])
                cur = (ex, ey)
                continue
            if cmd == "C":
                # 贝塞尔：按直线近似到终点
                x1 = float(toks[i]); y1 = float(toks[i + 1])
                x2 = float(toks[i + 2]); y2 = float(toks[i + 3])
                x3 = float(toks[i + 4]); y3 = float(toks[i + 5]); i += 6
                if not chain:
                    chain = [cur]
                for k in range(1, 9):
                    u = k / 8.0
                    b0 = (1 - u) ** 3
                    b1 = 3 * (1 - u) ** 2 * u
                    b2 = 3 * (1 - u) * u ** 2
                    b3 = u ** 3
                    chain.append((b0 * cur[0] + b1 * x1 + b2 * x2 + b3 * x3,
                                  b0 * cur[1] + b1 * y1 + b2 * y2 + b3 * y3))
                cur = (x3, y3)
                continue
            i += 1
        except (IndexError, ValueError):
            break
    if chain:
        flush()
    return contours


# --------------------------------------------------------------- find input
def _resolve_root(root, out_dir):
    root = os.path.abspath(root)
    if not os.path.exists(root):
        raise SystemExit("输入不存在: %s" % root)
    if os.path.isfile(root) and root.lower().endswith(".zip"):
        out = out_dir or tempfile.mkdtemp(prefix="easyeda_std_")
        return common.materialize(root, out)
    return root


def sniff(root):
    """Cheap check: is this (a dir/file containing) an EasyEDA Standard PCB JSON?"""
    try:
        root = os.path.abspath(root)
        if not os.path.exists(root):
            return False
        if os.path.isfile(root):
            low = root.lower()
            if not (low.endswith(".json") or low.endswith(".json5")):
                return False
            if _peek_doc_type(root) in (DOC_TYPE_PCB, DOC_TYPE_PCB_MODULE,
                                       DOC_TYPE_FOOTPRINT):
                return True
            return _peek_legacy_pcb(root)
        if os.path.isdir(root):
            for p in _candidate_files(root):
                if _peek_doc_type(p) in (DOC_TYPE_PCB, DOC_TYPE_PCB_MODULE,
                                         DOC_TYPE_FOOTPRINT):
                    return True
                if _peek_doc_type(p) is None and _peek_legacy_pcb(p):
                    return True
    except Exception:
        return False
    return False


def parse(root, out_dir=None, **opts):
    title_opt = opts.get("title")
    pcb_hint = opts.get("pcb_hint")
    spur_trim = opts.get("spur_trim", True)

    work_root = _resolve_root(root, out_dir)
    path = _pick_doc(work_root, pcb_hint)
    doc = _load_doc(path)
    if not isinstance(doc, dict):
        raise SystemExit("不是有效的 JSON 对象：%s" % path)

    doc_type = _doc_type_of(doc)
    assumed = False
    if doc_type not in (DOC_TYPE_PCB, DOC_TYPE_PCB_MODULE, DOC_TYPE_FOOTPRINT):
        # 没有 head.docType 的老文件：靠结构判断
        if isinstance(doc.get("shape"), list) and doc.get("canvas"):
            doc_type = DOC_TYPE_PCB
            assumed = True
        elif any(k in doc for k in ("TRACK", "PAD", "VIA")):
            doc_type = DOC_TYPE_PCB
            assumed = True
    if doc_type not in (DOC_TYPE_PCB, DOC_TYPE_PCB_MODULE, DOC_TYPE_FOOTPRINT):
        raise SystemExit("%s 不是 PCB 文档（docType=%s，PCB 应为 3）" % (path, doc_type))

    base = os.path.splitext(os.path.basename(path))[0]
    doc_title = doc.get("title")
    title = title_opt or (doc_title if isinstance(doc_title, str) else "") or base

    data = common.new_data(title)
    data["meta"]["source"] = {
        "pcb": path,
        "format": FORMAT,
        "doc_type": doc_type,
        "editor_version": _editor_version(doc),
    }
    if assumed:
        builder0_warn = "文件没有 head.docType，按 PCB 文档结构解析"
    else:
        builder0_warn = ""

    builder = _Builder(data, doc, path)
    builder.spur_trim = bool(spur_trim)
    if builder0_warn:
        builder._warn(builder0_warn)

    shapes = doc.get("shape")
    if doc_type == DOC_TYPE_FOOTPRINT:
        _parse_footprint_doc(builder, doc)
    elif isinstance(shapes, list):
        skipped_objs = 0
        for rec in shapes:
            if isinstance(rec, str) and rec:
                if rec.startswith("LIB" + "~"):
                    builder.parse_lib(rec)
                else:
                    builder.handle(rec)
            elif rec:
                skipped_objs += 1
        if skipped_objs:
            builder._warn("shape 里有 %d 条非字符串记录（未知格式），已忽略" % skipped_objs)
    elif isinstance(doc, dict) and any(k in doc for k in ("TRACK", "PAD", "VIA")):
        # 2019 年前的「文件对象」格式（dict + itemOrder），尽力解析
        _parse_legacy_dict(builder, doc)
    elif shapes is not None:
        builder._warn("shape 字段不是数组（可能是未知的旧版格式），只解析了文首信息")

    builder.finish_outline()
    builder.finish_warnings()

    if doc_type == DOC_TYPE_FOOTPRINT:
        builder._warn("这是封装库文件(docType=4)，没有 PCB 位号/坐标，只画出了封装本身")

    # ---- 元件位号缺失提示 --------------------------------------------
    missing = [i for i, c in enumerate(data["components"]) if not c.get("d")]
    if missing:
        for i in missing:
            data["components"][i]["d"] = "?"
        builder._warn("%d 个元件没有找到位号(TEXT~P)，已用 ? 占位" % len(missing))

    # ---- 统计/警告兜底（finalize 会补 stats） --------------------------
    if not data["components"] and not data["pads"]:
        builder._warn("没有解析到任何元件或焊盘：这个 json 可能不是 PCB 文档")

    return data


def _parse_footprint_doc(builder, doc):
    """docType=4：单个封装库。做成一个合成元件，画在原点附近。"""
    shapes = doc.get("shape")
    if not isinstance(shapes, list):
        shapes = []
    params = _kv_params(_params_field(doc))
    comp = {
        "d": params.get("pre", "") or "FP1",
        "fp": params.get("package", "") or params.get("name", "") or "FOOTPRINT",
        "side": "top", "x": 0.0, "y": 0.0, "rot": 0.0,
        "val": "", "comment": "", "value": "", "mpn": "",
    }
    idx = len(builder.data["components"])
    builder.data["components"].append(comp)
    for rec in shapes:
        if not isinstance(rec, str) or not rec:
            continue
        f = rec.split("~")
        kind = f[0]
        if kind == "TEXT":
            ttype = _f(f, 1)
            text = _clean_text(_f(f, 10))
            if ttype == "P":
                comp["d"] = text or comp["d"]
            elif ttype == "N":
                comp["value"] = text
            continue
        builder.handle(rec, comp_index=idx, in_footprint=True)
    comp["val"] = comp["value"]
    # 封装文件的坐标原点通常就是 (0,0)，用焊盘包围盒中心当元件原点更稳
    ps = [p for p in builder.data["pads"] if p.get("c") == idx]
    if ps:
        comp["x"] = common.rnd((min(p["x"] - p["w"] / 2 for p in ps)
                                + max(p["x"] + p["w"] / 2 for p in ps)) / 2)
        comp["y"] = common.rnd((min(p["y"] - p["h"] / 2 for p in ps)
                                + max(p["y"] + p["h"] / 2 for p in ps)) / 2)


def _params_field(doc):
    """封装库/老格式里参数可能出现的位置。"""
    for key in ("c_para", "params"):
        v = doc.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v:
            return "`".join("%s`%s" % (k, v[k]) for k in v)
    head = doc.get("head")
    if isinstance(head, dict):
        c = head.get("c_para")
        if isinstance(c, str) and c:
            return c
        if isinstance(c, dict) and c:
            return "`".join("%s`%s" % (k, c[k]) for k in c)
    return ""


def _parse_legacy_dict(builder, doc):
    """旧版（<2020）EasyEDA 文件对象格式：{"TRACK": {gId: {...}}, "itemOrder": [...]}。

    只做尽力而为的转换：TRACK/PAD/VIA/ARC/CIRCLE/RECT/SOLIDREGION/COPPERAREA/TEXT，
    以及带 x/y/rotation/layerid 的 FOOTPRINT。命中不了就如实警告。
    """
    builder._warn("检测到旧版「文件对象」dict 格式（2020 年前），按兼容模式尽力解析，"
                  "可能缺少部分图元")
    order = doc.get("itemOrder") or []

    def each(key):
        table = doc.get(key)
        if not isinstance(table, dict):
            return
        ids = [k for k in order if k in table] + [k for k in table if k not in order]
        for gid in ids:
            val = table.get(gid)
            if isinstance(val, dict):
                yield gid, val

    def pts_field(obj):
        arr = obj.get("pointArr") or obj.get("points") or []
        out = []
        for p in arr:
            if isinstance(p, dict):
                out.extend([p.get("x", 0), p.get("y", 0)])
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                out.extend([p[0], p[1]])
        return " ".join("%s %s" % (v, out[i + 1]) for i, v in enumerate(out) if i % 2 == 0)

    def pt(obj):
        if isinstance(obj.get("pointArr"), list) and obj["pointArr"]:
            p = obj["pointArr"][0]
            if isinstance(p, dict):
                return p.get("x", 0), p.get("y", 0)
        return obj.get("x", 0), obj.get("y", 0)

    for gid, obj in each("PAD"):
        x, y = pt(obj)
        rec = "PAD~%s~%s~%s~%s~%s~%s~%s~%s~%s~~%s~%s~0~" % (
            obj.get("shape", "RECT"), x, y, obj.get("width", 0), obj.get("height", 0),
            obj.get("layerid", "1"), obj.get("net", ""), obj.get("number", ""),
            obj.get("holeR", 0), obj.get("rotation", 0), gid)
        builder.handle(rec)
    for gid, obj in each("TRACK"):
        rec = "TRACK~%s~%s~%s~%s~%s~0" % (
            obj.get("strokeWidth", 1), obj.get("layerid", "1"),
            obj.get("net", ""), pts_field(obj), gid)
        builder.handle(rec)
    for gid, obj in each("VIA"):
        rec = "VIA~%s~%s~%s~%s~%s~%s~0" % (
            obj.get("x", 0), obj.get("y", 0), obj.get("diameter", 0),
            obj.get("net", ""), obj.get("holeR", 0), gid)
        builder.handle(rec)
    for gid, obj in each("CIRCLE"):
        rec = "CIRCLE~%s~%s~%s~%s~%s~%s~0" % (
            obj.get("cx", 0), obj.get("cy", 0), obj.get("r", 0),
            obj.get("strokeWidth", 0), obj.get("layerid", "1"), gid)
        builder.handle(rec)
    for gid, obj in each("RECT"):
        rec = "RECT~%s~%s~%s~%s~%s~~~%s" % (
            obj.get("x", 0), obj.get("y", 0), obj.get("width", 0),
            obj.get("height", 0), obj.get("layerid", "1"),
            obj.get("strokeWidth", 0))
        builder.handle(rec)
    for gid, obj in each("ARC"):
        rec = "ARC~%s~%s~%s~%s" % (
            obj.get("strokeWidth", 1), obj.get("layerid", "1"),
            obj.get("net", ""), obj.get("d", ""))
        builder.handle(rec)
    for gid, obj in each("SOLIDREGION"):
        rec = "SOLIDREGION~%s~%s~%s~%s" % (
            obj.get("layerid", "1"), obj.get("net", ""), obj.get("pathStr", ""),
            obj.get("type", "solid"))
        builder.handle(rec)
    for gid, obj in each("COPPERAREA"):
        rec = "COPPERAREA~~%s~%s~%s" % (
            obj.get("layerid", "1"), obj.get("net", ""), obj.get("pathStr", ""))
        builder.handle(rec)
    fp_count = sum(1 for _gid, _obj in each("FOOTPRINT"))
    if fp_count:
        builder._warn("旧版格式里的 %d 个封装(FOOTPRINT)没有可靠的摆放信息，"
                      "只画了散件图元，元件/焊盘可能缺失" % fp_count)


if __name__ == "__main__":                       # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="嘉立创EDA 标准版 PCB .json -> pcb_data.json")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default="pcb_data.json")
    ap.add_argument("--title")
    args = ap.parse_args()
    d = common.finalize(parse(args.input, os.path.dirname(os.path.abspath(args.output)) or ".",
                              title=args.title))
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print("wrote", args.output, os.path.getsize(args.output), "bytes")
