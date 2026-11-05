> ⚠️ **本仓库不包含下面这些 gerber / 坐标文件**（上游为 GPL 许可证）。
> 需要跑回归时先执行 `./tests/fetch_fixtures.sh` 自动下载，来源见下表。
> 随仓库提供、可直接使用的合成夹具见 `kicad_x2/`。

# Gerber / Excellon / 坐标文件 测试夹具来源

这些文件**只用于测试** `scripts/parsers/gerber.py`，不是本项目代码的一部分。
各文件版权归原作者/原项目所有，按原许可证使用；若再分发请保留对应许可证。

## 1. `protel_pnp/` —— 真实的 2 层板（Protel 2005 导出）+ Pick&Place CSV

| 项 | 内容 |
| --- | --- |
| 来源 | gerbv 项目的示例目录 `example/protel-pnp/`（作者捐赠给 gerbv，donor: Karl Schmittschneider, 2005-04-19） |
| URL | https://github.com/gerbv/gerbv/tree/develop/example/protel-pnp |
| 原始文件 | `SE_SG_IF_V2.GTL/.GBL/.GTO/.GBO/.GTS/.GBS/.GM1/.GM2/.GD1/.GG1/.DRL` + `Pick_Place_for_SE_SG_IF_V2.csv` + `README.txt` |
| 许可证 | GPL-2.0（gerbv 项目） |
| 内容 | 105 × 105 mm、2 层；顶层铜/底层铜、丝印、阻焊、机械层（GM1 里有板框矩形 + 槽）、钻孔（**.DRL 是旧版 Protel 二进制格式**）、284 行坐标文件（mil，带 Designator/Footprint/Mid X/Mid Y/Ref X/Ref Y/Pad X/Pad Y/Layer/Rotation/Comment 表头） |
| 修改 | 无（原样拷贝） |
| 用途 | 主夹具：验证 RS-274X（`%FSLAX23Y23%` + `%MOIN%`）、R/O 焊盘、G02/G03 圆弧、板框拼接、mil 坐标文件、二进制钻孔的降级警告。CSV 里 `HEAD` 一行本就在板框外（真实数据如此），用来测试「元件在板框外」警告。 |

## 2. `geda_hellboard/` —— 真实的 2 层板，**没有**坐标文件

| 项 | 内容 |
| --- | --- |
| 来源 | gerbv 项目的示例目录 `example/hellboard/`（gEDA/PCB 1.99v 生成，DJ Delorie 做来测 thermal 的板子） |
| URL | https://github.com/gerbv/gerbv/tree/develop/example/hellboard |
| 原始文件 | `hellboard.front.gbr`（顶层铜）、`hellboard.back.gbr`（底层铜）、`hellboard.fab.gbr`（外形/铣切层）、`hellboard.plated-drill.cnc`（**Excellon 文本钻孔**） |
| 许可证 | GPL-2.0（gerbv 项目） |
| 修改 | 只取了上面 4 个文件；原目录里 `group1.gbr`/`group2.gbr` 是 front/back 的重复导出，已去掉避免重复渲染 |
| 用途 | 验证：无坐标文件时的降级（警告 + 只画板子/铜箔/钻孔）、gEDA 风格 `%LNFRONT*%` 层名、G36 覆铜区域、Excellon `INCH,TZ` + 2.4 格式钻孔、只有机械层当板框。 |

## 3. `geda_led_xy/` —— 真实的丝印 + `.xy` 坐标文件（一对）

| 项 | 内容 |
| --- | --- |
| 来源 | gerbv 项目的示例目录 `example/pick-and-place/` |
| URL | https://github.com/gerbv/gerbv/tree/develop/example/pick-and-place |
| 文件 | `LED.frontsilk.gbr`（只有顶层丝印的板子）、`LED.xy`（gEDA `pcb -x xy` 格式：`# RefDes, Description, Value, X, Y, rotation, top/bottom`，单位 mil，注释里写明 "X,Y in mils"） |
| 许可证 | GPL-2.0（gerbv 项目） |
| 修改 | 无 |
| 用途 | 验证：gEDA `.xy` 表头/单位提示、只有丝印层时用丝印包围盒当板框、46 个元件落位到丝印元件框上。 |

## 4. `kicad_x2/` —— **合成**的 KiCad X2 文件集（由脚本生成）

| 项 | 内容 |
| --- | --- |
| 来源 | **本项目合成**，不是真实工程。生成脚本：[`kicad_x2/make_fixture.py`](kicad_x2/make_fixture.py) |
| 生成方法 | `python3 tests/fixtures/gerber/kicad_x2/make_fixture.py` |
| 内容 | 40 × 30 mm、2 层：`x2demo-F_Cu.gbr` / `-B_Cu.gbr` / `-F_Silkscreen.gbr` / `-B_Silkscreen.gbr` / `-Edge_Cuts.gbr` / `x2demo.drl` / `x2demo-pos.csv`；带 `%TF.FileFunction%`、`%TF.GenerationSoftware,KiCad…%`、`%FSLAX46Y46%`、`%MOMM%` 等 X2 头；底层铜用 G36 区域做覆铜；4 个 SMD 元件（R1/R2/C1/U1）+ 2 个 THT 焊盘 + 4 个 NPTH 安装孔 |
| 许可证 | 本项目测试数据（CC0 / 与 skill 相同许可证均可） |
| 用途 | 验证：KiCad 默认命名（`-F_Cu`/`-Edge_Cuts`）、`%TF.FileFunction%` 判层、`%FSLAX46Y46%` + mm、G36 覆铜、Excellon 小数坐标 + `;FORMAT=` 注释、KiCad `.pos` 表头（Ref/Val/Package/PosX/PosY/Rot/Side）、THT/NPTH 钻孔落到焊盘上。 |

## 5. `edge/` —— 各种边角格式的真实小文件

全部来自 **gerbv 的回归测试输入**（https://github.com/gerbv/gerbv/tree/develop/test/inputs ，GPL-2.0）
和 **KiCad 的 QA 数据**（https://github.com/KiCad/kicad-source-mirror/tree/master/qa/data/gerbview ，GPL-3.0+）。
这些是单个文件（不是完整板子），用于验证解析器不崩、指令覆盖：

| 文件 | 验证点 |
| --- | --- |
| `test-aperture-circle-1.gbx` | C 孔径（含孔径内孔）画线/画弧 |
| `test-aperture-polygon-flash-1.gbx` | P 多边形孔径闪光（3/6/10 边形） |
| `test-aperture-macro-line21-rotation.gbx` | 孔径宏（AM）定义 + 宏孔径闪光 → 跳过并警告 |
| `test-circular-interpolation-1.gbx`、`-mq-ccw.gbx` | G74/G75 圆弧、整圆 |
| `test-drill-leading-zero-1.exc`、`-trailing-zero-1.exc` | Excellon 前导/后导零抑制 |
| `test-drill-slot-drilled-g85.exc` | G85 槽孔（一行两组坐标） |
| `test-drill-arc-routing.exc`、`-routed-circle.exc`、`-route-milling.exc` | 铣槽/圆弧走线（M15/M16、G32/G33、模态坐标） |
| `test-drill-repeat-1.exc` | R 重复孔展开 |
| `test-gerber-x2-attributes.gbx` | X2 属性（TF/TA/TO/TD）写法 |
| `test-image-polarity-1.gbx` | `%IPNEG%` 负片（不支持 → 忽略 + 警告） |
| `test-layer-knockout-1.gbx`、`test-polygon-fill-1.gbx` | G36 区域 / 覆铜 / 挖空 |
| `test-merge-a.gbx` | 常规 RS-274X |
| `clear_polarity_plane.gbr`（KiCad QA） | 真实 KiCad 的 `%TF.FileFunction,Copper,L1,Top%`（纯靠 FileFunction 判层） |
| `mixed_coordinates.gbr`、`fractional_scale.gbr`、`step_and_repeat_block.gbr`、`aperture_macro_truncated.gbr`（KiCad QA） | 非法/截断/缩放/step&repeat 输入不崩 |
| `repeat_holes.drl`（KiCad QA） | KiCad Excellon（METRIC,TZ + R 重复孔） |

## 复现测试

```bash
cd ~/.agents/skills/pcb-smt-viewer
python3 scripts/test_backend.py gerber tests/fixtures/gerber/protel_pnp       --build
python3 scripts/test_backend.py gerber tests/fixtures/gerber/kicad_x2        --build
python3 scripts/test_backend.py gerber tests/fixtures/gerber/geda_led_xy     --build
python3 scripts/test_backend.py gerber tests/fixtures/gerber/geda_hellboard  --build
```

> `geda_hellboard/` 没有元件，`verify.mjs` 会因「元件数为 0」判失败——这是预期行为，
> 该夹具只验证「没有坐标文件时仍能画出板子」。
