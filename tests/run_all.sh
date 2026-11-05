#!/usr/bin/env bash
# Build every bundled fixture through its backend + check the dispatcher auto-detects it.
#
#   ./tests/run_all.sh              build only (no browser needed)
#   ./tests/run_all.sh --verify     also run scripts/verify.mjs (needs Playwright:
#                                   PLAYWRIGHT_MODULE_DIR / PLAYWRIGHT_BROWSERS_PATH)
set -uo pipefail
cd "$(dirname "$0")/.."
VERIFY=""
[ "${1:-}" = "--verify" ] && VERIFY="--verify"

OUT="${TMPDIR:-/tmp}/pcb-smt-viewer-tests"
rm -rf "$OUT"; mkdir -p "$OUT"
PY="${PYTHON:-python3}"
fail=0
n=0

check() { # <format> <fixture> <expected-substring>
  local fmt="$1" fx="$2" want="$3"
  [ -e "$fx" ] || { echo "SKIP  $fmt  ($fx not present — run tests/fetch_fixtures.sh)"; return; }
  n=$((n+1))
  echo "==> $fmt  $fx"
  local log="$OUT/$fmt.log"
  if ! "$PY" scripts/test_backend.py "$fmt" "$fx" --build "$OUT/$fmt" $VERIFY >"$log" 2>&1; then
    echo "  FAIL (exit != 0) — see $log"; tail -5 "$log" | sed 's/^/    /'; fail=$((fail+1)); return
  fi
  if grep -q "Traceback" "$log"; then
    echo "  FAIL (python traceback) — see $log"; tail -8 "$log" | sed 's/^/    /'; fail=$((fail+1)); return
  fi
  if [ -n "$want" ] && ! grep -q "$want" "$log"; then
    echo "  FAIL (expected '$want' in output) — see $log"; fail=$((fail+1)); return
  fi
  # dispatcher must recognise the same file
  if ! "$PY" scripts/pcb2viewer.py "$fx" "$OUT/disp_$fmt" 2>&1 | grep -q "识别为 : $fmt"; then
    echo "  FAIL (dispatcher did not detect '$fmt')"; fail=$((fail+1)); return
  fi
  echo "  ok  ($(grep -c '⚠️' "$log" 2>/dev/null || true) warnings)"
}

check kicad      tests/fixtures/kicad/simple.kicad_pcb                        "components     3"
check easyeda_pro tests/fixtures/easyeda_pro/v3_sample                        "components     5"
check easyeda_pro tests/fixtures/easyeda_pro/sqlite_sample.eprj               "components     1"
check easyeda_pro tests/fixtures/easyeda_pro/real/Daaga1.3V.epro              "components     45"
check easyeda_std tests/fixtures/easyeda_std/synthetic_footprint_docType4.json "components     1"
check gerber     tests/fixtures/gerber/kicad_x2                               "components     4"
# The Altium backend needs a real .PcbDoc, which cannot be redistributed here:
# put one at tests/fixtures/altium/ (any small board) and add:
# check altium   tests/fixtures/altium/myboard.PcbDoc                         "components"

echo
if [ "$fail" -ne 0 ]; then
  echo "❌ $fail of $n fixture runs failed (logs in $OUT)"
  exit 1
fi
echo "✅ all $n bundled fixture runs passed (artifacts in $OUT)"
