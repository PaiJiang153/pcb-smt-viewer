#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pcb_data.json  +  assets/  ->  单文件 HTML（SMT 贴片定位图）

用法:
    python3 build_html.py pcb_data.json [out.html] [--title 标题] [--no-readme]

产物是**单文件**网页：数据、样式、脚本全部内联，可以单独拷走、离线打开。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")


def read(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return f.read()


def build(data, html_out, title=None):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # make the JSON payload safe inside <script>
    payload = (payload.replace("<", "\\u003c")
                      .replace(">", "\\u003e")
                      .replace("&", "\\u0026"))
    css = read("viewer.css")
    js = read("viewer.js").replace("</script", "<\\/script")
    tpl = read("viewer.html")

    html = tpl.replace("/*__CSS__*/", css)
    html = html.replace("/*__DATA__*/", payload)
    html = html.replace("/*__JS__*/", js)

    if title:
        data["title"] = title
    comps = data.get("components", [])
    n_top = sum(1 for c in comps if c.get("side") == "top")
    n_bot = len(comps) - n_top
    html = html.replace('<b data-count="top"></b>', "<b>%d</b>" % n_top)
    html = html.replace('<b data-count="bottom"></b>', "<b>%d</b>" % n_bot)

    with open(html_out, "w", encoding="utf-8") as f:
        f.write(html)
    return n_top, n_bot


def write_readme(data, path, html_name=None):
    st = data.get("meta", {}).get("stats", {})
    src = data.get("meta", {}).get("source", {})
    warns = data.get("meta", {}).get("warnings", [])
    comps = data.get("components", [])
    lines = []
    lines.append("# %s · SMT 贴片定位图\n" % data.get("title", "PCB"))
    lines.append("由 Altium 工程文件自动生成（`altium-smt-viewer` skill），数据未手工修改。\n")
    lines.append("## 文件\n")
    lines.append("- `%s` — 双击用浏览器打开即可（单文件、离线）\n"
                 % (html_name or (os.path.splitext(os.path.basename(path))[0] + ".html")))
    lines.append("## 板上信息\n")
    lines.append("| 项目 | 数值 |")
    lines.append("| --- | --- |")
    if st:
        lines.append("| 板尺寸 | %.2f × %.2f mm |" % tuple(st.get("board_mm", (0, 0))))
        lines.append("| 元件 | %d 个（顶层 %d / 底层 %d） |"
                     % (st.get("components", len(comps)), st.get("top", 0), st.get("bottom", 0)))
        lines.append("| 焊盘 / 过孔 | %d / %d |" % (st.get("pads", 0), st.get("vias", 0)))
        lines.append("| 走线 / 覆铜线段 | %d / %d |" % (st.get("routes", 0), st.get("pour_lines", 0)))
        lines.append("| 铜层 | %s |" % (st.get("copper_layers", []),))
    if src.get("pcb"):
        lines.append("| 数据来源 | `%s` |" % os.path.basename(src["pcb"]))
        if src.get("sch"):
            lines.append("| 原理图 | `%s`（提供型号/值） |" % os.path.basename(src["sch"]))
    lines.append("")
    lines.append("## 元件清单\n")
    lines.append("| 位号 | 层 | 值 | 型号 | 封装 | 板内 X | 板内 Y | 旋转 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    b = data.get("bbox", [0, 0, 1, 1])
    for c in sorted(comps, key=lambda c: c.get("d", "")):
        lines.append("| %s | %s | %s | %s | %s | %.2f | %.2f | %s° |" % (
            c.get("d", ""), "顶层" if c.get("side") == "top" else "底层",
            c.get("value") or c.get("val") or "",
            c.get("comment") or "",
            c.get("fp") or "",
            c.get("x", 0) - b[0], c.get("y", 0) - b[1],
            int(round((c.get("rot", 0) % 360 + 360) % 360))))
    if warns:
        lines.append("\n## 生成时的提示\n")
        for w in warns:
            lines.append("- %s" % w)
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="pcb_data.json -> 单文件 HTML")
    ap.add_argument("data", help="parse_pcb.py 产出的 pcb_data.json")
    ap.add_argument("out", nargs="?", default="smt_viewer.html", help="输出 html")
    ap.add_argument("--title", help="覆盖网页标题（默认用 pcb_data.json 里的 title）")
    ap.add_argument("--no-readme", action="store_true", help="不生成同名 .README.md")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    if args.title:
        data["title"] = args.title

    html_out = os.path.abspath(args.out)
    n_top, n_bot = build(data, html_out, args.title)
    print("built:", html_out, os.path.getsize(html_out), "bytes")
    print("components: %d (top %d / bottom %d)" % (n_top + n_bot, n_top, n_bot))

    if not args.no_readme:
        readme = os.path.splitext(html_out)[0] + ".README.md"
        write_readme(data, readme, os.path.basename(html_out))
        print("readme:", readme)


if __name__ == "__main__":
    main()
