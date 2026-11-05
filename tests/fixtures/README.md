# 测试夹具说明

这些文件只用于回归测试，skill 运行时不依赖它们。

## ✅ 随仓库提供（可再分发）

| 路径 | 来源 | 许可 |
| --- | --- | --- |
| `kicad/simple.kicad_pcb` | 本项目手写（30×20 mm，3 元件：顶层 0° / 底层 90° / 通孔件，含走线/圆弧/过孔/覆铜） | MIT（本项目） |
| `easyeda_pro/v3_sample/` | 按官方 `.eprj3` 规范合成（`make_v3_fixture.py` 可重新生成） | MIT（本项目） |
| `easyeda_pro/sqlite_sample.eprj` | 合成的旧版 SQLite 工程，用于验证 `.eprj` 分支 | MIT（本项目） |
| `easyeda_pro/real/Daaga1.3V.epro` | [enkhbold470/epro2kicad](https://github.com/enkhbold470/epro2kicad) `examples/` | **MIT** |
| `easyeda_std/synthetic_footprint_docType4.json` | 本项目手写（docType=4 封装库） | MIT（本项目） |
| `gerber/kicad_x2/` | 本项目按 KiCad 7 风格合成（`make_fixture.py`） | MIT（本项目） |

## ⚠️ 不随仓库提供，需要时下载

运行 `./tests/fetch_fixtures.sh` 会按下面的来源下载到对应目录（脚本幂等，已存在则跳过）。
这些文件**保留原许可证**，仅用于本地/CI 回归测试，不要再分发。

| 路径 | 来源 | 许可 |
| --- | --- | --- |
| `kicad/StickHub.kicad_pcb` | KiCad 官方 demo `demos/stickhub/` | GPL-3.0-or-later |
| `easyeda_std/stm32f030_breakout_uCbasic.json` | [andrea-cpu96/stm32f030-BreakoutBoard](https://github.com/andrea-cpu96/stm32f030-BreakoutBoard) @`7ad3878` | 无 LICENSE 文件 |
| `easyeda_std/foc_esp32ctrl.json`、`foc_stm32.json` | [Skythinker616/foc-wheel-legged-robot](https://github.com/Skythinker616/foc-wheel-legged-robot) @`e244439` | GPL-3.0 |
| `gerber/protel_pnp/` | [gerbv](https://github.com/gerbv/gerbv) `example/protel-pnp/` | GPL-2.0 |
| `gerber/geda_hellboard/`、`gerber/geda_led_xy/` | gerbv `example/hellboard/`、`example/pick-and-place/` | GPL-2.0 |

`gerber/edge/`（本次开发用的边界用例：光圈宏、G85 槽孔、G74/75 整圆、负片、截断文件等）来自
[KiCad QA](https://github.com/KiCad/kicad-source-mirror/tree/master/qa/data/gerbview)（GPL-3.0），
同样不随仓库分发，需要时从上游取。

## Altium 夹具

Altium `.PcbDoc` 是 OLE 复合文档，无法在仓库里合成。想测 `altium` 后端，把自己的
`.PcbDoc`（和同名 `.SchDoc`）放到 `tests/fixtures/altium/`，然后：

```bash
python3 scripts/test_backend.py altium tests/fixtures/altium/your.PcbDoc --build out/
```

`tests/run_all.sh` 里有一行注释掉的 `check altium ...`，取消注释即可加入日常回归。
