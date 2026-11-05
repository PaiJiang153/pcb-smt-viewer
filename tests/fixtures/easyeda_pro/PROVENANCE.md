> ✅ 本仓库内置：`real/Daaga1.3V.epro`（MIT）、`v3_sample/`、`sqlite_sample.eprj`（合成）。
> 下面「未随仓库分发」的真实工程可用 `./tests/fetch_fixtures.sh` 下载后复现。

# easyeda_pro 测试数据来源与许可

后端：`scripts/parsers/easyeda_pro.py`（嘉立创EDA 专业版 / EasyEDA Pro）
验证命令（在 skill 根目录执行）：

```bash
python3 scripts/test_backend.py easyeda_pro tests/fixtures/easyeda_pro/real/Daaga1.3V.epro --build --verify
python3 scripts/test_backend.py easyeda_pro tests/fixtures/easyeda_pro/v3_sample --build
python3 scripts/test_backend.py easyeda_pro tests/fixtures/easyeda_pro/sqlite_sample.eprj --build
```

## 1. 真实工程（随仓库提供）

| 文件 | 来源 | 许可 | 说明 |
| --- | --- | --- | --- |
| `real/Daaga1.3V.epro` | <https://github.com/enkhbold470/epro2kicad> → `examples/Daaga1.3V.epro` | **MIT**（仓库 LICENSE） | EasyEDA Pro **2.x** 归档：`project.json` + `PCB/<uuid>.epcb` + `FOOTPRINT/*.efoo`。122.2 × 113.5 mm，45 元件（顶 37 / 底 8），181 焊盘，657 走线，411 过孔 |

原始文件未做任何修改（500 787 字节，sha256 见下）。

```
20d7e191a50dd32cb00ad2403ea731fa00e9673cd5fbdf488c840be931efc7a0  real/Daaga1.3V.epro
```
（用 `sha256sum real/Daaga1.3V.epro` 复核）

## 2. 真实工程（仅本地验证，**未随仓库分发**）

这两个工程在开发时用于交叉验证，但所属仓库没有声明许可证，因此不放进 fixtures，
只记录 URL 供复现（下载后可直接用上面的命令跑）：

| 工程 | 来源 URL | 许可 | 结果 |
| --- | --- | --- | --- |
| ESP32-S3 Breakout | <https://github.com/MollyWat3r/ESP32-S3-WROOM-1-Breakout-Board-for-EasyEDA-Pro> → `ESP32-S3-WROOM-1 Breakout.epro` | 无（未声明） | 25.4 × 65.5 mm，15 元件，133 焊盘，52 过孔，无警告 |
| Bio-impedance sensing board | <https://github.com/MX-Liu/Bio-impedance-sensing-board> → `Bio-impedance-PCB-project.epro` | 无（未声明） | 47.0 × 35.3 mm，68 元件，309 焊盘，29 圆弧，144 过孔，无警告 |

## 3. 合成数据（随仓库提供，**不是真实工程**）

`v3_sample/`、`sqlite_sample.eprj` 由 `make_v3_fixture.py` 生成，**完全按官方格式文档手写**，
不含任何第三方设计数据，可自由使用。用途：覆盖真实样例里没有的输入形态与边界情况。

| 路径 | 形态 | 覆盖点 |
| --- | --- | --- |
| `v3_sample/` | `.eprj3` **文件夹工程**（`pcb/V3Sample.epcb2` + `FOOTPRINT/*.efoo` + `V3Sample.eprj3` 索引） | 3.x `{"type":..}||{..}|` 记录方言、`DOCHEAD` 分文档、`BOARD` 板框图元、ticket 去重（同一 id 两行取大）、删除墓碑（空载荷整行丢弃）、未知图元类型容错、底层元件镜像、内层走线、圆弧、覆铜 `POUR`/`FILL`、丝印 `POLY`、4 个元件取值 40 × 25.4 mm |
| `sqlite_sample.eprj` | 旧版 **SQLite** 工程（`.eprj`） | sqlite3 只读打开、按列扫描出文档文本、2.x 位置数组方言、封装按名字回查 |

生成方式（可重复生成）：

```bash
cd tests/fixtures/easyeda_pro && python3 make_v3_fixture.py
```

坐标以 **mil、Y 向下**书写（真实文件就是这样），后端会转成 mm / Y 向上。

## 4. 格式规范来源

- 官方格式文档（图元字段、示例、`FORMATLOG`）：
  <https://github.com/easyeda/easyeda-format-skill> —
  `documents/PCB.md`、`primitives/PCB/{board,pad,line,arc,via,poly,fill,pour,poured,component,attr,meta,region}.md`、
  `primitives/REFERENCE/{e-layer-code,ty-axis-direction,t-pcb-single-polygon,t-pcb-complex-polygon}.md`
- `.eprj3` 文件夹工程格式总览：<https://github.com/easyeda/easyeda-eprj3-format>
- 官方在线文档：<https://prodocs.lceda.cn/cn/format/index/> ·
  <https://prodocs.easyeda.com/en/format/index/>
- 参考实现（记录去重、坐标缩放、图层映射、封装展开、多边形解码）：
  KiCad `pcbnew/pcb_io/easyedapro/`（GPL-2.0）与
  <https://dev-docs.kicad.org/en/import-formats/easyeda/>

## 5. 已知缺口

- 覆铜结果 `POURED` 不画（数据量极大），只画覆铜边框 `POUR` / `FILL`；会给出提示。
- `IMAGE`、`DIMENSION`、`STRING`、`TEARDROP`、`PANELIZE` 图元暂不输出。
- 旧 `.eprj` / `.eprj2` 的 SQLite 表结构没有公开文档，采用「扫描所有文本列找记录」的通用办法；
  已在合成样例上验证，但**没有真实 `.eprj` 样例**可测。
