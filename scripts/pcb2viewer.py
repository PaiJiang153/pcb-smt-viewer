#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用入口：任何支持的 PCB 文件 -> 单文件「SMT 贴片定位图」网页

    python3 pcb2viewer.py <输入> [输出目录]

支持的格式（自动识别，也可以用 --format 指定）：

    altium       Altium Designer  .PcbDoc（+ 同名 .SchDoc 取型号/值）/ 工程 .zip
    kicad        KiCad            .kicad_pcb / 工程 .zip
    easyeda_pro  嘉立创EDA 专业版 .epro / 工程 .zip / .eprj3 文件夹 / .eprj
    easyeda_std  嘉立创EDA 标准版 PCB .json / 含 json 的 .zip
    gerber       Gerber + 钻孔 + 坐标文件（任何 EDA 都能导出；嘉立创下单用的那套）

输出目录（默认 <输入名>_smt/）里得到：
    <板名>_贴片定位图.html      ← 交付物，单文件、双击打开
    pcb_data.json               中间数据
    <板名>_贴片定位图.README.md 元件清单
    source/…                    输入是 zip 时解出来的原始工程

例子：
    python3 pcb2viewer.py 我的板子.zip
    python3 pcb2viewer.py 项目.kicad_pcb out/
    python3 pcb2viewer.py --format gerber gerber目录/ --centroid 坐标文件.csv
    python3 pcb2viewer.py --list-formats
"""
import argparse
import importlib
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True        # don't litter the skill directory

from parsers import common            # noqa: E402
import build_html                     # noqa: E402

# order matters: the more specific sniffers come first
BACKEND_ORDER = ["altium", "kicad", "easyeda_pro", "easyeda_std", "gerber"]
BACKENDS = {}


def load_backends(verbose=False):
    for name in BACKEND_ORDER:
        try:
            BACKENDS[name] = importlib.import_module("parsers." + name)
        except ImportError as exc:
            if verbose:
                sys.stderr.write("（跳过 %s：%s）\n" % (name, exc))
        except Exception as exc:            # a broken backend must not kill the CLI
            sys.stderr.write("警告：加载 %s 后端失败：%s\n" % (name, exc))
    return BACKENDS


def detect(root):
    """Return (name, module) for the first backend that claims `root`."""
    for name, mod in BACKENDS.items():
        try:
            if mod.sniff(root):
                return name, mod
        except Exception as exc:            # sniff must never break detection
            sys.stderr.write("（%s.sniff 出错，跳过：%s）\n" % (name, exc))
    return None, None


def call_parse(mod, root, out_dir, opts):
    """Call backend.parse passing only the keyword arguments it accepts."""
    try:
        params = inspect.signature(mod.parse).parameters
        kwargs = {k: v for k, v in opts.items() if k in params and v is not None}
    except (TypeError, ValueError):
        kwargs = {}
    return mod.parse(root, out_dir, **kwargs)


def safe_filename(name):
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if ch in bad else ch for ch in name).strip(" .")
    return out or "board"


def main():
    ap = argparse.ArgumentParser(
        description="任何 PCB 文件 -> SMT 贴片定位图（单文件网页）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("例子：")[-1])
    ap.add_argument("input", nargs="?", help=".zip / 目录 / 具体格式的文件")
    ap.add_argument("outdir", nargs="?", help="输出目录（默认 <输入名>_smt/）")
    ap.add_argument("--format", help="强制指定后端（默认自动识别）")
    ap.add_argument("--list-formats", action="store_true", help="列出可用后端")
    ap.add_argument("--title", help="网页/文件名里的板名（默认取文件名）")
    ap.add_argument("--pcb", help="工程里有多个板时按文件名关键词选择")
    ap.add_argument("--centroid", help="Gerber 模式下的坐标文件 CSV（不填则自动找）")
    ap.add_argument("--no-spur-trim", action="store_true",
                    help="不剔除板框细毛刺（默认剔除 <1mm 宽的贴边缺口/切割线）")
    ap.add_argument("--no-readme", action="store_true", help="不生成 README")
    ap.add_argument("--json-only", action="store_true", help="只解析出 pcb_data.json，不打包网页")
    args = ap.parse_args()

    load_backends(verbose=args.format is not None)
    if args.list_formats or not args.input:
        print("可用后端：")
        for name, mod in BACKENDS.items():
            print("  %-12s %s" % (name, getattr(mod, "LABEL", "")))
        if not args.input:
            if not args.list_formats:
                ap.print_help()
            return
        return

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        sys.exit("输入不存在: %s" % src)
    stem = os.path.splitext(os.path.basename(src.rstrip("/")))[0]
    outdir = os.path.abspath(args.outdir or (stem + "_smt"))
    os.makedirs(outdir, exist_ok=True)

    root = common.materialize(src, outdir)
    if args.format:
        if args.format not in BACKENDS:
            sys.exit("未知格式 %r，可用：%s" % (args.format, ", ".join(BACKENDS)))
        name, mod = args.format, BACKENDS[args.format]
    else:
        name, mod = detect(root)
    if mod is None:
        sys.exit("认不出这个文件是什么格式。\n"
                 "  已尝试：%s\n"
                 "  请用 --format 指定，或把它导出成 Gerber + 坐标文件（--format gerber）。"
                 % ", ".join(BACKENDS))

    print("输入   :", src)
    print("识别为 :", name, "-", getattr(mod, "LABEL", ""))
    if root != src:
        print("解压到 :", root)
    print("输出   :", outdir)

    opts = {
        "title": args.title,
        "pcb_hint": args.pcb,
        "spur_trim": not args.no_spur_trim,
        "centroid": args.centroid,
    }
    data = call_parse(mod, root, outdir, opts)
    data = common.finalize(data)
    data.setdefault("title", args.title or stem)

    json_path = os.path.join(outdir, "pcb_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    st = data["meta"]["stats"]
    print("\n=== 完成 ===")
    print("板尺寸 : %.2f x %.2f mm" % tuple(st["board_mm"]))
    print("元件   : %d（顶层 %d / 底层 %d）"
          % (st["components"], st["top"], st["bottom"]))
    print("焊盘   : %d  过孔 %d  走线 %d  丝印 %d"
          % (st["pads"], st["vias"], st["routes"], st["silk"]))
    for w in data["meta"].get("warnings", []):
        print("⚠️ ", w)
    print("\n数据   :", json_path)

    if not args.json_only:
        title = data.get("title") or stem
        html_path = os.path.join(outdir, safe_filename(title) + "_贴片定位图.html")
        build_html.build(data, html_path)
        if not args.no_readme:
            build_html.write_readme(data, os.path.splitext(html_path)[0] + ".README.md",
                                    os.path.basename(html_path))
        print("网页   :", html_path, "(%d bytes)" % os.path.getsize(html_path))
        if not args.no_readme:
            print("清单   :", os.path.splitext(html_path)[0] + ".README.md")
        print("\n双击打开网页即可离线使用。")


if __name__ == "__main__":
    main()
