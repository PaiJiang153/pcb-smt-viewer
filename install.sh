#!/usr/bin/env bash
# Install this skill into the user-level skill root that DeepSeek Harness scans.
#
#   ./install.sh            -> ~/.agents/skills/pcb-smt-viewer      (default, shared with other agent tools)
#   ./install.sh --dsh      -> ~/.dsh/skills/pcb-smt-viewer         (DeepSeek Harness specific root)
#   ./install.sh --link     -> symlink instead of copy (keeps `git pull` working)
#   ./install.sh --dir DIR  -> custom target root
#
# Safe to re-run: it replaces an existing installation.
set -euo pipefail

NAME="pcb-smt-viewer"
ROOT="${HOME}/.agents/skills"
MODE="copy"

while [ $# -gt 0 ]; do
  case "$1" in
    --dsh)  ROOT="${HOME}/.dsh/skills" ;;
    --dir)  ROOT="${2:?--dir needs a path}"; shift ;;
    --link) MODE="link" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${ROOT}/${NAME}"

if [ "$SRC" = "$DST" ]; then
  echo "Already installed at $DST (this is the source directory). Nothing to do."
  exit 0
fi

mkdir -p "$ROOT"
rm -rf "$DST"
if [ "$MODE" = "link" ]; then
  ln -s "$SRC" "$DST"
  echo "Linked $DST -> $SRC"
else
  mkdir -p "$DST"
  # keep the repo metadata out of the installed copy
  ( cd "$SRC" && tar cf - --exclude=./.git --exclude=__pycache__ --exclude='*.pyc' . ) | ( cd "$DST" && tar xf - )
  echo "Copied $SRC -> $DST"
fi

PY="${PYTHON:-python3}"
if command -v "$PY" >/dev/null 2>&1; then
  "$PY" - "$DST" <<'PYEOF'
import json, os, sys
root = sys.argv[1]
md = os.path.join(root, "SKILL.md")
ok = os.path.isfile(md)
print("SKILL.md:", "found" if ok else "MISSING")
if ok:
    head = open(md, encoding="utf-8").read(400)
    print("frontmatter:", "ok" if head.startswith("---") and "name:" in head else "looks wrong")
PYEOF
fi

echo
echo "Installed. DeepSeek Harness picks up skills from ~/.agents/skills/ and ~/.dsh/skills/ automatically."
echo "Try: ask the agent \"use pcb-smt-viewer to build a placement map from <file>\""
echo "CLI:  python3 \"$DST/scripts/pcb2viewer.py\" --list-formats"
