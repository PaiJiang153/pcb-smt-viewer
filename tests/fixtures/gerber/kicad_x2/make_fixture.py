#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 kicad_x2 测试夹具（**合成数据**，见 PROVENANCE.md）。

模拟 KiCad 7 导出的 Gerber X2 文件集 + .pos 坐标文件：
  * %TF.FileFunction% / %TF.GenerationSoftware% / %TA.AperFunction% 等 X2 属性
  * KiCad 默认文件名 xxx-F_Cu.gbr / xxx-B_Cu.gbr / xxx-Edge_Cuts.gbr
  * KiCad 风格 Excellon（;FORMAT= 注释 + 小数坐标 + METRIC）
  * KiCad 风格 .pos（Ref/Val/Package/PosX/PosY/Rot/Side）

板子 40 x 30 mm，2 层；元件：R1/R2/C1（SMD）+ U1（SOIC-8）+ 2 个 THT 焊盘 + 4 个 NPTH 安装孔。

    python3 make_fixture.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NM = 1000000                     # FSLAX46 -> 1e6 单位/mm


def co(v):
    return int(round(v * NM))


class Gerber:
    def __init__(self, title, filefunc, polarity="Positive"):
        self.lines = [
            "G04 %s*" % title,
            "%TF.GenerationSoftware,KiCad,Pcbnew,7.0.0*%",
            "%TF.CreationDate,2025-01-01T00:00:00+00:00*%",
            "%%TF.FileFunction,%s*%%" % filefunc,
            "%%TF.FilePolarity,%s*%%" % polarity,
            "%FSLAX46Y46*%",
            "%MOMM*%",
            "%LPD*%",
            "G01*",
            "G75*",
        ]
        self.ap = {}

    def aperture(self, code, spec):
        self.ap[code] = spec
        self.lines.append("%%ADD%d%s*%%" % (code, spec))

    def select(self, code):
        self.lines.append("D%d*" % code)

    def flash(self, code, x, y):
        self.select(code)
        self.lines.append("X%dY%dD03*" % (co(x), co(y)))

    def line(self, code, x1, y1, x2, y2):
        self.select(code)
        self.lines.append("X%dY%dD02*" % (co(x1), co(y1)))
        self.lines.append("X%dY%dD01*" % (co(x2), co(y2)))

    def region(self, pts):
        self.lines.append("G36*")
        self.lines.append("X%dY%dD02*" % (co(pts[0][0]), co(pts[0][1])))
        for x, y in pts[1:]:
            self.lines.append("X%dY%dD01*" % (co(x), co(y)))
        self.lines.append("X%dY%dD01*" % (co(pts[0][0]), co(pts[0][1])))
        self.lines.append("G37*")

    def rect_outline(self, code, x0, y0, x1, y1):
        self.line(code, x0, y0, x1, y0)
        self.line(code, x1, y0, x1, y1)
        self.line(code, x1, y1, x0, y1)
        self.line(code, x0, y1, x0, y0)

    def write(self, name):
        self.lines.append("%TD*%")
        self.lines.append("M02*")
        with open(os.path.join(HERE, name), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(self.lines) + "\n")


# --------------------------------------------------------------------- 元件
SMD = [
    # (ref, val, pkg, x, y, rot, pads[(px,py,w,h,rot)])
    ("R1", "10k", "R_0805", 10.0, 20.0, 0.0, [
        (9.05, 20.0, 1.2, 1.4, 0.0), (10.95, 20.0, 1.2, 1.4, 0.0)]),
    ("R2", "4k7", "R_0805", 20.0, 20.0, 90.0, [
        (20.0, 19.05, 1.4, 1.2, 90.0), (20.0, 20.95, 1.4, 1.2, 90.0)]),
    ("C1", "100n", "C_0603", 30.0, 20.0, 0.0, [
        (29.25, 20.0, 0.9, 1.0, 0.0), (30.75, 20.0, 0.9, 1.0, 0.0)]),
]
U1_PADS = []
for i in range(4):
    U1_PADS.append((17.3, 9.905 - i * 1.27, 1.55, 0.6, 0.0))     # 1..4
for i in range(4):
    U1_PADS.append((22.7, 6.095 + i * 1.27, 1.55, 0.6, 0.0))     # 5..8
SMD.append(("U1", "LM358", "SOIC-8", 20.0, 8.0, 0.0, U1_PADS))

THT = [(5.0, 5.0, 1.0), (5.0, 10.0, 1.0)]      # (x, y, drill)
NPTH = [(3.0, 3.0, 3.0), (37.0, 3.0, 3.0), (3.0, 27.0, 3.0), (37.0, 27.0, 3.0)]

# ------------------------------------------------------------------ 顶层铜
fc = Gerber("F.Cu - top copper", "Copper,L1,Top")
fc.aperture(10, "C,0.250000")
fc.aperture(11, "R,1.200000X1.400000")
fc.aperture(12, "R,1.400000X1.200000")
fc.aperture(13, "R,0.900000X1.000000")
fc.aperture(14, "R,1.550000X0.600000")
fc.aperture(15, "C,2.000000")
for _ref, _v, _p, _x, _y, _r, pads in SMD:
    for px, py, w, h, rot in pads:
        code = 11 if (w, h) == (1.2, 1.4) else 12 if (w, h) == (1.4, 1.2) else \
            13 if (w, h) == (0.9, 1.0) else 14
        fc.flash(code, px, py)
for x, y, _d in THT:
    fc.flash(15, x, y)
# 走线
fc.line(10, 9.05, 20.0, 9.05, 15.0)
fc.line(10, 9.05, 15.0, 10.3, 10.2)
fc.line(10, 10.95, 20.0, 17.3, 9.905)          # R1 -> U1 pin1
fc.line(10, 10.95, 20.0, 20.0, 16.0)
fc.line(10, 20.0, 16.0, 20.0, 19.05)           # R2 pad1
fc.line(10, 20.95, 20.0, 30.0, 12.0)
fc.line(10, 30.0, 12.0, 30.75, 20.0)           # C1 pad2
fc.line(10, 29.25, 20.0, 22.7, 9.905)          # C1 pad1 -> U1 pin8
fc.line(10, 17.3, 8.635, 5.0, 10.0)            # U1 pin2 -> THT
fc.write("x2demo-F_Cu.gbr")

# ---------------------------------------------------------------- 底层铜
bc = Gerber("B.Cu - bottom copper", "Copper,L2,Bot")
bc.aperture(10, "C,0.250000")
bc.aperture(15, "C,2.000000")
bc.region([(0.6, 0.6), (39.4, 0.6), (39.4, 29.4), (0.6, 29.4)])  # 底层覆铜
bc.line(10, 5.0, 5.0, 20.0, 16.0)
bc.line(10, 20.0, 16.0, 30.0, 18.0)
bc.line(10, 30.0, 18.0, 35.0, 25.0)
for x, y, _d in THT:
    bc.flash(15, x, y)
bc.write("x2demo-B_Cu.gbr")

# ---------------------------------------------------------------- 顶层丝印
fs = Gerber("F.Silkscreen", "Legend,Top")
fs.aperture(10, "C,0.120000")
fs.aperture(11, "C,0.150000")
fs.rect_outline(10, 9.0, 19.375, 11.0, 20.625)       # R1 本体
fs.rect_outline(10, 19.375, 19.0, 20.625, 21.0)     # R2 本体
fs.rect_outline(10, 29.4, 19.6, 30.6, 20.4)         # C1 本体
fs.rect_outline(10, 16.8, 6.0, 23.2, 10.0)          # U1 本体
fs.line(11, 16.8, 10.0, 16.8, 10.6)                 # U1 pin1 标记
for x, y, _d in THT:                                # THT 焊盘外圈
    fs.line(10, x - 1.2, y, x - 0.9, y)
    fs.line(10, x + 0.9, y, x + 1.2, y)
fs.write("x2demo-F_Silkscreen.gbr")

# ---------------------------------------------------------------- 底层丝印
bs = Gerber("B.Silkscreen", "Legend,Bot")
bs.aperture(10, "C,0.120000")
bs.line(10, 2.0, 28.0, 38.0, 28.0)
bs.line(10, 2.0, 2.0, 2.0, 28.0)
bs.write("x2demo-B_Silkscreen.gbr")

# ------------------------------------------------------------------ 板框
ed = Gerber("Edge.Cuts", "Profile,NP")
ed.aperture(10, "C,0.050000")
ed.rect_outline(10, 0.0, 0.0, 40.0, 30.0)
ed.write("x2demo-Edge_Cuts.gbr")

# ---------------------------------------------------------------- 钻孔文件
drl = [
    "M48",
    ";DRILL file {KiCad 7.0.0} date 2025-01-01",
    ";FORMAT={-:-/ absolute / metric / decimal}",
    "FMAT,2",
    "METRIC",
    "T1C1.000",
    "T2C3.000",
    "%",
    "G90",
    "G05",
    "T1",
]
for x, y, _d in THT:
    drl.append("X%.3fY%.3f" % (x, y))
drl.append("T2")
for x, y, _d in NPTH:
    drl.append("X%.3fY%.3f" % (x, y))
drl += ["M30"]
with open(os.path.join(HERE, "x2demo.drl"), "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(drl) + "\n")

# -------------------------------------------------------------- 坐标文件
pos = ["# Ref,Val,Package,PosX,PosY,Rot,Side"]
for ref, val, pkg, x, y, rot, _pads in SMD:
    pos.append("%s,%s,%s,%.3f,%.3f,%.1f,top" % (ref, val, pkg, x, y, rot))
with open(os.path.join(HERE, "x2demo-pos.csv"), "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(pos) + "\n")

print("wrote fixture files to", HERE)
