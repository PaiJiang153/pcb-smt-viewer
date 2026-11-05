> ⚠️ **本仓库只内置 `synthetic_footprint_docType4.json`（本项目合成）**。
> 下面这些真实工程（GPL-3.0 / 无许可证）不随仓库分发，跑
> `./tests/fetch_fixtures.sh` 可下载到本地用于回归测试。

# EasyEDA 标准版 PCB `.json` 测试数据来源

解析器：`scripts/parsers/easyeda_std.py`（`FORMAT = "easyeda_std"`）。
自检：`python3 scripts/test_backend.py easyeda_std tests/fixtures/easyeda_std/<file>.json --build`

> 这些文件是**测试夹具**，只用于验证解析结果（板框 / 元件 / 焊盘 / 走线 / 丝印）是否正确；
> skill 运行时不依赖它们。大文件（~1.5 MB）请不要重复拷贝。

## 真实工程文件（real）

| 本地文件 | 来源 | 版本 | 说明 |
| --- | --- | --- | --- |
| `stm32f030_breakout_uCbasic.json` | https://github.com/andrea-cpu96/stm32f030-BreakoutBoard （commit `7ad3878bda99e0ee3367f8886ae15fdd9aa4d8c2`，文件 `1-PCB_PCB_uCbasic.json`） | editorVersion 6.5.34 | 104.9 × 54.6 mm 矩形板，33 个元件（全顶层）、LQFP-48、4 个排针、2 个电池座、覆铜 2 块。**仓库没有 LICENSE 文件**（只有 README.txt，未声明许可）；仅作本地测试夹具用，如需再分发请先换成有明确许可的工程。 |
| `foc_esp32ctrl.json` | https://github.com/Skythinker616/foc-wheel-legged-robot （commit `e2444395dd3a76c20b0683fbb1e123c21186a502`，文件 `esp32-controller/hardware/ESP32CTRL_LCEDA_PCB.json`） | editorVersion 6.5.29 | 42.4 × 21.6 mm 矩形板（板框由 3 段 TRACK 串成），27 个元件、大量 SOLIDREGION 铜皮、3 个 NPTH 安装孔。仓库许可 **GPL-3.0**。 |
| `foc_stm32.json` | 同上仓库（commit `e2444395dd3a76c20b0683fbb1e123c21186a502`，文件 `stm32-foc/hardware/STM32FOC_LCEDA_PCB.json`） | editorVersion 6.5.29 | 30.0 × 30.0 mm **圆形板**（板框是 BoardOutLine 层的一个 CIRCLE），42 个元件（顶层 36 / **底层 6**），含丝印圆弧、底层丝印、覆铜。仓库许可 **GPL-3.0**。 |

## 官方文档示例 / 合成数据（synthetic）

| 本地文件 | 来源 | 说明 |
| --- | --- | --- |
| `docs_pcb_example_v1.11.json` | EasyEDA 官方文档「PCB JSON File Source」gist：https://gist.github.com/dillonHe/279c55659bb7065258b6 （文档页 https://docs.easyeda.com/cn/DocumentFormat/1-Common-Information/ 引用） | 旧版（editorVersion 1.11.3）`head` 是字符串 `"3~1.11.3~"` 而不是对象；只用 5 个图元演示格式，用来回归「老 head 字符串」这条分支。gist 未声明许可。 |
| `synthetic_footprint_docType4.json` | 作者手工构造（依据 https://docs.easyeda.com/cn/DocumentFormat/EasyEDA-Format-Standard/ 的 PAD/TRACK/ARC/CIRCLE/TEXT/VIA 字段规范） | `docType=4` 的**单个封装库**文件，测 `parse()` 的封装库分支（没有板框/位号，按 1 个合成元件处理）。100% 合成，无第三方数据。 |

## 已知格式覆盖

- `docType=3`（PCB，真实文件）——主力路径。
- `docType=14`（PCB module）按 PCB 处理。
- `docType=4`（封装库）——尽力画成单个元件，见 `synthetic_footprint_docType4.json`。
- `docType=1/2/5`（原理图/符号/图页列表）——**不认**，`sniff()` 返回 False，`parse()` 会报错。
- 2020 年前的 dict（`{"TRACK": {gId: {...}}}` + `itemOrder`）旧「文件对象」格式——兼容模式尽力解析，
  封装摆放信息不可靠，会在 warnings 里如实说明。
