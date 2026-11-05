---
name: pcb-smt-viewer
description: 把各种 PCB 设计文件（Altium .PcbDoc、嘉立创EDA/EasyEDA 标准版与专业版、KiCad .kicad_pcb、以及任意 EDA 导出的 Gerber+坐标文件）一键转成单文件「SMT 贴片定位图」网页：板子图形 + 位号搜索定位 + 贴片勾选清单 + 坐标/角度/封装。当用户要做贴片对照图、焊接定位图、元件位置/分布图、手工贴片辅助页，或把 PCB 做成可搜索的位号清单时使用。
whenToUse: 用户给出 PCB 设计文件（Altium .PcbDoc/.zip、嘉立创EDA .json/.epro/.eprj3、KiCad .kicad_pcb、或 Gerber 压缩包+坐标文件）并要求生成贴片/焊接用的元器件位置网页或清单；或要求把已有 PCB 做成"哪个位号在板子哪里"的可视化页面。
---

# PCB → SMT 贴片定位图（多格式）

> 源码 / 更新 / issue：<https://github.com/PaiJiang153/pcb-smt-viewer>
> 安装：`git clone <仓库> && ./pcb-smt-viewer/install.sh`

把 PCB 设计文件解析成 `pcb_data.json`，再打包成一个**单文件 HTML**（数据/样式/脚本全内联，
约 0.9 MB，双击即可离线打开）。网页里能按位号/值/封装搜索并自动缩放高亮到元件，切换顶层/底层
（底层为镜像视图），标记已贴并统计进度，导出 CSV。

所有格式最终都归一到同一套数据 + 同一个网页，所以功能完全一致。

## 支持哪些文件

| 后端 id | 认什么文件 | 元件/位号来自 | 板图来自 |
| --- | --- | --- | --- |
| `altium` | `.PcbDoc`（+ 同名 `.SchDoc`）、工程 `.zip` | Components6 / Pads6 | BoardRegions / Tracks6 / Polygons6 |
| `kicad` | `.kicad_pcb`、工程 `.zip` | footprint + pad | Edge.Cuts / segment / zone |
| `easyeda_pro` | 嘉立创EDA 专业版 `.epro`、工程 `.zip`、`.eprj3` 文件夹、`.eprj` | COMPONENT / PAD | BOARD / LINE / POUR |
| `easyeda_std` | 嘉立创EDA 标准版 PCB `.json`、含 json 的 `.zip` | FOOTPRINT / PAD | outline / TRACK / COPPERAREA |
| `gerber` | Gerber(RS-274X) + Excellon 钻孔 + **坐标文件 CSV** 的目录/压缩包 | 坐标文件（Pick&Place） | Gerber 轮廓层 / 铜层 |

**任何其它 EDA**（PADS、Allegro、Eagle、立创下单页…）都能走 `gerber` 后端：
导出 Gerber + 坐标文件即可 —— 这也正是嘉立创 SMT 下单要求的那套文件。

## 一条命令

```bash
python3 scripts/pcb2viewer.py <输入> [输出目录]
```

- `<输入>`：具体格式文件、工程目录或 `.zip`（自动解压到输出目录的 `source/`）
- 自动识别格式；认不出来时用 `--format <id>`，或用 `--list-formats` 看可用后端
- 输出目录默认 `<输入名>_smt/`，里面得到：
  - `<板名>_贴片定位图.html` ← **交付给用户的文件**
  - `pcb_data.json`（中间数据，可再喂给 `build_html.py` 换样式重打包）
  - `<板名>_贴片定位图.README.md`（板尺寸 + 元件清单表）
  - `source/…`（输入是 zip 时解出来的原始工程）

成功后把**识别到的格式 / 板尺寸 / 元件数（顶层·底层）/ 网页绝对路径**告诉用户，并提示「双击打开」。
脚本打印的 ⚠️ 警告（例如"没有坐标文件，图上没有位号"）要如实转告。

### 依赖

只需要 **Python 3.7+**；Altium 后端额外需要 `olefile`（读 OLE 复合文档）：

```bash
python3 -c "import olefile" || python3 -m pip install --user olefile
```

## 各格式怎么拿到输入文件（若用户不知道给什么）

| 用户的 EDA | 让用户这样导出 |
| --- | --- |
| 嘉立创EDA 专业版 | 文件 → 导出 → **EasyEDA Pro 工程**（`.epro`）；或 文件 → 导出 → **Altium**（`.zip`，走 `altium` 后端）；离线客户端可直接给 `.eprj`/`.eprj3` 工程 |
| 嘉立创EDA 标准版 | PCB 文档里 文件 → **另存为/导出 → EasyEDA 文件 (.json)** |
| KiCad | 直接给工程里的 `.kicad_pcb`（或整个工程 zip） |
| Altium Designer | 直接给 `.PcbDoc`（原理图 `.SchDoc` 同名放着，型号/值才完整）；或整个工程 zip |
| 其它（PADS / Allegro / Eagle / 立创下单…） | 导出 **Gerber**（含板框/铜层/丝印）+ **钻孔** + **坐标文件**（Pick&Place CSV），打包成 zip 或放一个目录 |

Gerber 模式的坐标文件：嘉立创下单页的**坐标文件**（CSV，列名一般是
`Designator, Layer, Mid X, Mid Y, Rotation`）、Altium 的 *Pick and Place*、KiCad 的 `.pos` 都行。
放同目录一起给，脚本会自动找；也可以用 `--centroid 文件.csv` 指定。

```bash
python3 scripts/pcb2viewer.py --format gerber 我的板_gerber.zip --centroid 坐标文件.csv
```

> Gerber 本身没有元件信息：**没有坐标文件时**网页仍会画出板子/铜皮/丝印，但没有位号（会给出警告）。
> 想按位号定位必须提供坐标文件。

## 逐个后端调试 / 只重新打包网页

```bash
python3 scripts/test_backend.py <format> <输入> [--build <目录>] [--verify]
python3 scripts/build_html.py pcb_data.json out.html --title "板名"
```

`test_backend.py` 打印解析统计（板尺寸/元件数/焊盘/走线/过孔/丝印）、前几个元件和全部警告；
`--build` 顺带生成网页，`--verify` 再用无头浏览器跑一遍检查。

## 可选：用无头浏览器自检（推荐）

```bash
node scripts/verify.mjs <板名_贴片定位图.html> [截图目录]
```

检查 `window.__SMT_READY`、0 个 console/page 错误、界面元件数与数据一致、双面切换、搜索高亮，
并输出 `01_top.png / 02_bottom.png / 03_search.png / 04_zoom.png`，用 `read_image` 看一眼版式。
Playwright 缺失时脚本会打印安装命令（沙箱里 `~/.npm`、`~/.cache` 只读时，把 cache 和
`PLAYWRIGHT_BROWSERS_PATH` 指到工作目录，并用 `PLAYWRIGHT_MODULE_DIR` 指向安装目录）。

## 网页里有什么（用户视角）

| 功能 | 说明 |
| --- | --- |
| 顶层/底层切换 | 底层是镜像翻转，和把板翻过来贴片的方向一致；「对侧透视」可半透明看另一面 |
| 搜索 | 位号（R19）、值（22uF）、封装（0603）都能搜；自动切到对应面并缩放高亮 |
| 元件信息卡 | 封装预览、值/型号、焊盘数、板内坐标（从板左下角起）、原始坐标、旋转角 |
| 1 脚标记 | 青色小点标在 1 号焊盘，用来确认方向 |
| 贴片清单 | 空格/勾选框标记已贴，自动跳下一颗未贴；进度存在浏览器 localStorage |
| 导出 | 导出 CSV（位号/层/值/型号/封装/坐标/角度/已贴） |
| 快捷键 | `/` 搜索、`↑↓` 切换、`T`/`B` 层面、`F` 适配、`+`/`-` 缩放、`空格` 标记、`Esc` 取消 |

## pcb_data.json 结构（所有后端统一的契约）

```text
title      板名（网页标题、localStorage key、CSV 文件名都用它）
bbox       [x0,y0,x1,y1] 板框包围盒，单位 mm，Y 轴向上
outline    [[[x,y],...]]  板框轮廓（可多条，含挖槽）
components [{d 位号, fp 封装, side top/bottom, x,y 元件原点(mm), rot 旋转角,
             val 库名/值, comment 型号, value 值, mpn 料号,
             bb 焊盘包围盒, np 焊盘数, pin1 1脚坐标}]
pads       [{x,y,w,h,s 形状(1圆/2矩形/3八角/4椭圆),r 角度,l TOP|BOTTOM|MULTILAYER,c 元件序号,p 焊盘号,hole 孔径}]
tracks     [[x1,y1,x2,y2,线宽,layer]]   layer: 1顶层 32底层 2/3内层
filltracks 覆铜扫描线段（Altium 专用，同 tracks；其它格式为空）
arcs       [[cx,cy,r,起始角,终止角,线宽,layer]]
silk       [[x1,y1,x2,y2,线宽,layer,元件序号]]  layer: 33顶层 34底层
silkarcs   丝印圆弧（同上）
vias       [[x,y,外径,孔径]]
polygons   覆铜/铜皮轮廓 [{layer:"TOP"|"BOTTOM", contours:[[[x,y],...]]}]
meta       {stats 统计, warnings 解析警告, source 源文件路径}
```

单位一律 **mm**、**Y 轴向上**；各后端内部坐标系（Altium 1/10000 mil、KiCad Y 向下、
EasyEDA 0.01 mil 等）在解析时已换算。

## 定制网页（`scripts/assets/`）

- `viewer.css`：配色（`:root` 变量 + js 里的 `COL`）、布局、响应式断点
- `viewer.js`：渲染与交互。常用改动点：
  - `COL` 对象：顶层/底层铜箔、焊盘、丝印、选中色
  - `select()` 里 `clamp(Math.min(W, H) / 19, 12, 140)`：点选元件时的自动缩放程度
  - `drawOverlays()` 里的 `minZ` / `area < 1.0` 阈值：小元件位号标签在什么缩放下显示
  - `FONT_LABEL`、`#legend`（在 `viewer.html`）里写死的图例文字
- `viewer.html`：页面骨架，`/*__CSS__*/`、`/*__DATA__*/`、`/*__JS__*/` 是构建时注入的占位符
- 改完 `assets/` 后重跑 `build_html.py` 即可（不必重新解析原始 PCB）

## 已知限制（如实告知用户）

- **Gerber 后端**：没有元件信息，位号完全依赖坐标文件；光圈宏（`%AM%`）按外接矩形近似；
  负片/内层/阻焊层不渲染；拼板（多块板）会当成一整块。
- **嘉立创EDA 标准版**：`COPPERAREA` 直接按轮廓画实心，不还原网格/热焊盘；文字类图元不渲染。
- **嘉立创EDA 专业版**：旧版 `.eprj`（SQLite）支持程度取决于内部表结构，失败时提示改用 `.epro`
  或「导出 → Altium」。
- **KiCad**：自定义焊盘（trapezoid/custom）按矩形近似；焊盘/丝印文字不渲染（网页自己画位号）。
- **Altium**：自定义形状焊盘按矩形近似；内层铜只做淡显。
- **通用**：4 层以上板子主要看顶/底两面（贴片通常只看外层）；板框里的细长毛刺会自动剔除
  （`--no-spur-trim` 可关闭）。板子很大或走线极多时 HTML 可能到 2–3 MB，仍可正常打开。

## 排错表

| 现象 | 处理 |
| --- | --- |
| `认不出这个文件是什么格式` | 用 `--format <id>` 指定；其它 EDA 请导出 Gerber + 坐标文件走 `gerber` |
| `ModuleNotFoundError: olefile` | `python3 -m pip install --user olefile` |
| 嘉立创专业版解析失败 | 优先「导出 → EasyEDA Pro 工程(.epro)」，或「导出 → Altium」后走 `altium` 后端 |
| 网页上没有位号 | 没有坐标文件（Gerber 模式），或给的文件是封装库/原理图 |
| 位号位置整体偏移/镜像 | 坐标文件的 X/Y 单位或镜像约定不同：核对导出选项（mm、正面朝上） |
| 板框形状怪 / 有长毛刺 | `--no-spur-trim` 对比一次；仍不对说明板框本身如此 |
| 网页打开一片空白 | 用 `node scripts/verify.mjs` 看 console 错误 |
| 元件跑到板外（有警告） | 坐标文件与板框坐标系不一致（常见于手改的坐标文件） |

## 目录

```text
SKILL.md
scripts/pcb2viewer.py        统一入口（自动识别格式）
scripts/altium2viewer.py     旧名字兼容入口（= --format altium）
scripts/parsers/common.py    统一数据契约 + 公共几何/校验/finalize
scripts/parsers/altium.py    Altium PcbDoc 后端
scripts/parsers/kicad.py     KiCad .kicad_pcb 后端
scripts/parsers/easyeda_pro.py  嘉立创EDA 专业版后端
scripts/parsers/easyeda_std.py  嘉立创EDA 标准版后端
scripts/parsers/gerber.py       Gerber + 钻孔 + 坐标文件后端
scripts/build_html.py        pcb_data.json + assets → 单文件 HTML（含 README）
scripts/test_backend.py      单后端调试/自检
scripts/verify.mjs           Playwright 自检（可选）
scripts/assets/              viewer.html / viewer.css / viewer.js
tests/fixtures/<format>/     各后端的测试样例
```

## 新增一种格式（开发指引）

1. 在 `scripts/parsers/` 新建 `<name>.py`，实现：

   ```python
   FORMAT = "myfmt"
   LABEL  = "我的格式（.xxx）"
   def sniff(root) -> bool          # root 是文件或目录；要快、不能抛异常
   def parse(root, out_dir=None, **opts) -> dict
   ```

2. `parse` 返回**原始** canonical dict（只需填你有的字段），然后：

   ```python
   from . import common
   data = common.finalize(data)     # 补 bbox / 元件焊盘框 / pin1 / 统计 / 警告
   ```

3. 把 `"myfmt"` 加到 `scripts/pcb2viewer.py` 的 `BACKEND_ORDER`（越具体越靠前）。
4. 放一份真实样例到 `tests/fixtures/myfmt/`，用
   `python3 scripts/test_backend.py myfmt tests/fixtures/myfmt/xxx --build --verify` 验证。
5. 在 SKILL.md 的支持表和目录里补一行。
