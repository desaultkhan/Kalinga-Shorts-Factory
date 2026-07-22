"""
Voice recording — let the creator record their OWN narration instead of (or
beside) the AI voice, section by section. Two capture paths share this module:

  * terminal  — `mic_record()` runs ffmpeg avfoundation (macOS mic), stoppable
    by sending 'q' to its stdin; `interactive.py` drives the press-Enter loop.
    `list_devices()` finds the input device index. Falls back to a creator-
    provided file path when the mic can't be opened.
  * browser   — `webui.py` receives a MediaRecorder blob (webm/opus) over an
    upload endpoint and calls `ingest()` to transcode it to the canonical mp3.

`ingest(src, out)` transcodes any recorded/provided audio to a clean mono mp3 at
the canonical artifact path, light-normalising the level so a phone/laptop take
sits in the same loudness band as the TTS voices. Everything is best-effort and
returns a bool / None rather than raising — recording is an optional input.
"""
from __future__ import annotations

import re
import signal
import subprocess
import sys
from pathlib import Path


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffmpeg_ok() -> bool:
    try:
        return _run(["ffmpeg", "-version"]).returncode == 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- device probe
def list_devices() -> list:
    """macOS avfoundation audio input devices as [(index, name)]. Empty list on
    any platform without avfoundation (the UI then offers the file fallback)."""
    if sys.platform != "darwin":
        return []
    r = _run(["ffmpeg", "-hide_banner", "-f", "avfoundation",
              "-list_devices", "true", "-i", ""])
    out = (r.stderr or "") + (r.stdout or "")
    devices, in_audio = [], False
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        m = re.search(r"\[(\d+)\]\s+(.*)", line)
        if in_audio and m:
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def default_device() -> int | None:
    """The first avfoundation audio input index, or None when none/unsupported."""
    devs = list_devices()
    return devs[0][0] if devs else None


# ---------------------------------------------------------------- mic capture
def mic_record(out: Path, device: int = None):
    """Start recording the mic to `out` (a .wav). Returns the running ffmpeg
    Popen — the caller stops it cleanly with `stop(proc)` (sends 'q' so ffmpeg
    finalises the file). Returns None if the mic can't be opened. avfoundation
    only (macOS); other platforms get None → the file-path fallback."""
    if sys.platform != "darwin":
        return None
    dev = default_device() if device is None else device
    if dev is None:
        return None
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "avfoundation", "-i", f":{dev}",
             "-ac", "1", "-ar", "44100", str(out)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
    except (OSError, ValueError):
        return None
    return proc


def stop(proc) -> bool:
    """Stop a `mic_record` process so ffmpeg finalises the file (write the
    moov atom). Sends 'q' on stdin, then escalates to SIGINT/terminate."""
    if proc is None:
        return False
    try:
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
    except Exception:                            # noqa: BLE001
        try:
            proc.terminate()
        except Exception:                        # noqa: BLE001
            pass
    return proc.returncode in (0, 255, None)     # 255 = SIGINT'd ffmpeg (ok)


# ---------------------------------------------------------------- ingest / transcode
def ingest(src, out: Path, normalize: bool = True) -> bool:
    """Transcode a recorded/provided take at `src` (any container ffmpeg reads —
    wav/webm/m4a/mp3/…) to a clean mono mp3 at the canonical `out`. Light
    loudness-normalisation by default so a recorded take matches the TTS band.
    Returns True on success."""
    src, out = Path(src), Path(out)
    if not src.exists():
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    af = ["-af", "loudnorm=I=-18:TP=-2:LRA=11"] if normalize else []
    r = _run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "44100",
              *af, "-c:a", "libmp3lame", "-q:a", "3", str(out)])
    if r.returncode != 0 or not out.exists():
        # normalisation can fail on a very short/odd take — retry a plain copy
        r = _run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "44100",
                  "-c:a", "libmp3lame", "-q:a", "3", str(out)])
    return r.returncode == 0 and out.exists()
