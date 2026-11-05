#!/usr/bin/env bash
# Download the third-party boards / gerber sets that are NOT redistributed with this repo.
# Sources, commits and licences are recorded in tests/fixtures/*/PROVENANCE.md.
set -uo pipefail
cd "$(dirname "$0")/fixtures"

dl() { # <url> <dest>
  mkdir -p "$(dirname "$2")"
  if [ -s "$2" ]; then echo "have  $2"; return; fi
  if curl -fsSL --retry 2 -o "$2" "$1"; then echo "got   $2"; else echo "FAIL  $2  ($1)"; rm -f "$2"; fi
}

GERRV="https://raw.githubusercontent.com/gerbv/gerbv/develop/example"

# ---- KiCad demo board (GPL-3.0, KiCad repo) --------------------------------
dl "https://gitlab.com/kicad/code/kicad/-/raw/master/demos/stickhub/StickHub.kicad_pcb" \
   "kicad/StickHub.kicad_pcb"

# ---- 嘉立创EDA 标准版 real boards (GPL-3.0 / no licence — local use only) ---
C1="7ad3878bda99e0ee3367f8886ae15fdd9aa4d8c2"
dl "https://raw.githubusercontent.com/andrea-cpu96/stm32f030-BreakoutBoard/${C1}/1-PCB_PCB_uCbasic.json" \
   "easyeda_std/stm32f030_breakout_uCbasic.json"
C2="e2444395dd3a76c20b0683fbb1e123c21186a502"
dl "https://raw.githubusercontent.com/Skythinker616/foc-wheel-legged-robot/${C2}/esp32-controller/hardware/ESP32CTRL_LCEDA_PCB.json" \
   "easyeda_std/foc_esp32ctrl.json"
dl "https://raw.githubusercontent.com/Skythinker616/foc-wheel-legged-robot/${C2}/stm32-foc/hardware/STM32FOC_LCEDA_PCB.json" \
   "easyeda_std/foc_stm32.json"

# ---- gerbv example boards (GPL-2.0) ----------------------------------------
for f in SE_SG_IF_V2.GTL SE_SG_IF_V2.GBL SE_SG_IF_V2.GTO SE_SG_IF_V2.GBO \
         SE_SG_IF_V2.GTS SE_SG_IF_V2.GBS SE_SG_IF_V2.GM1 SE_SG_IF_V2.GM2 \
         SE_SG_IF_V2.GD1 SE_SG_IF_V2.GG1 SE_SG_IF_V2.DRL \
         Pick_Place_for_SE_SG_IF_V2.csv README.txt; do
  dl "$GERRV/protel-pnp/$f" "gerber/protel_pnp/$f"
done
for f in hellboard.front.gbr hellboard.back.gbr hellboard.fab.gbr hellboard.plated-drill.cnc; do
  dl "$GERRV/hellboard/$f" "gerber/geda_hellboard/$f"
done
dl "$GERRV/pick-and-place/LED.frontsilk.gbr" "gerber/geda_led_xy/LED.frontsilk.gbr"
dl "$GERRV/pick-and-place/LED.xy"            "gerber/geda_led_xy/LED.xy"

echo
echo "Done. Now run ./tests/run_all.sh to include these in the regression run."
