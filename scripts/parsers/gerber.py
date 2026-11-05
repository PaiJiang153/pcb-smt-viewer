#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gerber (RS-274X / X2) + Excellon 钻孔 + 坐标文件 (Centroid / Pick&Place) 后端。

这是「通用」后端：任何 EDA（嘉立创EDA、Altium、KiCad、PADS、Allegro、立创下单页…）
都能导出 Gerber + 坐标文件，因此只要用户给出这套文件，就能渲染板子并放上全部位号。

输入（目录，或已经由 dispatcher 解压好的 zip 目录）：
  * Gerber 层文件：.GTL/.GBL/.GTO/.GBO/.GKO/.GM1/.G1/.GBR …（也支持 X2 的
    %TF.FileFunction% 自动判层、KiCad 的 `xxx-F_Cu.gbr` 命名、嘉立创
    `Gerber_TopLayer.GTL` 命名）
  * 钻孔文件：.DRL/.XLN/.EXC/.NC/.CNC/.TAP（Excellon 文本格式，含 KiCad/Altium/
    Protel/gEDA 各种写法；二进制（旧版 Protel）只警告不崩溃）
  * 坐标文件（可选）：*.csv/*.txt/*.pos/*.xy，按表头模糊匹配列（中英文都行）

坐标约定：输出 mm、**Y 轴向上**（Gerber 原生就是 Y up）。元件旋转角原样输出
（JLC/Protel/Altium 的 Rotation 是逆时针；KiCad .pos 的 Rot 也是逆时针），
本后端不做镜像换算——贴片机导出时各家的正负号本来就不同，这里只保证「数值来自
原文件、不擅自改符号」，网页上显示的就是坐标文件里写的角度。

实现要点：
  * 单位/格式自适应：`%FS` 的前导/后导零抑制、显式小数点、`%MO` 的 mm/inch 都支持；
  * 孔径：C/R/O/P 标准孔径 + **孔径宏**（AM，支持 $1..$n 表达式和 1/2/20/21/22/4/5/6/7 图元，
    按外接矩形近似成焊盘，位置/尺寸基本准、异形形状略粗糙）；
  * 圆弧：G01/G02/G03、G74/G75、整圆（起终点相同）；
  * 区域：G36/G37 -> polygons（覆铜）或板框轮廓；
  * 判层优先级：`%TF.FileFunction%` > 扩展名 > 文件名关键词 > `%LN%` 层名（
    嘉立创 `Gerber_TopLayer.GTL`、KiCad `-F_Cu.gbr`、Protel `.GTL/.GM1/.GKO`、
    gEDA `%LNFRONT%` 都能认）；认不出的层会在缺顶/底铜时兜底当铜箔用；
  * 钻孔：Excellon（M48 头、INCH/METRIC、LZ/TZ、`;FILE_FORMAT`/`;FORMAT`、小数坐标、
    G85 槽孔、G01/G02/G03 铣槽——按两端点近似、R 重复孔展开、G32/G33/M15/M16、模态坐标）。
    格式有歧义时**用铜箔闪光位置/板框包围盒打分**挑最合理的解释（gEDA 2.3、Altium 2.4 都能对）；
  * 坐标文件：自动找 *.csv/*.txt/*.pos/*.xy（也容忍中文表头/表格前有说明行），
    列名模糊匹配，mm/mil 逐格识别 + 无单位时按板框打分定单位；坐标明显偏移时
    会尝试上下翻转/平移对齐并给出警告。旋转角原样输出（JLC/Protel/Altium 逆时针，
    KiCad .pos 也是逆时针），不擅自改符号。

已知限制：
  * 孔径宏焊盘按外接矩形近似（形状略粗糙，位置/尺寸基本准）；
  * 反极性（%LPC%）图元被跳过（覆铜保持整片，挖空效果不还原）；
  * 内层铜、阻焊/钢网层不渲染；
  * RS-274D（无 %ADD% 孔径定义）按 0.2mm 圆笔近似；
  * 拼板 step&repeat / 镜像 / 旋转 / 缩放指令忽略；
  * 二进制钻孔（旧版 Protel/PCAD）只警告不解析；
  * Gerber 没有「哪个焊盘属于哪个位号」的信息，元件框是按附近铜箔焊盘估计的
    （只影响显示框，不影响坐标；因此 finalize 会报「N 个元件没有焊盘」属正常）。
"""
import csv
import io
import math
import os
import re
import sys

try:                                  # 作为包导入：from parsers import gerber
    from . import common
except ImportError:                   # 直接运行：python3 scripts/parsers/gerber.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from parsers import common

FORMAT = "gerber"
LABEL = "Gerber / Excellon / 坐标文件 (通用)"

MM_PER_INCH = 25.4
DEFAULT_APERTURE_MM = 0.2      # RS-274D（无孔径定义）时的默认圆笔
DEFAULT_DRILL_MM = 0.8        # 钻孔文件里引用了未定义刀具时

NO_COMPONENT = common.NO_COMPONENT

# ---------------------------------------------------------------------------
# 文件类型 / 层名识别
# ---------------------------------------------------------------------------
_GERBER_EXTS = {
    ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp",
    ".gko", ".gm1", ".gm2", ".gml", ".gbr", ".gbx", ".ger", ".art", ".pho",
    ".gdo", ".grb", ".g1", ".g2", ".g3", ".g4", ".g5", ".g6", ".g7", ".g8",
    ".g9", ".g10", ".gp1", ".gp2", ".gg1", ".gd1",
    ".top", ".bot", ".cmp", ".sol", ".plc", ".pls", ".ast", ".asb",
    ".sst", ".ssb", ".smt", ".smb",
}
_DRILL_EXTS = {".drl", ".xln", ".exc", ".ncd", ".nc", ".cnc", ".tap", ".xnc"}
_CENTROID_EXTS = {".csv", ".pos", ".xy", ".pnp", ".cpl", ".mnt", ".mnb"}
_SKIP_EXTS = {".zip", ".rar", ".7z", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
              ".md", ".json", ".gbrjob", ".html", ".xlsx", ".xls", ".doc",
              ".docx", ".log", ".bak", ".db", ".gpi", ".drr", ".apr", ".rep",
              ".rul", ".ldp", ".ps", ".svg", ".py", ".pyc", ".sh", ".bat",
              ".cmd", ".ini", ".cfg", ".toml", ".yaml", ".yml", ".gitignore"}

_EXT_KIND = {
    ".gtl": "copper_top", ".gbl": "copper_bottom",
    ".gto": "silk_top", ".gbo": "silk_bottom",
    ".gts": "mask_top", ".gbs": "mask_bottom",
    ".gtp": "paste_top", ".gbp": "paste_bottom",
    ".gko": "outline", ".gm1": "outline", ".gml": "outline", ".gm2": "mech",
    ".cmp": "copper_top", ".sol": "copper_bottom",
    ".plc": "silk_top", ".pls": "silk_bottom",
    ".sst": "silk_top", ".ssb": "silk_bottom",
    ".smt": "mask_top", ".smb": "mask_bottom",
    ".ast": "mech", ".asb": "mech",
    ".top": "copper_top", ".bot": "copper_bottom",
    ".gp1": "mech", ".gp2": "mech", ".gg1": "mech", ".gd1": "mech",
}

# 文件名里的关键词（小写；已把非字母数字替换成 _）
_NAME_RULES = (
    ("outline", ("edge_cuts", "edgecuts", "outline", "board_outline", "boardoutline",
                 "profile", "keepout", "keep_out", "外形", "边框", "板框")),
    ("copper_top", ("f_cu", "front_cu", "top_cu", "toplayer", "top_layer",
                    "topcopper", "top_copper", "copper_top", "cu_top", "top_etch")),
    ("copper_bottom", ("b_cu", "back_cu", "bottom_cu", "bottomlayer", "botlayer",
                       "bottomcopper", "bottom_copper", "copper_bottom", "cu_bottom",
                       "bot_etch", "back_copper")),
    ("copper_inner", ("in1_cu", "in2_cu", "in3_cu", "in4_cu", "inner", "mid1",
                      "mid2", "内层")),
    ("silk_top", ("f_silks", "f_silk", "topsilk", "top_silk", "topsilkscreen",
                  "top_silkscreen", "silkscreen_top", "topoverlay", "top_overlay",
                  "overlay_top", "legend_top", "top_legend", "frontsilk",
                  "front_silk", "silks_front")),
    ("silk_bottom", ("b_silks", "b_silk", "botsilk", "bottom_silk", "bottomsilkscreen",
                     "bottom_silkscreen", "silkscreen_bottom", "bottomoverlay",
                     "bottom_overlay", "overlay_bottom", "legend_bottom",
                     "backsilk", "back_silk", "silks_back")),
    ("mask_top", ("f_mask", "f_masks", "topmask", "top_mask", "topsoldermask",
                  "soldermask_top", "solder_top", "top_solder")),
    ("mask_bottom", ("b_mask", "b_masks", "bottommask", "bottom_mask",
                     "bottomsoldermask", "soldermask_bottom", "solder_bottom")),
    ("paste_top", ("f_paste", "toppaste", "top_paste", "paste_top")),
    ("paste_bottom", ("b_paste", "bottompaste", "bottom_paste", "paste_bottom")),
    ("mech", ("mechanical", "mech", "milling", "milled", "dimension", "assembly",
              "courtyard", "fab", "drill_drawing", "drilldrawing")),
)


def _norm_name(name):
    """小写 + 非字母数字转下划线，便于关键词匹配。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", name.lower())


def _kind_from_filefunction(ff):
    """%TF.FileFunction,Copper,L1,Top*% -> 内部层类别。"""
    if not ff:
        return None
    parts = [p.strip().lower() for p in ff.split(",") if p.strip()]
    if not parts:
        return None
    head = parts[0]
    joined = ",".join(parts)
    side = None
    if "top" in joined:
        side = "top"
    elif "bot" in joined or "back" in joined:
        side = "bottom"
    if head.startswith("copper"):
        if "inr" in joined or "inner" in joined:
            return "copper_inner"
        return "copper_top" if side != "bottom" else "copper_bottom"
    if head.startswith(("legend", "silkscreen", "silk")):
        return "silk_top" if side != "bottom" else "silk_bottom"
    if head.startswith("profile"):
        return "outline"
    if head.startswith(("keep-out", "keepout", "keep_out")):
        return "outline"
    if head.startswith("soldermask"):
        return "mask_top" if side != "bottom" else "mask_bottom"
    if head.startswith("paste"):
        return "paste_top" if side != "bottom" else "paste_bottom"
    if head.startswith(("assembly", "glue", "carbon", "other", "mechanical")):
        return "mech"
    return None


def _kind_from_layername(ln):
    """%LNFRONT*% / %LNTOPSILK*% 等（gEDA/PCB 会写）。"""
    if not ln:
        return None
    n = _norm_name(ln)
    for kind, keys in _NAME_RULES:
        for k in keys:
            if k in n:
                return kind
    if "front" in n or n.endswith("_top") or n == "top":
        return "copper_top"
    if "back" in n or n.endswith("_bottom") or n == "bottom":
        return "copper_bottom"
    return None


def _kind_from_name(path):
    """扩展名 + 文件名关键词。"""
    name = os.path.basename(path)
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    if re.fullmatch(r"\.g\d+", ext):
        return "copper_inner"
    n = _norm_name(name)
    for kind, keys in _NAME_RULES:
        for k in keys:
            if k in n:
                return kind
    if ext in (".gbr", ".gbx", ".ger", ".art", ".pho", ".gdo", ".grb"):
        return "unknown"
    return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _read_text(path):
    raw = _read_bytes(path)
    return _decode(raw), raw


def _looks_binary(raw):
    sample = raw[:4096]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    bad = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return bad / float(len(sample)) > 0.10


def _looks_like_gerber_head(head):
    if not head:
        return False
    h = head.upper()
    if "%FS" in h or "%MO" in h or "%ADD" in h or "%AM" in h:
        return True
    if h.lstrip().startswith("G04") and "*" in h:
        return True
    if h.lstrip().startswith(("G70", "G71", "G90", "G91", "D10", "G54")) and "*" in h:
        return True
    return False


def _looks_like_excellon(head):
    if not head:
        return False
    h = head.upper()
    if re.search(r"(?m)^\s*M48\s*$", h) or re.search(r"(?m)^\s*M48[\s*]", h):
        return True
    if re.search(r"(?m)^\s*(METRIC|INCH)\s*(,|$)", h) and re.search(r"(?m)^\s*T\d+C", h):
        return True
    if re.search(r"(?m)^\s*T\d*(F\d+)?(S\d+)?C[\d.]+", h) and "%" in h:
        return True
    return False


def _fmt_num(v, nd=4):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Gerber 解析器
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"%([^%]*)%|([^*%]*)\*", re.S)
_WORD_RE = re.compile(r"([GMDXYIJ])([+-]?[0-9]*(?:\.[0-9]*)?)", re.I)
_FS_RE = re.compile(r"FS([LTD]?)([AI]?)X(\d)(\d)Y(\d)(\d)", re.I)
_ADD_RE = re.compile(r"ADD(\d+)([^,]+)(?:,(.*))?$", re.I | re.S)
_MACRO_RE = re.compile(r"AM([^,*]*)")
_FF_RE = re.compile(r"TF\.FileFunction,([^*%]*)", re.I)
_LN_RE = re.compile(r"LN([^*%]*)", re.I)
_OF_RE = re.compile(r"OF(A)?X([+-]?[\d.]+)Y([+-]?[\d.]+)", re.I)


class _LayerData(object):
    """解析一个 Gerber 文件得到的原始图元（单位 mm）。"""

    def __init__(self, path):
        self.path = path
        self.lines = []       # [x1, y1, x2, y2, width]  D01 直线
        self.arcs = []        # [cx, cy, r, a0, a1, width]  a0->a1 逆时针
        self.pads = []        # {x,y,w,h,s,r,hole}   D03 闪光（及零长度 D01）
        self.regions = []     # [[ [x,y], ... ], ...]  G36/G37 区域（只收反极性=D）
        self.node_count = 0
        self.file_function = ""
        self.layer_name = ""
        self.unit_mm = MM_PER_INCH
        self.negative = False
        self.polarity = "D"
        self.clear_skipped = 0
        self.macro_skipped = 0
        self.macro_approx = 0
        self.missing_aperture = 0
        self.unsupported = set()
        self.warnings = []


class _GerberParser(object):
    def __init__(self, data):
        self.d = data

    # -- 状态 ---------------------------------------------------------
    def run(self):
        text, raw = _read_text(self.d.path)
        if _looks_binary(raw):
            self.d.warnings.append("看起来是二进制文件（旧版 Protel/PCAD 的钻孔或 Gerber），未解析：%s"
                                   % os.path.basename(self.d.path))
            return self.d
        m = _FF_RE.search(text[:8000])
        if m:
            self.d.file_function = m.group(1).strip()
        m = _LN_RE.search(text[:8000])
        if m:
            self.d.layer_name = m.group(1).strip()

        self.fmt = None          # (xi, xd, yi, yd)
        self.sup = "L"           # L 前导零省略 / T 后导零省略 / D 显式小数点
        self.dec = False
        self.x = self.y = 0.0
        self.aper = 0
        self.apertures = {}
        self.macros = set()
        self.macro_defs = {}
        self.interp = 1          # 1 直线 2 顺时针 3 逆时针
        self.multi = True        # G75 多象限
        self.region = False
        self.region_cur = None
        self.region_contours = []
        self.ox = self.oy = 0.0  # %OF% 偏移（文件单位）
        self.pending = None      # (x, y) 或 None
        self.pi = self.pj = 0.0
        self.fs_seen = False
        self.g74_arcs = 0
        self.unsupported_names = set()

        for m in _TOKEN_RE.finditer(text):
            ext = m.group(1)
            if ext is not None:
                self._extended(ext)
            else:
                blk = m.group(2)
                if blk and blk.strip():
                    self._block(blk)
        # 未闭合的区域
        self._close_region()
        if not self.fs_seen:
            self.d.warnings.append("没有 %%FS%% 格式语句，按 %s 默认格式解析：%s"
                                   % ("3.3 mm" if self.d.unit_mm == 1.0 else "2.4 inch",
                                      os.path.basename(self.d.path)))
        if self.g74_arcs:
            self.d.unsupported.add("G74 单象限圆弧(按几何反推圆心)")
        if self.unsupported_names:
            self.d.unsupported |= self.unsupported_names
        return self.d

    # -- 扩展指令 -----------------------------------------------------
    def _extended(self, s):
        s = s.strip()
        if not s:
            return
        parts = [p.strip() for p in s.split("*")]
        for idx, p in enumerate(parts):
            if not p:
                continue
            if idx == 0 and p[:2].upper() == "AM":
                mm = _MACRO_RE.match(p)
                name = mm.group(1).strip() if mm else (p[2:].strip() if len(p) > 2 else "")
                if name:
                    self.macro_defs[name.upper()] = [
                        [t.strip() for t in body.split(",") if t.strip()]
                        for body in parts[1:] if body.strip()]
                return                      # 宏体已收进宏定义表
            self._ext_cmd(p)

    def _ext_cmd(self, p):
        u = p.upper()
        if u.startswith("FS"):
            m = _FS_RE.match(u)
            if m:
                self.sup = m.group(1) or "L"
                self.dec = self.sup == "D"
                xi = int(m.group(3))
                xd = int(m.group(4))
                yi = int(m.group(5))
                yd = int(m.group(6))
                self.fmt = (xi, xd, yi, yd)
                self.fs_seen = True
            else:
                self.d.unsupported.add("无法识别的 FS 格式: %s" % p[:20])
            return
        if u.startswith("MO"):
            if "MM" in u:
                self.d.unit_mm = 1.0
            elif "IN" in u:
                self.d.unit_mm = MM_PER_INCH
            return
        if u.startswith("ADD"):
            self._aperture(p)
            return
        if u.startswith("LP"):
            tail = u[2:].strip()
            self.d.polarity = "C" if tail.startswith("C") else "D"
            return
        if u.startswith("TF."):
            ff = _FF_RE.match(p)
            if ff and not self.d.file_function:
                self.d.file_function = ff.group(1).strip()
            return
        if u.startswith(("TA.", "TO.", "TD", "TC.", "TR.")):
            return
        if u.startswith("LN"):
            self.d.layer_name = p[2:].strip()
            return
        if u.startswith("IP"):
            if "NEG" in u:
                self.d.negative = True
                self.d.unsupported.add("IPNEG 负片")
            return
        if u.startswith("OF"):
            m = _OF_RE.match(p)
            if m:
                self.ox = float(m.group(2))
                self.oy = float(m.group(3))
            return
        if u.startswith("AM"):
            return
        if u.startswith(("MI", "SF", "IR", "AS", "SR", "AB", "IN", "IO", "KO")):
            self.unsupported_names.add(u[:2])
            return
        # 未知扩展指令：忽略
        self.unsupported_names.add(u[:2] if u else "?")

    def _aperture(self, p):
        m = _ADD_RE.match(p)
        if not m:
            self.unsupported_names.add("ADD?")
            return
        code = int(m.group(1))
        name = m.group(2).strip()
        params_raw = (m.group(3) or "").strip()
        params = []
        for t in params_raw.split("X"):
            t = t.strip()
            if not t:
                continue
            try:
                params.append(float(t))
            except ValueError:
                params.append(0.0)
        un = self.d.unit_mm
        ap = {"shape": "C", "w": DEFAULT_APERTURE_MM, "h": DEFAULT_APERTURE_MM,
              "hole": 0.0, "rot": 0.0, "n": 0, "macro": False}
        key = name.upper()
        if key == "C":
            d = params[0] * un if params else DEFAULT_APERTURE_MM
            ap.update(w=abs(d), h=abs(d), shape="C")
            if len(params) > 1:
                ap["hole"] = abs(params[1]) * un
        elif key in ("R", "O"):
            w = abs(params[0]) * un if params else DEFAULT_APERTURE_MM
            h = abs(params[1]) * un if len(params) > 1 else w
            ap.update(w=max(w, 1e-4), h=max(h, 1e-4), shape=key)
            if len(params) > 2:
                ap["hole"] = abs(params[2]) * un
            if len(params) > 3:
                ap["rot"] = params[3]
        elif key == "P":
            d = abs(params[0]) * un if params else DEFAULT_APERTURE_MM
            ap.update(w=d, h=d, shape="P")
            if len(params) > 1:
                ap["n"] = int(round(params[1]))
            if len(params) > 2:
                ap["rot"] = params[2]
            if len(params) > 3:
                ap["hole"] = abs(params[3]) * un
        else:
            ap["macro"] = True
            ap["macro_name"] = name.upper()
            ap["macro_params"] = params
        self.apertures[code] = ap

    # -- 坐标换算 -----------------------------------------------------
    def _conv(self, tok):
        if not tok:
            return 0.0
        tok = tok.strip()
        s = -1.0 if tok.startswith("-") else 1.0
        t = tok.lstrip("+-")
        if "." in t:
            try:
                return float(t) * s * self.d.unit_mm
            except ValueError:
                return 0.0
        try:
            n = int(t or "0")
        except ValueError:
            return 0.0
        if self.fmt is None:
            xi, xd = (3, 3) if self.d.unit_mm == 1.0 else (2, 4)
        else:
            xi, xd = self.fmt[0], self.fmt[1]
        total = xi + xd
        if self.sup == "T" and len(t) < total:
            n *= 10 ** (total - len(t))
        return (n / float(10 ** xd)) * s * self.d.unit_mm

    def _xy(self, tok, base):
        return self._conv(tok) + base * self.d.unit_mm

    # -- 数据块 -------------------------------------------------------
    def _block(self, blk):
        s = blk.strip()
        if not s:
            return
        up = s.upper()
        if up.startswith("G04"):
            if "#@!" in s:                       # X2 属性写成注释的兼容格式
                attr = s.split("#@!", 1)[1].strip()
                if attr.upper().startswith("TF.FILEFUNCTION,") and not self.d.file_function:
                    self.d.file_function = attr.split(",", 1)[1].strip().rstrip("*")
            return
        px = py = None
        self.pi = self.pj = 0.0
        for m in _WORD_RE.finditer(s):
            c = m.group(1).upper()
            v = m.group(2)
            if c == "G":
                try:
                    code = int(float(v)) if v else 0
                except ValueError:
                    continue
                self._gcode(code)
            elif c == "M":
                try:
                    code = int(float(v)) if v else -1
                except ValueError:
                    continue
                if code in (0, 2):
                    self._close_region()
                    return
            elif c == "D":
                try:
                    code = int(float(v)) if v else 0
                except ValueError:
                    continue
                if code >= 10:
                    self.aper = code
                elif code in (1, 2, 3):
                    ex = px if px is not None else self.x
                    ey = py if py is not None else self.y
                    self._operation(code, self.x, self.y, ex, ey)
                    self.x, self.y = ex, ey
                    px = py = None
                    self.pi = self.pj = 0.0
            elif c == "X":
                px = self._xy(v, self.ox)
            elif c == "Y":
                py = self._xy(v, self.oy)
            elif c == "I":
                self.pi = self._conv(v)
            elif c == "J":
                self.pj = self._conv(v)
        if px is not None:
            self.x = px
        if py is not None:
            self.y = py

    def _gcode(self, code):
        if code in (1, 2, 3):
            self.interp = code
        elif code == 36:
            self.region = True
            self.region_cur = None
            self.region_contours = []
        elif code == 37:
            self._close_region()
        elif code == 70:
            self.d.unit_mm = MM_PER_INCH
        elif code == 71:
            self.d.unit_mm = 1.0
        elif code == 74:
            self.multi = False
        elif code == 75:
            self.multi = True
        elif code in (54, 55, 0, 4, 90, 91):
            pass
        else:
            self.unsupported_names.add("G%02d" % code)

    # -- 图元 ---------------------------------------------------------
    def _aperture_width(self):
        ap = self.apertures.get(self.aper)
        if ap is None:
            if self.d.missing_aperture == 0:
                self.d.unsupported.add("RS-274D 无孔径定义(用 %.2fmm 近似)" % DEFAULT_APERTURE_MM)
            self.d.missing_aperture += 1
            return DEFAULT_APERTURE_MM
        if ap.get("macro"):
            geo = _macro_geometry(self, ap)
            if geo is None:
                self.d.macro_skipped += 1
                return None
            self.d.macro_approx += 1
            return max(min(geo[0], geo[1]), 1e-3)
        if ap["shape"] == "C":
            return max(ap["w"], 1e-3)
        return max(min(ap["w"], ap["h"]), 1e-3)

    def _operation(self, op, sx, sy, ex, ey):
        if self.d.polarity == "C":
            self.d.clear_skipped += 1
            return
        if self.region:
            if op == 2:
                self._region_move(ex, ey)
            elif op == 1:
                self._region_draw(sx, sy, ex, ey)
            return
        if op == 3:
            self._flash(ex, ey)
        elif op == 1:
            if self.interp == 1:
                if math.hypot(ex - sx, ey - sy) < 1e-9:
                    self._flash(ex, ey)      # 零长度 D01 = 一个点（等同闪光）
                else:
                    w = self._aperture_width()
                    if w is not None:
                        self.d.lines.append([sx, sy, ex, ey, w])
            else:
                self._arc(sx, sy, ex, ey)    # 起终点相同 = 整圆

    def _flash(self, x, y):
        ap = self.apertures.get(self.aper)
        if ap is None:
            self.d.missing_aperture += 1
            if self.d.missing_aperture == 1:
                self.d.unsupported.add("RS-274D 无孔径定义(用 %.2fmm 近似)" % DEFAULT_APERTURE_MM)
            ap = {"shape": "C", "w": DEFAULT_APERTURE_MM, "h": DEFAULT_APERTURE_MM,
                  "hole": 0.0, "rot": 0.0, "n": 0, "macro": False}
        if ap.get("macro"):
            geo = _macro_geometry(self, ap)
            if geo is None:
                self.d.macro_skipped += 1
                return
            self.d.macro_approx += 1
            w, h, s = geo
            self.d.pads.append({"x": x, "y": y, "w": w, "h": h, "s": s,
                                "r": 0.0, "hole": 0.0})
            return
        shape = ap["shape"]
        if shape == "C":
            s, w, h, rot = 1, ap["w"], ap["w"], 0.0
        elif shape == "O":
            s, w, h, rot = 1, ap["w"], ap["h"], ap.get("rot", 0.0)
        elif shape == "P":
            n = ap.get("n") or 8
            if n == 4:
                side = ap["w"] / math.sqrt(2.0)
                s, w, h, rot = 2, side, side, ap.get("rot", 0.0) + 45.0
            elif n == 8:
                s, w, h, rot = 2, ap["w"], ap["w"], ap.get("rot", 0.0)
            else:
                s, w, h, rot = 1, ap["w"], ap["w"], ap.get("rot", 0.0)
        else:                                   # R
            s, w, h, rot = 2, ap["w"], ap["h"], ap.get("rot", 0.0)
        self.d.pads.append({
            "x": x, "y": y, "w": max(w, 1e-3), "h": max(h, 1e-3),
            "s": s, "r": _fmt_num(rot, 2), "hole": ap.get("hole", 0.0),
        })

    def _region_move(self, x, y):
        if self.region_cur is not None and len(self.region_cur) >= 3:
            self.region_contours.append(self.region_cur)
        self.region_cur = [[x, y]]

    def _region_draw(self, sx, sy, ex, ey):
        if self.region_cur is None:
            self.region_cur = [[sx, sy]]
        if self.interp == 1:
            self.region_cur.append([ex, ey])
        else:
            geo = self._arc_geom(sx, sy, ex, ey)
            if geo is None:
                self.region_cur.append([ex, ey])
            else:
                pts = geo[5]
                for p in pts:
                    self.region_cur.append([p[0], p[1]])

    def _close_region(self):
        if not self.region:
            return
        if self.region_cur is not None and len(self.region_cur) >= 3:
            self.region_contours.append(self.region_cur)
        for c in self.region_contours:
            self.d.regions.append(c)
        self.region_cur = None
        self.region_contours = []
        self.region = False

    def _arc_geom(self, sx, sy, ex, ey):
        """返回 (cx, cy, r, a0_deg, a1_deg, pts)；逆时针 a0->a1。"""
        i, j = self.pi, self.pj
        better = None
        if not self.multi:
            self.g74_arcs += 1
            for si in (1.0, -1.0):
                for sj in (1.0, -1.0):
                    cx = sx + si * abs(i)
                    cy = sy + sj * abs(j)
                    r0 = math.hypot(sx - cx, sy - cy)
                    r1 = math.hypot(ex - cx, ey - cy)
                    err = abs(r0 - r1)
                    if better is None or err < better[0]:
                        better = (err, cx, cy, r0)
            if better is None:
                return None
            err, cx, cy, r = better
        else:
            cx = sx + i
            cy = sy + j
            r0 = math.hypot(sx - cx, sy - cy)
            r1 = math.hypot(ex - cx, ey - cy)
            r = (r0 + r1) / 2.0
            if r < 1e-9:
                return None
        r = max(r, 1e-9)
        a0 = math.atan2(sy - cy, sx - cx)
        a1 = math.atan2(ey - cy, ex - cx)
        same = math.hypot(ex - sx, ey - sy) < 1e-7
        ccw = self.interp == 3
        if same:
            sweep = 2.0 * math.pi            # 整圆
        elif ccw:
            sweep = (a1 - a0) % (2.0 * math.pi)
        else:
            sweep = -((a0 - a1) % (2.0 * math.pi))
        if not self.multi and abs(sweep) > math.pi / 2.0 + 1e-3:
            sweep = math.copysign(min(abs(sweep), math.pi / 2.0), sweep)
        # 统一成「逆时针 a0 -> a0+sweep」
        if ccw:
            start, total = a0, sweep
        else:
            start, total = a1, abs(sweep)
        pts = _flatten_arc(cx, cy, r, start, total)
        return (cx, cy, r, math.degrees(start), math.degrees(start + total), pts)

    def _arc(self, sx, sy, ex, ey):
        geo = self._arc_geom(sx, sy, ex, ey)
        if geo is None:
            return
        w = self._aperture_width()
        if w is None:
            return
        cx, cy, r, a0, a1, _pts = geo
        self.d.arcs.append([cx, cy, r, a0, a1, w])


_MACRO_TOKEN_RE = re.compile(r"\s*(?:([0-9]*\.?[0-9]+)|(\$\d+)|([-+xX/()]))")


def _macro_expr(expr, params):
    """求值孔径宏里的算术表达式（支持 + - x / 括号 和 $1..$n 参数）。"""
    tokens = []
    pos = 0
    e = expr.strip()
    while pos < len(e):
        m = _MACRO_TOKEN_RE.match(e, pos)
        if not m:
            return 0.0
        pos = m.end()
        num, var, op = m.group(1), m.group(2), m.group(3)
        if var:
            idx = int(var[1:]) - 1
            tokens.append(("n", params[idx] if 0 <= idx < len(params) else 0.0))
        elif num is not None:
            try:
                tokens.append(("n", float(num)))
            except ValueError:
                tokens.append(("n", 0.0))
        else:
            tokens.append(("o", op.lower()))
    if not tokens:
        return 0.0
    p = [0]

    def peek():
        return tokens[p[0]] if p[0] < len(tokens) else (None, None)

    def take():
        t = peek()
        p[0] += 1
        return t

    def expr_():
        v = term()
        while peek()[0] == "o" and peek()[1] in "+-":
            op = take()[1]
            rhs = term()
            v = v + rhs if op == "+" else v - rhs
        return v

    def term():
        v = factor()
        while peek()[0] == "o" and peek()[1] in "x*/":
            op = take()[1]
            rhs = factor()
            if op == "/":
                v = v / rhs if rhs else 0.0
            else:
                v = v * rhs
        return v

    def factor():
        t = take()
        if t[0] == "n":
            return t[1]
        if t[0] == "o" and t[1] == "-":
            return -factor()
        if t[0] == "o" and t[1] == "+":
            return factor()
        if t[0] == "o" and t[1] == "(":
            v = expr_()
            if peek() == ("o", ")"):
                take()
            return v
        return 0.0

    try:
        return expr_()
    except (IndexError, RecursionError):
        return 0.0


def _rot_pts(pts, deg):
    """绕原点旋转（宏里的角度是逆时针度）。"""
    if not deg:
        return pts
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]


def _macro_geometry(parser, ap):
    """把孔径宏近似成 (w, h, shape)；shape == 1 圆 / 2 矩形。无法求值时 None。"""
    if "bbox" in ap:
        return ap["bbox"]
    layer = parser.d
    prims = parser.macro_defs.get(ap.get("macro_name", ""), [])
    params = ap.get("macro_params", [])
    pts = []
    circle = None
    for prim in prims:
        try:
            code = int(_macro_expr(prim[0], params))
        except (ValueError, IndexError):
            return None
        try:
            args = [_macro_expr(t, params) for t in prim[1:]]
        except Exception:
            return None
        if code == 1 and len(args) >= 4:                # 圆
            dia, cx, cy = abs(args[1]), args[2], args[3]
            circle = (dia, cx, cy)
            pts += [(cx - dia / 2, cy - dia / 2), (cx + dia / 2, cy - dia / 2),
                    (cx + dia / 2, cy + dia / 2), (cx - dia / 2, cy + dia / 2)]
        elif code in (2, 20) and len(args) >= 5:        # 矢量线
            w, x1, y1, x2, y2 = abs(args[1]), args[2], args[3], args[4], args[5]
            rot = args[6] if len(args) > 6 else 0.0
            p = _rot_pts([(x1, y1), (x2, y2)], rot)
            for x, y in p:
                pts += [(x - w / 2, y - w / 2), (x + w / 2, y + w / 2)]
        elif code in (21, 22) and len(args) >= 5:       # 中心线/左下角线
            w, h, cx, cy = abs(args[1]), abs(args[2]), args[3], args[4]
            if code == 22:
                cx, cy = cx + w / 2, cy + h / 2
            rot = args[5] if len(args) > 5 else 0.0
            pts += _rot_pts([(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                             (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)], rot)
        elif code == 4 and len(args) >= 3:              # 轮廓
            n = int(abs(args[1]))
            coords = args[2:2 + 2 * n]
            rot = args[2 + 2 * n] if len(args) > 2 + 2 * n else 0.0
            raw = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
            pts += _rot_pts(raw, rot)
        elif code == 5 and len(args) >= 5:              # 正多边形
            n = int(abs(args[1]))
            cx, cy = args[2], args[3]
            dia = abs(args[4])
            rot = args[5] if len(args) > 5 else 0.0
            r = dia / 2.0
            raw = [(r * math.cos(2 * math.pi * k / max(n, 3)),
                    r * math.sin(2 * math.pi * k / max(n, 3))) for k in range(max(n, 3))]
            pts += [(x + cx, y + cy) for x, y in _rot_pts(raw, rot)]
        elif code == 6 and len(args) >= 9:              # 靶标
            cx, cy, outer = args[1], args[2], abs(args[3])
            cross = abs(args[8]) if len(args) > 8 else 0.0
            d = max(outer, cross) / 2.0
            pts += [(cx - d, cy - d), (cx + d, cy - d), (cx + d, cy + d), (cx - d, cy + d)]
        elif code == 7 and len(args) >= 5:              # 散热盘
            cx, cy, outer = args[1], args[2], abs(args[3])
            r = outer / 2.0
            pts += [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
        else:
            return None                                 # 不认识的宏图元
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    un = layer.unit_mm                        # 宏参数是文件单位（inch/mm）
    w = (max(xs) - min(xs)) * un
    h = (max(ys) - min(ys)) * un
    shape = 1 if (circle is not None and len(prims) == 1) else 2
    geo = (max(w, 1e-3), max(h, 1e-3), shape)
    ap["bbox"] = geo
    return geo


def _flatten_arc(cx, cy, r, start_rad, sweep_rad, step_deg=3.0):
    steps = max(2, int(abs(math.degrees(sweep_rad)) / max(step_deg, 0.5)) + 1)
    out = []
    for k in range(steps + 1):
        a = start_rad + sweep_rad * k / steps
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def _parse_gerber_file(path):
    d = _LayerData(path)
    try:
        return _GerberParser(d).run()
    except Exception as exc:                  # 任何解析异常都不应拖垮整体
        d.warnings.append("Gerber 解析失败(%s)：%s" % (exc, os.path.basename(path)))
        return d


# ---------------------------------------------------------------------------
# 外形拼接
# ---------------------------------------------------------------------------
def _chain_contours(polylines, tol=0.08):
    """把一堆折线按端点首尾相接成闭合轮廓。"""
    polys = [p for p in polylines if p and len(p) >= 2]
    n = len(polys)
    if not n:
        return []
    cell = max(tol * 2.0, 0.05)
    buckets = {}
    for idx, pl in enumerate(polys):
        for pos, pt in ((0, pl[0]), (1, pl[-1])):
            k = (int(math.floor(pt[0] / cell)), int(math.floor(pt[1] / cell)))
            buckets.setdefault(k, []).append((idx, pos))
    used = [False] * n

    def find(pt):
        kx, ky = int(math.floor(pt[0] / cell)), int(math.floor(pt[1] / cell))
        best = None
        bd = tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx, pos in buckets.get((kx + dx, ky + dy), ()):
                    if used[idx]:
                        continue
                    p = polys[idx][0] if pos == 0 else polys[idx][-1]
                    d = math.hypot(p[0] - pt[0], p[1] - pt[1])
                    if d <= bd:
                        bd = d
                        best = (idx, pos)
        return best

    def extend(pts):
        while len(pts) < 500000:
            hit = find(pts[-1])
            if hit is None:
                return False
            idx, pos = hit
            used[idx] = True
            pl = polys[idx]
            if pos == 0:
                pts.extend([tuple(p) for p in pl[1:]])
            else:
                pts.extend([tuple(p) for p in reversed(pl[:-1])])
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= tol:
                return True
        return False

    contours = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        pts = [tuple(p) for p in polys[i]]
        closed = False
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= tol:
            closed = True
        else:
            closed = extend(pts)
            if not closed:                    # 反向再接一次
                rev = list(reversed(pts))
                used2_before = used[:]
                if extend(rev):
                    pts = rev
                    closed = True
                else:
                    used = used2_before
        if not closed:
            continue
        if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0],
                                       pts[0][1] - pts[-1][1]) <= tol:
            pts.pop()
        if len(pts) >= 3:
            contours.append([[p[0], p[1]] for p in pts])
    return contours


def _poly_area(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    j = n - 1
    for i in range(n):
        a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1])
        j = i
    return abs(a) * 0.5


def _polylines_from_layer(res):
    """把一层的线条 + 圆弧展平成折线（用于轮廓拼接）。"""
    polys = []
    for ln in res.lines:
        polys.append([[ln[0], ln[1]], [ln[2], ln[3]]])
    for ar in res.arcs:
        cx, cy, r, a0, a1 = ar[0], ar[1], ar[2], ar[3], ar[4]
        total = math.radians(a1 - a0)
        polys.append([list(p) for p in _flatten_arc(cx, cy, r, math.radians(a0), total)])
    return polys


def _outline_from_result(res):
    contours = [[[p[0], p[1]] for p in c] for c in res.regions]
    chained = _chain_contours(_polylines_from_layer(res))
    contours.extend(chained)
    scored = []
    for c in contours:
        a = _poly_area(c)
        if a > 0.01:
            scored.append((a, c))
    scored.sort(key=lambda t: -t[0])
    if not scored:
        return []
    best = scored[0][0]
    out = []
    for a, c in scored:
        if a >= max(0.5, best * 0.01):
            out.append([[common.rnd(p[0]), common.rnd(p[1])] for p in c])
    return out


# ---------------------------------------------------------------------------
# Excellon 钻孔
# ---------------------------------------------------------------------------
_FMT_CANDIDATES = {
    "mm": [(3, 3), (3, 2), (2, 3), (3, 4), (4, 3), (2, 2)],
    "inch": [(2, 4), (2, 3), (3, 3), (3, 2), (4, 2), (2, 2), (3, 4)],
}
_COORD_TOKEN_RE = re.compile(r"([XYIJ])([+-]?[\d.]+)")


def _sup_from_header(line):
    up = line.upper()
    if ",TZ" in up or " TZ" in up or up.endswith("TZ"):
        return "T"
    if ",LZ" in up or " LZ" in up or up.endswith("LZ"):
        return "L"
    return None


def _fmt_from_header(line):
    m = re.search(r"(\d+)\s*[.:]\s*(\d+)", line)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= 6 and 0 < b <= 6:
            return (a, b)
    return None


def _parse_excellon(path):
    info = {"path": path, "hits": [], "tools": {}, "units": "inch",
            "fmt": None, "sup": None, "warns": [], "binary": False}
    text, raw = _read_text(path)
    if _looks_binary(raw):
        info["binary"] = True
        info["warns"].append("二进制钻孔文件（旧版 Protel/PCAD），未解析：%s"
                             % os.path.basename(path))
        return info
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = False
    cur_tool = None
    tool_down = False
    last_x = last_y = None
    repeat_warned = False

    def emit(x, y, rep=None, extra_only=False):
        info["hits"].append({"x": x, "y": y, "tool": cur_tool,
                             "rep": rep, "extra_only": extra_only})

    for raw_line in lines:
        s = raw_line.strip()
        if not s:
            continue
        if s.startswith(";"):
            m = re.search(r"FILE_FORMAT\s*=\s*(\d+)\s*[:.]\s*(\d+)", s, re.I)
            if m:
                info["fmt"] = (int(m.group(1)), int(m.group(2)))
            m = re.search(r"FORMAT=\{[^}]*?(\d)\s*[:.]\s*(\d)", s, re.I)
            if m and not info["fmt"]:
                info["fmt"] = (int(m.group(1)), int(m.group(2)))
            if not repeat_warned and re.search(r"\d+\.\d+\s*/\s*(absolute|ab)", s, re.I):
                pass
            continue
        up = s.upper()
        if up.startswith("M48"):
            header = True
            continue
        if up.startswith(("M30", "M00", "M95")):
            break
        if up.startswith("FMAT"):
            continue
        if up.startswith("INCH"):
            info["units"] = "inch"
            info["sup"] = _sup_from_header(up) or info["sup"]
            if info["fmt"] is None:
                info["fmt"] = _fmt_from_header(up)
            header = True
            continue
        if up.startswith("METRIC"):
            info["units"] = "mm"
            info["sup"] = _sup_from_header(up) or info["sup"]
            if info["fmt"] is None:
                info["fmt"] = _fmt_from_header(up)
            header = True
            continue
        if up.startswith("M71"):
            info["units"] = "mm"
            continue
        if up.startswith("M72"):
            info["units"] = "inch"
            continue
        if s == "%":
            if header:
                header = False
            elif info["hits"]:               # 老式「重复上一孔」
                last = info["hits"][-1]
                emit(last["x"], last["y"])
            continue
        # 有些老文件不用 % 结束头部：遇到裸刀号/G05/G90/坐标行时头部自动结束
        if header and (re.match(r"T\d+\s*$", up) or up.startswith(("G05", "G90", "G91"))
                       or up.startswith(("X", "Y"))):
            header = False
        if header:
            m = re.match(r"T(\d+)(?:[FSR]\d+)*C([\d.]+)", up)
            if m:
                info["tools"][int(m.group(1))] = float(m.group(2))
                continue
            # 头部其余指令（ICI/VER/ATC/…）忽略
            continue
        # ---- body ----
        m = re.match(r"T(\d+)\s*$", up)
        if m:
            cur_tool = int(m.group(1))
            tool_down = False
            continue
        if up.startswith(("M15", "G32")):
            tool_down = True
            continue
        if up.startswith(("M16", "M17", "G33")):
            tool_down = False
            continue
        if up.startswith("G05"):
            continue
        pairs = _coord_pairs(s)
        if not pairs and not up.startswith("R"):
            continue
        # 重复孔：R#X#Y# 在上一个孔基础上按增量重复
        m = re.match(r"R(\d+)", up)
        if m and last_x is not None:
            n = int(m.group(1))
            xs = [v for l, v in _COORD_TOKEN_RE.findall(up) if l == "X"]
            ys = [v for l, v in _COORD_TOKEN_RE.findall(up) if l == "Y"]
            dx = xs[0] if xs else "0"
            dy = ys[0] if ys else "0"
            emit(last_x, last_y, rep=(n, dx, dy), extra_only=True)
            if not repeat_warned:
                info["warns"].append("钻孔文件里的 R 重复孔已按增量展开：%s"
                                     % os.path.basename(path))
                repeat_warned = True
            continue
        if not pairs:
            continue
        xs = [v for l, v in _COORD_TOKEN_RE.findall(up) if l == "X"]
        ys = [v for l, v in _COORD_TOKEN_RE.findall(up) if l == "Y"]
        x1 = xs[0] if xs else last_x
        y1 = ys[0] if ys else last_y
        # 槽孔 G85：同一条指令里两组坐标
        if "G85" in up and len(xs) >= 2 and len(ys) >= 2:
            x2, y2 = xs[1], ys[1]
            emit(x1, y1)
            emit(x2, y2)
            last_x, last_y = x2, y2
            continue
        if x1 is None or y1 is None:
            continue
        if up.startswith("G00"):             # 快速定位，不下刀
            last_x, last_y = x1, y1
            continue
        if up.startswith(("G01", "G02", "G03")) or tool_down:
            # 铣槽/圆弧走线：用两端点近似（中间路径不展开）
            if last_x is not None and (last_x, last_y) != (x1, y1):
                emit(last_x, last_y)
            emit(x1, y1)
            last_x, last_y = x1, y1
            continue
        emit(x1, y1)
        last_x, last_y = x1, y1
    return info


def _coord_pairs(s):
    """把一行里的坐标拆成 [(x?, y?), ...]（G85 会有两组）。"""
    out = []
    cur = {}
    for letter, val in _COORD_TOKEN_RE.findall(s.upper()):
        if letter in ("X", "Y"):
            if letter in cur:
                out.append(cur)
                cur = {}
            cur[letter] = val
        if cur.get("X") and cur.get("Y"):
            out.append(cur)
            cur = {}
    if cur:
        out.append(cur)
    return out


def _conv_excellon(tok, fmt, sup, unit_mm):
    if tok is None:
        return 0.0
    tok = tok.strip()
    neg = tok.startswith("-")
    t = tok.lstrip("+-")
    if "." in t:
        try:
            v = float(t) * unit_mm
        except ValueError:
            v = 0.0
        return -v if neg else v
    try:
        n = int(t or "0")
    except ValueError:
        return 0.0
    xi, xd = fmt
    total = xi + xd
    if sup == "T":
        if len(t) < total:
            n *= 10 ** (total - len(t))
    v = n / float(10 ** xd) * unit_mm
    return -v if neg else v


def _expand_hits(info, fmt, sup, unit_mm):
    """展开 R 重复孔 -> (coords, tool_ids)。"""
    coords = []
    tools = []
    for h in info["hits"]:
        bx = _conv_excellon(h["x"], fmt, sup, unit_mm)
        by = _conv_excellon(h["y"], fmt, sup, unit_mm)
        rep = h.get("rep")
        if rep:
            n, dx, dy = rep
            ddx = _conv_excellon(dx, fmt, sup, unit_mm)
            ddy = _conv_excellon(dy, fmt, sup, unit_mm)
            for k in range(1, n + 1):
                coords.append((bx + k * ddx, by + k * ddy))
                tools.append(h["tool"])
        else:
            coords.append((bx, by))
            tools.append(h["tool"])
    return coords, tools


def _score_coords(coords, board_box, pad_index):
    """坐标解释是否合理的打分：在板内 + 靠近铜箔焊盘 + 分布范围。"""
    n = len(coords)
    if not n:
        return -1e9, {}
    bx0, by0, bx1, by1 = board_box
    m = max(1.0, 0.02 * max(bx1 - bx0, by1 - by0))
    inside = 0
    near = 0
    xs = []
    ys = []
    for x, y in coords:
        xs.append(x)
        ys.append(y)
        if bx0 - m <= x <= bx1 + m and by0 - m <= y <= by1 + m:
            inside += 1
        if pad_index is not None:
            d = pad_index.nearest(x, y, 0.6)
            if d is not None:
                near += 1
    frac_in = inside / float(n)
    frac_near = near / float(n) if pad_index is not None else 0.0
    diag_hits = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    diag_board = max(1e-6, math.hypot(bx1 - bx0, by1 - by0))
    spread = min(1.0, diag_hits / diag_board)
    score = frac_in * 2.0 + frac_near * 3.0 + spread * 0.5
    return score, {"inside": frac_in, "near": frac_near, "spread": spread}


def _interpret_drill(info, board_box, pad_index):
    """在多种 (格式, 零抑制) 里挑一个能对上板子/焊盘的解释。"""
    if not info["hits"]:
        return [], "无钻孔数据", []
    unit_mm = 1.0 if info["units"] == "mm" else MM_PER_INCH
    header_fmt = info.get("fmt")
    header_sup = info.get("sup")
    cands = []
    if header_fmt:
        cands.append((header_fmt, header_sup or "L", "头部声明"))
        cands.append((header_fmt, "L" if (header_sup or "L") == "T" else "T",
                      "头部声明(换抑制)"))
    for f in _FMT_CANDIDATES["mm" if info["units"] == "mm" else "inch"]:
        for sup in ("L", "T"):
            if header_fmt and f == header_fmt and (
                    sup == (header_sup or "L")):
                continue
            cands.append((f, sup, "自动推断"))
    best = None
    for fmt, sup, why in cands:
        coords, tools = _expand_hits(info, fmt, sup, unit_mm)
        score, detail = _score_coords(coords, board_box, pad_index)
        tag = "%s %d.%d %s" % ("mm" if info["units"] == "mm" else "inch",
                               fmt[0], fmt[1], sup)
        if best is None or score > best[0]:
            best = (score, coords, tag, detail, tools)
    if best is None:
        return [], "无法解析钻孔坐标", []
    return best[1], best[2], best[4]


# ---------------------------------------------------------------------------
# 空间索引（焊盘/闪光位置）
# ---------------------------------------------------------------------------
class _PadIndex(object):
    def __init__(self, pads, cell=1.0, sides=None):
        self.cell = cell
        self.grid = {}
        self.n = 0
        for p in pads:
            if sides is not None and p.get("l") not in sides:
                continue
            self._add(p["x"], p["y"], p)
            self.n += 1

    def _add(self, x, y, extra):
        k = (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))
        self.grid.setdefault(k, []).append((x, y, extra))

    def nearest(self, x, y, maxd=1.0):
        if not self.grid:
            return None
        c = self.cell
        kx, ky = int(math.floor(x / c)), int(math.floor(y / c))
        reach = max(1, int(math.ceil(maxd / c)))
        best = maxd
        found = None
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for px, py, extra in self.grid.get((kx + dx, ky + dy), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= best:
                        best = d
                        found = (d, px, py)
        return found


# ---------------------------------------------------------------------------
# 坐标文件（Centroid / Pick & Place）
# ---------------------------------------------------------------------------
_D_RE = [re.compile(p) for p in (
    r"^(designator|designators|refdes|refdesx|reference|ref|referencex|part|parts|"
    r"component|componentx|comp|refs|位号|器件位号|元件位号)$",)]
# 列名匹配用「包含」而不是全等：Designator[mm] / Mid X(mm) / 中间X坐标 都能认
_X_KEYS = [("MID", None), ("POS", None), ("CENTER", None), ("中间", "X"),
           ("中心", "X"), ("REF", "X"), ("PAD", "X"), ("CTR", None)]
_Y_KEYS = [("MID", None), ("POS", None), ("CENTER", None), ("中间", "Y"),
           ("中心", "Y"), ("REF", "Y"), ("PAD", "Y"), ("CTR", None)]
_X_EXACT = ("x", "x坐标", "坐标x", "xcoord", "xpos", "xmm", "xmil", "xposmm", "xposition")
_Y_EXACT = ("y", "y坐标", "坐标y", "ycoord", "ypos", "ymm", "ymil", "yposmm", "yposition")
_LAYER_RE = [re.compile(p) for p in (
    r"^(layer|side|tb|topbottom|layerx|板层|层|面|顶底层)$",)]
_ROT_RE = [re.compile(p) for p in (
    r"^(rotation|rot|angle|orient|orientation|rotdeg|rotationdeg|旋转|角度|旋转角度)$",)]
_COMMENT_RE = [re.compile(p) for p in (
    r"^(comment|comments|val|value|型号|值|partnumber|partno|description|desc|"
    r"commentx|元件值)$",)]
_FP_RE = [re.compile(p) for p in (
    r"^(footprint|footprintref|package|封装|pattern|pkg|封装名|封装规格)$",)]
_MPN_RE = [re.compile(p) for p in (
    r"^(lcscpart|lcscpartno|lcsc|mpn|partnumber|manufacturerpartnumber|manufacturerpart|"
    r"料号|厂商料号|立创料号)$",)]


def _norm_header(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.strip().strip('"').strip("'").lower())


def _rank_col(name, res):
    for i, rx in enumerate(res):
        if rx.match(name):
            return i
    return None


def _rank_axis_col(name, keys, exact, axis_char):
    """X/Y 列名打分：数字越小越优先；None 表示不是坐标列。

    Mid X / Mid Y 这种「轴名在前」的列必须靠结尾的 x/y 区分，
    否则 Mid X 会被同时认成 Y 列。
    """
    low = name.lower()
    if low in exact:
        return len(keys) + 1
    tail = re.compile(axis_char + r"(mm|mil|coord|pos|position)?$")
    for i, (key, axis) in enumerate(keys):
        if key.lower() not in low:
            continue
        if axis is not None:
            if axis.lower() in low:
                return i
        elif tail.search(low):
            return i
    return None


def _rank_cols(names, res):
    """返回 [(rank, col_index)]。"""
    out = []
    for idx, n in enumerate(names):
        r = _rank_col(n, res)
        if r is not None:
            out.append((r, idx))
    return sorted(out)


def _rank_designator(name):
    """位号列：先全等，再包含（Designator[mm] / 器件位号 / RefDes…）。"""
    r = _rank_col(name, _D_RE)
    if r is not None:
        return r
    for i, key in enumerate(("designator", "位号", "refdes", "reference")):
        if key in name and "x" not in name.replace("refdes", ""):
            return 10 + i
    return None


def _map_columns(cells):
    names = [_norm_header(c) for c in cells]
    cols = {}
    scored_d = []
    for idx, n in enumerate(names):
        r = _rank_designator(n)
        if r is not None:
            scored_d.append((r, idx))
    if not scored_d:
        return {}
    cols["d"] = sorted(scored_d)[0][1]
    for key, res in (("layer", _LAYER_RE), ("rot", _ROT_RE),
                     ("comment", _COMMENT_RE), ("fp", _FP_RE), ("mpn", _MPN_RE)):
        r = _rank_cols(names, res)
        if r:
            cols[key] = r[0][1]
    for axis, keys, exact in (("x", _X_KEYS, _X_EXACT), ("y", _Y_KEYS, _Y_EXACT)):
        scored = []
        for idx, n in enumerate(names):
            r = _rank_axis_col(n, keys, exact, axis)
            if r is not None:
                scored.append((r, idx))
        if scored:
            cols[axis] = sorted(scored)[0][1]
    if "x" not in cols or "y" not in cols:
        return {}
    return cols


def _split_line(line, delim):
    if delim == " ":
        return line.split()
    return line.split(delim)


def _detect_table(lines):
    """返回 (delim, header_index, cols, rows)。容忍表格前有若干行说明。"""
    best = None
    for delim in (",", ";", "\t", "|"):
        for i, line in enumerate(lines[:80]):
            s = line.strip()
            if not s:
                continue
            s2 = s.lstrip("#").strip()
            if not s2:
                continue
            cells = _split_line(s2, delim)
            if len(cells) < 3:
                continue
            cols = _map_columns(cells)
            if not cols:
                continue
            rows = _read_rows(lines[i + 1:], delim, cols)
            if not rows:
                continue
            cand = (len(rows) * 10 + len(cols), delim, i, cols, rows)
            if best is None or cand[0] > best[0]:
                best = cand
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _read_rows(rest, delim, cols):
    rows = []
    try:
        reader = csv.reader(io.StringIO("\n".join(rest)), delimiter=delim,
                            skipinitialspace=True)
    except Exception:
        return rows
    for row in reader:
        if not row or len(row) <= max(cols.values()):
            continue
        d = (row[cols["d"]] or "").strip().strip('"').strip("'")
        if not d:
            continue
        if _norm_header(d) in ("designator", "ref", "refdes", "位号", "器件位号"):
            continue
        if d.upper() in ("HEAD", "END"):
            pass
        vals = {}
        ok = True
        for key in ("x", "y"):
            v = (row[cols[key]] or "").strip().strip('"')
            num = _parse_len(v)
            if num is None:
                ok = False
                break
            vals[key] = num
        if not ok:
            continue
        for key in ("layer", "rot", "comment", "fp", "mpn"):
            if key in cols and cols[key] < len(row):
                vals[key] = (row[cols[key]] or "").strip().strip('"').strip("'")
        vals["d"] = d
        rows.append(vals)
    return rows


_UNIT_SUFFIX_RE = re.compile(r"^\s*([+-]?[\d.]+)\s*(mil|mils|mm|millimeter|millimetre|"
                             r"in|inch|inches|\"|um|µm)\s*$", re.I)


def _parse_len(s):
    """返回 (mm 值, 单位) ；单位可能是 'mm'/'mil' 或 None（待定）。"""
    if s is None:
        return None
    s = s.strip()
    if not s or s in ("-", "--", "N/A", "NA", "n/a"):
        return None
    m = _UNIT_SUFFIX_RE.match(s)
    if m:
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        u = m.group(2).lower()
        if u.startswith("mil"):
            return v * 0.0254, "mil"
        if u in ("in", "inch", "inches", '"'):
            return v * MM_PER_INCH, "mm"
        if u in ("um", "µm"):
            return v / 1000.0, "mm"
        return v, "mm"
    try:
        return float(s), None
    except ValueError:
        return None


def _parse_centroid(path):
    text, _raw = _read_text(path)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    table = _detect_table(lines)
    if table is None:
        return None
    _delim, hidx, _cols, rows = table
    # 单位提示：表头 / 表格前的注释
    hint = None
    head_text = ("\n".join(lines[:min(len(lines), hidx + 4)])).lower()
    if re.search(r"\bmils?\b", head_text):
        hint = "mil"
    elif re.search(r"\bmm\b|\b毫米\b", head_text):
        hint = "mm"
    return {"path": path, "rows": rows, "hint": hint,
            "preamble": [" ".join(lines[i].strip().split()) for i in range(min(hidx, 5))]}


def _side_from_str(s):
    if not s:
        return None
    n = _norm_header(s)
    if n in ("t", "top", "toplayer", "front", "1", "顶层", "上面", "正"):
        return "top"
    if n in ("b", "bottom", "bot", "bottomlayer", "back", "2", "底层", "下面", "反"):
        return "bottom"
    if n in ("topbottom", "tb"):
        return None
    if "top" in n or "顶层" in n:
        return "top"
    if "bot" in n or "bottom" in n or "底层" in n:
        return "bottom"
    return None


def _parse_rot(s):
    if not s:
        return 0.0
    m = re.search(r"([+-]?[\d.]+)", s)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _scan_files(root):
    files = []
    for p in common.iter_files(root):
        base = os.path.basename(p)
        if base.startswith("."):
            continue
        try:
            if os.path.getsize(p) > 300 * 1024 * 1024:
                continue
        except OSError:
            continue
        files.append(p)
    files.sort()
    return files


def _head_text(path, n=8192):
    try:
        with open(path, "rb") as f:
            return _decode(f.read(n))
    except OSError:
        return ""


def _kind_for_file(path):
    """综合文件头里的 %TF.FileFunction% / %LN% + 扩展名 + 文件名判层（X2 优先）。"""
    head = _head_text(path)
    m = _FF_RE.search(head)
    if m:
        k = _kind_from_filefunction(m.group(1))
        if k:
            return k
    kind = _kind_from_name(path)
    if kind not in (None, "unknown"):
        return kind
    m = _LN_RE.search(head)
    if m:
        k = _kind_from_layername(m.group(1))
        if k:
            return k
    return kind or "unknown"


def _bbox_of_points(pts):
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _copper_bbox(tracks, pads, arcs, polys):
    pts = []
    for t in tracks:
        pts.append((t[0], t[1]))
        pts.append((t[2], t[3]))
    for p in pads:
        pts.append((p["x"] - p["w"] / 2.0, p["y"] - p["h"] / 2.0))
        pts.append((p["x"] + p["w"] / 2.0, p["y"] + p["h"] / 2.0))
    for a in arcs:
        cx, cy, r = a[0], a[1], a[2]
        pts.append((cx - r, cy - r))
        pts.append((cx + r, cy + r))
    for pg in polys:
        for c in pg.get("contours", []):
            for p in c:
                pts.append((p[0], p[1]))
    return _bbox_of_points(pts)


_TITLE_SUFFIX_RE = re.compile(
    r"[._ -]+(frontsilk|backsilk|front_silk|back_silk|topsilk|botsilk|top_silk|"
    r"bottom_silk|topsilkscreen|bottomsilkscreen|silkscreen|silk|"
    r"toplayer|bottomlayer|top_layer|bottom_layer|botlayer|"
    r"topoverlay|bottomoverlay|overlay|copper|component|components|"
    r"f_cu|b_cu|f_silks|b_silks|edge_cuts|edgecuts|profile|outline|"
    r"mechanical|mech|assembly|soldermask|mask|paste|legend|fab|"
    r"gtl|gbl|gto|gbo|gko|gm1|gm2|gts|gbs|drill)+$", re.I)


def _clean_title(st):
    """去掉板名后面粘着的层名（LED.frontsilk -> LED）。"""
    prev = None
    while prev != st:
        prev = st
        st = _TITLE_SUFFIX_RE.sub("", st).strip(" ._-")
    return st or prev


def _title_for(root, layer_paths, title_opt):
    if title_opt:
        return title_opt
    stems = []
    for p in layer_paths:
        st = os.path.splitext(os.path.basename(p))[0]
        st = re.sub(r"[._-]+$", "", st.strip())
        if st:
            stems.append(st)
    if stems:
        cp = os.path.commonprefix(stems).strip(" _-.")
        cp = _clean_title(cp)
        low = cp.lower()
        if len(cp) >= 3 and low not in ("gerber", "board", "pcb", "top", "bottom",
                                        "front", "back", "layer", "copper"):
            return cp
    if os.path.isfile(root):
        base = os.path.splitext(os.path.basename(root))[0]
    else:
        base = os.path.basename(os.path.abspath(root).rstrip(os.sep))
    if base and base not in (".", "/"):
        return _clean_title(base)
    return "PCB"


def _component_bb_from_pads(comps, top_pads, bottom_pads):
    """Gerber 没有元件-焊盘归属，只能按「离元件中心最近的铜箔焊盘」估一个框；
    顺便把找到的焊盘回挂到元件（pad["c"]），这样网页能显示焊盘数、点选框也更准。"""
    idx_top = _PadIndex(top_pads, cell=1.5)
    idx_bottom = _PadIndex(bottom_pads, cell=1.5)
    for ci, c in enumerate(comps):
        cx, cy = c["x"], c["y"]

        def fallback():
            # 网页的封装预览会直接读 c.bb，所以每个元件都必须有一个框
            c["bb"] = [common.rnd(cx - 1.0), common.rnd(cy - 0.6),
                       common.rnd(cx + 1.0), common.rnd(cy + 0.6)]

        idx = idx_bottom if c.get("side") == "bottom" else idx_top
        if not idx.grid:
            idx = idx_top if idx is idx_bottom else idx_bottom
        if not idx.grid:
            fallback()
            continue
        d = idx.nearest(cx, cy, 30.0)
        if d is None:
            fallback()
            continue
        d1 = max(d[0], 0.4)
        radius = min(6.0, max(1.2, d1 * 2.5))
        x0 = y0 = 1e9
        x1 = y1 = -1e9
        hit = 0
        owned = []
        cidx = idx.cell
        kx, ky = int(math.floor(cx / cidx)), int(math.floor(cy / cidx))
        reach = int(math.ceil(radius / cidx))
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for px, py, extra in idx.grid.get((kx + dx, ky + dy), ()):
                    if math.hypot(px - cx, py - cy) > radius:
                        continue
                    hit += 1
                    if hit > 80:
                        break
                    x0 = min(x0, px)
                    y0 = min(y0, py)
                    x1 = max(x1, px)
                    y1 = max(y1, py)
                    if isinstance(extra, dict) and \
                            extra.get("c", common.NO_COMPONENT) == common.NO_COMPONENT:
                        owned.append(extra)
        if hit and x1 > x0 and y1 > y0 and (x1 - x0) < 25.0 and (y1 - y0) < 25.0:
            pad = 0.25
            c["bb"] = [common.rnd(x0 - pad), common.rnd(y0 - pad),
                       common.rnd(x1 + pad), common.rnd(y1 + pad)]
            # attribute the nearby copper flashes to this component (best effort;
            # a pad belongs to at most one component)
            if owned:
                for pd in owned:
                    pd["c"] = ci
                    pd.setdefault("p", "")
                c["np"] = len(owned)
        else:
            fallback()


def parse(root, out_dir=None, centroid=None, title=None, spur_trim=True, **opts):
    root = os.path.abspath(root)
    data = common.new_data("PCB")

    def warn(msg):
        common.add_warning(data, msg)

    files = _scan_files(root)
    layer_files, drill_files, cent_files = [], [], []
    for p in files:
        ext = os.path.splitext(p.lower())[1]
        if ext in _DRILL_EXTS:
            drill_files.append(p)
            continue
        if ext in _CENTROID_EXTS:
            cent_files.append(p)
            continue
        if ext in _SKIP_EXTS:
            continue
        if ext == ".txt":
            head = _head_text(p)
            if _looks_like_excellon(head):
                drill_files.append(p)
            else:
                cent_files.append(p)
            continue
        if ext in _GERBER_EXTS:
            layer_files.append(p)
        else:
            head = _head_text(p, 2048)
            if _looks_like_gerber_head(head):
                layer_files.append(p)
            elif _looks_like_excellon(head):
                drill_files.append(p)

    if not layer_files:
        warn("这个目录里没有找到 Gerber 文件（.GTL/.GBL/.GTO/.GBR…）")
        data["title"] = title or _title_for(root, [], title)
        return data

    # ---- 解析各层 ------------------------------------------------------
    parsed = []          # (result, kind)
    outline_cands = []   # (priority, result)
    ignored = []
    raw_unknown = []
    for p in layer_files:
        kind = _kind_for_file(p)
        if kind in ("mask_top", "mask_bottom", "paste_top", "paste_bottom",
                    "copper_inner"):
            ignored.append((os.path.basename(p), kind))
            continue
        res = _parse_gerber_file(p)
        for w in res.warnings:
            warn(w)
        # 文件里发现的 X2 属性可能修正判层
        k2 = _kind_from_filefunction(res.file_function)
        if k2:
            kind = k2
        elif kind == "unknown" and res.layer_name:
            k3 = _kind_from_layername(res.layer_name)
            if k3:
                kind = k3
        if kind == "unknown":
            raw_unknown.append(res)
        elif kind in ("outline",):
            outline_cands.append((3, res))
        elif kind == "mech":
            outline_cands.append((1, res))
        else:
            parsed.append((res, kind))

    # 未知层：如果顶/底层铜缺失，就把最大的未知层当铜箔用
    have = set(k for _r, k in parsed)
    for res in raw_unknown:
        target = None
        if "copper_top" not in have:
            target = "copper_top"
        elif "copper_bottom" not in have:
            target = "copper_bottom"
        if target:
            parsed.append((res, target))
            have.add(target)
            warn("有文件无法从名字判断层用途，按%s渲染：%s"
                 % ("顶层铜" if target == "copper_top" else "底层铜",
                    os.path.basename(res.path)))
        else:
            ignored.append((os.path.basename(res.path), "unknown"))

    tracks, arcs, pads, silk, silkarcs = [], [], [], [], []
    polys_by_layer = {"TOP": [], "BOTTOM": []}
    top_flashes, bottom_flashes = [], []

    for res, kind in parsed:
        if kind == "copper_top":
            layer = common.COPPER_TOP
            pname = "TOP"
            assign_flashes = top_flashes
        elif kind == "copper_bottom":
            layer = common.COPPER_BOTTOM
            pname = "BOTTOM"
            assign_flashes = bottom_flashes
        elif kind == "silk_top":
            layer = common.SILK_TOP
            pname = None
            assign_flashes = None
        elif kind == "silk_bottom":
            layer = common.SILK_BOTTOM
            pname = None
            assign_flashes = None
        else:
            continue
        pl = common.COPPER_TOP if layer == common.COPPER_TOP else \
            (common.COPPER_BOTTOM if layer == common.COPPER_BOTTOM else None)
        for ln in res.lines:
            if pl is not None:
                tracks.append([common.rnd(ln[0]), common.rnd(ln[1]),
                               common.rnd(ln[2]), common.rnd(ln[3]),
                               common.rnd(ln[4]), pl])
            else:
                silk.append([common.rnd(ln[0]), common.rnd(ln[1]),
                             common.rnd(ln[2]), common.rnd(ln[3]),
                             common.rnd(ln[4]), layer, None])
        for ar in res.arcs:
            item = [common.rnd(ar[0]), common.rnd(ar[1]), common.rnd(ar[2]),
                    _fmt_num(ar[3], 2), _fmt_num(ar[4], 2),
                    common.rnd(ar[5])]
            if pl is not None:
                arcs.append(item + [pl])
            else:
                silkarcs.append(item + [layer, None])
        for pd in res.pads:
            if pl is not None:
                pad = {"x": common.rnd(pd["x"]), "y": common.rnd(pd["y"]),
                       "w": common.rnd(pd["w"]), "h": common.rnd(pd["h"]),
                       "s": pd["s"], "r": pd["r"], "l": pname,
                       "c": NO_COMPONENT, "p": "",
                       "hole": common.rnd(pd.get("hole", 0.0))}
                pads.append(pad)
                if assign_flashes is not None:
                    assign_flashes.append(pad)
            else:
                # 丝印上的闪光：用一个小十字近似（网页里丝印线宽固定 0.13mm）
                d = max(pd["w"], pd["h"], 0.2) * 0.5
                silk.append([common.rnd(pd["x"] - d), common.rnd(pd["y"]),
                             common.rnd(pd["x"] + d), common.rnd(pd["y"]),
                             0.13, layer, None])
                silk.append([common.rnd(pd["x"]), common.rnd(pd["y"] - d),
                             common.rnd(pd["x"]), common.rnd(pd["y"] + d),
                             0.13, layer, None])
        if pl is not None:
            for c in res.regions:
                if len(c) >= 3:
                    polys_by_layer[pname].append(
                        [[common.rnd(x), common.rnd(y)] for x, y in c])
        else:
            for c in res.regions:             # 丝印区域：画边线
                for i in range(len(c)):
                    a = c[i]
                    b = c[(i + 1) % len(c)]
                    silk.append([common.rnd(a[0]), common.rnd(a[1]),
                                 common.rnd(b[0]), common.rnd(b[1]),
                                 0.15, layer, None])

    data["tracks"] = tracks
    data["arcs"] = arcs
    data["pads"] = pads
    data["silk"] = silk
    data["silkarcs"] = silkarcs
    data["polygons"] = [{"layer": k, "contours": v}
                        for k, v in polys_by_layer.items() if v]

    # ---- 板框 ---------------------------------------------------------
    outline = []
    chosen_name = ""
    if outline_cands:
        best = None
        for prio, res in outline_cands:
            cs = _outline_from_result(res)
            if not cs:
                continue
            area = max(_poly_area(c) for c in cs)
            score = area * (1.0 + 0.05 * prio)
            if best is None or score > best[0]:
                best = (score, cs, res)
        if best is not None:
            outline = best[1]
            chosen_name = os.path.basename(best[2].path)
    if outline and spur_trim:
        trimmed, removed = common.trim_outline_slivers(outline)
        if removed:
            outline = trimmed
            warn("板框里去掉了 %d 个细长毛刺顶点（<1mm 宽的贴边缺口/切割线）" % removed)
    copper_box = _copper_bbox(tracks, pads, arcs, data["polygons"])
    silk_pts = [(t[0], t[1]) for t in silk] + [(t[2], t[3]) for t in silk]
    silk_box = _bbox_of_points(silk_pts)
    if not outline:
        box = copper_box or silk_box
        if box:
            x0, y0, x1, y1 = box
            outline = [[[common.rnd(x0), common.rnd(y0)],
                        [common.rnd(x1), common.rnd(y0)],
                        [common.rnd(x1), common.rnd(y1)],
                        [common.rnd(x0), common.rnd(y1)]]]
            if copper_box:
                warn("没有找到外形/板框层（.GKO/.GM1/Profile），用铜箔包围盒代替")
            else:
                warn("只有丝印层（没有铜箔/外形层），用丝印包围盒当板框")
        else:
            warn("没有找到外形层，也没有铜箔/丝印图元")
    data["outline"] = outline
    board_box = _bbox_of_points([p for c in outline for p in c]) if outline else \
        (copper_box or silk_box or [0.0, 0.0, 10.0, 10.0])

    # ---- 钻孔 ---------------------------------------------------------
    drill_info_lines = []
    drill_pads = []
    if drill_files:
        for dp in drill_files:
            info = _parse_excellon(dp)
            for w in info["warns"]:
                warn(w)
            if info["binary"]:
                drill_info_lines.append({"file": os.path.basename(dp), "binary": True})
                continue
            pad_index = _PadIndex(top_flashes + bottom_flashes, cell=1.0)
            coords, how, hit_tools = _interpret_drill(info, board_box, pad_index)
            if not coords:
                warn("钻孔文件没有解析出孔位：%s" % os.path.basename(dp))
                continue
            unit = 1.0 if info["units"] == "mm" else MM_PER_INCH
            tool_mm = {}
            for k, v in info["tools"].items():
                tool_mm[k] = v * unit
            seen_holes = set()
            for (x, y), tool in zip(coords, hit_tools):
                key = (round(x, 3), round(y, 3), tool)
                if key in seen_holes:
                    continue
                seen_holes.add(key)
                d = tool_mm.get(tool)
                if not d or d <= 0:
                    d = DEFAULT_DRILL_MM
                drill_pads.append({"x": common.rnd(x), "y": common.rnd(y),
                                   "w": common.rnd(d), "h": common.rnd(d),
                                   "s": 1, "r": 0.0, "l": "MULTILAYER",
                                   "c": NO_COMPONENT, "p": "",
                                   "hole": common.rnd(d)})
            drill_info_lines.append({"file": os.path.basename(dp),
                                     "format": how,
                                     "hits": len(coords),
                                     "tools": len(info["tools"])})
        data["pads"].extend(drill_pads)
    else:
        if any(k in ("copper_top", "copper_bottom") for _r, k in parsed):
            warn("没有钻孔文件（.DRL/.XLN），图上不会有通孔/过孔")

    # ---- 坐标文件 -----------------------------------------------------
    comps = []
    cent_info = None
    cent_choice = None
    if centroid:
        cand = os.path.abspath(centroid)
        if os.path.isfile(cand):
            cent_choice = _parse_centroid(cand)
            if cent_choice is None:
                warn("指定的坐标文件解析不出表格（没有「位号 + X/Y」表头）：%s" % centroid)
        else:
            warn("指定的坐标文件不存在：%s" % centroid)
    if cent_choice is None and cent_files:
        best = None
        for cf in cent_files:
            t = _parse_centroid(cf)
            if t is None or not t["rows"]:
                continue
            n = _norm_name(os.path.basename(cf))
            bonus = 0
            for k in ("centroid", "pick", "place", "pnp", "cpl", "pos", "xy", "坐标"):
                if k in n:
                    bonus = 3
                    break
            score = len(t["rows"]) * 10 + bonus * 100
            if best is None or score > best[0]:
                best = (score, t)
        if best:
            cent_choice = best[1]

    if cent_choice is not None and cent_choice["rows"]:
        comps, cent_info = _build_components(
            cent_choice, board_box, top_flashes, bottom_flashes, warn)
        _component_bb_from_pads(comps, top_flashes, bottom_flashes)
    elif cent_files or centroid:
        names = ([os.path.basename(centroid)] if centroid else []) + \
                [os.path.basename(p) for p in cent_files]
        names = list(dict.fromkeys(names))
        looks_like = [n for n in names
                      if any(k in _norm_name(n) for k in
                             ("centroid", "pick", "place", "pos", "xy", "cpl",
                              "pnp", "坐标", "贴片"))]
        if looks_like:
            warn("找到了坐标文件但没能读出位号/坐标（表头不认识？）：%s"
                 % ", ".join(looks_like)[:120])
        else:
            warn("没有坐标文件（CSV/POS），图上只有板子和铜箔，没有位号；"
                 "用 --centroid 坐标文件.csv 指定，或把坐标文件放到同一目录"
                 "（这些 txt/csv 里没找到「位号 + X/Y」表头：%s）"
                 % ", ".join(names)[:120])
    else:
        warn("没有坐标文件（CSV/POS），图上只有板子和铜箔，没有位号；"
             "用 --centroid 坐标文件.csv 指定，或把坐标文件放到同一目录")

    data["components"] = comps
    data["title"] = _title_for(root, layer_files, title)
    macro_skipped = sum(r.macro_skipped for r, _k in parsed)
    if macro_skipped:
        warn("忽略了 %d 个无法求值的孔径宏（Aperture Macro）图元" % macro_skipped)
    macro_approx = sum(r.macro_approx for r, _k in parsed)
    if macro_approx:
        warn("%d 个孔径宏焊盘按外接矩形近似（异形/散热盘，形状略粗糙，位置尺寸基本准）"
             % macro_approx)
    clear_skipped = sum(r.clear_skipped for r, _k in parsed)
    if clear_skipped:
        warn("忽略了 %d 个反极性（%%LPC%%）图元：整片铜/挖空效果无法还原"
             % clear_skipped)
    data["meta"]["source"] = {
        "gerber": [os.path.basename(p) for p in layer_files],
        "drill": [os.path.basename(p) for p in drill_files],
        "centroid": cent_choice["path"] if cent_choice else "",
        "outline_file": chosen_name,
        "ignored": ignored,
    }
    data["meta"]["gerber"] = {
        "drill": drill_info_lines,
        "ignored_layers": ignored,
        "outline_file": chosen_name,
        "centroid_file": cent_choice["path"] if cent_choice else "",
        "macro_skipped": sum(r.macro_skipped for r, _k in parsed),
        "macro_approx": sum(r.macro_approx for r, _k in parsed),
        "clear_skipped": sum(r.clear_skipped for r, _k in parsed),
        "missing_aperture": sum(r.missing_aperture for r, _k in parsed),
        "unsupported": sorted(set(x for r, _k in parsed for x in r.unsupported)
                              | set(x for _p, r in outline_cands for x in r.unsupported)),
    }
    if ignored:
        kinds = sorted(set(k for _n, k in ignored))
        warn("忽略了 %d 个不渲染的层文件（%s）：%s"
             % (len(ignored), "/".join(kinds),
                ", ".join(n for n, _k in ignored[:6])))
    if cent_info and cent_info.get("outside") and comps:
        frac = len(cent_info["outside"]) / float(len(comps))
        if frac >= 0.3:
            warn("%d/%d 个元件坐标落在板框外（坐标文件与 Gerber 的原点/单位不一致？）：%s"
                 % (len(cent_info["outside"]), len(comps),
                    ", ".join(cent_info["outside"][:8])))
    if cent_info and cent_info.get("transform"):
        warn(cent_info["transform"])
    if comps and top_flashes + bottom_flashes:
        near = 0
        idx = _PadIndex(top_flashes + bottom_flashes, cell=1.5)
        for c in comps:
            if idx.nearest(c["x"], c["y"], 1.2):
                near += 1
        if near < len(comps) * 0.3:
            warn("坐标文件里只有 %d/%d 个元件中心附近有铜箔焊盘，"
                 "坐标或单位可能对不上" % (near, len(comps)))
    return data


def _build_components(table, board_box, top_flashes, bottom_flashes, warn):
    rows = table["rows"]
    hint = table.get("hint")
    # 先收集原始数值与显式单位
    raw = []
    explicit_units = set()
    for r in rows:
        xv, xu = r["x"]
        yv, yu = r["y"]
        if xu:
            explicit_units.add(xu)
        if yu:
            explicit_units.add(yu)
        raw.append((r, xv, xu, yv, yu))
    units_cands = []
    if explicit_units:
        # 有显式单位的行按显式来，没有的用最常见显式单位
        default_u = "mil" if "mil" in explicit_units else "mm"
        units_cands = [default_u]
    else:
        units_cands = ["mm", "mil"]
        if hint in ("mm", "mil"):
            units_cands = [hint] + [u for u in units_cands if u != hint]

    pad_index = _PadIndex(top_flashes + bottom_flashes, cell=1.0)

    def make(unit):
        # _parse_len() 已经把带单位的数值换成 mm；没有单位的按 unit 解释
        pts = []
        for r, xv, xu, yv, yu in raw:
            if xu:
                x = xv
            else:
                x = xv * 0.0254 if unit == "mil" else xv
            if yu:
                y = yv
            else:
                y = yv * 0.0254 if unit == "mil" else yv
            pts.append((x, y))
        return pts

    best = None
    for unit in units_cands:
        pts = make(unit)
        score, detail = _score_coords(pts, board_box, pad_index)
        if best is None or score > best[0]:
            best = (score, unit, pts, detail)
    unit = best[1]
    pts = best[2]
    detail = best[3]

    # 坐标文件与 Gerber 原点不一致时的兜底：整体平移 / 上下翻转
    transform = ""
    bx0, by0, bx1, by1 = board_box
    if detail.get("inside", 0) < 0.5 and len(pts) >= 4:
        n = len(pts)
        cx = sum(p[0] for p in pts) / n
        cy = sum(p[1] for p in pts) / n
        bcx = (bx0 + bx1) / 2.0
        bcy = (by0 + by1) / 2.0
        flips = [("identity", lambda p: p),
                 ("flip", lambda p: (p[0], by0 + by1 - p[1])),
                 ("move", lambda p: (p[0] + bcx - cx, p[1] + bcy - cy)),
                 ("flip+move", lambda p: (p[0] + bcx - cx, by0 + by1 - (p[1] + bcy - cy)))]
        best2 = None
        for name, fn in flips:
            cand = [fn(p) for p in pts]
            sc, det = _score_coords(cand, board_box, pad_index)
            if best2 is None or sc > best2[0]:
                best2 = (sc, name, cand, det)
        if best2 and best2[3].get("inside", 0) >= 0.8 and best2[0] > best[0] + 0.5:
            transform = "坐标文件与 Gerber 原点不一致，已自动%s对齐（%s）" % (
                {"flip": "上下翻转", "move": "平移", "flip+move": "翻转+平移"}
                .get(best2[1], ""), best2[3])
            pts = best2[2]
            detail = best2[3]

    comps = []
    outside = []
    for (r, _xv, _xu, _yv, _yu), (x, y) in zip(raw, pts):
        side = _side_from_str(r.get("layer"))
        if side is None:
            side = _infer_side(x, y, top_flashes, bottom_flashes)
        comment = r.get("comment", "") or ""
        fp = r.get("fp", "") or ""
        comp = {
            "d": r["d"],
            "fp": fp,
            "side": side,
            "x": common.rnd(x),
            "y": common.rnd(y),
            "rot": common.rnd(_parse_rot(r.get("rot"))),
            "val": comment,
            "comment": comment,
            "value": comment,
        }
        if r.get("mpn"):
            comp["mpn"] = r["mpn"]
        comps.append(comp)
        bx0, by0, bx1, by1 = board_box
        if not (bx0 - 1.0 <= x <= bx1 + 1.0 and by0 - 1.0 <= y <= by1 + 1.0):
            outside.append(r["d"])
    info = {"outside": outside, "transform": transform, "unit": unit,
            "file": table["path"]}
    return comps, info


def _infer_side(x, y, top_flashes, bottom_flashes):
    best = None
    for side, lst in (("top", top_flashes), ("bottom", bottom_flashes)):
        idx = _PadIndex(lst, cell=1.0)
        d = idx.nearest(x, y, 1.5)
        if d and (best is None or d[0] < best[0]):
            best = (d[0], side)
    if best:
        return best[1]
    return "top"


# ---------------------------------------------------------------------------
# sniff
# ---------------------------------------------------------------------------
def sniff(root):
    try:
        files = _scan_files(root)
    except Exception:
        return False
    if not files:
        return False
    for p in files[:400]:
        ext = os.path.splitext(p.lower())[1]
        if ext in _DRILL_EXTS:
            return True
        if ext in _CENTROID_EXTS:
            continue
        if ext in _SKIP_EXTS:
            continue
        if ext == ".txt":
            if _looks_like_excellon(_head_text(p, 4096)):
                return True
            continue
        if ext in _GERBER_EXTS:
            head = _head_text(p, 2048)
            if _looks_like_gerber_head(head) or not head.strip():
                return True
            if _looks_like_excellon(head):
                return True
            continue
        head = _head_text(p, 1024)
        if _looks_like_gerber_head(head) or _looks_like_excellon(head):
            return True
    return False


# ---------------------------------------------------------------------------
# 独立运行（开发/调试用）
# ---------------------------------------------------------------------------
def main():
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Gerber/Excellon/坐标文件 -> pcb_data.json")
    ap.add_argument("input", help="Gerber 目录（或 zip / 单个 Gerber 文件）")
    ap.add_argument("output", nargs="?", default="pcb_data.json")
    ap.add_argument("--centroid", help="坐标文件 CSV")
    ap.add_argument("--title", help="板名")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."
    os.makedirs(out_dir, exist_ok=True)
    root = common.materialize(args.input, out_dir)
    data = common.finalize(parse(root, out_dir, centroid=args.centroid, title=args.title))
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    st = data["meta"]["stats"]
    print("板尺寸  : %.2f x %.2f mm" % tuple(st["board_mm"]))
    print("元件    : %d（顶 %d / 底 %d）" % (st["components"], st["top"], st["bottom"]))
    print("焊盘    : %d  走线 %d  丝印 %d" % (st["pads"], st["routes"], st["silk"]))
    for w in data["meta"]["warnings"]:
        print("⚠️ ", w)
    print("写入    :", args.output)


if __name__ == "__main__":
    main()
