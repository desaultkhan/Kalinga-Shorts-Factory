from __future__ import annotations
"""
elevenlabs.py — ElevenLabs connector (stdlib only, no `requests`).

Three capabilities, each needs ELEVENLABS_API_KEY and each DEGRADES GRACEFULLY
(returns False/None) when the key or the plan's access is missing, so the
pipeline falls back to its default (local music / edge TTS / no SFX):

- music()         royalty-free, commercially-cleared background tracks
                  (POST /v1/music) — the fix for Content-ID copyright strikes
                  from licensed-track background music.
- tts()           text-to-speech with word timings (for karaoke captions).
- sound_effect()  diegetic SFX from a text prompt.

Music generated on a paid plan is cleared for commercial use, so it won't trip
YouTube Content-ID the way a downloaded/licensed track does.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.elevenlabs.io/v1"
MAX_MUSIC_MS = 600_000          # ElevenLabs music length cap (10 min)
MIN_MUSIC_MS = 3_000


def available() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY"))


def _request(path: str, body: dict, query: str = "", timeout: int = 300):
    """POST JSON to the API. Returns (status, raw_bytes, error|None). Never
    raises — network/HTTP errors come back as (code/0, b'', message)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return 0, b"", "no ELEVENLABS_API_KEY"
    req = urllib.request.Request(
        f"{API}/{path}{query}", data=json.dumps(body).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), r.read(), None
    except urllib.error.HTTPError as e:
        try:
            return e.code, b"", e.read().decode()[:400]
        except Exception:                       # noqa: BLE001
            return e.code, b"", f"HTTP {e.code}"
    except Exception as e:                       # noqa: BLE001 — DNS/timeout/etc.
        return 0, b"", str(e)[:200]


def _get(path: str, timeout: int = 60):
    """GET JSON from the API. Returns (status, parsed_json|None, error|None)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return 0, None, "no ELEVENLABS_API_KEY"
    req = urllib.request.Request(
        f"{API}/{path}", headers={"xi-api-key": key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:                       # noqa: BLE001
        return 0, None, str(e)[:200]


_voices_cache = None


def list_voices(refresh: bool = False) -> list:
    """The account's available voices — [{id, name, accent, gender, age,
    description, labels}], or [] when the key/endpoint is unavailable. Cached
    in-process (the library rarely changes within a run)."""
    global _voices_cache
    if _voices_cache is not None and not refresh:
        return _voices_cache
    st, j, err = _get("voices")
    if st != 200 or not j:
        _voices_cache = []
        return _voices_cache
    out = []
    for v in j.get("voices", []):
        lab = v.get("labels") or {}
        out.append({
            "id": v.get("voice_id", ""),
            "name": v.get("name", ""),
            "accent": (lab.get("accent") or "").lower(),
            "gender": (lab.get("gender") or "").lower(),
            "age": (lab.get("age") or "").lower(),
            "description": (v.get("description")
                            or lab.get("description") or "").lower(),
            "labels": " ".join(str(x).lower() for x in lab.values()),
        })
    _voices_cache = out
    return out


_VOICE_ID_RE = __import__("re").compile(r"^[A-Za-z0-9]{16,}$")


def resolve_voice(spec: str) -> str:
    """Map a cast voice SPEC to an ElevenLabs voice_id. A spec may be:
      • a raw voice_id (20-ish alphanumerics) → returned as-is, no lookup;
      • a voice NAME ("Charlotte") → matched case-insensitively on the account;
      • a DESCRIPTOR ("australian female", "british narrator") → matched against
        each voice's accent/gender/age/name/labels (ALL tokens must hit).
    Returns the id, or None to let the caller fall back to ELEVENLABS_VOICE_ID.
    A name/descriptor needs the API (to read the library); without it, returns
    None and the connector's default voice is used."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if _VOICE_ID_RE.match(spec):
        return spec
    voices = list_voices()
    if not voices:
        return None
    low = spec.lower()
    for v in voices:                              # exact name first
        if v["name"].lower() == low:
            return v["id"]
    toks = [t for t in low.replace(",", " ").split() if t]
    best = None
    for v in voices:                              # ALL descriptor tokens present
        hay = f"{v['name']} {v['accent']} {v['gender']} {v['age']} {v['labels']} {v['description']}".lower()
        if all(t in hay for t in toks):
            best = best or v["id"]
    if best:
        return best
    for v in voices:                              # last resort: name substring
        if low in v["name"].lower():
            return v["id"]
    return None


def music(prompt: str, out, length_s: float, instrumental: bool = True,
          model: str = "music_v1") -> bool:
    """Generate a royalty-free background track → `out` (audio bytes). True on
    success. `instrumental` forces no vocals (default — it sits under a
    voiceover). False (with a message) on any failure; caller falls back."""
    ms = max(MIN_MUSIC_MS, min(int(length_s * 1000), MAX_MUSIC_MS))
    st, data, err = _request("music", {
        "prompt": prompt, "model_id": model,
        "music_length_ms": ms, "force_instrumental": instrumental})
    if st != 200 or not data:
        print(f"  ! ElevenLabs music failed ({st}): {err}", file=sys.stderr)
        return False
    Path(out).write_bytes(data)
    print(f"  ElevenLabs music: {Path(out).name} ({ms/1000:.0f}s, "
          f"{'instrumental' if instrumental else 'with vocals'}, royalty-free)")
    return True


def tts(text: str, out, voice_id: str = None,
        model: str = "eleven_turbo_v2_5") -> list:
    """Text-to-speech with WORD TIMINGS (for karaoke captions). Returns the word
    list, or None on failure. voice_id defaults to ELEVENLABS_VOICE_ID env."""
    voice_id = voice_id or os.environ.get(
        "ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    st, data, err = _request(
        f"text-to-speech/{voice_id}/with-timestamps",
        {"text": text, "model_id": model})
    if st != 200 or not data:
        print(f"  ! ElevenLabs TTS failed ({st}): {err}", file=sys.stderr)
        return None
    j = json.loads(data)
    Path(out).write_bytes(base64.b64decode(j["audio_base64"]))
    return _words_from_alignment(j.get("alignment") or {})


def sound_effect(prompt: str, out, duration_s: float = None) -> bool:
    """Generate a sound effect from a text prompt → `out`. True on success.
    duration_s clamped to the API's 0.5–22s range; omit to let it auto-size."""
    body = {"text": prompt}
    if duration_s:
        body["duration_seconds"] = max(0.5, min(float(duration_s), 22.0))
    st, data, err = _request("sound-generation", body)
    if st != 200 or not data:
        print(f"  ! ElevenLabs SFX failed ({st}): {err}", file=sys.stderr)
        return False
    Path(out).write_bytes(data)
    return True


def _words_from_alignment(al: dict) -> list:
    """Char-level alignment → word timings [{word, start, end}]."""
    chars = al.get("characters", [])
    t0s = al.get("character_start_times_seconds", [])
    t1s = al.get("character_end_times_seconds", [])
    words, cur, start = [], "", None
    for ch, a, b in zip(chars, t0s, t1s):
        if ch.isspace():
            if cur:
                words.append({"word": cur, "start": start, "end": a})
                cur, start = "", None
        else:
            if not cur:
                start = a
            cur += ch
    if cur:
        words.append({"word": cur, "start": start,
                      "end": t1s[-1] if t1s else start})
    return words
