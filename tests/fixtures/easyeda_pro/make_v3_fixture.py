#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the *synthetic* EasyEDA Pro fixtures.

The two epro archives are real projects (see PROVENANCE.md); everything this
script writes is hand-built from the published format specification
(https://github.com/easyeda/easyeda-format-skill, documents/PCB.md and
primitives/PCB/*.md) so that the v3 (`.epcb2` / `.eprj3`) and legacy SQLite
(`.eprj`) input forms are covered without shipping anybody else's design.

Run from this directory:  python3 make_v3_fixture.py
"""
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_W = 1574.803          # 40 mm in mil
BOARD_H = 1000.0            # 25.4 mm

# --------------------------------------------------------------------------
# v3 record helpers:  {"type":..,"ticket":..,"id":..}||{payload}|
# --------------------------------------------------------------------------
_ticket = [0]


def _ticket_next():
    _ticket[0] += 1
    return _ticket[0]


def rec(rtype, id_, payload):
    outer = {"type": rtype, "ticket": _ticket_next()}
    if id_ is not None:
        outer["id"] = id_
    return "%s||%s|" % (json.dumps(outer, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def outline():
    return [[0, 0, "L", BOARD_W, 0, BOARD_W, BOARD_H, 0, BOARD_H, 0, 0]]


def pad(id_, layer, num, x, y, w, h, shape="RECT", angle=0.0, hole=None):
    return rec("PAD", id_, {
        "layerId": layer, "num": num,
        "centerX": x, "centerY": y, "padAngle": angle,
        "hole": hole,
        "defaultPad": {"padType": shape, "width": w, "height": h},
        "specialPad": [], "padOffsetX": 0, "padOffsetY": 0, "relativeAngle": 0,
        "plated": bool(hole), "padType": "NORMAL", "netName": "",
        "groupId": 0, "locked": False, "zIndex": -1,
    })


def line(id_, layer, x1, y1, x2, y2, width, net=""):
    return rec("LINE", id_, {
        "layerId": layer, "netName": net, "width": width,
        "startX": x1, "startY": y1, "endX": x2, "endY": y2,
        "groupId": 0, "locked": False, "zIndex": -1,
    })


def arc(id_, layer, x1, y1, x2, y2, angle, width):
    return rec("ARC", id_, {
        "layerId": layer, "netName": "", "width": width, "angle": angle,
        "arcType": "DOT", "startX": x1, "startY": y1, "endX": x2, "endY": y2,
        "groupId": 0, "locked": False, "zIndex": -1,
    })


def via(id_, x, y, drill, dia):
    return rec("VIA", id_, {
        "centerX": x, "centerY": y, "holeDiameter": drill, "viaDiameter": dia,
        "netName": "GND", "ruleName": "", "viaType": "NORMAL",
        "groupId": 0, "locked": False,
    })


def poly(id_, layer, path, width=10, ptype="NORMAL"):
    return rec("POLY", id_, {
        "layerId": layer, "netName": "", "width": width, "path": path,
        "polyType": ptype, "groupId": 0, "locked": False, "zIndex": -1,
    })


def fill(id_, layer, path):
    return rec("FILL", id_, {
        "layerId": layer, "netName": "GND", "width": 4,
        "fillStyle": "SOLID", "path": path,
        "groupId": 0, "locked": False, "zIndex": -1,
    })


def component(id_, layer, x, y, angle, fp_uuid):
    return rec("COMPONENT", id_, {
        "layerId": layer, "x": x, "y": y, "angle": angle,
        "attrs": {"Footprint": fp_uuid},
        "groupId": 0, "locked": False, "zIndex": -1,
    })


def attr(id_, layer, parent, key, value, x=None, y=None):
    return rec("ATTR", id_, {
        "parentId": parent, "layerId": layer, "key": key, "value": value,
        "x": x, "y": y, "keyVisible": False, "valueVisible": True,
        "fontFamily": None, "fontSize": None, "strokeWidth": 6,
        "bold": False, "italic": None, "origin": "CENTER_MIDDLE",
        "angle": 0, "reverse": False, "expansion": 0, "mirror": False,
        "specialColor": None, "groupId": 0, "locked": False, "zIndex": None,
    })


def dochead(doc_type, uuid, title=""):
    return rec("DOCHEAD", None, {
        "docType": doc_type, "uuid": uuid, "client": "fixture",
        "title": title, "version": "3.0",
    })


# --------------------------------------------------------------------------
# footprint documents (embedded in their own .efoo files, like a real project)
# --------------------------------------------------------------------------
def fp_0402(uuid, name, pad_w, pad_h, pitch):
    out = [dochead("FOOTPRINT", uuid, name)]
    out.append(pad(uuid + "e1", 1, "1", -pitch, 0, pad_w, pad_h))
    out.append(pad(uuid + "e2", 1, "2", pitch, 0, pad_w, pad_h))
    out.append(poly(uuid + "e3", 3, [-59.0, -29.5, "L", 59.0, -29.5, 59.0, 29.5, -59.0, 29.5], 8))
    out.append(attr(uuid + "e4", 3, "", "Footprint", name))
    return "\n".join(out) + "\n"


def fp_sot23(uuid, name):
    out = [dochead("FOOTPRINT", uuid, name)]
    out.append(pad(uuid + "e1", 1, "1", -39.37, -39.37, 23.6, 31.5))
    out.append(pad(uuid + "e2", 1, "2", -39.37, 39.37, 23.6, 31.5))
    out.append(pad(uuid + "e3", 1, "3", 39.37, 0, 23.6, 31.5))
    out.append(poly(uuid + "e4", 3, [-59.0, -78.7, "L", 59.0, -78.7, 59.0, 78.7, -59.0, 78.7], 8))
    out.append(attr(uuid + "e5", 3, "", "Footprint", name))
    return "\n".join(out) + "\n"


def fp_header(uuid, name):
    out = [dochead("FOOTPRINT", uuid, name)]
    for k in range(4):
        y = -150.0 + k * 100.0
        out.append(pad(uuid + "e%d" % (k + 1), 12, str(k + 1), 0, y, 60.0, 60.0,
                       "ELLIPSE", hole={"holeType": "ROUND", "width": 32, "height": 32}))
    out.append(poly(uuid + "e10", 3, [-50.0, -200.0, "L", 50.0, -200.0, 50.0, 200.0, -50.0, 200.0], 8))
    out.append(attr(uuid + "e11", 3, "", "Footprint", name))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
def build_v3_project(root):
    os.makedirs(os.path.join(root, "pcb"), exist_ok=True)
    os.makedirs(os.path.join(root, "FOOTPRINT"), exist_ok=True)

    fp1 = "f0000000000000000000000000000001"      # 0402
    fp2 = "f0000000000000000000000000000002"      # SOT-23
    fp3 = "f0000000000000000000000000000003"      # 4-pin header
    open(os.path.join(root, "FOOTPRINT", fp1 + ".efoo"), "w", encoding="utf-8").write(
        fp_0402(fp1, "R0402", 23.6, 25.6, 19.685))
    open(os.path.join(root, "FOOTPRINT", fp2 + ".efoo"), "w", encoding="utf-8").write(
        fp_sot23(fp2, "SOT-23-3"))
    open(os.path.join(root, "FOOTPRINT", fp3 + ".efoo"), "w", encoding="utf-8").write(
        fp_header(fp3, "HDR-2.54-4P"))

    pcb_uuid = "d0000000000000000000000000000001"
    lines = []
    lines.append(dochead("PCB", pcb_uuid, "V3Sample"))
    lines.append(rec("META", "META", {
        "board": "", "parent": "", "title": "V3Sample", "source": "",
        "zIndex": None,
    }))
    lines.append(rec("BOARD", "BOARD", {"path": outline()}))

    # --- components ------------------------------------------------------
    # R1/R2 top, C1 bottom (exercises the mirror transform), J1 through-hole
    comps = [
        ("c1", 1, 300, 350, 0, "R1", fp1, "dev_resistor"),
        ("c2", 1, 550, 350, 90, "R2", fp1, "dev_resistor"),
        ("c3", 1, 800, 500, 180, "U1", fp2, "dev_sot"),
        ("c4", 2, 1100, 500, 0, "C1", fp1, "dev_resistor"),      # bottom side
        ("c5", 1, 1400, 250, 0, "J1", fp3, "dev_header"),
    ]
    for cid, layer, x, y, ang, des, fpuuid, dev in comps:
        lines.append(component(cid, layer, x, y, ang, fpuuid))
        lines.append(attr(cid + "a1", 3, cid, "Designator", des, x, y + 40))
        lines.append(attr(cid + "a2", 3, cid, "Device", dev))
        lines.append(attr(cid + "a3", 3, cid, "Footprint", fpuuid))

    # append-only log: an older ticket for R1's designator followed by the
    # winning one, plus a deletion tombstone that must be ignored entirely
    lines.append('{"type":"ATTR","ticket":1,"id":"c1a1"}||'
                 '{"parentId":"c1","layerId":3,"key":"Designator","value":"R99"}|')
    lines.append('{"type":"ATTR","ticket":2,"id":"c1a1"}||'
                 '{"parentId":"c1","layerId":3,"key":"Designator","value":"R1"}|')
    lines.append('{"type":"POLY","ticket":1,"id":"dead"}||'
                 '{"layerId":3,"width":10,"path":[0,0,"L",1,1],"polyType":"NORMAL"}|')
    lines.append('{"type":"POLY","ticket":9,"id":"dead"}|||')

    # --- copper ----------------------------------------------------------
    lines.append(line("t1", 1, 300 - 19.685, 350, 250, 350, 10, "N1"))
    lines.append(line("t2", 1, 300 + 19.685, 350, 400, 350, 10, "N2"))
    lines.append(line("t3", 1, 550, 350 - 19.685, 550, 250, 10, "N1"))
    lines.append(line("t4", 2, 550, 350 + 19.685, 550, 430, 10, "N2"))
    lines.append(line("t5", 1, 800 - 39.37, 500 - 39.37, 700, 400, 8, "N3"))
    lines.append(line("t6", 1, 800 + 39.37, 500, 950, 500, 8, "N4"))
    lines.append(line("t7", 2, 1100 - 19.685, 500, 1000, 500, 8, "N5"))
    lines.append(arc("t8", 1, 500, 600, 600, 600, 90, 10))
    lines.append(via("v1", 650, 300, 16, 32))
    lines.append(via("v2", 950, 400, 16, 32))

    # --- silk ------------------------------------------------------------
    lines.append(line("s1", 3, 150, 150, 1450, 150, 8))
    lines.append(poly("s2", 3, [200, 700, "L", 700, 700, 700, 800, 200, 800], 8))
    lines.append(line("s3", 4, 150, 850, 1450, 850, 8))

    # --- copper areas ----------------------------------------------------
    lines.append(fill("f1", 1, [[[100, 500, "L", 400, 500, 400, 900, 100, 900]]]))
    lines.append(rec("POUR", "p1", {
        "layerId": 2, "netName": "GND", "width": 8, "name": "POUR1", "order": 0,
        "path": outline(), "pourType": {"type": "SOLID", "fineness": 8},
        "keepIsland": False, "groupId": 0, "locked": False, "zIndex": -1,
    }))

    # --- tolerated noise: unknown primitive types -------------------------
    lines.append('{"type":"SOMETHING_NEW","ticket":5,"id":"x1"}||{"foo":1}|')
    lines.append('{"type":"FONT","ticket":6,"id":"f1x"}||{"family":"default"}|')

    with open(os.path.join(root, "pcb", "V3Sample.epcb2"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    index = {
        "schematics": {},
        "boards": {"V3SampleBoard": {"schematic": "", "pcb": pcb_uuid}},
        "pcbs": {pcb_uuid: "V3Sample"},
        "footprints": {
            fp1: {"title": "R0402", "type": 4},
            fp2: {"title": "SOT-23-3", "type": 4},
            fp3: {"title": "HDR-2.54-4P", "type": 4},
        },
        "devices": {
            "dev_resistor": {"title": "10k", "attributes": {
                "Name": "10k", "Designator": "R?", "Footprint": fp1,
                "Supplier Part": "C25804", "Manufacturer Part": "0603WAF1002T5E",
                "Description": "10kΩ 1% 0603"}},
            "dev_sot": {"title": "AO3400", "attributes": {
                "Name": "AO3400", "Designator": "Q?", "Footprint": fp2,
                "Supplier Part": "C20917", "Manufacturer Part": "AO3400A"}},
            "dev_header": {"title": "2.54-4P", "attributes": {
                "Name": "2.54-4P", "Designator": "J?", "Footprint": fp3,
                "Supplier Part": "C124375"}},
        },
    }
    with open(os.path.join(root, "V3Sample.eprj3"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
def build_eprj_sqlite(path):
    """Legacy .eprj / .eprj2: one SQLite database holding the document texts."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("CREATE TABLE project (id INTEGER PRIMARY KEY, type TEXT, data TEXT)")
    cur.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, docType TEXT, "
                "title TEXT, data TEXT)")

    uuid = "e0000000000000000000000000000001"
    fp = "e0000000000000000000000000000002"
    # v2 dialect: one JSON array per line
    fp_lines = [
        json.dumps(["DOCTYPE", "FOOTPRINT", "1.8"]),
        json.dumps(["PAD", "p1", 0, "", 1, "1", -19.685, 0, 0, None,
                    ["RECT", 23.6, 25.6, 0], [], 0, 0.002, 0, 1, 0, 2, 2, 0, 0, 0]),
        json.dumps(["PAD", "p2", 0, "", 1, "2", 19.685, 0, 0, None,
                    ["RECT", 23.6, 25.6, 0], [], 0, 0.002, 0, 1, 0, 2, 2, 0, 0, 0]),
        json.dumps(["ATTR", "f1", 0, "", 3, None, None, "Footprint", fp, 0, 0,
                    "default", 45, 6, 0, 0, 3, 0, 0, 0, 0, 0]),
    ]
    pcb_lines = [
        json.dumps(["DOCTYPE", "PCB", "1.8"]),
        json.dumps(["POLY", "b1", 0, "", 11, 10, [0, 0, "L", 800, 0, 800, 600, 0, 600], 1]),
        json.dumps(["COMPONENT", "c1", 0, 1, 200, 300, 0,
                    {"Origin Footprint": "R0402"}, 0, None]),
        json.dumps(["ATTR", "c1a1", 0, "c1", 3, 200, 340, "Designator", "R1", 0, 1,
                    "default", 45, 6, 0, 0, 3, 0, 0, 0, 0, 0]),
        json.dumps(["ATTR", "c1a2", 0, "c1", 3, None, None, "Footprint", fp, 0, 0,
                    "default", 45, 6, 0, 0, 3, 0, 0, 0, 0, 0]),
        json.dumps(["LINE", "t1", 0, "N1", 1, 180.315, 300, 100, 300, 10, 0]),
        json.dumps(["LINE", "t2", 0, "N2", 2, 219.685, 300, 300, 300, 10, 0]),
        json.dumps(["VIA", "v1", 0, "N1", "", 400, 300, 16, 32, 0, None, None, 0, []]),
    ]
    cur.execute("INSERT INTO project (id, type, data) VALUES (?,?,?)",
                (1, "PCBSETTINGS", json.dumps({"pcbs": {uuid: "SqliteSample"},
                                               "boards": {"SqliteSampleBoard":
                                                          {"pcb": uuid}},
                                               "footprints": {fp: {"title": "R0402"}},
                                               "devices": {}})))
    cur.execute("INSERT INTO documents (id, docType, title, data) VALUES (?,?,?,?)",
                (1, "FOOTPRINT", "R0402", "\n".join(fp_lines)))
    cur.execute("INSERT INTO documents (id, docType, title, data) VALUES (?,?,?,?)",
                (2, "PCB", "SqliteSample", "\n".join(pcb_lines)))
    con.commit()
    con.close()


# --------------------------------------------------------------------------
def main():
    v3 = os.path.join(HERE, "v3_sample")
    build_v3_project(v3)
    build_eprj_sqlite(os.path.join(HERE, "sqlite_sample.eprj"))
    print("wrote", v3)
    print("wrote", os.path.join(HERE, "sqlite_sample.eprj"))


if __name__ == "__main__":
    main()
