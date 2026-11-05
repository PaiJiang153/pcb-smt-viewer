#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backend self-check harness (development helper).

    python3 test_backend.py <format> <input> [--build [outdir]] [--verify] [--json]

Loads `parsers.<format>`, runs sniff/parse/finalize, prints the statistics and a
few components, optionally builds the HTML and (with Playwright available) runs
`verify.mjs` on it.
"""
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from parsers import common          # noqa: E402
import build_html                   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="single-backend smoke test")
    ap.add_argument("format", help="backend id, e.g. altium / kicad / easyeda_std / easyeda_pro / gerber")
    ap.add_argument("input", help="file / directory / zip")
    ap.add_argument("--build", nargs="?", const="", default=None,
                    help="also build the HTML (optional output dir)")
    ap.add_argument("--verify", action="store_true", help="also run verify.mjs (needs Playwright)")
    ap.add_argument("--json", action="store_true", help="dump pcb_data.json next to the html")
    ap.add_argument("--limit", type=int, default=8, help="how many components to print")
    ap.add_argument("--show-warnings", action="store_true", default=True)
    args = ap.parse_args()

    try:
        mod = importlib.import_module("parsers." + args.format)
    except ImportError as exc:
        sys.exit("no such backend 'parsers.%s': %s" % (args.format, exc))

    outdir = os.path.abspath(args.build) if args.build else \
        os.path.join(os.getcwd(), "_out_" + args.format)
    os.makedirs(outdir, exist_ok=True)
    print("input   :", os.path.abspath(args.input))
    print("backend :", args.format, "-", getattr(mod, "LABEL", ""))

    root = common.materialize(args.input, outdir)
    print("root    :", root)

    sniffed = False
    try:
        sniffed = bool(mod.sniff(root))
    except Exception as exc:                       # sniff must never crash
        print("!! sniff() raised:", exc)
    print("sniff   :", sniffed, "" if sniffed else "  (still running parse to see what happens)")

    data = mod.parse(root, outdir)
    data = common.finalize(data)
    st = data["meta"]["stats"]
    print("\n--- stats ---")
    for k in ("components", "top", "bottom", "pads", "routes", "pour_lines",
              "arcs", "vias", "silk", "pours", "board_mm", "outline_points",
              "copper_layers"):
        print("  %-14s %s" % (k, st.get(k)))
    print("  title         %s" % data.get("title"))
    print("  bbox          %s" % data.get("bbox"))

    print("\n--- first %d components ---" % args.limit)
    for c in data["components"][:args.limit]:
        print("  %-6s %-6s %-22s x=%-8s y=%-8s rot=%-6s np=%s"
              % (c.get("d"), c.get("side"), (c.get("fp") or "")[:22],
                 c.get("x"), c.get("y"), c.get("rot"), c.get("np")))

    warnings = data["meta"].get("warnings") or []
    if warnings and args.show_warnings:
        print("\n--- warnings (%d) ---" % len(warnings))
        for w in warnings:
            print("  ⚠️ ", w)

    html_path = None
    if args.build is not None:
        title = data.get("title") or "PCB"
        safe = "".join("_" if ch in '<>:"/\\|?*\n\r\t' else ch for ch in title).strip() or "board"
        html_path = os.path.join(outdir, safe + "_贴片定位图.html")
        build_html.build(data, html_path)
        build_html.write_readme(data, os.path.splitext(html_path)[0] + ".README.md",
                                os.path.basename(html_path))
        if args.json:
            with open(os.path.join(outdir, "pcb_data.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print("\nhtml    :", html_path, "(%d bytes)" % os.path.getsize(html_path))

    if args.verify and html_path:
        shots = os.path.join(outdir, "shots")
        cmd = ["node", os.path.join(HERE, "verify.mjs"), html_path, shots]
        print("\n$ " + " ".join(cmd))
        try:
            r = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
            if r.stdout:
                print(r.stdout.rstrip())
            if r.stderr:
                print(r.stderr.rstrip())
            print("verify.mjs exit:", r.returncode)
        except FileNotFoundError:
            print("!! node not found — skip verify")
        except subprocess.TimeoutExpired:
            print("!! verify.mjs timed out")


if __name__ == "__main__":
    main()
