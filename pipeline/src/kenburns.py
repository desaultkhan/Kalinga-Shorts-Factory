from __future__ import annotations
"""Ken Burns animator — pluggable.

The still (non-`animate`) segments get motion at assembly. By default that's
the built-in ffmpeg `zoompan` pan/zoom (see `make_video._conform`). This module
is the OPTIONAL upgrade: a Remotion (React) plugin under `plugins/remotion/`
that renders a richer animated graphic from the keyframe — eased zoom + pan,
a drifting light glow, vignette and film grain.

It is a true plugin: OFF by default, and when ON it degrades to the ffmpeg
Ken Burns the moment Node/Remotion/its deps are missing or a render fails —
the pipeline never breaks because the plugin isn't installed.

Toggle (env beats template, like RENDERER / HF_VOICE):
  template.json  : "kenburns": "remotion" | "ffmpeg"   (default "ffmpeg")
  env            : KALINGA_KENBURNS=remotion|ffmpeg|off

Setup once (only if you want the plugin):  cd plugins/remotion && npm install
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config

PLUGIN_DIR = config.REPO / "plugins" / "remotion"
COMPOSITION = "KenBurns"
_ENTRY = "src/index.ts"

_warned = {"miss": False, "fail": False}      # warn once each, then stay quiet
_avail = None                                  # cached availability probe


def _resolve(tpl: dict) -> str:
    """Which animator to use: 'remotion' or 'ffmpeg'. env > template > default."""
    env = (os.environ.get("KALINGA_KENBURNS") or "").strip().lower()
    if env in ("ffmpeg", "off", "none", "0", "false"):
        return "ffmpeg"
    if env in ("remotion", "on", "1", "true"):
        return "remotion"
    return str(tpl.get("kenburns", "ffmpeg")).strip().lower()


def enabled(tpl: dict) -> bool:
    return _resolve(tpl) == "remotion"


def available() -> bool:
    """Node + the installed Remotion plugin are present (cached)."""
    global _avail
    if _avail is not None:
        return _avail
    ok = bool(shutil.which("npx")
              and (PLUGIN_DIR / "package.json").exists()
              and (PLUGIN_DIR / "node_modules").exists())
    _avail = ok
    return ok


def _motion(i: int) -> dict:
    """Deterministic per-segment motion variety (no randomness — resume-safe)."""
    zoom_in = (i % 2 == 0)            # alternate push-in / pull-out
    pans = [(1, 1), (-1, 1), (1, -1), (-1, -1), (0, 1), (1, 0)]
    px, py = pans[i % len(pans)]
    return {
        "zoomFrom": 1.0 if zoom_in else 1.14,
        "zoomTo": 1.14 if zoom_in else 1.0,
        "panX": px * 0.05,           # fraction of frame travelled
        "panY": py * 0.05,
        "glow": True,
        "grain": True,
        "vignette": True,
    }


def render(shot: dict, folder: Path, i: int, need: float, tpl: dict,
           fps: int = 30, width: int = 1080, height: int = 1920) -> Path | None:
    """Render an animated still for `shot` to `_kb{i}.mp4`; None to fall back.

    Returns the rendered mp4 (caller feeds it through the overlay graph and
    cleans it up) or None on any problem, so make_video can use the ffmpeg
    Ken Burns instead. Never raises."""
    if not enabled(tpl):
        return None
    if not available():
        if not _warned["miss"]:
            print("  (kenburns plugin requested but Remotion not installed — "
                  "using ffmpeg Ken Burns; run `cd plugins/remotion && "
                  "npm install` to enable)", file=sys.stderr)
            _warned["miss"] = True
        return None

    key = shot.get("key")
    if not key or not Path(key).exists():
        return None

    out = config.art(folder, f"_kb{i}.mp4")
    frames = int(round(need * fps)) + 1
    # keyframes live in a subfolder now; staticFile resolves this relative to
    # --public-dir (the run folder)
    src = str(Path(key).relative_to(Path(folder)))
    props = dict(_motion(i),
                 src=src,                       # resolved via --public-dir
                 durationInFrames=frames,
                 fps=fps, width=width, height=height)
    try:
        r = subprocess.run(
            ["npx", "remotion", "render", _ENTRY, COMPOSITION, str(out),
             "--props=" + json.dumps(props),
             "--public-dir=" + str(folder),
             "--log=error", "--concurrency=1"],
            cwd=str(PLUGIN_DIR), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=600)
    except Exception as e:                       # noqa: BLE001 — never break assembly
        r = None
        err = str(e)
    else:
        err = r.stderr[-300:] if r.returncode != 0 else ""

    if r is None or r.returncode != 0 or not out.exists():
        if not _warned["fail"]:
            print(f"  ! kenburns plugin render failed — using ffmpeg Ken Burns "
                  f"({err.strip()[:160]})", file=sys.stderr)
            _warned["fail"] = True
        try:
            out.unlink()
        except OSError:
            pass
        return None
    print(f"  part{i} kenburns: remotion animated graphic")
    return out
