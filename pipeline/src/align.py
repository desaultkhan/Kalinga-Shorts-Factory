"""
Word-timing alignment for audio whose engine doesn't ship timings — RECORDED
voice and Higgsfield Inworld TTS (edge-tts / ElevenLabs already return words at
synthesis). Used so a recorded section can still get karaoke captions.

`words_for(audio, text)`:
  1. WHISPER (openai-whisper or faster-whisper, whichever imports) → real
     per-word timestamps (the "forced alignment if available" path);
  2. else None — the caller falls back to a held section-text caption.

`estimate_words(text, duration)` spreads `text` evenly across `duration`
(longer words get a bigger slice) — a deterministic, dependency-free timing the
caption renderer can tile, used when whisper isn't installed AND a held block
isn't wanted.

Everything degrades gracefully: a missing model / a transcription error returns
None, never raises — captions are advisory, never a gate.
"""
from __future__ import annotations

import sys
from pathlib import Path


def whisper_available() -> bool:
    """True if some whisper backend can be imported (no model download here)."""
    for mod in ("faster_whisper", "whisper"):
        try:
            __import__(mod)
            return True
        except Exception:                       # noqa: BLE001 (probe only)
            continue
    return False


def _model_size() -> str:
    import os
    return os.environ.get("KALINGA_WHISPER_MODEL", "base.en")


def _faster_whisper(audio: Path):
    from faster_whisper import WhisperModel
    model = WhisperModel(_model_size(), device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(audio), word_timestamps=True)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            tok = (w.word or "").strip()
            if tok:
                words.append({"word": tok, "start": float(w.start),
                              "end": float(w.end)})
    return words


def _openai_whisper(audio: Path):
    import whisper
    model = whisper.load_model(_model_size())
    r = model.transcribe(str(audio), word_timestamps=True)
    words = []
    for seg in r.get("segments", []):
        for w in seg.get("words", []):
            tok = (w.get("word") or "").strip()
            if tok:
                words.append({"word": tok, "start": float(w["start"]),
                              "end": float(w["end"])})
    return words


def words_for(audio, text: str = None):
    """Per-word timings [{word,start,end}] from whisper, or None when no whisper
    backend is installed / transcription fails. `text` is unused by whisper
    (it transcribes the actual audio) but kept for a future forced-aligner."""
    audio = Path(audio)
    if not audio.exists():
        return None
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        words = _faster_whisper(audio)
        if words:
            return words
    except Exception as e:                       # noqa: BLE001
        if "faster_whisper" not in str(e):
            print(f"  ! faster-whisper alignment failed ({e})", file=sys.stderr)
    try:
        import whisper  # noqa: F401
        words = _openai_whisper(audio)
        if words:
            return words
    except Exception as e:                       # noqa: BLE001
        if "whisper" not in str(e) or "load_model" in str(e):
            print(f"  ! whisper alignment failed ({e})", file=sys.stderr)
    return None


def estimate_words(text: str, duration: float) -> list:
    """Spread `text`'s words evenly across `duration` (char-weighted), so the
    karaoke renderer can still tile them — no real alignment, just a readable
    even pace. Deterministic, no dependencies."""
    ws = (text or "").split()
    if not ws or duration <= 0:
        return []
    weights = [len(w) + 2 for w in ws]
    total = sum(weights) or 1
    out, t = [], 0.0
    for w, wt in zip(ws, weights):
        d = duration * wt / total
        out.append({"word": w, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out
