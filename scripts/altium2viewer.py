#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口（旧名字）。等价于：python3 pcb2viewer.py --format altium <输入> [输出目录]

新代码请用 pcb2viewer.py（支持 Altium / 嘉立创EDA / EasyEDA / KiCad / Gerber）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

import pcb2viewer   # noqa: E402


if __name__ == "__main__":
    if "--format" not in sys.argv:
        sys.argv[1:1] = ["--format", "altium"]
    pcb2viewer.main()
