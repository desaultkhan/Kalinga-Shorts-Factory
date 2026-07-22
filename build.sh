#!/usr/bin/env bash
# build.sh — "did it compile?" in one command.
#
# Python isn't compiled ahead of time, so a typo or a bad import only surfaces
# when the offending module is first run — which in this project means a stale
# `--ui` server quietly serving old code (the recurring gotcha). This script
# makes the check explicit and fast:
#   1. clears every __pycache__ (stale .pyc has bitten us before),
#   2. byte-compiles ALL of pipeline/src (catches syntax errors everywhere),
#   3. imports each module (catches import-time errors — bad names, circular
#      imports, missing siblings) — missing OPTIONAL third-party deps
#      (matplotlib, yfinance, whisper, …) are SKIPPED, not failed, so it's
#      honest on a fresh/CI box too.
#
# Exit 0 = clean.  Flags:
#   --kill   also kill any running `kalinga.py … --ui` / make servers first
#            (so the next launch can't serve old bytecode)
#   -q       quiet: only the final PASS/FAIL line
set -uo pipefail
cd "$(dirname "$0")"
SRC=pipeline/src
PY=${PYTHON:-python3}
QUIET=0; KILL=0
for a in "$@"; do
  case "$a" in
    -q|--quiet) QUIET=1 ;;
    --kill)     KILL=1 ;;
    *) echo "build.sh: unknown flag $a" >&2; exit 2 ;;
  esac
done
say(){ [ "$QUIET" = 1 ] || echo "$@"; }

if [ "$KILL" = 1 ]; then
  say "▸ stopping any running UI servers…"
  pkill -9 -f "kalinga.py .* --ui" 2>/dev/null
  pkill -9 -f "kalinga.py make"    2>/dev/null
  for pid in $(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -i python \
               | awk '{print $2}' | sort -u); do kill -9 "$pid" 2>/dev/null; done
fi

say "▸ clearing __pycache__…"
find pipeline -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

say "▸ byte-compiling $SRC (syntax)…"
if ! "$PY" -m compileall -q "$SRC"; then
  echo "✗ BUILD FAILED — syntax error (see above)"; exit 1
fi

# a channel is resolved lazily, but pick a real one (skip _backlog / *.md) so any
# config-touching import is happy
export KALINGA_CHANNEL="${KALINGA_CHANNEL:-$(ls channels 2>/dev/null \
  | grep -vE '^_|\.md$' | head -1)}"

say "▸ importing every module (import-time errors)…"
BUILD_QUIET=$QUIET "$PY" - <<'PY'
import os, sys, pathlib, importlib
sys.path.insert(0, "pipeline/src")
quiet = os.environ.get("BUILD_QUIET") == "1"
ours = {p.stem for p in pathlib.Path("pipeline/src").glob("*.py")}
# packages (a dir with __init__.py) import-test as one unit (their __init__ pulls
# in the submodules), so a split-out package like webui/ is still validated
ours |= {p.parent.name for p in pathlib.Path("pipeline/src").glob("*/__init__.py")}
# top-level packages that are genuinely optional at runtime — a missing one is a
# SKIP (the module's own code is fine), never a build failure
OPTIONAL = {"matplotlib", "numpy", "yfinance", "faster_whisper", "whisper",
            "PIL", "anthropic", "googleapiclient", "google", "yt_dlp",
            "elevenlabs", "requests", "bs4", "cv2", "moviepy", "scipy", "pandas"}
ok, skip, fail = [], [], []
for m in sorted(ours - {"__init__"}):
    try:
        importlib.import_module(m)
        ok.append(m)
    except ModuleNotFoundError as e:
        miss = (e.name or "").split(".")[0]
        if miss in OPTIONAL and miss not in ours:
            skip.append((m, miss))
        else:
            fail.append((m, f"{e.__class__.__name__}: {e}"))
    except Exception as e:                          # NameError, circular, …
        fail.append((m, f"{e.__class__.__name__}: {e}"))
if not quiet:
    for m, d in skip:
        print(f"  ~ {m} (skipped: needs {d})")
    for m in ok:
        print(f"  ✓ {m}")
for m, d in fail:
    print(f"  ✗ {m}: {d}", file=sys.stderr)
if fail:
    print(f"\n✗ BUILD FAILED — {len(fail)} module(s) failed to import")
    sys.exit(1)
print(f"✓ BUILD OK — {len(ok)} modules imported"
      + (f", {len(skip)} skipped (optional deps)" if skip else "")
      + " — clean")
PY
