"""
Voiceover — TTS per script segment with word-level timestamps.

Engines: edge (default, free) / elevenlabs (ELEVENLABS_API_KEY) /
preview (TTS_ENGINE=preview, silent + estimated timings).

Usage:
    python voiceover.py AAPL
Reads/writes in output/<date>_<TICKER>/ (seg<N>.mp3 + audio.json).
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import config

EDGE_VOICE = "en-US-ChristopherNeural"  # try also en-GB-RyanNeural, en-US-GuyNeural


def duration(mp3: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(mp3)], capture_output=True, text=True)
    return float(r.stdout.strip())


async def edge_segment(text: str, out: Path, voice: str = None,
                       rate: str = None, pitch: str = None,
                       volume: str = None) -> list[dict]:
    import edge_tts
    # edge-tts >=7 defaults to SentenceBoundary; karaoke captions need words.
    # rate/pitch/volume carry per-line vocal EXPRESSION (None = the +20% base
    # pace, flat pitch/volume — unchanged default delivery).
    kw = {"boundary": "WordBoundary", "rate": rate or "+20%"}
    if pitch:
        kw["pitch"] = pitch
    if volume:
        kw["volume"] = volume
    comm = edge_tts.Communicate(text, voice or EDGE_VOICE, **kw)
    words, audio = [], b""
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            words.append({"word": chunk["text"],
                          "start": chunk["offset"] / 1e7,
                          "end": (chunk["offset"] + chunk["duration"]) / 1e7})
    out.write_bytes(audio)
    return words


def elevenlabs_segment(text: str, out: Path) -> list[dict]:
    import requests
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        json={"text": text, "model_id": "eleven_turbo_v2_5"})
    r.raise_for_status()
    j = r.json()
    out.write_bytes(base64.b64decode(j["audio_base64"]))
    al = j["alignment"]
    words, cur, start = [], "", None
    for ch, t0, t1 in zip(al["characters"],
                          al["character_start_times_seconds"],
                          al["character_end_times_seconds"]):
        if ch.isspace():
            if cur:
                words.append({"word": cur, "start": start, "end": t0})
                cur, start = "", None
        else:
            if start is None:
                start = t0
            cur += ch
    if cur:
        words.append({"word": cur, "start": start,
                      "end": al["character_end_times_seconds"][-1]})
    return words


def preview_segment(text: str, out: Path) -> list[dict]:
    ws = text.split()
    weights = [len(w) + 2 for w in ws]
    total = max(sum(weights) / 16.5, 1.0)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", f"{total:.2f}", "-q:a", "9", str(out)],
                   check=True, capture_output=True)
    words, t = [], 0.0
    for w, wt in zip(ws, weights):
        d = total * wt / sum(weights)
        words.append({"word": w, "start": t, "end": t + d})
        t += d
    return words


def trim_tail(mp3: Path, words: list[dict], tail: float = 0.18):
    """Cut trailing TTS silence — punchy segment-to-segment pacing."""
    if not words:
        return
    keep = words[-1]["end"] + tail
    if duration(mp3) - keep < 0.2:
        return
    tmp = mp3.with_suffix(".trim.mp3")
    subprocess.run(["ffmpeg", "-y", "-i", str(mp3), "-t", f"{keep:.3f}",
                    "-c:a", "libmp3lame", "-q:a", "4", str(tmp)],
                   check=True, capture_output=True)
    tmp.replace(mp3)


def run(ticker: str) -> Path:
    folder = config.stock_dir(ticker)
    d = json.loads((folder / "script.json").read_text())
    engine = ("preview" if os.environ.get("TTS_ENGINE") == "preview"
              else "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY")
              else "edge")
    manifest = []
    for i, seg in enumerate(d["segments"]):
        out = folder / f"seg{i}.mp3"
        if engine == "preview":
            words = preview_segment(seg["text"], out)
        elif engine == "elevenlabs":
            words = elevenlabs_segment(seg["text"], out)
        else:
            words = asyncio.run(edge_segment(seg["text"], out))
        if engine != "preview":
            trim_tail(out, words)
        manifest.append({"index": i, "label": seg["label"], "text": seg["text"],
                         "overlay": seg.get("overlay", ""),
                         "file": out.name, "duration": duration(out),
                         "words": words})
        print(f"  seg{i} [{seg['label']}] {manifest[-1]['duration']:.1f}s")
    total = sum(m["duration"] for m in manifest)
    outp = folder / "audio.json"
    outp.write_text(json.dumps({"ticker": d["ticker"], "verdict": d["verdict"],
                                "engine": engine, "total": total,
                                "segments": manifest}, indent=2))
    print(f"  total {total:.1f}s ({engine}) -> {folder.name}/audio.json")
    return outp


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
