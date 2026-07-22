"""
make_video.py — ONE command: topic in, finished viral Short out.

    python3 make_video.py MSFT

Everything is generated through Higgsfield AI (script visuals, voiceover,
virality score) via the `higgsfield` CLI; ffmpeg only concatenates. Every
artifact is cached in the topic folder, so reruns/resumes are free — delete a
file (or use `kalinga.py redo <stage>`) to regenerate it.

The content comes from the channel (channels/<name>/channel.yaml); the
visual world, models, voice and pacing come from the run's pinned template
(template.json in the topic folder, written from the channel's
templates/<name>.yaml).

Steps (all skipped when their output already exists):
  1. facts.json      channel research adapter (research.py)
  2. script.json     viral segments — kept if already written
                     (kalinga.py/creative.py writes one via the LLM;
                     /makeshort writes one in-session; the stock adapter's
                     dry template otherwise)
  3. segN.mp3        voiceover — Higgsfield Inworld TTS (template `voice`;
                     TTS_ENGINE=edge env or template for free edge-tts)
  4. keyN.png        cinematic keyframe per segment, overlay text baked in
  5. clipN.mp4       image-to-video (≤8s; moderation flag -> automatic
                     ken-burns fallback from the keyframe)
  6. short.mp4       conform 1080x1920@30 + VO mux + concat (+ karaoke
                     captions when ffmpeg has libass and word timings exist)
  7. seo.md          upload metadata
  8. virality.md     Higgsfield Virality Predictor score on the final video

One-time setup:  curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh
                 higgsfield auth login
"""
from __future__ import annotations
import prompts
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import config
import templates
import usage

W, H, FPS = 1080, 1920, 30
SEG_PAD = 0.25
# how long the DEFAULT overlay reveal may wait — a short lead so the frame reads
# first, then the text comes in WITH the line. Caps the old fraction-of-duration
# reveal, which on a long beat/section landed the overlay ~10s late (the
# "long sentence then the overlay finally shows up" bug). An EXPLICIT per-segment
# `overlay_reveal_at` is honoured exactly (uncapped) for a deliberate late reveal.
OVERLAY_MAX_LEAD = 1.2

VERSIONS_DIR = ".versions"      # superseded artifacts live here, never deleted


class StepFailed(RuntimeError):
    """A pipeline step failed in a way a rerun might fix."""


# ---------- versioned artifacts (never delete; archive + index) ----------
def archive(folder: Path, *names) -> list:
    """Move existing canonical artifacts into <folder>/.versions/ instead of
    deleting them — nothing the pipeline supersedes is ever lost. The active
    artifact always keeps its canonical name (so assembly/caching are
    unchanged); prior versions are indexed in versions.json and can be
    restored. Returns the list of canonical names actually archived.

    Subdir is dotted so the top-level globs that drive caching
    (key*.png, seg*.mp3, …) never see archived copies."""
    from datetime import datetime
    folder = Path(folder)
    log_path = folder / "versions.json"
    try:
        log = json.loads(log_path.read_text()) if log_path.exists() else {}
    except ValueError:
        log = {}
    vdir = folder / VERSIONS_DIR
    moved = []
    for name in names:
        src = config.art(folder, name)
        if not src.exists() or src.is_dir():
            continue
        vdir.mkdir(exist_ok=True)
        seq = len(log.get(name, [])) + 1
        dest = vdir / f"{src.stem}.{seq}{src.suffix}"
        while dest.exists():
            seq += 1
            dest = vdir / f"{src.stem}.{seq}{src.suffix}"
        shutil.move(str(src), str(dest))
        log.setdefault(name, []).append(
            {"file": f"{VERSIONS_DIR}/{dest.name}",
             "ts": datetime.now().isoformat(timespec="seconds")})
        moved.append(name)
    if moved:
        log_path.write_text(json.dumps(log, indent=2))
    return moved


def list_versions(folder: Path) -> dict:
    """{canonical_name: [{file, ts}, … newest last]} — the version index."""
    p = Path(folder) / "versions.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def restore(folder: Path, archived_rel: str) -> str:
    """Make an archived version active again, archiving the current canonical
    first (so the swap loses nothing either way). archived_rel is a
    '.versions/<file>' path from versions.json. Returns the canonical name."""
    folder = Path(folder)
    src = folder / archived_rel
    if not src.exists():
        raise FileNotFoundError(archived_rel)
    log = list_versions(folder)
    name = None
    for canonical, entries in log.items():
        if any(e.get("file") == archived_rel for e in entries):
            name = canonical
            break
    if name is None:                      # derive key0.3.png -> key0.png
        stem = Path(archived_rel).name
        name = stem.split(".")[0] + Path(archived_rel).suffix
    archive(folder, name)                 # park the current active copy
    shutil.move(str(src), str(config.art(folder, name)))
    # archive() just rewrote versions.json — re-read so we don't clobber the
    # entry it added, then drop the now-active version from its history list
    log = list_versions(folder)
    if name in log:
        log[name] = [e for e in log[name] if e.get("file") != archived_rel]
        if not log[name]:
            del log[name]
        (folder / "versions.json").write_text(json.dumps(log, indent=2))
    return name


# ---------- helpers ----------
def sh(cmd: list, **kw) -> subprocess.CompletedProcess:
    # stdin closed: ffmpeg reads interactive controls from an inherited
    # stdin and would swallow the interactive session's piped/typed input
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def die(msg: str):
    raise StepFailed(msg)


def ffprobe_duration(p: Path) -> float:
    return float(sh(["ffprobe", "-v", "quiet", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(p)]).stdout.strip())


def has_audio_stream(p: Path) -> bool:
    """True if the media file carries at least one audio stream — used to
    confirm a clip whose audio we mean to keep (native_audio / clip_voice)
    actually has one before mapping it (else fall back to the TTS driver)."""
    if not Path(p).exists():
        return False
    r = sh(["ffprobe", "-v", "quiet", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(p)])
    return bool((r.stdout or "").strip())


def hf(args: list, timeout=1200) -> dict | list:
    """Run a higgsfield CLI command with --json and parse the result."""
    r = sh(["higgsfield"] + args + ["--json"], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"higgsfield {' '.join(args[:3])}: "
                           f"{(r.stderr or r.stdout).strip()[:300]}")
    out = r.stdout.strip()
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0),
                default=-1)
    if start < 0:
        raise RuntimeError(f"no JSON in CLI output: {out[:200]}")
    return json.loads(out[start:])


def result_url(job, exts=(".mp4", ".png", ".jpg", ".webp", ".mp3", ".wav")) -> str | None:
    """Find the generated asset URL anywhere in the job object."""
    urls = re.findall(r'https://[^\s"\']+', json.dumps(job))
    for u in urls:
        if any(u.split("?")[0].endswith(e) for e in exts):
            return u
    return None


# ---------------- generation provider registry ----------------
# One GenProvider per media backend, SELF-CONTAINED (the publish._PLATFORMS
# pattern): submit-and-wait, asset-URL extraction from its job shape, and the
# installed/authenticated probe. generate() and ensure_cli() dispatch through
# it, so a second provider (Replicate, fal, …) is ONE registry entry —
# models stay template tokens (image_model/video_model), making a
# provider+model swap pure config. Select with KALINGA_PROVIDER (default
# higgsfield).
class GenProvider:
    __slots__ = ("name", "_create", "_asset_url", "_ensure")

    def __init__(self, name, create, asset_url, ensure):
        self.name = name
        self._create = create        # (model, prompt, extra_args) -> job obj
        self._asset_url = asset_url  # (job, exts) -> url | None
        self._ensure = ensure        # () -> None, die()s when unusable

    def create(self, model, prompt, extra):
        return self._create(model, prompt, extra)

    def asset_url(self, job, exts):
        return self._asset_url(job, exts)

    def ensure(self):
        self._ensure()


def _hf_create(model, prompt, extra):
    return hf(["generate", "create", model, "--prompt", prompt, "--wait"]
              + list(extra or []))


def _hf_ensure():
    if not shutil.which("higgsfield"):
        die("higgsfield CLI missing. Install:\n"
            "    curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/"
            "cli/main/install.sh | sh\n    higgsfield auth login")
    if sh(["higgsfield", "account", "status"]).returncode != 0:
        die("higgsfield CLI not authenticated — run once:  higgsfield auth login")


PROVIDERS = {
    "higgsfield": GenProvider("higgsfield", _hf_create, result_url,
                              _hf_ensure),
}


def provider() -> GenProvider:
    """The active generation provider ($KALINGA_PROVIDER, default higgsfield);
    an unknown name dies with the known ones listed."""
    name = os.environ.get("KALINGA_PROVIDER", "higgsfield")
    p = PROVIDERS.get(name)
    if p is None:
        die(f"unknown KALINGA_PROVIDER '{name}' — known: "
            + ", ".join(PROVIDERS))
    return p


def generate(model: str, prompt: str, out: Path, exts, extra: list,
             what: str, alt_prompts=None, validate=None, postprocess=None) -> bool:
    """One cached Higgsfield generation -> downloaded file. True on success.

    alt_prompts: rephrasings tried IN ORDER when an attempt trips a
    moderation false-positive (nsfw/ip_detected/failed), errors, or returns
    no asset — each is a fresh generation. The seedance video model throws
    these false-positives often, so callers pass progressively safer
    rephrasings to claw the shot back before the ken-burns fallback.

    validate(out) -> bool: an optional QUALITY check on the downloaded file. When
    it returns False the result is treated like a failed attempt (try the next
    alt_prompt) — this catches a DEGENERATE generation (a washed-out / near-blank
    keyframe) that downloaded fine but is unusable. On the LAST attempt a
    validate-failing file is KEPT anyway (better than nothing) with a warning, so
    a fussy check never hard-fails the run.

    A TIMEOUT is NEVER retried: a rephrase can't un-stick a slow/hung job, so
    a second --wait just burns another full timeout (and risks a double
    charge). On a timeout we bail straight to the caller's fallback."""
    if out.exists():
        print(f"  {out.name} (cached)")
        return True
    prompts = [prompt] + list(alt_prompts or [])
    for n, pr in enumerate(prompts):
        last = n + 1 == len(prompts)
        try:
            prov = provider()
            job = prov.create(model, pr, extra)
            url = prov.asset_url(job, exts)
            flag = None if url else next(
                (s for s in ("nsfw", "ip_detected", "failed")
                 if s in json.dumps(job)), "no result")
        except RuntimeError as e:
            url, flag = None, str(e)[:120]
        if url:
            urllib.request.urlretrieve(url, out)
            # validate is ADVISORY only — a washed-out/blank result is FLAGGED but
            # KEPT, never auto-regenerated (owner call: don't burn a credit on a
            # retry that usually fails too; re-roll the stage manually if needed).
            if validate is not None and not _safe_validate(validate, out):
                print(f"  ! {what}: looks washed-out/blank — keeping it as-is "
                      "(re-roll the stage manually if you want)", file=sys.stderr)
            # deterministic post-process on a FRESH download only (never on cache,
            # so it's applied at most once) — e.g. lift a too-dark keyframe
            if postprocess is not None:
                _safe_postprocess(postprocess, out)
            print(f"  {out.name}" + (f" (attempt {n + 1})" if n else ""))
            usage.record_generation(model=model, label=what)  # FYI + ledger
            return True
        print(f"  ! {what}: {flag}", file=sys.stderr)
        if flag and "timeout" in str(flag).lower():
            print(f"  ✗ {what}: timed out — NOT retrying (a rephrase won't "
                  "un-stick a slow job); using the fallback", file=sys.stderr)
            break
        if not last:
            print(f"  ↻ retrying {what} with a different prompt "
                  f"(attempt {n + 2}/{len(prompts)})")
    return False


def _safe_validate(fn, out: Path) -> bool:
    """Run a generate() validate callback, swallowing any error (a flaky check
    must never block a real generation — treat an erroring check as 'pass')."""
    try:
        return bool(fn(out))
    except Exception:        # noqa: BLE001
        return True


def _safe_postprocess(fn, out: Path) -> None:
    """Run a generate() postprocess callback, swallowing any error — a flaky
    touch-up must never break a real generation."""
    try:
        fn(out)
    except Exception:        # noqa: BLE001
        pass


# keyframes below this mean luminance (0-255) get relit; also the target the
# gamma lift aims for. A clearly-lit frame sits well above this.
_DARK_FLOOR = 84


def _relight_if_dark(path: Path) -> None:
    """Deterministic SAFETY-NET against a too-dark keyframe. The image model
    keeps grading frames dark (moody palettes + a 'bottom third kept dark'
    legacy habit); the prompt now pushes bright, but probabilistic prompts drift,
    so this GUARANTEES the deliverable is visible. If the frame's mean luminance
    is below `_DARK_FLOOR`, lift the midtones with a gamma curve that PINS
    black→black and white→white — it brightens the scene WITHOUT clipping
    highlights or washing it out (a moody grade stays moody, just legible).
    NOT a regeneration (owner: don't burn a credit on a re-roll) — the existing
    pixels are relit in place. A near-pure-black frame (mean < 6, the real
    'black image' bug) is left alone: there's no detail to recover, re-roll it.
    Best-effort; never raises (wrapped by _safe_postprocess)."""
    import math as _m
    from PIL import Image, ImageStat
    im = Image.open(str(path)).convert("RGB")
    mean = ImageStat.Stat(im.convert("L")).mean[0]
    if mean >= _DARK_FLOOR or mean < 6:
        return
    # gamma (<1) mapping the current mean up to the floor, clamped so a slightly
    # dim frame gets a gentle nudge and a very dark one a strong (but bounded) lift
    g = _m.log(_DARK_FLOOR / 255.0) / _m.log(mean / 255.0)
    g = max(0.42, min(0.95, g))
    lut = [round(255 * ((i / 255.0) ** g)) for i in range(256)] * 3
    im.point(lut).save(str(path))
    print(f"  ↑ relit {path.name}: mean {mean:.0f}→~{_DARK_FLOOR} (γ={g:.2f}) "
          "— too dark out of the model, brightened in place")


def _healthy_image(path: Path) -> bool:
    """False for a DEGENERATE keyframe — a washed-out / near-uniform fog with no
    real blacks or highlights (nano_banana occasionally returns these). The tell
    is a compressed DYNAMIC RANGE: a good frame spans most of 0-255, a foggy one
    sits in a narrow mid-grey band (e.g. key1 of the Brian-Chesky run: 17-150,
    span 133, vs a healthy 0-253). Pillow-only; passes (True) if Pillow/the file
    is unreadable so it never blocks on its own failure."""
    try:
        from PIL import Image, ImageStat
        im = Image.open(str(path)).convert("L")
        lo, hi = im.getextrema()
        span = hi - lo
        std = ImageStat.Stat(im).stddev[0]
        # washed-out: narrow tonal range AND low contrast; or almost flat
        if span < 150 and std < 40:
            return False
        if std < 10:
            return False
        return True
    except Exception:        # noqa: BLE001 — never block on a check failure
        return True


# ---------- steps ----------
def ensure_cli():
    """Verify the ACTIVE generation provider is installed + authenticated."""
    provider().ensure()


def step_research(topic: str, folder: Path) -> dict:
    import research
    try:
        return research.ensure(topic, folder)
    except RuntimeError as e:
        die(str(e))


def step_script(d: dict, folder: Path) -> dict:
    p = folder / "script.json"
    if p.exists():
        s = json.loads(p.read_text())
        print(f"  script.json (cached, {s.get('mode')})")
        return s
    import research
    segs = research.dry_script(d)
    if not segs:
        die("no LLM backend and this channel has no dry-script fallback — "
            "install the claude CLI or export ANTHROPIC_API_KEY")
    s = {"topic": d.get("topic"), "verdict": d.get("verdict"),
         "mode": "template", "segments": segs}
    p.write_text(json.dumps(s, indent=2))
    print("  script.json (dry template — kalinga.py/creative.py writes the AI one)")
    return s


# ---------------- TTS engine registry ----------------
# One TTSEngine per backend, SELF-CONTAINED (the publish._PLATFORMS pattern):
# its multi-character voice POOL, whether it's free / yields word timings /
# honours vocal expression, an availability probe, the per-engine default
# voice, and the synth function. Adding an engine = ONE entry in ENGINES —
# _voice_resolver, _synth_line, step_voice's fallback, and both UIs' engine
# toggle (next_engine) all derive from it; no scattered `if engine ==` edits.
class TTSEngine:
    __slots__ = ("name", "pool", "free", "timings", "expression", "label",
                 "_synth", "_available", "_default_voice")

    def __init__(self, name, pool, free, timings, expression, label, synth,
                 available=None, default_voice=None):
        self.name = name
        self.pool = pool                # extra characters draw distinct voices
        self.free = free                # $0 marginal (UI cost labels)
        self.timings = timings          # word timings → karaoke captions
        self.expression = expression    # honours vocal expression/prosody
        self.label = label              # one-line UI description
        self._synth = synth             # (text, out, voice, expression)->words
        self._available = available     # () -> (ok, reason); None = always
        self._default_voice = default_voice   # (tpl_voice) -> engine default

    def available(self):
        return (True, "ready") if self._available is None \
            else self._available()

    def default_voice(self, tpl_voice: str) -> str:
        return (self._default_voice(tpl_voice) if self._default_voice
                else tpl_voice)

    def synth(self, text, out, voice, expression=None) -> list:
        return self._synth(text, out, voice, expression)


def _edge_synth(text, out, voice, expression=None) -> list:
    import voiceover
    import asyncio
    rate = pitch = volume = None
    pros = _expression_prosody(expression)
    if pros:
        rate, pitch, volume = pros
    try:
        words = asyncio.run(voiceover.edge_segment(
            text, out, voice=voice, rate=rate, pitch=pitch, volume=volume))
    except ValueError as e:                # a bad/typo voice name — never fatal
        print(f"  ! invalid edge voice {voice!r} ({e}); "
              f"falling back to {voiceover.EDGE_VOICE}", file=sys.stderr)
        words = asyncio.run(voiceover.edge_segment(
            text, out, voice=None, rate=rate, pitch=pitch, volume=volume))
    voiceover.trim_tail(out, words)
    return words


def _eleven_synth(text, out, voice, expression=None) -> list:
    import elevenlabs
    import voiceover
    # the cast voice may be a raw ElevenLabs id, a voice NAME, or a descriptor
    # ("australian female") — resolve_voice maps any of them to an id (against
    # the account), else None → the connector's default voice
    vid = elevenlabs.resolve_voice(voice)
    words = elevenlabs.tts(text, out, voice_id=vid)
    if words is None:
        die("ElevenLabs voiceover failed (check ELEVENLABS_API_KEY / the "
            "voice id, or use TTS_ENGINE=edge)")
    voiceover.trim_tail(out, words)
    return words


def _inworld_synth(text, out, voice, expression=None) -> list:
    if expression and _expression_prosody(expression):
        global _inworld_expr_warned
        if not _inworld_expr_warned:
            print("  ! expression is honoured on the edge TTS engine; the "
                  "Inworld API has no affect control (delivery stays neutral). "
                  "Use TTS_ENGINE=edge for expressive lines.", file=sys.stderr)
            _inworld_expr_warned = True
    ok = generate("inworld_text_to_speech", text, out,
                  (".mp3", ".wav", ".m4a", ".ogg"), ["--voice", voice],
                  "TTS line")
    if not ok:
        die("voiceover failed")
    return []


def _eleven_available():
    import elevenlabs
    return ((True, "ready") if elevenlabs.available()
            else (False, "no ELEVENLABS_API_KEY"))


def _edge_default_voice(_tpl_voice):
    import voiceover
    return voiceover.EDGE_VOICE


ENGINES = {
    "edge": TTSEngine(
        "edge",
        ["en-US-AriaNeural", "en-GB-RyanNeural", "en-US-JennyNeural",
         "en-US-GuyNeural", "en-US-AnaNeural", "en-GB-SoniaNeural",
         "en-US-EricNeural"],
        free=True, timings=True, expression=True,
        label="free edge-tts + karaoke word timings",
        synth=_edge_synth, default_voice=_edge_default_voice),
    "higgsfield": TTSEngine(
        "higgsfield",
        # inworld names are best-effort — set them in the cast for reliability
        ["Ashley (en)", "Hades (en)", "Olivia (en)", "Craig (en)"],
        free=False, timings=False, expression=False,
        label="Inworld (paid, richer voices, no word timings)",
        synth=_inworld_synth),
    "elevenlabs": TTSEngine(
        "elevenlabs",
        # preset voice NAMES, resolved to ids by elevenlabs.resolve_voice
        ["Charlotte", "Charlie", "Rachel", "Antoni", "Domi", "Josh"],
        free=False, timings=True, expression=False,
        label="ElevenLabs (needs ELEVENLABS_API_KEY, word timings)",
        synth=_eleven_synth, available=_eleven_available),
}
FALLBACK_ENGINE = "edge"                 # free + always available


def tts_engine(name: str) -> TTSEngine:
    """The registry entry for `name`; an unknown name gets the Inworld engine
    (the historical default for unrecognized TTS_ENGINE values)."""
    return ENGINES.get(name) or ENGINES["higgsfield"]


def next_engine(current: str) -> str:
    """The next AVAILABLE engine after `current`, cycling ENGINES in order —
    both UIs' engine toggle, so a newly-registered engine is reachable
    immediately (the old toggle hardwired edge<->higgsfield and made
    elevenlabs unreachable)."""
    names = list(ENGINES)
    i = names.index(current) if current in names else -1
    for step in range(1, len(names) + 1):
        cand = names[(i + step) % len(names)]
        if ENGINES[cand].available()[0]:
            return cand
    return current


def _voice_resolver(cast: dict, engine: str, default_voice: str):
    """(resolve, assigned): map a speaker NAME to a voice for `engine`. The
    narrator/default keeps the channel voice; each additional character gets a
    distinct voice — an explicit cast `voice` wins, else one is auto-assigned
    from the engine's pool in cast order (so N characters → N voices)."""
    eng = tts_engine(engine)
    base = eng.default_voice(default_voice)
    pool = [base] + list(eng.pool)
    assigned, auto = {}, 0
    for name in (cast or {}):
        v = (cast.get(name) or {}).get("voice")
        if isinstance(v, dict):          # engine-keyed: {"edge": …, "higgsfield": …}
            v = v.get(engine) or v.get("default") or ""
        v = str(v or "").strip()
        if v and v.lower() != "auto":
            assigned[name] = v
        else:
            assigned[name] = pool[auto % len(pool)]
            auto += 1

    def resolve(speaker):
        return assigned.get(speaker, base)
    return resolve, assigned


# Expression → edge-tts prosody. The script may direct HOW a line is said
# (an `expression`/`delivery` like "excited", "deadpan", "whispering",
# "concerned"); a flat default leaves every line sounding the same. edge-tts
# honours rate/pitch/volume, so a coarse keyword map turns the direction into
# real delivery. (rate %, pitch Hz, volume %) deltas off the +20% base pace.
# Inworld's TTS API exposes no affect knob, so expression is an edge feature.
_EXPRESSIONS = (
    (("excited", "hyped", "thrilled", "energetic", "ecstatic"), (14, 14, 0)),
    (("urgent", "alarmed", "panicked", "frantic"),              (16, 6, 5)),
    (("happy", "cheerful", "upbeat", "bright", "delighted"),    (6, 8, 0)),
    (("playful", "teasing", "cheeky", "sarcastic", "smug"),     (4, 10, 0)),
    (("curious", "intrigued", "questioning", "wondering"),      (2, 7, 0)),
    (("confident", "bold", "assertive", "emphatic", "proud"),   (-2, 2, 12)),
    (("warm", "friendly", "kind", "gentle", "reassuring"),      (-4, 0, 0)),
    (("calm", "soft", "soothing", "relaxed", "measured"),       (-8, -3, -5)),
    (("whisper", "hushed", "quiet", "secret", "confiding"),     (-8, -2, -28)),
    (("serious", "stern", "grave", "concerned", "worried"),     (-9, -6, 0)),
    (("deadpan", "flat", "dry", "monotone", "bored"),           (-4, -8, 0)),
    (("sad", "somber", "somber", "downcast", "disappointed"),   (-11, -9, -5)),
    (("angry", "furious", "outraged", "indignant"),             (4, -4, 12)),
)


def _expression_prosody(expr: str):
    """(rate, pitch, volume) edge-tts strings for an expression, or None for a
    neutral/blank one. Matches the first expression keyword found."""
    if not expr:
        return None
    low = str(expr).lower()
    for keys, (r, p, v) in _EXPRESSIONS:
        if any(k in low for k in keys):
            return (f"{20 + r:+d}%", f"{p:+d}Hz", f"{v:+d}%")
    return None


_inworld_expr_warned = False


def _synth_line(text: str, out: Path, engine: str, voice: str,
                expression: str = None) -> list:
    """Synthesize one spoken line to `out` in `voice`, with optional vocal
    `expression` (prosody, engines that support it). Returns word timings
    (timing-capable engines). Fatal on TTS failure (a missing voiceover
    can't be faked). Dispatches through the ENGINES registry."""
    return tts_engine(engine).synth(text, out, voice, expression)


def _line_start(ln: dict, prev_end: float) -> float:
    """When this dialogue line should START within the segment (seconds).
    Explicit `start` (absolute) wins; else it follows the previous line after
    an optional `gap` (default 0 = back-to-back, the legacy behaviour)."""
    if ln.get("start") is not None:
        try:
            return max(0.0, float(ln["start"]))
        except (TypeError, ValueError):
            pass
    try:
        gap = float(ln.get("gap", 0) or 0)
    except (TypeError, ValueError):
        gap = 0.0
    return max(0.0, prev_end + gap)


def _compose_lines(placed: list, out: Path, folder: Path) -> None:
    """Mix already-synthesized dialogue line clips onto one timeline at their
    chosen start offsets (silence fills the gaps; overlaps mix). `placed` is
    [(path, start_seconds), …]. One line → a plain copy."""
    (folder / "build").mkdir(exist_ok=True)
    if len(placed) == 1 and placed[0][1] <= 0.001:
        r = sh(["ffmpeg", "-y", "-i", str(placed[0][0]),
                "-c:a", "libmp3lame", "-q:a", "4", str(out)])
        if r.returncode != 0 or not out.exists():
            die(f"voice copy failed: {r.stderr[-200:]}")
        return
    args, filt, labels = [], [], []
    for k, (p, start) in enumerate(placed):
        args += ["-i", str(p)]
        ms = int(round(max(0.0, start) * 1000))
        filt.append(f"[{k}:a]adelay={ms}:all=1[a{k}]")
        labels.append(f"[a{k}]")
    filt.append("".join(labels)
                + f"amix=inputs={len(placed)}:normalize=0:"
                  "dropout_transition=0[mix]")
    r = sh(["ffmpeg", "-y", *args, "-filter_complex", ";".join(filt),
            "-map", "[mix]", "-c:a", "libmp3lame", "-q:a", "4", str(out)])
    if r.returncode != 0 or not out.exists():
        die(f"voice mix failed: {r.stderr[-200:]}")


def _seg_dialogue(seg: dict) -> list:
    """The segment's non-empty dialogue lines (or [] for a single-voice
    segment)."""
    return [ln for ln in (seg.get("lines") or [])
            if (ln.get("text") or "").strip()]


def _seg_speaks(seg: dict) -> bool:
    """True when this segment is delivered by a CHARACTER (it names a `speaker`
    or carries a `lines` dialogue) — the eligibility gate for letting the
    generated clip carry the voice instead of a separate TTS voiceover."""
    return bool((seg.get("speaker") or "").strip()) or bool(_seg_dialogue(seg))


def _spoken_text(seg: dict) -> str:
    """The full spoken words of a segment — the joined dialogue lines, else the
    plain text. Used as the speech the generated clip should lip-sync to."""
    dlg = _seg_dialogue(seg)
    if dlg:
        return " ".join((ln.get("text") or "").strip() for ln in dlg).strip()
    return (seg.get("text") or "").strip()


def clip_voice_on(seg: dict) -> bool:
    """Whether this segment's VOICE comes from the GENERATED clip (lip-synced to
    a TTS reference passed to the model) instead of a separate voiceover.
    Explicit per-segment opt-in (`clip_voice: true`), valid only when a
    character actually speaks the segment. The TTS is still synthesized — but
    only as the voice REFERENCE handed to the video model so the clip's spoken
    delivery matches the character — and the clip's own audio is what plays."""
    return bool(seg.get("clip_voice")) and _seg_speaks(seg)


def compose_segment(seg: dict, out: Path, folder: Path, idx: int,
                    line_files: list) -> tuple:
    """Re-mix a dialogue segment's segN.mp3 from EXISTING per-line clips and
    the lines' current timing — NO TTS, so editing when each line starts/ends
    is free. Returns (word_timings, [{start,duration} per line]). Word timings
    are offset to each line's actual start."""
    real = _seg_dialogue(seg)
    placed, words, timing, prev_end = [], [], [], 0.0
    for k, (ln, pf) in enumerate(zip(real, line_files)):
        start = _line_start(ln, prev_end)
        dur = ffprobe_duration(pf)
        for ww in (ln.get("words") or []):
            words.append({**ww, "start": ww["start"] + start,
                          "end": ww["end"] + start})
        placed.append((pf, start))
        timing.append({"start": round(start, 3), "duration": round(dur, 3)})
        prev_end = start + dur
    _compose_lines(placed, out, folder)
    return words, timing


def _line_paths(folder: Path, idx: int, n: int) -> list:
    return [config.art(folder, f"seg{idx}_l{k}.mp3") for k in range(n)]


def _line_files_ready(folder: Path, idx: int, dlg: list, prev_lines,
                      seg_expr=None) -> bool:
    """True iff every per-line clip for this dialogue segment is on disk AND
    its spoken text AND expression are unchanged from the previous render — so
    segN.mp3 can be recomposed from existing audio (a free timing-only edit),
    no TTS. A text OR expression change alters the audio and forces re-synth."""
    if not prev_lines or len(prev_lines) != len(dlg):
        return False
    for k, ln in enumerate(dlg):
        if not config.art(folder, f"seg{idx}_l{k}.mp3").exists():
            return False
        if (ln.get("text") or "").strip() != (prev_lines[k] or {}).get("text", ""):
            return False
        if (ln.get("expression") or seg_expr or "") != (
                prev_lines[k] or {}).get("expression", ""):
            return False
    return True


def _line_manifest(folder: Path, idx: int, dlg: list, seg_expr=None) -> list:
    """Per-line audio.json metadata: speaker/text/expression/file + resolved
    start & duration (the independent timing) + per-line word timings."""
    out, prev_end = [], 0.0
    for k, ln in enumerate(dlg):
        pf = config.art(folder, f"seg{idx}_l{k}.mp3")
        dur = ffprobe_duration(pf) if pf.exists() else 0.0
        start = _line_start(ln, prev_end)
        out.append({"speaker": ln.get("speaker"),
                    "text": (ln.get("text") or "").strip(),
                    "expression": ln.get("expression") or seg_expr or "",
                    "file": pf.name, "start": round(start, 3),
                    "duration": round(dur, 3),
                    "words": ln.get("words") or []})
        prev_end = start + dur
    return out


def _synth_segment(seg: dict, out: Path, engine: str, resolve,
                   folder: Path, idx: int) -> list:
    """Render a segment's audio to segN.mp3. A `lines` list (dialogue) is
    synthesized line-by-line in each speaker's voice — each KEPT as its own
    artifact segN_lK.mp3 — then mixed onto one timeline at each line's chosen
    start (default back-to-back), so line timing can be re-edited for free
    later. A single `text`/one-line segment is the speaker's voice directly.
    Returns the combined word timings; per-line words are stashed on the
    line dicts for free recomposition."""
    seg_expr = seg.get("expression") or seg.get("delivery")
    lines = _seg_dialogue(seg)
    if not lines:
        return _synth_line((seg.get("text") or "").strip(), out, engine,
                           resolve(seg.get("speaker")), expression=seg_expr)
    if len(lines) == 1:
        ln = lines[0]
        return _synth_line(ln["text"].strip(), out, engine,
                           resolve(ln.get("speaker") or seg.get("speaker")),
                           expression=ln.get("expression") or seg_expr)
    line_files = []
    for k, ln in enumerate(lines):
        piece = config.art(folder, f"seg{idx}_l{k}.mp3")
        piece.unlink(missing_ok=True)            # never reuse a stale line clip
        w = _synth_line(ln["text"].strip(), piece, engine,
                        resolve(ln.get("speaker")),
                        expression=ln.get("expression") or seg_expr)
        ln["words"] = w                          # stash for free recompose
        line_files.append(piece)
    words, _timing = compose_segment(seg, out, folder, idx, line_files)
    return words


def _align_recorded(audio: Path, say: str) -> list:
    """Word timings for a RECORDED (or Inworld) section so it can still get
    karaoke captions: whisper forced alignment if installed, else [] (the caller
    falls back to a held section-text caption). Never raises."""
    try:
        import align
        return align.words_for(audio, say) or []
    except Exception as e:                       # noqa: BLE001 (captions advisory)
        print(f"  ! alignment skipped ({e})", file=sys.stderr)
        return []


def _backfill_words(audio: dict, folder: Path) -> bool:
    """Add karaoke word timings to any CACHED section/segment that has spoken
    text but no `words` — i.e. it was voiced BEFORE whisper was installed (or by a
    no-timing engine) and cached wordless, so it would show a held caption while
    its neighbours get karaoke. Aligns the EXISTING audio in place (no
    re-synthesis — the takes are untouched). No-op without a whisper backend.
    Returns True if anything was backfilled, so the caller can rewrite audio.json.
    Native/clip_voice beats are skipped (they carry no TTS caption on purpose)."""
    try:
        import align
        if not align.whisper_available():
            return False
    except Exception:                            # noqa: BLE001
        return False
    changed = False
    for s in audio.get("segments", []):
        if s.get("native_audio") or s.get("clip_voice"):
            continue
        secs = s.get("sections")
        if secs:
            for sc in secs:
                say = (sc.get("say") or "").strip()
                af = config.art(folder, sc.get("file", ""))
                if not say or sc.get("words") or not af.exists():
                    continue
                w = _align_recorded(af, say)
                if w:
                    sc["words"] = w
                    changed = True
        else:
            text = (s.get("text") or "").strip()
            af = config.art(folder, s.get("file", ""))
            if not text or s.get("words") or not s.get("file") or not af.exists():
                continue
            w = _align_recorded(af, text)
            if w:
                s["words"] = w
                changed = True
    return changed


def _synth_sections(seg: dict, engine: str, resolve, folder: Path, idx: int,
                    specs: list, prev_secs: list) -> list:
    """Voice each SECTION of a section-native beat to seg<idx>_s<k>.mp3 and
    return the sections manifest. A section whose RECORDING is present (kept from
    a prior render, marked `recorded`) is preserved — a creator recording always
    wins over TTS; a TTS section is (re)synthesized only when its file is missing
    or its spoken `say`/expression changed. Recorded/Inworld sections get whisper
    word timings when available; edge/ElevenLabs return them from synthesis."""
    seg_expr = seg.get("expression") or seg.get("delivery") or ""
    prev = {s.get("k"): s for s in (prev_secs or [])}
    spk = resolve(seg.get("speaker") or (seg.get("lines") or [{}])[0].get("speaker"))
    out = []
    for k, sp in enumerate(specs):
        say = (sp.get("say") or "").strip()
        af = config.art(folder, section_audio_name(idx, k))
        pm = prev.get(k) or {}
        # a recording is kept when the section spec OR the prior render marks it
        # recorded AND the take is on disk — the creator's voice always wins
        recorded = (bool(sp.get("recorded")) or bool(pm.get("recorded"))) \
            and af.exists()
        if recorded:
            # creator's own take — keep it; (re)align only if we have no words
            words = pm.get("words") or _align_recorded(af, say)
            print(f"  seg{idx}.s{k} — recorded take kept "
                  f"({ffprobe_duration(af):.1f}s)")
        elif af.exists() and pm.get("say", "") == say \
                and pm.get("expression", "") == seg_expr and not pm.get("recorded"):
            # unchanged TTS section → reuse its words, but BACKFILL via whisper
            # when they're missing (sections voiced before whisper was installed,
            # or by a no-timing engine, cached wordless — without this they stay
            # held-caption forever while later sections get karaoke, so the video
            # switches caption style mid-way)
            words = pm.get("words") or _align_recorded(af, say)
        else:
            af.unlink(missing_ok=True)
            words = _synth_line(say, af, engine, spk, expression=seg_expr) or []
            if not words:                         # Inworld/no-timing engine
                words = _align_recorded(af, say)
            print(f"  seg{idx}.s{k} [{seg['label']}] {ffprobe_duration(af):.1f}s")
        out.append({"k": k, "say": say, "file": af.name,
                    "duration": round(ffprobe_duration(af), 3),
                    "expression": seg_expr, "recorded": recorded, "words": words})
    return out


def recordable_units(script: dict) -> list:
    """The voice units the creator can record/re-record, in order — one per
    section of a section-native beat, else one per segment (dialogue/native beats
    excluded). Each: {index, section|None, label, say, name (canonical audio)}."""
    units = []
    for i, seg in enumerate(script.get("segments", [])):
        if seg.get("native_audio"):
            continue
        specs = _section_specs(seg)
        if specs:
            for k, sp in enumerate(specs):
                units.append({"index": i, "section": k, "label": seg["label"],
                              "say": (sp.get("say") or "").strip(),
                              "name": section_audio_name(i, k)})
        else:
            units.append({"index": i, "section": None, "label": seg["label"],
                          "say": (seg.get("text") or "").strip(),
                          "name": f"seg{i}.mp3"})
    return units


def apply_recording(folder: Path, seg_index: int, section, src: Path) -> dict:
    """Ingest a creator's RECORDED take for one voice unit: transcode it to the
    canonical audio path, mark it recorded in script.json (so step_voice keeps it
    and never TTSes over it), align word timings for captions (whisper if
    installed, else a held section-text caption), and refresh audio.json so the
    cut + the UI see it at once. Returns {ok, duration, words, aligned}."""
    import recording
    folder = Path(folder)
    sp = folder / "script.json"
    script = json.loads(sp.read_text())
    seg = script["segments"][seg_index]
    if section is None:
        name = f"seg{seg_index}.mp3"
        say = (seg.get("text") or "").strip()
    else:
        name = section_audio_name(seg_index, section)
        specs = seg.get("shots") or []
        say = (specs[section].get("say") or "").strip() if section < len(specs) else ""
    dest = config.art(folder, name)
    if not recording.ingest(src, dest):
        return {"ok": False, "error": "could not transcode the recording"}
    # mark the intent in script.json (durable across audio.json regeneration)
    if section is None:
        seg["voice_recorded"] = True
    else:
        seg["shots"][section]["recorded"] = True
    sp.write_text(json.dumps(script, indent=2))
    words = _align_recorded(dest, say)
    dur = round(ffprobe_duration(dest), 3)
    # refresh audio.json in place if it exists (so the cut/UI use it immediately)
    ap = folder / "audio.json"
    if ap.exists():
        a = json.loads(ap.read_text())
        for entry in a.get("segments", []):
            if entry.get("index") != seg_index:
                continue
            if section is None:
                entry.update({"file": name, "recorded": True,
                              "duration": dur, "words": words})
                entry.pop("native_audio", None)
            else:
                secs = entry.setdefault("sections", [])
                row = next((s for s in secs if s.get("k") == section), None)
                if row is None:
                    row = {"k": section, "say": say, "file": name}
                    secs.append(row)
                    secs.sort(key=lambda s: s.get("k", 0))
                row.update({"file": name, "recorded": True,
                            "duration": dur, "words": words})
            entry["duration"] = round(
                sum(s["duration"] for s in entry["sections"]), 3) \
                if entry.get("sections") else dur
        ap.write_text(json.dumps(a, indent=2))
    # any assembled cut is now stale
    archive(folder, "short.mp4", "merged.mp4", "ai_review.json")
    return {"ok": True, "duration": dur, "words": len(words),
            "aligned": bool(words)}


def clear_recording(folder: Path, seg_index: int, section) -> bool:
    """Drop a recorded take and revert the unit to AI voice: archive the file,
    clear the recorded marker, and bust audio.json so step_voice re-synthesizes."""
    folder = Path(folder)
    sp = folder / "script.json"
    script = json.loads(sp.read_text())
    seg = script["segments"][seg_index]
    name = (section_audio_name(seg_index, section) if section is not None
            else f"seg{seg_index}.mp3")
    archive(folder, name)
    if section is None:
        seg.pop("voice_recorded", None)
    elif (seg.get("shots") or []) and section < len(seg["shots"]):
        seg["shots"][section].pop("recorded", None)
    sp.write_text(json.dumps(script, indent=2))
    archive(folder, "audio.json", "short.mp4", "merged.mp4", "ai_review.json")
    return True


def keyframe_name(i: int, section) -> str:
    """The canonical keyframe filename for segment i, sub-shot `section`
    (None/0 → key<i>.png; k≥1 → key<i>_<k>.png) — the SAME names generation +
    caching use, so an uploaded image drops straight into the pipeline."""
    return (f"key{i}.png" if section in (None, 0) else f"key{i}_{section}.png")


def import_keyframe(folder: Path, i: int, section, src: Path) -> dict:
    """Save a creator-UPLOADED image as the canonical keyframe (`key<i>[_k].png`),
    cover-cropped to the 9:16 keyframe format — so a hand-picked / hand-made frame
    drops straight into assembly + as the clip start-image. Archives any existing
    keyframe (restorable) and busts the cut. Returns {ok, name}."""
    folder = Path(folder)
    src = Path(src)
    if not src.exists():
        return {"ok": False, "error": "no uploaded file"}
    name = keyframe_name(i, section)
    archive(folder, name)                         # version the old keyframe
    dest = config.art(folder, name)
    base = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}")
    r = sh(["ffmpeg", "-y", "-i", str(src), "-vf", base, "-frames:v", "1",
            str(dest)])
    if r.returncode != 0 or not dest.exists():    # odd input → plain convert
        r = sh(["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", str(dest)])
    if not dest.exists():
        return {"ok": False, "error": "could not read that image"}
    # the cut is stale; a clip generated from the OLD keyframe still PLAYS at
    # assembly (presence wins) — flagged so the UI can offer to regen it
    archive(folder, "short.mp4", "merged.mp4", "ai_review.json")
    clip = config.art(folder, (f"clip{i}.mp4" if section in (None, 0)
                               else f"clip{i}_{section}.mp4"))
    return {"ok": True, "name": name, "has_clip": clip.exists()}


def import_clip(folder: Path, i: int, section, src: Path) -> dict:
    """Save a creator-UPLOADED video as the canonical clip (`clip<i>[_k].mp4`),
    conformed (cover-cropped) to the 1080x1920@30 clip format — so a hand-picked
    or self-shot clip drops straight into assembly in place of a generated one.
    Keeps the clip's own audio (used when the segment is native_audio/mix).
    Archives any existing clip (restorable) and busts the cut. {ok, name}."""
    folder = Path(folder)
    src = Path(src)
    if not src.exists():
        return {"ok": False, "error": "no uploaded file"}
    name = (f"clip{i}.mp4" if section in (None, 0)
            else f"clip{i}_{section}.mp4")
    archive(folder, name)                         # version the old clip
    dest = config.art(folder, name)
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    r = sh(["ffmpeg", "-y", "-i", str(src), "-vf", base,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(dest)])
    if r.returncode != 0 or not dest.exists():    # no/odd audio → drop the track
        r = sh(["ffmpeg", "-y", "-i", str(src), "-vf", base,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                "-movflags", "+faststart", str(dest)])
    if not dest.exists():
        return {"ok": False, "error": "could not read that video"}
    archive(folder, "short.mp4", "merged.mp4", "ai_review.json")  # bust the cut
    return {"ok": True, "name": name}


def _spoken_norm(text) -> str:
    """Whitespace-normalized spoken text, for drift comparisons (a sectioned
    beat's manifest text is the joined per-section `say`, which collapses the
    segment text's internal whitespace)."""
    return " ".join((text or "").split())


def step_voice(script: dict, folder: Path, tpl: dict) -> dict:
    """segN.mp3 per segment + audio.json. Higgsfield Inworld TTS by default;
    TTS_ENGINE=edge (env or template) uses free edge-tts (adds karaoke word
    timings). Multi-character: a script `cast` assigns a distinct voice per
    character and each segment is voiced by its `speaker` (or a `lines`
    dialogue list, each line in its speaker's voice)."""
    ap = folder / "audio.json"
    if ap.exists():
        a = json.loads(ap.read_text())
        amap = {s["index"]: s for s in a["segments"]}
        # bust the cache if a segment's audio SOURCE changed since the last
        # render — native_audio (clip's own track, no seg.mp3) or clip_voice
        # (clip speaks; seg.mp3 kept as the driver but its captions are dropped)
        flags_ok = all(
            bool(seg.get("native_audio"))
            == bool(amap.get(i, {}).get("native_audio"))
            and bool(clip_voice_on(seg))
            == bool(amap.get(i, {}).get("clip_voice"))
            and bool(_sectioned(seg)) == bool(amap.get(i, {}).get("sections"))
            for i, seg in enumerate(script["segments"]))
        # …and if a segment's SPOKEN TEXT drifted from what was last voiced
        # (a script edit that reached voice without an explicit invalidation)
        # — self-heal by re-voicing instead of replaying the old lines
        text_ok = all(
            _spoken_norm(seg.get("text"))
            == _spoken_norm(amap.get(i, {}).get("text"))
            for i, seg in enumerate(script["segments"])
            if not seg.get("native_audio"))
        flags_ok = flags_ok and text_ok
        # a sectioned segment caches on its per-section files; a plain one on its
        # seg<i>.mp3 (native segments carry no audio file)
        def _seg_cached(s):
            secs = s.get("sections")
            if secs:
                return all(config.art(folder, sc["file"]).exists() for sc in secs)
            return (not s.get("file") or s.get("native_audio")
                    or config.art(folder, s["file"]).exists())
        if flags_ok and all(_seg_cached(s) for s in a["segments"]):
            # cached — but BACKFILL karaoke timings onto any wordless beat (voiced
            # before whisper was installed) by aligning the existing audio in
            # place, so the captions don't switch style mid-video. No re-synthesis.
            if _backfill_words(a, folder):
                ap.write_text(json.dumps(a, indent=2))
                print("  audio.json (cached) — backfilled karaoke word timings")
            else:
                print(f"  audio.json (cached, {a['total']:.1f}s)")
            return a
    engine = os.environ.get("TTS_ENGINE", tpl.get("tts_engine", "higgsfield"))
    if engine in ENGINES:
        ok, reason = ENGINES[engine].available()
        if not ok:
            print(f"  ! tts_engine={engine} unavailable ({reason}) — using "
                  f"free edge-tts (with karaoke timings) instead",
                  file=sys.stderr)
            engine = FALLBACK_ENGINE
    voice = os.environ.get("HF_VOICE", tpl.get("voice", "Mark (en)"))
    cast = script.get("cast") or {}
    resolve, assigned = _voice_resolver(cast, engine, voice)
    if assigned:
        print("  cast: " + ", ".join(f"{n}→{v}" for n, v in assigned.items()))
    # keep word timings of cached segments (e.g. after a hook-only reset —
    # edge-tts only yields them while generating)
    prev = json.loads(ap.read_text())["segments"] if ap.exists() else []
    prev_words = {s["index"]: s["words"] for s in prev
                  if s.get("words") and config.art(folder, s["file"]).exists()}
    prev_lines = {s["index"]: s.get("lines") or [] for s in prev}
    prev_secs = {s["index"]: s.get("sections") or [] for s in prev}
    prev_texts = {s["index"]: s.get("text") for s in prev}
    manifest = []
    script_dirty = False
    for i, seg in enumerate(script["segments"]):
        out = config.art(folder, f"seg{i}.mp3")
        dlg = _seg_dialogue(seg)
        seg_expr = seg.get("expression") or seg.get("delivery") or ""
        line_meta = []
        sect_specs = _section_specs(seg)
        if sect_specs and (
                _spoken_norm(" ".join((sp.get("say") or "").strip()
                                      for sp in sect_specs))
                != _spoken_norm(seg.get("text"))):
            # the sections' derived `say` no longer matches the locked text —
            # a script edit reached voice without a re-split. Re-derive each
            # section's `say` from the CURRENT text (self-heal), then persist.
            print(f"  seg{i} [{seg['label']}] — text changed, re-deriving its "
                  f"sections from the new script")
            import creative
            creative._sectionize_beats([seg])
            script_dirty = True
            sect_specs = _section_specs(seg)
        if sect_specs:
            # SECTION-NATIVE beat: voice each section to its own file so caption +
            # audio + video stay in sync section by section (no segment seg<i>.mp3)
            secs = _synth_sections(seg, engine, resolve, folder, i,
                                   sect_specs, prev_secs.get(i))
            spk = seg.get("speaker") or (seg.get("lines") or [{}])[0].get("speaker")
            entry = {"index": i, "label": seg["label"],
                     "text": " ".join(s["say"] for s in secs).strip(),
                     "overlay": seg.get("overlay", ""), "speaker": spk,
                     "file": "", "sections": secs,
                     "duration": round(sum(s["duration"] for s in secs), 3),
                     "words": []}
            if seg_expr:
                entry["expression"] = seg_expr
            manifest.append(entry)
            print(f"  seg{i} [{seg['label']}] — {len(secs)} sections, "
                  f"{entry['duration']:.1f}s")
            continue
        if seg.get("native_audio"):
            # the generated clip carries this segment's audio — skip TTS
            manifest.append({"index": i, "label": seg["label"],
                             "text": seg.get("text", ""),
                             "overlay": seg.get("overlay", ""), "speaker": None,
                             "file": "", "native_audio": True,
                             "duration": 0.0, "words": []})
            print(f"  seg{i} [{seg['label']}] — native clip audio (no TTS)")
            continue
        if seg.get("voice_recorded") and out.exists():
            # a whole-segment RECORDING (not sectioned) — keep the creator's take,
            # never TTS over it; align for captions (whisper) if we have no words
            words = (prev_words.get(i)
                     or _align_recorded(out, seg.get("text", "")))
            spk = seg.get("speaker")
            entry = {"index": i, "label": seg["label"],
                     "text": seg.get("text", ""),
                     "overlay": seg.get("overlay", ""), "speaker": spk,
                     "file": out.name, "recorded": True,
                     "duration": round(ffprobe_duration(out), 3), "words": words}
            if seg_expr:
                entry["expression"] = seg_expr
            manifest.append(entry)
            print(f"  seg{i} [{seg['label']}] — recorded take "
                  f"({entry['duration']:.1f}s)")
            continue
        # a cached seg.mp3 is stale when the manifest says it voiced DIFFERENT
        # text (only ever judged against a real manifest entry — a missing
        # audio.json alone never forces a re-synthesis of kept takes)
        stale = (out.exists() and i in prev_texts
                 and _spoken_norm(prev_texts[i]) != _spoken_norm(seg.get("text")))
        if stale:
            print(f"  seg{i} [{seg['label']}] — text changed, re-voicing")
            archive(folder, f"seg{i}.mp3")       # supersede, never delete
        if out.exists() and not stale:
            words = prev_words.get(i, [])
            line_meta = prev_lines.get(i, [])
        elif len(dlg) > 1 and _line_files_ready(folder, i, dlg,
                                                prev_lines.get(i), seg_expr):
            # timing changed but the spoken audio is unchanged → recompose FREE
            for k, ln in enumerate(dlg):
                ln["words"] = (prev_lines[i][k] or {}).get("words", [])
            words, _t = compose_segment(seg, out, folder, i,
                                        _line_paths(folder, i, len(dlg)))
            line_meta = _line_manifest(folder, i, dlg, seg_expr)
            print("  seg%d recomposed (line timing)" % i)
        else:
            words = _synth_segment(seg, out, engine, resolve, folder, i)
            line_meta = _line_manifest(folder, i, dlg, seg_expr) if len(dlg) > 1 else []
        spk = seg.get("speaker") or (seg.get("lines") or [{}])[0].get("speaker")
        entry = {"index": i, "label": seg["label"],
                 "text": seg.get("text", ""),
                 "overlay": seg.get("overlay", ""), "speaker": spk,
                 "file": out.name,
                 "duration": ffprobe_duration(out), "words": words}
        # clip_voice keeps the seg.mp3 as the lip-sync driver but plays the
        # clip's own audio at assembly → its captions are dropped (the TTS word
        # times would drift against the clip); the flag does it, not nulled words
        if clip_voice_on(seg):
            entry["clip_voice"] = True
        if seg_expr:
            entry["expression"] = seg_expr
        if line_meta:
            entry["lines"] = line_meta
        manifest.append(entry)
        print(f"  seg{i} [{seg['label']}]" + (f" ({spk})" if spk else "")
              + f" {manifest[-1]['duration']:.1f}s")
    if script_dirty:
        # sections were re-derived from the current text — persist so the
        # script on disk and the voiced sections agree
        (folder / "script.json").write_text(json.dumps(script, indent=2))
    a = {"topic": script.get("topic"), "verdict": script.get("verdict"),
         "engine": engine, "cast": assigned,
         "total": sum(m["duration"] for m in manifest), "segments": manifest}
    ap.write_text(json.dumps(a, indent=2))
    return a


def _direction(folder: Path) -> dict:
    p = folder / "script.json"
    if p.exists():
        return json.loads(p.read_text()).get("direction") or {}
    return {}


def _image_prompt(seg: dict, d: dict, tpl: dict, style: str = None) -> str:
    ch = config.channel()
    seg_cfg = ch.segment(seg["label"])
    scene = seg.get("visual") or (f"{d.get('title', '')} "
                                  f"{seg['label'].lower()} scene")
    overlay = (seg.get("overlay") or "").strip()
    txt = ""
    # when reveal is on (default) the answer text is composited LATER as a
    # timed overlay (so the frame doesn't spoil it); only bake it into the
    # keyframe when reveal is disabled
    if overlay and not tpl.get("reveal_overlay_text", True):
        rule = seg_cfg.get("ink")
        if rule == "verdict" and ch.positive_verdict:
            ink = ("deep green" if d.get("verdict") == ch.positive_verdict
                   else "crimson red")
        else:
            ink = rule if rule and rule != "verdict" else "ivory white"
        typo = tpl.get("overlay_text_style",
                       "Bold, clean editorial typography")
        txt = (f' {typo} reading "{overlay}" in {ink} ink, placed high in '
               "the frame, slightly rotated.")
    # a brand badge (channel.yaml segment `badge:`) — a corner mark on that
    # segment's keyframe; its LOOK is the template's `badge_style` token
    # ({badge} substituted), so the aesthetic is channel content, not engine
    badge = (seg_cfg.get("badge") or "").strip()
    badge_style = tpl.get(
        "badge_style",
        'A small, tasteful badge emblem reading "{badge}" set into the '
        'TOP-LEFT corner, matching the channel\'s look.')
    btxt = (" " + badge_style.replace("{badge}", badge).strip()
            if badge else "")
    extra = (seg.get("visual_extra") or "").strip()
    extra = f" {extra.rstrip('.')}." if extra else ""
    # in reveal mode every caption/figure is composited as a timed overlay,
    # so the keyframe itself must be a CLEAN wordless scene — otherwise the
    # image model bakes in its own captions and they collide with our
    # overlays. (The brand stamp, if any, is the only allowed mark.)
    notext = ""
    if tpl.get("reveal_overlay_text", True):
        notext = (f" The ONLY stamp/seal/text anywhere in the image is that "
                  f"single top-left badge — no other stamps, seals, rubber "
                  f"marks or insignia anywhere else in the frame; "
                  if btxt else
                  " There are NO rubber-stamps, seals, badges or brand marks "
                  "anywhere in the frame; ") + (
            "render a completely CLEAN, WORDLESS scene — absolutely no "
            "captions, titles, labels, numbers, charts, signage, logos, "
            "wall decals, subtitles or writing of any kind anywhere in the "
            "frame.")
    return f"{scene}.{extra} {style or tpl['style']}.{txt}{btxt}{notext}"


def shot_list(script: dict, audio: dict, folder: Path) -> list[dict]:
    cast = script.get("cast") or {}            # script-declared character descs
    shots = []
    for seg, am in zip(script["segments"], audio["segments"]):
        i = am["index"]
        shots.append({"index": i, "label": seg["label"], "seg": seg,
                      "cast": cast,
                      "need": am["duration"] + SEG_PAD,
                      "key": config.art(folder, f"key{i}.png"),
                      "clip": config.art(folder, f"clip{i}.mp4")})
    return shots


# ---------- multi-shot segments (a beat split into several distinct shots) ----
# A segment may carry `shots: [ {visual, clip_motion, motion, animate, …}, … ]`
# — the director's plan to cover a long beat with SEVERAL distinct shots stitched
# together (instead of one shot stretched/frozen, which reads as a repeat). When
# absent (or a single entry) the segment is single-shot — exactly the old
# behaviour, byte-for-byte. Sub-shot 0 reuses the canonical key{i}.png /
# clip{i}.mp4 names so single-shot caching/versioning is untouched; sub-shots
# k≥1 use key{i}_{k}.png / clip{i}_{k}.mp4. Overlays, the voiceover and
# captions stay SEGMENT-level (one VO plays continuously while the visuals cut);
# segment-level audio modes (native_audio / clip_voice / mix_clip_audio) only
# apply to single-shot segments — a multi-shot beat is a narrated montage.

# fields a sub-shot may override on its parent segment (everything else inherits)
_SUBSHOT_FIELDS = ("visual", "clip_motion", "motion", "animate", "tease",
                   "cast_in_shot", "outfit", "angle", "image_prompt",
                   "clip_prompt_text", "motion_extra", "visual_extra",
                   "ref_idx", "clip_fill", "clip_mode", "use_still")
# fields a sub-shot must NOT inherit (segment-level only)
_SEGMENT_ONLY = ("native_audio", "clip_voice", "mix_clip_audio", "full_clip",
                 "overlay", "overlays", "shots")


def _shot_specs(seg: dict):
    """The raw sub-shot spec list for a segment, or None when single-shot."""
    specs = seg.get("shots")
    if isinstance(specs, list) and len(specs) >= 2:
        return specs
    return None


def shot_plan(seg: dict, i: int) -> list[dict]:
    """Canonical slot list for segment i — the SINGLE source of truth shared by
    generation (step_keyframes/step_clips) and presence-caching (_done_map), so
    they never disagree on which files should exist. Each slot:
    {k, key, clip, animate, clip_voice}."""
    specs = _shot_specs(seg)
    if not specs:
        return [{"k": 0, "key": f"key{i}.png", "clip": f"clip{i}.mp4",
                 "animate": seg.get("animate", True),
                 "clip_voice": clip_voice_on(seg)}]
    return [{"k": k,
             "key": (f"key{i}.png" if k == 0 else f"key{i}_{k}.png"),
             "clip": (f"clip{i}.mp4" if k == 0 else f"clip{i}_{k}.mp4"),
             "animate": sp.get("animate", True), "clip_voice": False}
            for k, sp in enumerate(specs)]


def _subshots(shot: dict, folder: Path) -> list[dict]:
    """Expand a segment `shot` into shot-shaped sub-shot dicts (one per slot),
    each reusing the per-shot machinery (step_keyframe/step_clip/_conform). A
    single-shot segment returns [shot] unchanged."""
    seg = shot["seg"]
    specs = _shot_specs(seg)
    if not specs:
        return [shot]
    i = shot["index"]
    weights = [max(float(sp.get("weight") or 1.0), 0.01) for sp in specs]
    total_w = sum(weights) or 1.0
    subs = []
    for slot, sp, wt in zip(shot_plan(seg, i), specs, weights):
        view = {k: v for k, v in seg.items() if k not in _SEGMENT_ONLY}
        for f in _SUBSHOT_FIELDS:
            if sp.get(f) is not None:
                view[f] = sp[f]
        subs.append({"index": i, "sub": slot["k"], "label": shot["label"],
                     "seg": view, "cast": shot.get("cast") or {},
                     "key": config.art(folder, slot["key"]),
                     "clip": config.art(folder, slot["clip"]),
                     "weight": wt,
                     # each sub-shot's clip is generated to cover its weighted
                     # slice of the beat (matches the assembly split)
                     "need": shot["need"] * wt / total_w})
    return subs


def clips_per_segment_cap(tpl: dict) -> int:
    """Max ANIMATED clips per multi-shot segment (the rest are free Ken Burns
    stills) — bounds credit spend when a beat is split into many shots."""
    try:
        return max(1, int(tpl.get("max_clips_per_segment", 2)))
    except (TypeError, ValueError):
        return 2


def _plan_clip_wanted(plan: list[dict], tpl: dict) -> list[bool]:
    """Per-slot: should a generated clip exist? `animate` (default True) gated by
    the per-segment cap for multi-shot beats; a clip_voice slot always animates
    and is exempt from the cap."""
    cap = clips_per_segment_cap(tpl)
    multi = len(plan) > 1
    want, animated = [], 0
    for slot in plan:
        must = slot.get("clip_voice")
        # `use_still` is the creator's explicit "render this beat as a still" — it
        # forces a Ken Burns frame instead of a generated clip (a clip_voice shot
        # must still animate to speak, so it's exempt)
        w = bool(must or (slot.get("animate") is not False
                          and not slot.get("use_still")))
        if w and not must and multi and animated >= cap:
            w = False
        if w:
            animated += 1
        want.append(w)
    return want


def expected_keyframe_names(segs: list[dict]) -> list[str]:
    return [slot["key"] for i, seg in enumerate(segs)
            for slot in shot_plan(seg, i)]


def expected_clip_names(segs: list[dict], tpl: dict) -> list[str]:
    names = []
    for i, seg in enumerate(segs):
        plan = shot_plan(seg, i)
        for slot, w in zip(plan, _plan_clip_wanted(plan, tpl)):
            if w:
                names.append(slot["clip"])
    return names


# ---------- section-native segments (each shot voices its OWN sentence) -------
# A multi-shot beat becomes SECTION-NATIVE when every shot carries a `say` — the
# exact sentence(s) that shot narrates. Then caption + audio + video are produced
# SECTION BY SECTION so they stay in lock-step: each section voices its own
# `seg<i>_s<k>.mp3` (TTS or a recording), its keyframe/clip slot (key<i>[_k] /
# clip<i>[_k], the same names as a plain montage), and renders as its own part
# `part<i>_s<k>.mp4` whose length is driven by THAT section's audio. The segment's
# overlays attach to the LAST section (the reveal). A multi-shot beat with
# NO `say` is the older narrated-montage (one VO, weighted visual slices).

def _section_specs(seg: dict):
    """The shot specs of a SECTION-NATIVE segment (every spec has its own `say`),
    else None. A multi-shot beat whose shots carry no `say` is a montage, not
    sectioned — it returns None here and flows through the montage path."""
    specs = _shot_specs(seg)
    if not specs:
        return None
    if all((sp.get("say") or "").strip() for sp in specs):
        return specs
    return None


def _sectioned(seg: dict) -> bool:
    return _section_specs(seg) is not None


def section_audio_name(i: int, k: int) -> str:
    """Canonical per-section voiceover name (section 0 still gets its own _s0 file
    so every section is symmetric — the plain seg<i>.mp3 stays the montage VO)."""
    return f"seg{i}_s{k}.mp3"


def _section_overlay_specs(norm: list, durs: list) -> list:
    """Distribute a beat's TIMED `overlays` (each `start`/`end` a fraction of the
    BEAT — or seconds) across its sections by BEAT-TIME, so every overlay shows in
    the section that actually plays at that moment, with section-relative timing.
    The fix for "the overlay shows up late": the director writes the overlays as a
    sequence over the whole beat, but section-native rendering used to cram them ALL
    onto the (often long) last section, so the early overlays appeared way out of
    sync with the narration. Returns a list of overlay-specs PER section."""
    D = sum(durs) or 1.0

    def frac(v, dflt):
        if v is None:
            return dflt
        try:
            v = float(v)
        except (TypeError, ValueError):
            return dflt
        if v < 0:
            return 0.0
        return min(v, 1.0) if v <= 1.0 else min(v / D, 1.0)   # >1 = seconds

    per, cum = [[] for _ in durs], 0.0
    for k, d in enumerate(durs):
        w0, w1 = cum / D, (cum + d) / D
        cum += d
        span = max(w1 - w0, 1e-6)
        for oi, ov in enumerate(norm):
            s, e = frac(ov.get("start"), 0.0), frac(ov.get("end"), 1.0)
            if s < w1 and e > w0:                       # overlaps this section
                no = dict(ov)
                # the overlay's STABLE index in the segment list → its PNG name
                # (text<i>_<oi>.png), so a distributed subset still points at the
                # right rendered image regardless of its position in the section
                no["_oi"] = oi
                no["start"] = round(max(0.0, min((s - w0) / span, 1.0)), 3)
                no["end"] = (None if e >= w1 - 1e-6     # runs to/past the section
                             else round(max(0.0, min((e - w0) / span, 1.0)), 3))
                # started before this section → it's a continuation: show at once
                if s <= w0 + 1e-6:
                    no["_continued"] = True
                per[k].append(no)
    return per


def _segment_sections(shot: dict, folder: Path, audio_seg: dict = None) -> list:
    """Expand a SECTION-NATIVE segment into synthetic single-shot `shot` dicts —
    one per section — each shaped so `_conform` renders it like a normal beat:
    its own audio (`seg<i>_s<k>.mp3`), keyframe/clip slot, and the segment's
    overlays distributed across sections by beat-time (so they track the
    narration). [] when not sectioned."""
    seg = shot["seg"]
    specs = _section_specs(seg)
    if not specs:
        return []
    i = shot["index"]
    plan = shot_plan(seg, i)
    secmeta = {s.get("k"): s for s in ((audio_seg or {}).get("sections") or [])}
    n = len(specs)
    # per-section beat durations (raw audio; ratios are speed-independent), to
    # place timed overlays in the section that plays at their beat-time
    durs = [float((secmeta.get(k) or {}).get("duration") or 0) for k in range(n)]
    if not any(durs):
        durs = [max(float(sp.get("weight") or 1.0), 0.01) for sp in specs]
    raw_ov = seg.get("overlays")
    is_seq = isinstance(raw_ov, list) and len(raw_ov) > 0
    seq = _section_overlay_specs(_seg_overlays(seg), durs) if is_seq else None
    out = []
    for k, (slot, sp) in enumerate(zip(plan, specs)):
        # _SEGMENT_ONLY already strips shots/overlay/overlays + the clip-audio
        # modes. Overlays are re-attached per section so they track the
        # narration: a TIMED `overlays` sequence is distributed across sections
        # by beat-time (`seq`); a single persistent `overlay` rides EVERY
        # section (capped-early reveal, instant on continuations).
        view = {kk: vv for kk, vv in seg.items() if kk not in _SEGMENT_ONLY}
        for f in _SUBSHOT_FIELDS:
            if sp.get(f) is not None:
                view[f] = sp[f]
        if is_seq:
            view["overlays"] = seq[k]
            # a continuation overlay (started in an earlier section) shows at once
            if any(o.get("_continued") for o in seq[k]):
                view["overlay_continued"] = True
        elif (seg.get("overlay") or "").strip():
            view["overlay"] = seg["overlay"]
            if k > 0:                          # already up — persist, no re-fade
                view["overlay_reveal_at"] = 0.0
                view["overlay_continued"] = True
        meta = secmeta.get(k) or {}
        out.append({"index": i, "section": k, "label": shot["label"],
                    "seg": view, "cast": shot.get("cast") or {},
                    "key": config.art(folder, slot["key"]),
                    "clip": config.art(folder, slot["clip"]),
                    "audio": config.art(folder, section_audio_name(i, k)),
                    "say": (sp.get("say") or "").strip(),
                    "words": meta.get("words", []),
                    "recorded": bool(meta.get("recorded")),
                    "duration": meta.get("duration", 0.0)})
    return out


SAFE_TAIL = prompts.load("clip.safe_tail")


def ref_candidates(idx: int, sub=None, seg: dict = None) -> list:
    """Ordered candidate keyframe NAMES this shot may visually continue from,
    MOST-SPECIFIC first — decided INDEPENDENTLY per shot, on paper (no disk
    check), so it drives BOTH the real `--image` reference (the first candidate
    that exists, `_resolve_continuity_ref`) AND the timeline dependency arrow
    (the first candidate, `keyframe_dep`). The fix for "every shot pulls from
    the very first shot": key0 is now only a LAST resort, never the forced lead.

      - a SUB-SHOT (k>=1) of a multi-shot beat chains off the PREVIOUS shot of
        its OWN beat (key<i>_<k-1>, or key<i>.png for k=1) — so a beat is
        internally coherent and anchored to its own lead shot, not segment 0;
      - the LEAD shot of a beat (or a single-shot segment) follows the
        DIRECTOR's `ref_idx` — the specific earlier beats it mirrors, in the
        director's own order (no key0 forced to the front). Undirected, it
        continues from the immediately-preceding beat. key0 (the look-setter)
        is appended only as a final fallback.
    An explicit `ref_idx: []` on a LEAD shot is the director's "fresh, distinct
    shot" signal → no continuity ref at all (empty)."""
    c = []
    if sub:                                   # a sub-shot chains within its beat
        c.append(keyframe_name(idx, sub - 1))
    if seg is not None and "ref_idx" in seg:
        want = list(seg["ref_idx"])
        if not want and not sub:
            return []                         # lead shot: deliberately fresh
        c += [f"key{j}.png" for j in want
              if isinstance(j, int) and 0 <= j < idx]
    elif idx > 0 and not sub:
        c.append(f"key{idx - 1}.png")         # undirected: continue prev beat
    if idx > 0 and "key0.png" not in c:
        c.append("key0.png")                  # last-resort look anchor
    seen, out = set(), []
    for n in c:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def keyframe_dep(idx: int, sub=None, seg: dict = None):
    """The single keyframe NAME this shot is PLANNED to depend on (the timeline
    arrow target) — independent of what's on disk. None = a fresh shot."""
    cands = ref_candidates(idx, sub, seg)
    return cands[0] if cands else None


def _resolve_continuity_ref(folder: Path, idx: int, sub, seg: dict):
    """The first independently-chosen candidate (ref_candidates) that EXISTS on
    disk — the actual continuity `--image`. None when the shot is fresh or
    nothing it could match has been generated yet."""
    for name in ref_candidates(idx, sub, seg):
        p = config.art(folder, name)
        if p.exists():
            return p
    return None


def _shot_characters(seg: dict) -> list:
    """(name, member) for each CHANNEL cast member who appears in this shot —
    so the keyframe features them, conditioned on their avatar. The DIRECTOR
    decides WHO is in the frame via `cast_in_shot` (a list of names, or [] for a
    scene with NO people — not every frame needs the cast); absent that field it
    defaults to whoever SPEAKS the segment."""
    ch = config.channel()
    if isinstance(seg.get("cast_in_shot"), list):
        names = list(seg["cast_in_shot"])      # director's explicit call ([]=none)
    else:
        names = []
        if seg.get("speaker"):
            names.append(seg["speaker"])
        for ln in seg.get("lines") or []:
            if ln.get("speaker"):
                names.append(ln["speaker"])
    out, seen = [], set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        m = ch.cast_member(n)
        if m:
            out.append((n, m))
    return out


def _shot_character_names(seg: dict) -> list:
    """Every character name the director/writer put in this shot (cast_in_shot,
    else the speaker(s)) — channel cast AND ad-hoc story characters alike. The
    raw names, deduped in order."""
    if isinstance(seg.get("cast_in_shot"), list):
        names = list(seg["cast_in_shot"])
    else:
        names = []
        if seg.get("speaker"):
            names.append(seg["speaker"])
        for ln in seg.get("lines") or []:
            if ln.get("speaker"):
                names.append(ln["speaker"])
    out, seen = [], set()
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# generic words that don't identify a PERSON — a shot whose only "name" is one
# of these isn't a recurring character to anchor (avoids pinning every "man" to
# the same face)
_GENERIC_NAME = {"young", "old", "older", "elderly", "teen", "teenage", "adult",
                 "a", "an", "the", "mr", "mrs", "ms", "dr", "present", "day",
                 "man", "woman", "men", "women", "person", "people", "guy",
                 "guys", "crowd", "someone", "figure", "narrator", "host",
                 "voice", "investor", "investors", "audience", "team"}


def _norm_name(n: str) -> set:
    """Identifying tokens of a character name, for fuzzy matching across shots
    ('young Brian' ~ 'Brian Chesky' via 'brian'). Generic person-words are
    dropped so a bare 'man'/'investor' never anchors."""
    import re as _re
    toks = _re.findall(r"[a-z]+", (n or "").lower())
    return {t for t in toks if t not in _GENERIC_NAME and len(t) > 1}


def _character_anchors(folder: Path, idx: int, seg: dict):
    """(--image args, prompt clause) pinning AD-HOC story characters — those
    WITHOUT a channel-cast avatar — to the FIRST earlier keyframe they appeared
    in, so a runtime-invented recurring character stays the SAME PERSON
    shot-to-shot instead of being redrawn from text every frame. Channel cast
    are anchored by their avatars in `_character_refs`; this covers everyone
    else (the whole point on a channel with no cast roster). Best-effort: never
    raises, empty when nothing matches."""
    ch = config.channel()
    # the director's deliberate fresh-look signal (ref_idx: []) means "this shot
    # is independent of earlier frames" — don't force an anchor image onto it
    # (it fights the distinct scene and can muddy the generation), same as the
    # key0 style anchor exemption in _reference_keys
    if isinstance(seg.get("ref_idx"), list) and not seg["ref_idx"]:
        return [], ""
    names = [n for n in _shot_character_names(seg) if not ch.cast_member(n)]
    if not names:
        return [], ""
    try:
        script = json.loads((folder / "script.json").read_text())
        segs = script.get("segments") or []
        scast = script.get("cast") or {}
    except (OSError, ValueError):
        return [], ""
    args, anchored = [], []
    for name in names:
        toks = _norm_name(name)
        if not toks:
            continue
        for j in range(min(idx, len(segs))):       # earliest prior match wins
            if any(toks & _norm_name(pn)
                   for pn in _shot_character_names(segs[j])):
                k = config.art(folder, f"key{j}.png")
                if k.exists():
                    args += ["--image", str(k)]
                    desc = ""
                    if isinstance(scast.get(name), dict):
                        desc = (scast[name].get("desc") or "").strip()
                    anchored.append(name + (f" ({desc})" if desc else ""))
                break
    if not args:
        return [], ""
    who = " and ".join(anchored)
    clause = (f" Keep {who} the SAME PERSON as in the reference portrait(s) — "
              "the identical face, age, hair and build, clearly recognizable as "
              "the same individual across shots; only the pose, expression and "
              "setting change.")
    return args, clause




def _character_refs(seg: dict):
    """(--image args for cast avatars, a prompt clause naming the characters)
    so a shot featuring channel cast renders the right people, matching their
    avatar. Avatars that don't exist on disk are still described, not imaged."""
    ch = config.channel()
    chars = _shot_characters(seg)
    if not chars:
        return [], ""
    # the director may dress a shot's characters for the scene; default formal
    outfit = (seg.get("outfit") or "formal").lower()
    # the director's camera angle picks the angle-matched face ref (a profile
    # shot uses the side portrait, not the front) — default front
    angle = (seg.get("angle") or "front").lower()
    if angle not in ("front", "side", "back"):
        angle = "front"
    args, descs = [], []
    for n, m in chars:
        # prefer the outfit + angle-matched FACE; fall back front, then avatar
        face = (ch.cast_ref(n, outfit=outfit, kind="face", angle=angle,
                            light="day")
                or ch.cast_ref(n, outfit=outfit, kind="face", angle="front",
                               light="day"))
        av = ch.cast_avatar(n)
        primary = face or (av if av.exists() else None)
        if primary is not None:
            args += ["--image", str(primary)]
        # the full-length shot adds body + outfit (the avatar is head-and-
        # shoulders only); match the angle, fall back to front
        full = (ch.cast_ref(n, outfit=outfit, kind="full", angle=angle)
                or ch.cast_ref(n, outfit=outfit, kind="full", angle="front")
                or ch.cast_ref(n, outfit="formal", kind="full", angle="front"))
        if full is not None:
            args += ["--image", str(full)]
        look = (m.get("appearance") or m.get("personality") or "").strip()
        descs.append(f"{n}" + (f" ({look})" if look else ""))
    who = " and ".join(descs)
    dressed = ("" if outfit == "formal" else
               f" Dress the character(s) in their {outfit} outfit.")
    clause = (f" This scene features {who}. Render each character faithfully — "
              "match their reference portrait for face, hair/hijab and outfit, "
              f"and keep them consistent and recognizable across shots.{dressed}")
    return args, clause


def _style_ref_args(tpl: dict):
    """(--image args, prompt clause) for a template's bundled STYLE reference
    images (`style_refs`) — the channel's look saved as art (e.g. one of the
    `templates/refs/<concept>/*.png` frames). Every keyframe is conditioned on
    them so the drawn medium/linework/palette stays on-style, while a clause
    tells the model to copy the STYLE, never the content. Paths resolve relative
    to the channel dir; missing files are skipped, so a template with no refs
    (or a stripped checkout) just degrades to the text `style` alone."""
    refs = tpl.get("style_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    base = config.channel().dir
    args = []
    for r in refs:
        p = Path(r)
        if not p.is_absolute():
            p = base / r
        if p.exists():
            args += ["--image", str(p)]
    if not args:
        return [], ""
    clause = (" Match the ILLUSTRATION STYLE of the style-reference image(s) — "
              "the medium, linework, brush/print texture, colour palette and "
              "overall rendering — but do NOT copy their content, characters, "
              "composition or any text; render THIS scene in that style.")
    return args, clause


def _first_image(args: list) -> list:
    """The FIRST `--image <path>` pair in an args list, or [] — the primary
    reference from a multi-image source (e.g. _character_refs returns the avatar
    portrait first, then a full-length)."""
    for i, a in enumerate(args):
        if a == "--image" and i + 1 < len(args):
            return ["--image", args[i + 1]]
    return []


def _single_keyframe_ref(shot: dict, folder: Path, tpl: dict):
    """ONE reference image (+ its matching prompt clause) for the keyframe.

    nano_banana muddies/averages when handed SEVERAL references, so we pass
    exactly one — the single most informative for this shot. Priority (identity
    first; a wrong face is the most jarring drift, and a prior keyframe of the
    subject already carries the world/style too):
      1. a channel cast member's avatar portrait (their canonical face)
      2. an ad-hoc recurring character's anchor — a prior in-style keyframe of them
      3. the continuity STYLE anchor key0 (the look-setter / established world)
      4. the template's bundled STYLE ref (the drawn medium, mainly for shot 0)
    Returns ([] , "") when nothing applies (the opening shot with no cast)."""
    seg = shot["seg"]
    img = _first_image(_character_refs(seg)[0])
    if img:
        return img, _character_refs(seg)[1]
    img = _first_image(_character_anchors(folder, shot["index"], seg)[0])
    if img:
        return img, _character_anchors(folder, shot["index"], seg)[1]
    if tpl.get("keyframe_continuity", True):
        idx = shot["index"]
        sub = shot.get("sub") if "sub" in shot else shot.get("section")
        ref = _resolve_continuity_ref(folder, idx, sub, seg)   # decided per shot
        if ref is not None:
            # an INTRA-beat ref (the previous shot of THIS same beat) is the next
            # instant of one continuous moment — keep subject + setting and
            # advance; a CROSS-beat ref is the established WORLD to re-enter
            intra = bool(sub) and ref.name in (keyframe_name(idx, (sub or 1) - 1),)
            clause = (" Continue directly from the reference image — the NEXT "
                      "instant of the same continuous shot: keep the same "
                      "subject, setting, wardrobe and palette, only advance the "
                      "action/framing; do NOT reproduce any text or overlay from "
                      "it." if intra else
                      " Match the reference image for the SAME world, lighting "
                      "and palette — a NEW, distinct shot in that world, not a "
                      "copy; do NOT reproduce any text or overlay from it, render "
                      "only the on-screen text above.")
            return ["--image", str(ref)], clause
    img = _first_image(_style_ref_args(tpl)[0])
    if img:
        return img, _style_ref_args(tpl)[1]
    return [], ""


def _keyframe_assembled(shot: dict, d: dict, folder: Path, tpl: dict):
    """(full text prompt, image --args, alt_prompts) for the keyframe — the
    EXACT prompt + reference image step_keyframe sends, factored out so the
    interactive session can SHOW (and let you edit) it before spending. A
    per-segment `image_prompt` override (set by the creator) is sent VERBATIM.

    Exactly ONE reference image is attached (see `_single_keyframe_ref`) — the
    image model degrades when given several, blending them into a muddy frame."""
    seg = shot["seg"]
    style = _direction(folder).get("style") or tpl["style"]
    ref_img, clause = _single_keyframe_ref(shot, folder, tpl)
    img_args = ["--aspect_ratio", "9:16"] + ref_img
    override = (seg.get("image_prompt") or "").strip()
    if override:
        return override, img_args, [f"{override} {SAFE_TAIL}"]
    base = _image_prompt(seg, d, tpl, style=style)
    full = base + clause
    alts = [f"{base}{clause} {SAFE_TAIL}",
            f"{seg.get('visual', '')}.{clause} {style}. {SAFE_TAIL}"]
    return full, img_args, alts


# -------- generation-prompt memory (the ⭐ staleness stars) --------
# Owner call 2026-07-02: a script rewrite KEEPS the paid images instead of
# archiving them — the UIs instead show a ⭐ on any keyframe/clip whose
# CURRENT prompt no longer matches the one it was generated from (a pure
# wording rewrite usually changes neither, so most images never star). The
# canonical prompt's hash is recorded in <run>/genmeta.json on every FRESH
# generation; regenerating a starred shot re-records and clears the star.
def _genmeta(folder: Path) -> dict:
    p = folder / "genmeta.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _prompt_sha(prompt: str) -> str:
    return hashlib.sha1((prompt or "").encode("utf-8", "replace")).hexdigest()


def record_gen_prompt(folder: Path, name: str, prompt: str) -> None:
    """Remember which canonical prompt `name` was generated from. Called only
    on a FRESH generation (never a cache hit, so a stale record is never
    silently overwritten). Best-effort — never fatal."""
    try:
        meta = _genmeta(folder)
        meta[name] = {"sha": _prompt_sha(prompt),
                      "when": datetime.now().isoformat(timespec="seconds")}
        (folder / "genmeta.json").write_text(json.dumps(meta, indent=2))
    except Exception:                            # noqa: BLE001 — advisory only
        pass


def gen_prompt_stale(folder: Path, name: str, current_prompt: str):
    """True = generated from a DIFFERENT prompt than today's (star it);
    False = matches; None = unknown (no record — an older run or a
    hand-dropped file; never starred, to avoid false alarms)."""
    rec = _genmeta(folder).get(name)
    if not rec or not rec.get("sha"):
        return None
    return rec["sha"] != _prompt_sha(current_prompt)


def keyframe_stale(shot: dict, d: dict, folder: Path, tpl: dict) -> bool:
    """⭐ for an on-disk keyframe whose current prompt differs from the one
    it was generated with. Never raises (a prompt-assembly hiccup in a state
    build must not break the UI)."""
    try:
        if not shot["key"].exists():
            return False
        return bool(gen_prompt_stale(folder, shot["key"].name,
                                     keyframe_prompt(shot, d, folder, tpl)))
    except Exception:                            # noqa: BLE001
        return False


def clip_stale(shot: dict, folder: Path, tpl: dict) -> bool:
    """⭐ for an on-disk clip whose current prompt differs from the one it
    was generated with (a clip_voice clip stars on a line rewrite — the
    spoken words are part of its prompt)."""
    try:
        if not shot["clip"].exists():
            return False
        return bool(gen_prompt_stale(folder, shot["clip"].name,
                                     clip_prompt(shot, tpl)))
    except Exception:                            # noqa: BLE001
        return False


def keyframe_prompt(shot: dict, d: dict, folder: Path, tpl: dict) -> str:
    """The full image prompt that WILL be sent — for the interactive preview."""
    return _keyframe_assembled(shot, d, folder, tpl)[0]


def step_keyframe(shot: dict, d: dict, folder: Path, tpl: dict) -> bool:
    """One keyframe (cached: generate() skips an existing file). A nsfw
    false-positive here is otherwise fatal, so retry with SFW rephrasings.
    Uses the director's per-episode `style` (bespoke world) when present,
    conditions on earlier keyframes (--image) so the world stays consistent
    shot-to-shot, and on the CAST avatars when a character is in the scene."""
    full, img_args, alts = _keyframe_assembled(shot, d, folder, tpl)
    fresh = not shot["key"].exists()             # vs a generate() cache hit
    ok = generate(tpl["image_model"], full,
                  shot["key"], (".png", ".jpg", ".webp"), img_args,
                  f"keyframe {shot['index']}", alt_prompts=alts,
                  validate=_healthy_image, postprocess=_relight_if_dark)
    if ok and fresh:
        record_gen_prompt(folder, shot["key"].name, full)   # ⭐ bookkeeping
    return ok


def step_keyframes(shots: list[dict], d: dict, folder: Path,
                   tpl: dict) -> list[dict]:
    for shot in shots:
        subs = _subshots(shot, folder)
        for sub in subs:
            tag = (f"{sub['index']}.{sub['sub']}" if len(subs) > 1
                   else str(sub["index"]))
            if not step_keyframe(sub, d, folder, tpl):
                die(f"keyframe {tag} failed — rerun to retry")
    return shots


def clip_duration(shot: dict, tpl: dict) -> int:
    """Seconds for this shot's clip — covers the VO, capped (clips >8s trip
    moderation false-positives)."""
    max_clip = tpl.get("max_clip_seconds", 8)
    return min(max_clip, max(4, math.ceil(shot["need"])))


CLIP_LOOK = prompts.load("clip.look")


def _clip_motion(shot: dict, tpl: dict) -> str:
    """The brief for a GENERATED clip — the director's dynamic `clip_motion`
    first (real action, not a zoom), then the gentler `motion`, then a
    template/default. A clip should never be just a zoom on a still."""
    seg = shot["seg"]
    return (seg.get("clip_motion") or seg.get("motion")
            or tpl["motion"].get(shot["label"])
            or "The subject comes alive with real movement and shifting light "
               "— elements animate, the camera glides with subtle parallax")


def _clip_keyframe_as_start(tpl: dict) -> bool:
    """Whether the clip pins the keyframe as the locked START frame
    (--start-image) vs a related reference (--image, the default — a fresh
    moving shot guided by the keyframe). Template `clip_keyframe_role`."""
    return str(tpl.get("clip_keyframe_role", "reference")).lower().startswith(
        "start")


def _speaker_desc(name: str, shot_cast: dict) -> str:
    """A short appearance line for a speaking character — the channel cast's
    `appearance` (the fixed roster) first, then the script's declared cast
    `desc`, so the video prompt can say WHO each speaker is."""
    m = config.channel().cast_member(name) or {}
    d = (m.get("appearance") or m.get("personality") or "").strip()
    if not d:
        d = ((shot_cast.get(name) or {}).get("desc") or "").strip()
    return d


def _scene_speakers(seg: dict) -> list:
    """Distinct speaker names in this segment, first-appearance order."""
    names = []
    if (seg.get("speaker") or "").strip():
        names.append(seg["speaker"].strip())
    for ln in _seg_dialogue(seg):
        spk = (ln.get("speaker") or "").strip()
        if spk:
            names.append(spk)
    out = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def _scene_characters(shot: dict) -> list:
    """Everyone PRESENT in the scene for the video roster: the speakers first,
    then any other declared/channel cast member NAMED in the action brief (a
    character who reacts but doesn't speak, like a background figure) — ordered
    by where they appear in the brief."""
    seg = shot["seg"]
    names = _scene_speakers(seg)
    candidates = set(shot.get("cast") or {})
    try:
        candidates |= set(config.channel().cast or {})
    except Exception:                            # noqa: BLE001
        pass
    brief = (seg.get("clip_motion") or seg.get("motion") or "")
    extras = [n for n in candidates if n not in names and n
              and re.search(rf"\b{re.escape(n)}\b", brief)]
    extras.sort(key=lambda n: brief.find(n))
    return names + extras


def _attributed_lines(seg: dict) -> str:
    """The exact spoken lines attributed to each speaker — 'Dani says "...".
    Noni says "...".' — so the generated clip speaks them. Single-speaker
    segments collapse to one attributed line."""
    dlg = _seg_dialogue(seg)
    if dlg:
        parts = []
        for ln in dlg:
            spk = (ln.get("speaker") or "").strip()
            txt = (ln.get("text") or "").strip()
            if txt:
                parts.append(f'{spk} says "{txt}"' if spk else f'"{txt}"')
        return (". ".join(parts) + ".") if parts else ""
    txt = _spoken_text(seg)
    if not txt:
        return ""
    spk = (seg.get("speaker") or "").strip()
    return f'{spk} says "{txt}".' if spk else f'"{txt}".'


def _clip_speak_clause(shot: dict) -> str:
    """For a clip_voice shot, the DIALOGUE block for the VIDEO prompt (#3): who
    is in the scene (with a brief look), the exact spoken lines attributed to
    each speaker, and the instruction to SPEAK them on-camera (lip-synced to the
    --audio voice reference) so the clip's OWN audio carries the dialogue — no
    separate voiceover to splice in."""
    seg = shot["seg"]
    if not clip_voice_on(seg):
        return ""
    shot_cast = shot.get("cast") or {}
    present = _scene_characters(shot)            # speakers + named non-speakers
    roster = ""
    if present:
        descs = [(f"{n} ({d})" if (d := _speaker_desc(n, shot_cast)) else n)
                 for n in present]
        verb = "is" if len(descs) == 1 else "are"
        noun = "character" if len(descs) == 1 else "characters"
        roster = (f" There {verb} {len(descs)} {noun} in the scene: "
                  + "; ".join(descs) + ".")
    lines = _attributed_lines(seg)
    return (roster + (f" {lines}" if lines else "")
            + " The characters SPEAK these exact lines ON-CAMERA, lip-synced and "
            "audible — the clip's OWN audio IS this dialogue (no separate "
            "voiceover); match the provided audio reference for voice and "
            "timing.")


def clip_prompt(shot: dict, tpl: dict) -> str:
    """The FULL prompt sent to the video model — the single source of truth, so
    the interactive session shows EXACTLY what's sent and can curate it BEFORE
    spending (every generation is billed). A per-segment `clip_prompt_text`
    override is returned VERBATIM; otherwise it's the dynamic clip_motion brief
    + creator note + the reference-look clause + (for a speaking clip) the
    attributed-dialogue block + the cinematic look."""
    seg = shot["seg"]
    override = (seg.get("clip_prompt_text") or "").strip()
    if override:
        return override
    extra = (seg.get("motion_extra") or "").strip()
    extra = f" {extra.rstrip('.')}." if extra else ""
    ref = ("" if _clip_keyframe_as_start(tpl) else
           " Use the reference image as the look guide — match its world, "
           "subject, characters, lighting and palette — but generate a fresh, "
           "fully MOVING shot, not a pan or zoom over that still.")
    motion = _clip_motion(shot, tpl).rstrip().rstrip(".")   # avoid a double period
    return f"{motion}.{extra}{ref}{_clip_speak_clause(shot)} {CLIP_LOOK}"


_CLIP_MODES = ("fast", "std")           # seedance 2.0: fast (cheap) | std (full)


def clip_video_params(shot: dict, tpl: dict) -> list:
    """The video-model --params for this clip: the template `video_params`,
    with `mode` overridden per-clip by `segment.clip_mode` (fast | std) — so
    each clip can pick the cheap-fast or full-quality seedance model."""
    vp = dict(tpl.get("video_params", {}))
    mode = str(shot["seg"].get("clip_mode") or "").strip().lower()
    if mode in _CLIP_MODES:
        vp["mode"] = mode
    args = []
    for k, v in vp.items():
        args += [f"--{k}", str(v)]
    return args


def step_clip(shot: dict, folder: Path, tpl: dict) -> bool:
    """One clip (cached). False = generation/moderation failure; the caller
    decides between retry and the ken-burns fallback (an absent clip becomes
    a zoompan on the keyframe at assemble)."""
    seg = shot["seg"]
    vparams = clip_video_params(shot, tpl)          # per-clip fast/std (#2)
    motion = _clip_motion(shot, tpl)
    extra = (seg.get("motion_extra") or "").strip()
    extra = f" {extra.rstrip('.')}." if extra else ""
    speak = _clip_speak_clause(shot)
    # clip_voice: pass the synthesized voice as the --audio reference so the
    # clip's spoken delivery matches it (native audio at assembly).
    aud_args = []
    if clip_voice_on(seg):
        ref = config.art(folder, f"seg{shot['index']}.mp3")
        if ref.exists():
            aud_args = ["--audio", str(ref)]
        else:
            print(f"  ! clip{shot['index']}: clip_voice set but "
                  f"seg{shot['index']}.mp3 missing — run the voice stage first",
                  file=sys.stderr)
    # how the keyframe guides the clip: a RELATED reference (--image, a fresh
    # moving shot) by default, or the locked START frame (--start-image) when
    # the template's `clip_keyframe_role` says "start". §7.
    key_flag = "--start-image" if _clip_keyframe_as_start(tpl) else "--image"
    # clip_prompt() is the single source of truth — the EXACT text the
    # interactive session shows (incl. the reference-look + lip-sync clauses
    # and any verbatim override)
    prompt = clip_prompt(shot, tpl)
    # moderation false-positive retries: progressively calmer/safer rephrasings
    calm = "Slow, smooth, gentle cinematic camera drift with subtle parallax."
    alts = [f"{motion}.{speak} {CLIP_LOOK} {SAFE_TAIL}",
            f"{calm}{extra}{speak} {CLIP_LOOK} {SAFE_TAIL}",
            f"{calm}{speak} {CLIP_LOOK} {SAFE_TAIL}"]
    fresh = not shot["clip"].exists()            # vs a generate() cache hit
    ok = generate(tpl["video_model"], prompt, shot["clip"],
                  (".mp4", ".webm"),
                  [key_flag, str(shot["key"]),
                   "--duration", str(clip_duration(shot, tpl)),
                   "--aspect_ratio", "9:16"] + vparams + aud_args,
                  f"clip {shot['index']}", alt_prompts=alts)
    if ok and fresh:
        record_gen_prompt(folder, shot["clip"].name, prompt)   # ⭐ bookkeeping
    return ok


def step_clips(shots: list[dict], folder: Path, tpl: dict) -> list[dict]:
    for shot in shots:
        subs = _subshots(shot, folder)
        plan = shot_plan(shot["seg"], shot["index"])
        wants = _plan_clip_wanted(plan, tpl)
        multi = len(subs) > 1
        for sub, want in zip(subs, wants):
            tag = f"{sub['index']}.{sub['sub']}" if multi else str(sub["index"])
            # director's call: a still beat gets a free Ken Burns (no clip),
            # only genuinely dynamic shots spend clip credits — but a clip_voice
            # beat MUST be a real clip (it delivers the spoken line on-camera).
            # For a multi-shot beat the per-segment cap also forces the surplus
            # shots to free Ken Burns stills.
            if not want:
                why = ("director — no animation needed"
                       if sub["seg"].get("animate") is not False or not multi
                       else f"over the {clips_per_segment_cap(tpl)}-clip cap")
                print(f"  clip{tag} [{sub['label']}]: Ken Burns ({why}, $0)")
                continue
            if not step_clip(sub, folder, tpl):
                print(f"  clip{tag}: falling back to keyframe motion")
    return shots


_INK_RGB = {"deep green": (46, 204, 113), "green": (46, 204, 113),
            "crimson red": (226, 72, 61), "red": (226, 72, 61),
            "ivory white": (245, 238, 220), "white": (245, 238, 220),
            "gold": (217, 178, 90), "amber": (255, 176, 32)}


def _overlay_rgb(seg: dict, d: dict, ch) -> tuple:
    """The overlay-text colour from the channel segment's ink rule (green/red
    by verdict, a named colour, or ivory)."""
    rule = ch.segment(seg["label"]).get("ink")
    if rule == "verdict" and ch.positive_verdict:
        return ((46, 204, 113) if d.get("verdict") == ch.positive_verdict
                else (226, 72, 61))
    if rule and rule != "verdict":
        return _INK_RGB.get(rule.lower(), (245, 238, 220))
    return (245, 238, 220)


# System fonts with broad Unicode coverage (Greek, superscripts, math) tried
# when the brand display font lacks a glyph the text needs — a science
# channel's overlays write things like 1/λ⁴, and Fredoka One is Latin-only
# (the λ and ⁴ silently render as NOTHING, shipping a "1/" on screen).
_GLYPH_FALLBACK_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",      # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",      # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialuni.ttf",                             # Windows
    "C:/Windows/Fonts/arialbd.ttf",
)


def _upper_display(s: str) -> str:
    """Uppercase for the display treatment — ASCII letters only, so a formula
    symbol keeps its meaning (1/λ⁴ must not become 1/Λ⁴)."""
    return re.sub(r"[a-z]+", lambda m: m.group().upper(), s or "")


def _missing_glyphs(font, text: str) -> set:
    """Characters `font` renders as BLANK (an empty FreeType mask on a
    non-space char) — the silent-tofu case that loses a λ. Best-effort: a
    font whose .notdef is a visible box slips through, but every vendored
    display font blanks, which is the failure that matters here."""
    out = set()
    for c in set(text):
        if c.isspace():
            continue
        try:
            if font.getmask(c).getbbox() is None:
                out.add(c)
        except Exception:                            # noqa: BLE001
            pass
    return out


def _display_font(text: str, size: int):
    """The brand display font at `size` — unless `text` needs glyphs it lacks,
    then the first system fallback that covers the whole text (one consistent
    font beats per-character mixing). Nothing covers it → the brand font (and
    the gaps are at least logged)."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(str(config.FONTS / "FredokaOne-Regular.ttf"),
                               size)
    except OSError:
        return ImageFont.load_default()
    missing = _missing_glyphs(f, text)
    if not missing:
        return f
    for cand in _GLYPH_FALLBACK_FONTS:
        try:
            fb = ImageFont.truetype(cand, size)
        except OSError:
            continue
        if not _missing_glyphs(fb, text):
            print(f"  text needs {''.join(sorted(missing))} — the display font "
                  f"lacks it, using {Path(cand).stem}")
            return fb
    print(f"  ! no installed font covers {''.join(sorted(missing))} — those "
          f"characters will not render", file=sys.stderr)
    return f


def _text_overlay_png(text: str, rgb: tuple, out: Path,
                      scrim: bool = False) -> bool:
    """Render punchy overlay text as a transparent PNG (bold display font,
    black stroke for legibility) for a timed reveal at assembly. `scrim` draws a
    semi-opaque rounded band behind the text — a guaranteed-legible treatment
    the AI-review repair turns on when an overlay reads as washed-out over a
    bright background (the stroke alone can't save pale text on a blown-out
    sky)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    text = _upper_display(text)
    font = _display_font(text, 96)
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    maxw, words, lines, cur = int(W * 0.88), text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if scratch.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    asc, desc = font.getmetrics()
    lh = asc + desc + 12
    pad = 52 if scrim else 20
    stroke = 8
    # TIGHT canvas around the text (not full screen width) — _conform places this
    # PNG by its own w/h, so a full-width canvas made every non-centred `pos`
    # (left/right) just shift a 1080px image off-screen. Sizing to the text makes
    # left | center | right placement actually land where the director asked.
    widest = max((scratch.textlength(ln, font=font) for ln in lines), default=0)
    cw = min(int(widest) + 2 * (pad + stroke), W)
    img = Image.new("RGBA", (cw, lh * len(lines) + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if scrim:
        # a semi-opaque band sized to the widest line — readable text over ANY
        # background (the washed-out fix)
        bw = min(int(widest) + 96, cw)
        x0 = (cw - bw) // 2
        draw.rounded_rectangle([x0, 12, x0 + bw, img.height - 12], radius=28,
                               fill=(0, 0, 0, 170))
        # high-contrast text on the scrim: force white if the ink is dark
        if sum(rgb) < 360:
            rgb = (255, 255, 255)
    y = pad
    for ln in lines:
        x = (cw - draw.textlength(ln, font=font)) / 2
        draw.text((x, y), ln, font=font, fill=rgb + (255,),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 235))
        y += lh
    img.save(str(out))
    return True


def _seg_overlays(seg: dict) -> list:
    """Normalized overlay specs for a segment. Supports MULTIPLE overlays via a
    list `overlays` ([{text, pos, start, end, color}], each independently placed
    and timed) AND the legacy single `overlay` string (one overlay). Returns []
    when none. `start`/`end` are fractions 0-1 of the segment (or seconds if
    >1); `pos` is like 'top-left' / 'middle' / 'bottom-right' (default
    top-center); `color` is an optional named ink (else the segment's ink)."""
    raw = seg.get("overlays")
    out = []
    if isinstance(raw, list) and raw:
        for o in raw:
            if isinstance(o, str) and o.strip():
                out.append({"text": o.strip()})
            elif isinstance(o, dict) and (o.get("text") or "").strip():
                out.append(dict(o))
        return out
    t = (seg.get("overlay") or "").strip()
    return [{"text": t}] if t else []


def _overlay_png_name(i: int, k: int) -> str:
    """Canonical PNG name for segment i's k-th overlay. k=0 keeps the legacy
    text<i>.png name so existing runs and caching are unchanged."""
    return f"text{i}.png" if k == 0 else f"text{i}_{k}.png"


def _text_placement(pos: str):
    """(x_expr, y_expr) for an overlay's position keyword — vertical
    top|middle|bottom × horizontal left|center|right. Default top-center
    (matches the legacy single-overlay placement); bottom clears the caption
    strip."""
    p = (pos or "").lower()
    horiz = "left" if "left" in p else "right" if "right" in p else "center"
    vert = ("top" if "top" in p else "bottom" if "bottom" in p
            else "middle" if ("middle" in p or "center" in p or "centre" in p)
            else "top")
    x = {"center": "(W-w)/2", "left": "64", "right": "W-w-64"}[horiz]
    y = {"top": str(int(H * 0.09)), "middle": "(H-h)/2",
         "bottom": "H-h-300"}[vert]
    return x, y


def _ov_time(v, need: float, default):
    """Resolve an overlay start/end to seconds within a `need`-long segment.
    None -> default; a value in 0-1 is a FRACTION of the segment; >1 is seconds
    (capped at the segment length)."""
    if v is None:
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v < 0:
        return 0.0
    return round(v * need, 2) if v <= 1.0 else round(min(v, need), 2)


def _overlay_color(ov: dict, seg: dict, facts: dict, ch) -> tuple:
    """An overlay's RGB: its explicit named `color` if set, else the segment's
    ink rule (green/red by verdict, a named colour, or ivory)."""
    c = (ov.get("color") or "").strip().lower()
    if c and c in _INK_RGB:
        return _INK_RGB[c]
    return _overlay_rgb(seg, facts, ch)


def _ensure_overlays(shots: list[dict], folder: Path, tpl: dict) -> None:
    """Render a PNG per overlay (text<N>.png, text<N>_1.png, …) so each can
    fade in/out at its own time and position in _conform, instead of being
    baked into the keyframe. Skipped when reveal is disabled."""
    if not tpl.get("reveal_overlay_text", True):
        return
    fp = folder / "facts.json"
    facts = json.loads(fp.read_text()) if fp.exists() else {}
    ch = config.channel()
    for shot in shots:
        seg = shot["seg"]
        for k, ov in enumerate(_seg_overlays(seg)):
            out = config.art(folder, _overlay_png_name(shot["index"], k))
            if out.exists() and not _overlay_png_stale(out):
                continue
            _text_overlay_png(ov["text"], _overlay_color(ov, seg, facts, ch),
                              out, scrim=bool(ov.get("scrim")))


def _overlay_png_stale(p: Path) -> bool:
    """True for a cached overlay PNG in the OLD full-screen-width format (which
    broke left/right placement) — so it's re-rendered once into the new tight
    format. New renders are always < W wide (text wraps at 0.88·W), so a
    full-width PNG can only be the legacy one. Unreadable → not stale (left as is,
    scrim re-render still respected via the spec)."""
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.width >= W
    except Exception:                            # noqa: BLE001
        return False


def _voice_end(path: Path) -> float:
    """Where the SPEECH actually ends — the file duration minus any trailing
    silence (Inworld TTS bakes in up to ~1.3s). Keeps a 0.12s breath so the
    last word isn't clipped. Falls back to the full duration on any trouble."""
    dur = ffprobe_duration(path)
    try:
        r = sh(["ffmpeg", "-nostats", "-i", str(path), "-af",
                "silencedetect=noise=-40dB:d=0.3", "-f", "null", "-"])
        out = r.stderr or r.stdout
        starts = re.findall(r"silence_start: ([\d.]+)", out)
        ends = re.findall(r"silence_end: ([\d.]+)", out)
        # a trailing silence runs to EOF → it has a start but no matching end
        if starts and len(starts) > len(ends):
            return max(min(float(starts[-1]) + 0.12, dur), 0.3)
    except Exception:
        pass
    return dur


def _boomerang(clip: Path, folder: Path, tag) -> Path:
    """Build a forward+reverse (ping-pong) cycle of the clip as scratch, so it
    can be -stream_loop'd seamlessly — no hard jump at the loop point. Returns
    the scratch path, or None on failure (caller falls back to a plain loop).
    `reverse` buffers the whole clip in memory, fine for our ≤8s clips."""
    out = config.art(folder, f"_bm{tag}.mp4")
    r = sh(["ffmpeg", "-y", "-i", str(clip), "-filter_complex",
            "[0:v]split[f][b];[b]reverse[r];[f][r]concat=n=2:v=1[v]",
            "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", str(out)])
    return out if (r.returncode == 0 and out.exists()) else None


def _part_need(shot: dict, folder: Path, speed: float) -> float:
    """How long this segment's part runs. A `native_audio` or `full_clip`
    segment plays for the WHOLE generated clip (the video plays out, its own
    audio kept for native); otherwise it's where the TTS speech ends ÷ speed.
    A `mix_clip_audio` segment runs for the LONGER of the two — so a clip longer
    than the voiceover plays OUT (its sfx kept under the VO), while a voiceover
    longer than the clip makes the clip's VIDEO loop/boomerang/slow to fill (per
    clip_fill), the sfx playing under it."""
    seg = shot["seg"]
    vo_need = (_voice_end(config.art(folder, f"seg{shot['index']}.mp3"))
               / max(speed, 0.1))
    # a multi-shot beat is always a narrated montage: the TTS VO sets the length
    # and the stitched shots fill it (segment-level clip-audio modes don't apply)
    if _shot_specs(seg):
        return vo_need
    if (seg.get("native_audio") or seg.get("full_clip")
            or clip_voice_on(seg)) and shot["clip"].exists():
        return ffprobe_duration(shot["clip"])
    if seg.get("mix_clip_audio") and shot["clip"].exists():
        return max(vo_need, ffprobe_duration(shot["clip"]))
    return vo_need


def _render_segment_base(subs: list[dict], folder: Path, i: int, need: float,
                         max_slow: float, tpl: dict):
    """Stitch a multi-shot segment's DISTINCT shots into one video-only base of
    length `need` (W×H@FPS): each sub-shot covers a weighted time slice — its own
    generated clip (filled per clip_fill) or a Ken Burns move over its keyframe —
    concatenated in order. Returns the base path, or None to fall back to the
    single-shot conform. The VO/overlays/captions are composited over this base
    by `_conform` exactly as for a single shot."""
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    weights = [max(float(s.get("weight") or 1.0), 0.01) for s in subs]
    total_w = sum(weights) or 1.0
    parts, acc = [], 0.0
    for k, (sub, wt) in enumerate(zip(subs, weights)):
        # the last slice absorbs rounding so the parts sum to exactly `need`
        dur = max(need - acc, 0.4) if k == len(subs) - 1 \
            else max(need * wt / total_w, 0.4)
        acc += dur
        out = config.art(folder, f"sub{i}_{k}.mp4")
        clip = sub["clip"]
        if clip.exists() and not sub["seg"].get("use_still"):  # forced still skips
            fill = str((sub["seg"].get("clip_fill")
                        or (tpl or {}).get("clip_fill", "slow"))).lower()
            cdur = ffprobe_duration(clip)
            if fill == "loop":
                vin, vf = ["-stream_loop", "-1", "-i", str(clip)], base
            elif fill in ("freeze", "playstop", "play-stop", "play_stop",
                          "hold", "once", "stop"):    # native once, then freeze
                vin = ["-i", str(clip)]
                vf = f"{base},tpad=stop_mode=clone:stop_duration={dur:.1f}"
            else:                                   # slow-fill + freeze remainder
                factor = min(max(dur / max(cdur, 0.1), 1.0), max_slow)
                vin = ["-i", str(clip)]
                vf = (f"{base},setpts={factor:.4f}*PTS,"
                      f"tpad=stop_mode=clone:stop_duration={dur:.1f}")
        else:                                       # still → built-in Ken Burns
            frames = int(dur * FPS) + 1
            vin = ["-i", str(sub["key"])]
            vf = (f"scale=2700:4800,zoompan=z='1+0.12*on/{frames}':"
                  f"x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={frames}:"
                  f"s={W}x{H}:fps={FPS}")
        r = sh(["ffmpeg", "-y"] + vin + ["-vf", vf, "-t", f"{dur:.3f}",
                "-an", "-r", str(FPS), "-c:v", "libx264", "-preset", "fast",
                "-pix_fmt", "yuv420p", str(out)])
        if r.returncode != 0 or not out.exists():
            print(f"  ! sub-shot {i}.{k} render failed — falling back to a "
                  f"single shot: {r.stderr[-160:]}", file=sys.stderr)
            return None
        parts.append(out)
    lst = config.art(folder, f"_subcat{i}.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    base_out = config.art(folder, f"base{i}.mp4")
    r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", str(base_out)])
    if r.returncode != 0 or not base_out.exists():   # copy needs identical params
        r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-r", str(FPS), "-c:v", "libx264", "-preset", "fast",
                "-pix_fmt", "yuv420p", str(base_out)])
    if r.returncode != 0 or not base_out.exists():
        print(f"  ! segment {i} stitch failed: {r.stderr[-160:]}", file=sys.stderr)
        return None
    print(f"  segment {i}: stitched {len(parts)} distinct shots "
          f"→ base{i}.mp4 ({need:.1f}s)")
    return base_out


def _freeze_kenburns(clip: Path, folder: Path, tag, need: float) -> Path:
    """The "freeze" / play-stop fill, ALIVE: play the clip ONCE at native speed,
    then — instead of a dead-frozen frame — KEN BURNS (a slow zoom-in) on its
    LAST frame for the remainder, so the held tail still drifts. Returns a
    W×H@FPS scratch of length ~need, or None to fall back to a plain hold (clip
    already ≥ need, or any ffmpeg step failed)."""
    cdur = ffprobe_duration(clip)
    if cdur >= need - 0.05:
        return None                         # clip covers it; just play + cut
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    played = config.art(folder, f"_fz{tag}_play.mp4")
    r = sh(["ffmpeg", "-y", "-i", str(clip), "-vf", base, "-an", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            str(played)])
    if r.returncode != 0 or not played.exists():
        return None
    # the EXACT last frame of the conformed played part (pixel-identical → the
    # cut into the ken-burns tail is seamless; zoompan starts at zoom 1.0)
    last = config.art(folder, f"_fz{tag}_last.png")
    r = sh(["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(played), "-update", "1",
            "-frames:v", "1", str(last)])
    if r.returncode != 0 or not last.exists():
        return None
    hold = max(need - cdur, 0.1)
    frames = int(hold * FPS) + 1
    kb = config.art(folder, f"_fz{tag}_kb.mp4")
    vf_kb = (f"scale=2700:4800,zoompan=z='1+0.12*on/{frames}':"
             f"x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={frames}:"
             f"s={W}x{H}:fps={FPS}")
    r = sh(["ffmpeg", "-y", "-loop", "1", "-i", str(last), "-vf", vf_kb,
            "-t", f"{hold:.3f}", "-an", "-r", str(FPS), "-c:v", "libx264",
            "-preset", "fast", "-pix_fmt", "yuv420p", str(kb)])
    if r.returncode != 0 or not kb.exists():
        return None
    lst = config.art(folder, f"_fz{tag}.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in (played, kb)))
    out = config.art(folder, f"_fz{tag}.mp4")
    r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-r", str(FPS), "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", str(out)])
    if r.returncode != 0 or not out.exists():
        return None
    return out


def _conform(shot: dict, folder: Path, i: int, reveal_frac: float = 0.6,
             speed: float = 1.0, text_on: bool = True,
             need: float = None, max_slow: float = 3.0,
             tpl: dict = None, vo_path: Path = None, out_path: Path = None,
             tag=None) -> Path:
    """1080x1920@30 part whose length matches its (optionally sped-up) VO, with
    the VO audio plus, when present, the segment's overlay text — fading in
    AFTER it's spoken (a reveal).
    speed>1 atempo-speeds the voice and shortens the part to match (so the
    clip covers the audio with less/no freeze). text_on gates the overlay-text
    reveal (False = a plain cut).
    need (the part length) defaults to where the speech ends ÷ speed.
    vo_path/out_path/tag override the voiceover, output and scratch names — set
    by the SECTION-NATIVE path so each section conforms to its own audio
    (`seg<i>_s<k>.mp3` → `part<i>_s<k>.mp4`)."""
    vo = vo_path or config.art(folder, f"seg{shot['index']}.mp3")
    out = out_path or config.art(folder, f"part{i}.mp4")
    tag = str(i) if tag is None else str(tag)
    # native_audio / clip_voice: this part's sound comes from the generated
    # CLIP's own audio track (model-spoken dialogue/SFX, or a clip lip-synced to
    # the voice reference), not the separate TTS voiceover.
    native = ((bool(shot["seg"].get("native_audio"))
               or clip_voice_on(shot["seg"])) and shot["clip"].exists())
    if native and not has_audio_stream(shot["clip"]):
        # the video model didn't embed an audio track — fall back to the TTS
        # voice we kept (clip_voice) so the segment isn't silent
        if vo.exists():
            print(f"  ! clip{shot['index']} carries no audio — using the TTS "
                  "voice instead")
            native = False
        else:
            print(f"  ! clip{shot['index']} carries no audio — segment is "
                  "silent", file=sys.stderr)
    if need is None:
        need = _part_need(shot, folder, speed)
    kb_scratch = None                 # optional plugin-rendered still to clean up
    clip_scratch = None               # optional boomerang scratch to clean up
    # how a generated clip fills a voice longer than itself:
    #   "slow"      (default) — setpts-slow up to max_slow× then freeze-hold
    #   "loop"      — repeat at NATIVE speed (no slow-mo/freeze; a hard cut at
    #                 the loop point)
    #   "boomerang" — forward+reverse cycle, looped (native speed, seamless)
    #   "freeze"    — play ONCE at native speed, then a slow KEN BURNS drift on
    #                 the last frame for the remainder ("play-stop", alive — not
    #                 a dead freeze; falls back to a plain hold if KB fails).
    # Per-segment `clip_fill` overrides the template default.
    base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    fill = str((shot["seg"].get("clip_fill")
                or (tpl or {}).get("clip_fill", "slow"))).lower()
    if fill in ("boomerang", "pingpong", "ping-pong", "loop-reverse", "bounce"):
        fill = "boomerang"
    elif fill in ("freeze", "playstop", "play-stop", "play_stop", "hold", "once",
                  "stop"):
        fill = "freeze"
    # MULTI-SHOT beat: stitch its distinct shots into one base of length `need`
    # first, then composite the VO/overlays/captions over it just like a single
    # shot. native segments never reach here (a montage uses the TTS VO).
    subs = _subshots(shot, folder)
    seg_base = (_render_segment_base(subs, folder, i, need, max_slow, tpl)
                if (len(subs) > 1 and not native) else None)
    # creator forced a still for this beat — ignore any generated clip on disk
    # (kept/restorable) and fall through to the Ken Burns branch
    force_still = bool(shot["seg"].get("use_still")) and not native \
        and not clip_voice_on(shot["seg"])
    if seg_base is not None:
        vin = ["-i", str(seg_base)]
        vf = base
    elif shot["clip"].exists() and not force_still:
        cdur = ffprobe_duration(shot["clip"])
        if native:
            # play the whole clip as-is at native speed (need == clip length);
            # its own audio is kept below — no fill manipulation. MUST skip the
            # fill logic entirely (a native segment that also carries a
            # clip_fill like "boomerang" would otherwise fall through to the
            # boomerang branch with clip_scratch=None → an `-i None` crash).
            vin = ["-i", str(shot["clip"])]
            vf = base
        elif fill == "boomerang":
            clip_scratch = _boomerang(shot["clip"], folder, tag)
            if clip_scratch is None:        # build failed → plain loop
                fill = "loop"
            if fill == "boomerang":
                # the output -t (need) cuts the looped ping-pong to length
                vin = ["-stream_loop", "-1", "-i", str(clip_scratch)]
                vf = base
            else:                           # degraded to loop
                vin = ["-stream_loop", "-1", "-i", str(shot["clip"])]
                vf = base
        elif fill == "loop":
            # loop the clip at native framerate; -t (need) cuts it to length
            vin = ["-stream_loop", "-1", "-i", str(shot["clip"])]
            vf = base
        elif fill == "freeze":
            # play ONCE at native speed, then KEN BURNS (slow zoom) on the last
            # frame for the rest — a "play then drift", not a dead freeze.
            # Falls back to a plain tpad hold if the ken-burns build fails or the
            # clip already covers the VO. -t (need) caps either way.
            fz = _freeze_kenburns(shot["clip"], folder, tag, need)
            if fz is not None:
                clip_scratch = fz
                vin = ["-i", str(fz)]
                vf = base
            else:
                vin = ["-i", str(shot["clip"])]
                vf = f"{base},tpad=stop_mode=clone:stop_duration={need:.1f}"
        else:
            # slow the clip to fill the voice (up to max_slow×) rather than
            # playing fast then freezing; only freeze the bit beyond max_slow
            fill = "slow"
            factor = min(max(need / cdur, 1.0), max_slow)
            vin = ["-i", str(shot["clip"])]
            vf = (f"{base},setpts={factor:.4f}*PTS,"
                  f"tpad=stop_mode=clone:stop_duration={need:.1f}")
    else:  # still segment: animate via the Ken Burns plugin or built-in zoompan
        import kenburns
        kb = kenburns.render(shot, folder, i, need, tpl or {},
                             fps=FPS, width=W, height=H)
        if kb is not None:
            # the plugin already rendered a conformed W×H@FPS clip of the right
            # length — pass it straight through (overlays still composite on top)
            kb_scratch = kb
            vin = ["-i", str(kb)]
            vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
        else:  # ken burns from the keyframe (default / plugin off or unavailable)
            frames = int(need * FPS) + 1
            vin = ["-i", str(shot["key"])]
            vf = (f"scale=2700:4800,zoompan=z='1+0.12*on/{frames}':"
                  f"x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={frames}:"
                  f"s={W}x{H}:fps={FPS}")
    # timed overlays: (png, scale_w|None, x_expr, y_expr, fade-in start). The
    # answer TEXT fades in only after it's spoken (overlay_reveal_at × the
    # segment), except the HOOK which shows at once. A per-segment
    # `overlay_reveal_at` (fraction 0-1) overrides the global default for THIS
    # segment (and can move even the HOOK off t=0).
    seg = shot["seg"]
    seg_rf = seg.get("overlay_reveal_at")
    if seg_rf is not None:                        # explicit timing — honour exactly
        default_reveal = round(max(0.0, min(float(seg_rf), 1.0)) * need, 2)
    elif shot["index"] == 0:                      # HOOK shows at once
        default_reveal = 0.0
    else:                                         # default: early, capped lead so
        default_reveal = min(round(reveal_frac * need, 2),  # a long beat can't
                             OVERLAY_MAX_LEAD)              # bury the overlay
    # ovs entries: (png, scale_w|None, x_expr, y_expr, fade_in_start,
    #               fade_out_end|None). Each text overlay fades in at its start
    #               and out at its end (None = stays to the end of the segment).
    ovs = []
    text_specs = _seg_overlays(seg) if text_on else []
    for k, ov in enumerate(text_specs):
        # `_oi` is the overlay's stable index in the SEGMENT list (set when a
        # timed sequence is distributed across sections) → the right PNG even for
        # a section's subset; falls back to k for a plain segment
        png = config.art(folder, _overlay_png_name(shot["index"], ov.get("_oi", k)))
        if not png.exists():
            continue
        eff = str(ov.get("effect") or "").strip().lower()
        eff = ("stamp" if eff in ("stamp", "slam", "stamp-slam", "stamp_slam")
               else "")
        # a stamp reads best centred (the verdict seal punching into frame)
        pos = ov.get("pos") or ("middle-center" if eff == "stamp" else None)
        x_expr, y_expr = _text_placement(pos)
        st = _ov_time(ov.get("start"), need, default_reveal)
        en = _ov_time(ov.get("end"), need, None)
        ovs.append((png, None, x_expr, y_expr, st, en, eff))

    inputs = list(vin)
    if native:                       # the clip (input 0) carries the audio
        aud, oi = "[0:a]", 1
    elif vo.exists():                # the TTS voiceover is a separate input
        inputs += ["-i", str(vo)]
        aud, oi = "[1:a]", 2
    else:                            # no audio source (e.g. native flag, clip
        inputs += ["-f", "lavfi",    # later removed) — silent, never crash
                   "-i", "anullsrc=r=44100:cl=stereo"]
        aud, oi = "[1:a]", 2
    # MIX the clip's OWN audio (sfx / ambience) UNDER the voiceover when the
    # segment asks for it (`mix_clip_audio`) — keep the VO on top, duck the clip
    # sound. Needs a real VO + a clip with an audio track, and is meaningless on
    # a native segment (that IS the clip's audio). The clip is added as its OWN
    # input so its audio is the NATIVE-speed sfx, independent of any video fill
    # (slow/loop) applied to the picture.
    sfx_label = None
    if (seg.get("mix_clip_audio") and not native and vo.exists()
            and shot["clip"].exists() and has_audio_stream(shot["clip"])):
        inputs += ["-i", str(shot["clip"])]
        sfx_label = f"[{oi}:a]"
        oi += 1
    parts, cur = [f"[0:v]{vf}[bg0]"], "bg0"
    for k, (png, w, x, y, st, en, eff) in enumerate(ovs):
        # -loop 1 gives the still a real timeline so the timed fade fires
        # (without it the image is one frame at t=0 and a delayed fade never
        # triggers); -t on the output caps the otherwise-infinite stream
        inputs += ["-loop", "1", "-i", str(png)]
        ox, oy = x, y
        if eff == "stamp":
            # rubber-stamp SLAM, flicker-free. The old version animated
            # `scale=...:h=-1:eval=frame` with a t-varying width — two problems:
            # `h=-1` re-rounds the output height every frame (a 1px jitter), and
            # re-sampling the text raster each frame shimmers its edges; the
            # result FLICKERED for the whole hold. Instead keep the raster at a
            # CONSTANT size (no per-frame resampling at all) and convey the slam
            # with a fast alpha POP plus a short drop-IN driven by the overlay
            # filter's `t` expression (well-supported everywhere, unlike scale's
            # eval=frame `t`). The text snaps into place from ~16px above over
            # ~0.14s — a stamp punching down — then sits rock-steady.
            sd = 0.14
            oy = f"({y})-16*(1-clip((t-{st})/{sd}\\,0\\,1))"
            fades = f"fade=t=in:st={st}:d=0.08:alpha=1"
            if en is not None and en < need - 0.05:
                fades += f",fade=t=out:st={max(en - 0.4, st):.2f}:d=0.4:alpha=1"
            parts.append(f"[{oi}:v]format=rgba,{fades}[o{k}]")
        else:
            scale = f"scale={w}:-1," if w else ""
            # a continuation section (the overlay was already up in the previous
            # section of this beat) appears INSTANTLY so it stays continuous —
            # no 0.4s re-fade dip at every section boundary
            din = 0.04 if (w is None and seg.get("overlay_continued")) else 0.4
            fades = f"fade=t=in:st={st}:d={din}:alpha=1"
            if en is not None and en < need - 0.05:  # timed exit (else stays)
                fades += f",fade=t=out:st={max(en - 0.4, st):.2f}:d=0.4:alpha=1"
            parts.append(f"[{oi}:v]{scale}format=rgba,{fades}[o{k}]")
        parts.append(f"[{cur}][o{k}]overlay=x={ox}:y={oy}:"
                     f"eof_action=pass[bg{k + 1}]")
        cur, oi = f"bg{k + 1}", oi + 1
    # native audio plays at native speed (no atempo); TTS gets the speed-up
    af = "" if native else (f"atempo={speed:.3f}," if abs(speed - 1.0) > 1e-3
                            else "")
    if sfx_label is not None:
        # voiceover (full level) + the clip's sfx ducked underneath. amix
        # normalize=0 keeps the VO at full and just SUMS the ducked sfx (the
        # default would halve both); duration=first ties the mix to the VO
        # timeline (the sfx simply stops when the clip ends).
        gain = seg.get("clip_audio_gain")
        if gain is None:
            gain = (tpl or {}).get("clip_audio_gain", 0.32)
        gain = max(0.0, min(float(gain), 1.0))
        parts.append(f"{aud}{af}apad[vo_main]")
        parts.append(f"{sfx_label}volume={gain:.2f}[sfx]")
        parts.append("[vo_main][sfx]amix=inputs=2:duration=first:"
                     "dropout_transition=0:normalize=0[a]")
    else:
        parts.append(f"{aud}{af}apad[a]")
    r = sh(["ffmpeg", "-y"] + inputs +
           ["-filter_complex", ";".join(parts),
            "-map", f"[{cur}]", "-map", "[a]", "-t", f"{need:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", str(out)])
    if r.returncode != 0:
        die(f"ffmpeg conform part{i}: {r.stderr[-300:]}")
    for scratch in (kb_scratch, clip_scratch):
        if scratch is not None:
            try:
                scratch.unlink()
            except OSError:
                pass
    motion = ((" (boomerang)" if fill == "boomerang"
               else " (looped)" if fill == "loop"
               else " (play-stop)" if fill == "freeze" else "")
              if shot["clip"].exists()
              else " (kenburns plugin)" if kb_scratch is not None
              else " (ken burns)")
    n_text = sum(1 for k in range(len(text_specs))
                 if config.art(folder,
                               _overlay_png_name(shot["index"], k)).exists())
    print(f"  part{i} [{shot['label']}] {need:.1f}s"
          + (f" @{speed}x" if abs(speed - 1.0) > 1e-3 else "")
          + motion
          + (f" +{n_text} overlay{'s' if n_text != 1 else ''}" if n_text
             else ""))
    return out


def _caption_ffmpeg() -> str | None:
    """An ffmpeg binary with libass, if any."""
    for cand in (os.environ.get("FFMPEG_FULL"),
                 "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
                 "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "ffmpeg"):
        if not cand:
            continue
        if shutil.which(cand) and "ass" in sh(
                [cand, "-hide_banner", "-filters"]).stdout.split():
            return cand
    return None


def _watermark_compose(folder: Path, tpl: dict):
    """Build the channel watermark → (png, pos, opacity) or None. The brand logo
    `channels/<name>/brand/watermark.png` (scaled to watermark_logo_px tall) is
    used ALONE when present — a branded logo already carries the name. The
    channel name only appears as the TEXT FALLBACK when there's no logo, or when
    `watermark_text` is explicitly set (then it's shown beside the logo).
    Disabled by `watermark: false` / when neither is present / no Pillow."""
    if not tpl.get("watermark", True):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    ch = config.channel()
    pos = str(tpl.get("watermark_pos", "bottom-right"))
    op = float(tpl.get("watermark_opacity", 0.5))
    logo_h = int(tpl.get("watermark_logo_px", 75))
    # the brand logo: channels/<name>/brand/watermark.png (preferred), or the
    # channel-root watermark.png as a fallback
    logo_p = ch.dir / "brand" / "watermark.png"
    if not logo_p.exists():
        logo_p = ch.dir / "watermark.png"
    logo = None
    if logo_p.exists():
        try:
            logo = Image.open(str(logo_p)).convert("RGBA")
            w = max(1, int(logo.width * logo_h / logo.height))
            logo = logo.resize((w, logo_h))
        except OSError:
            logo = None
    # the name: only when explicitly set (watermark_text), or as the fallback
    # with NO logo — the logo already carries the brand, so don't double it up
    explicit = str(tpl.get("watermark_text") or "").strip()
    name = explicit if logo is not None \
        else (explicit or str(ch.title or ch.name).strip())
    if logo is None and not name:
        return None
    fs = max(26, int(logo_h * 0.37))              # font scales with the logo
    font = _display_font(name or "", fs)
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = int(scratch.textlength(name, font=font)) if name else 0
    asc, desc = font.getmetrics()
    th = asc + desc
    pad, gap = max(8, int(logo_h * 0.11)), max(10, int(logo_h * 0.13))
    content_h = max(logo.height if logo else 0, th if name else 0)
    content_w = ((logo.width + gap) if logo else 0) + (tw if name else 0)
    img = Image.new("RGBA", (content_w + pad * 2, content_h + pad * 2),
                    (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = pad
    if logo:
        img.alpha_composite(logo, (x, (img.height - logo.height) // 2))
        x += logo.width + gap
    if name:
        d.text((x, (img.height - th) // 2), name, font=font,
               fill=(255, 255, 255, 255), stroke_width=4,
               stroke_fill=(0, 0, 0, 220))
    out = config.art(folder, "_watermark.png")
    img.save(str(out))
    return out, pos, op


def _apply_watermark(video: Path, folder: Path, tpl: dict) -> None:
    """Composite the channel watermark (the brand logo, or a name fallback) onto
    a finished video IN PLACE (corner, low opacity, native size). No-op when no
    watermark is configured. Keeps audio."""
    asset = _watermark_compose(folder, tpl)
    if not asset or not Path(video).exists():
        return
    wm, pos, op = asset
    p = pos.lower()
    m = 40
    x = f"W-w-{m}" if "right" in p else f"{m}"
    y = f"H-h-{m}" if "bottom" in p else f"{m}"
    tmp = config.art(folder, "_wm_tmp.mp4")
    r = sh(["ffmpeg", "-y", "-i", str(video), "-i", str(wm),
            "-filter_complex",
            f"[1:v]format=rgba,colorchannelmixer=aa={op:.2f}[wm];"
            f"[0:v][wm]overlay={x}:{y}[v]",
            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset",
            "fast", "-pix_fmt", "yuv420p", "-c:a", "copy", str(tmp)])
    if r.returncode == 0 and tmp.exists():
        tmp.replace(Path(video))
        print(f"  watermark applied ({pos})")
    else:
        tmp.unlink(missing_ok=True)
        print(f"  ! watermark skipped: {r.stderr[-160:]}", file=sys.stderr)


def _local_music(tpl: dict, odata: dict):
    """A saved background track from the channel's assets/music — overrides.json
    `music` (per-channel/experiment) wins, else the template's `music_file`
    DEFAULT (one track on every video). None when none is named / the file is
    missing."""
    name = odata.get("music") or tpl.get("music_file")
    if not name or name == "none":
        return None
    p = config.channel().music_dir / name
    if not p.exists():
        print(f"  ! music '{name}' not found in {p.parent} — skipping",
              file=sys.stderr)
        return None
    return p


def _resolve_music(folder: Path, tpl: dict, total_s: float):
    """The background-music track to mix, or None. Source is `music_source`
    (overrides.json > template > 'local'):
      'elevenlabs' — generate a ROYALTY-FREE track (cached <run>/music.mp3) from
                     an AI brief (creative.music_brief) — no Content-ID strikes;
      'local'      — a saved file in assets/music: overrides `music` > the
                     template's `music_file` default (one track on EVERY video).
    Falls back to the local default if ElevenLabs is unavailable / fails."""
    ov = config.channel().overrides
    odata = json.loads(ov.read_text()) if ov.exists() else {}
    source = (odata.get("music_source") or tpl.get("music_source")
              or "local").lower()
    if source != "elevenlabs":
        return _local_music(tpl, odata)
    import elevenlabs
    if not elevenlabs.available():
        print("  ! music_source=elevenlabs but no ELEVENLABS_API_KEY — using a "
              "local track instead", file=sys.stderr)
        return _local_music(tpl, odata)
    cached = folder / "music.mp3"
    if cached.exists():
        print(f"  music.mp3 (cached ElevenLabs track)")
        return cached
    topic = folder.name.split("_", 1)[-1] or folder.name
    brief = odata.get("music_brief") or tpl.get("music_brief")
    if not brief:
        try:
            import creative
            brief = creative.music_brief(topic)
        except Exception:                       # noqa: BLE001 — never fatal
            brief = ""
    brief = brief or ("a subtle modern cinematic underscore — soft pads and a "
                      "gentle low pulse, atmospheric, no vocals, sits quietly "
                      "under narration")
    print(f"  generating royalty-free music via ElevenLabs…\n    brief: {brief[:90]}")
    if elevenlabs.music(brief, cached, total_s, instrumental=True):
        return cached
    print("  ! ElevenLabs music failed — using a local track instead",
          file=sys.stderr)
    return _local_music(tpl, odata)


def _caption_unit(words: list, fallback: str, need: float, speed: float) -> dict:
    """One caption unit (a segment OR a section, in render order) for build_ass.
    `words` (start/end scaled by 1/speed) → karaoke; else `fallback` text → a
    held block (a recorded/Inworld take with no word timings); else no caption.
    `duration` is stored as need−SEG_PAD because build_ass re-adds SEG_PAD."""
    unit = {"duration": max(need - SEG_PAD, 0.1)}
    if words:
        unit["words"] = [{"word": w["word"],
                          "start": w.get("start", 0.0) / speed,
                          "end": w.get("end", 0.0) / speed} for w in words]
    elif (fallback or "").strip():
        unit["caption"] = fallback.strip()
    return unit


def step_assemble(shots: list[dict], audio: dict, folder: Path,
                  tpl: dict) -> Path:
    final = folder / "short.mp4"
    if final.exists():
        print(f"  short.mp4 (cached, {ffprobe_duration(final):.1f}s)")
        return final
    overlays = tpl.get("assemble_overlays", True)
    # text overlays are gated under the master `assemble_overlays`
    text_on = overlays and tpl.get("reveal_overlay_text", True)
    speed = float(tpl.get("audio_speed", 1.0))
    rf = float(tpl.get("overlay_reveal_at", 0.6))
    if text_on:
        _ensure_overlays(shots, folder, tpl)
    if not overlays:
        print("  plain cut — overlays OFF")
    elif not text_on:
        print("  text reveals OFF")
    if abs(speed - 1.0) > 1e-3:
        print(f"  voiceover sped to {speed}x")
    max_slow = float(tpl.get("max_slowdown", 3.0))
    amap = {s["index"]: s for s in audio["segments"]}
    # build the parts in render order — a SECTION-NATIVE beat emits one part per
    # section (each conformed to its OWN audio so caption/audio/video stay in
    # sync), a plain beat one part. cap_units parallels parts for build_ass. The
    # conformed sub-clips (build/part*.mp4) + scratch are RETAINED in build/.
    parts, cap_units = [], []
    for i, s in enumerate(shots):
        aseg = amap.get(s["index"], {})
        secs = _segment_sections(s, folder, aseg)
        if secs:
            for sec in secs:
                af = sec["audio"]
                need = (_voice_end(af) / max(speed, 0.1)) if af.exists() else 1.0
                parts.append(_conform(
                    sec, folder, i, rf, speed, text_on,
                    need=need, max_slow=max_slow, tpl=tpl, vo_path=af,
                    out_path=config.art(folder, f"part{i}_s{sec['section']}.mp4"),
                    tag=f"{i}s{sec['section']}"))
                # a section always captions its own line (words → karaoke, else
                # the section text held), so it's never silent on screen
                cap_units.append(_caption_unit(sec.get("words"), sec.get("say"),
                                               need, speed))
            continue
        need = _part_need(s, folder, speed)
        parts.append(_conform(s, folder, i, rf, speed, text_on,
                              need=need, max_slow=max_slow, tpl=tpl))
        if aseg.get("native_audio") or aseg.get("clip_voice"):
            cap_units.append({"duration": max(need - SEG_PAD, 0.1)})  # clip audio
        else:
            cap_units.append(_caption_unit(
                aseg.get("words"),
                aseg.get("text") if aseg.get("recorded") else None,
                need, speed))
    concat = config.art(folder, "concat.txt")
    # absolute paths in the concat list so it works regardless of cwd
    concat.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    merged = config.art(folder, "merged.mp4")
    # video: stream-copy concat (lossless, fast). audio: rebuilt GAPLESSLY with
    # the concat FILTER — copying AAC across a plain concat leaves encoder-
    # priming gaps at every segment boundary (audible stutter at each join);
    # decoding + concatenating the samples removes them. Input 0 is the concat
    # demuxer (its video is copied); inputs 1..N are the parts for clean audio.
    part_inputs = []
    for p in parts:
        part_inputs += ["-i", str(p)]
    n = len(parts)
    afilt = ("".join(f"[{k + 1}:a]" for k in range(n))
             + f"concat=n={n}:v=0:a=1[a]")
    r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
           + part_inputs
           + ["-filter_complex", afilt,
              "-map", "0:v", "-c:v", "copy",
              "-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
              str(merged)])
    if r.returncode != 0:
        die(f"concat: {r.stderr[-300:]}")

    burner = _caption_ffmpeg()
    # cap_units is one unit PER PART in render order (sections included), each
    # already scaled to the sped audio — karaoke words, a held section line, or
    # nothing (clip-sourced audio). Captions stay in lock-step with the cut.
    # `assemble_captions` (default on) is the master subtitle switch — off ships
    # a clean cut with NO burned-in captions (the make/--ui assemble toggle).
    captions_on = tpl.get("assemble_captions", True)
    has_caps = captions_on and any(
        u.get("words") or u.get("caption") for u in cap_units)
    if burner and has_caps:
        import captions
        ass = config.art(folder, "subs.ass")
        manifest = config.art(folder, "audio_caps.json")
        manifest.write_text(json.dumps({"segments": cap_units}))
        captions.build_ass(str(manifest), str(ass),
                           style=tpl.get("caption_style", "viral"))
        # cwd=folder so the ass filter takes a clean relative subpath
        r = sh([burner, "-y", "-i", config.art_rel("merged.mp4"),
                "-vf", f"ass=filename={config.art_rel('subs.ass')}:"
                f"fontsdir={config.FONTS}",
                "-c:a", "copy", "short.mp4"], cwd=folder)
        if r.returncode != 0:
            print(f"  ! caption burn failed, shipping without: "
                  f"{r.stderr[-200:]}", file=sys.stderr)
            merged.replace(final)
        else:
            print("  karaoke captions burned")
    else:
        if not captions_on:
            print("  subtitles OFF (assemble_captions) — clean cut, no captions")
        elif not burner:
            print("  (no libass ffmpeg — captions skipped; "
                  "brew install ffmpeg-full to enable)")
        merged.replace(final)

    # `music: false` (template/overrides) skips the background-music mix
    # entirely — for relying on the platform's own music/audio. Default on
    # (a track is still only mixed when one is actually resolved).
    music_on = tpl.get("music", True)
    total = sum(s["need"] for s in shots)
    music = _resolve_music(folder, tpl, total) if music_on else None
    if not music_on:
        print("  music OFF (rely on platform audio)")
    if music:
        # music may start partway in — at the start of a chosen segment, easing
        # in there (overrides.music_start_seg; 0/absent = from the top)
        ov = config.channel().overrides
        odata = json.loads(ov.read_text()) if ov.exists() else {}
        start_seg = max(0, min(int(odata.get("music_start_seg", 0) or 0),
                               len(shots) - 1))
        offset = sum(s["need"] for s in shots[:start_seg]) if start_seg else 0.0
        ms = int(round(offset * 1000))
        delay = f"adelay={ms}|{ms}," if ms > 0 else ""
        fadein = f"afade=t=in:st={offset:.1f}:d=1," if offset > 0 else ""
        plain = config.art(folder, "nomusic.mp4")
        final.replace(plain)
        r = sh(["ffmpeg", "-y", "-i", config.art_rel("nomusic.mp4"),
                "-stream_loop", "-1", "-i", str(music),
                "-filter_complex",
                f"[1:a]{delay}volume=0.07,{fadein}"
                f"afade=t=out:st={max(total-2, 0):.1f}:d=2[bg];"
                "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "short.mp4"], cwd=folder)
        if r.returncode != 0:
            print(f"  ! music mix failed, shipping without: "
                  f"{r.stderr[-200:]}", file=sys.stderr)
            plain.replace(final)
        else:
            # nomusic.mp4 is RETAINED in build/ (the no-music master)
            generated = music.parent == folder and music.name == "music.mp3"
            credit = ("ElevenLabs AI music — royalty-free, licensed for "
                      "commercial use (no attribution needed)" if generated
                      else music.name)
            (folder / "music.txt").write_text(credit + "\n")
            print(f"  music: {credit}")

    _apply_watermark(final, folder, tpl)         # channel watermark, corner
    print(f"  FINAL -> {folder.name}/short.mp4 "
          f"({ffprobe_duration(final):.1f}s)  ·  build scratch kept in "
          f"{folder.name}/build/")
    return final


def _score_clip(target: Path, folder: Path, note: str = "") -> None:
    """Run the Virality Predictor (brain_activity) on `target` and write
    virality.md (non-fatal). Shared by the end-of-run score and the hook-first
    early score."""
    vp = folder / "virality.md"
    if vp.exists():
        print("  virality.md (cached)")
        return
    try:
        job = hf(["generate", "create", "brain_activity",
                  "--video", str(target), "--wait"], timeout=1800)
    except RuntimeError as e:
        print(f"  ! virality scoring failed (non-fatal): {e}", file=sys.stderr)
        return
    usage.record_generation(model="brain_activity",  # FYI + ledger
                            label="virality predictor")
    txt = json.dumps(job, indent=2)
    report = re.search(r'https://[^\s"\']+virality[^\s"\']*', txt)
    vp.write_text(f"# Virality Predictor — {folder.name}\n\n" + note
                  + (f"Report: {report.group(0)}\n\n" if report else "")
                  + "```json\n" + txt[:6000] + "\n```\n")
    print("  virality.md written — read it before uploading")


def step_score(final: Path, folder: Path) -> None:
    vp = folder / "virality.md"
    if vp.exists():
        print("  virality.md (cached)")
        return
    # brain_activity (Virality Predictor) rejects clips >16s; our long-form
    # Shorts run 100s+, so score the OPENING ~15s — the hook, which is exactly
    # what the virality gate's hook score judges anyway.
    target, note, tmp = final, "", None
    dur = ffprobe_duration(final)
    if dur > 16:
        tmp = config.art(folder, "_hook16.mp4")
        r = sh(["ffmpeg", "-y", "-i", str(final), "-t", "15",
                "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                "-movflags", "+faststart", str(tmp)])
        if r.returncode == 0 and tmp.exists():
            target = tmp
            note = (f"_Scored on the first 15s (the hook) — the Virality "
                    f"Predictor caps at 16s; the full video is "
                    f"{dur:.0f}s._\n\n")
            print(f"  virality: scoring the first 15s (hook) — the predictor "
                  f"caps at 16s, video is {dur:.0f}s")
        else:
            tmp.unlink(missing_ok=True)
            tmp = None
    _score_clip(target, folder, note)
    if tmp:
        tmp.unlink(missing_ok=True)


def score_hook(topic: str) -> bool:
    """HOOK-FIRST virality: build a standalone clip of JUST the opening segment
    (its voice + key0 + clip0/ken-burns + overlays) and score it into
    virality.md — BEFORE the rest of the keyframes/clips are generated, so a
    weak hook is caught and rewritten before the expensive shots are spent.
    The later step_score then sees virality.md cached and skips. Returns True
    if a fresh score was written."""
    folder = config.topic_dir(topic)
    tpl = templates.load_pinned(folder)
    d = step_research(topic, folder)
    script = step_script(d, folder)
    audio = step_voice(script, folder, tpl)
    shots = shot_list(script, audio, folder)
    if not shots:
        return False
    shot = shots[0]
    if not step_keyframe(shot, d, folder, tpl):
        die("hook keyframe failed — rerun to retry")
    if shot["seg"].get("animate", True):
        if not step_clip(shot, folder, tpl):
            print("  hook clip fell back to ken burns")
    overlays = tpl.get("assemble_overlays", True)
    if overlays and tpl.get("reveal_overlay_text", True):
        _ensure_overlays([shot], folder, tpl)
    speed = float(tpl.get("audio_speed", 1.0))
    rf = float(tpl.get("overlay_reveal_at", 0.6))
    text_on = overlays and tpl.get("reveal_overlay_text", True)
    max_slow = float(tpl.get("max_slowdown", 3.0))
    preview = config.art(folder, "_hookpreview.mp4")
    # a SECTION-NATIVE opener conforms each of its sections (its own audio) and
    # concatenates them; a plain opener is one part
    secs = _segment_sections(shot, folder, audio["segments"][0])
    if secs:
        sec_parts = []
        for sec in secs:
            af = sec["audio"]
            sneed = (_voice_end(af) / max(speed, 0.1)) if af.exists() else 1.0
            sec_parts.append(_conform(
                sec, folder, 0, rf, speed, text_on, need=sneed,
                max_slow=max_slow, tpl=tpl, vo_path=af,
                out_path=config.art(folder, f"part0_s{sec['section']}.mp4"),
                tag=f"0s{sec['section']}"))
        lst = config.art(folder, "_hookcat.txt")
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in sec_parts))
        r = sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-r", str(FPS), "-c:v", "libx264", "-preset", "fast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                str(preview)])
        if r.returncode != 0:
            die(f"hook preview concat: {r.stderr[-200:]}")
        need = sum(ffprobe_duration(p) for p in sec_parts)
    else:
        need = _voice_end(config.art(folder, "seg0.mp3")) / max(speed, 0.1)
        part = _conform(shot, folder, 0, rf, speed, text_on,
                        need=need, max_slow=max_slow, tpl=tpl)
        shutil.copy(str(part), str(preview))
    archive(folder, "virality.md")   # drop any prior score so a re-check rescores
    _score_clip(preview, folder,
                note=(f"_Hook-first read: scored on the OPENING segment only "
                      f"(~{need:.0f}s), before the rest of the video is "
                      f"generated._\n\n"))
    return True


# cover-text element knobs (the browser UI's free cover editor): named sizes
# and inks; an element may also carry a raw px size / #rrggbb colour
_EL_SIZES = {"xl": 150, "l": 124, "m": 96, "s": 64}
_EL_COLORS = {"ivory": (243, 234, 211), "white": (255, 255, 255),
              "black": (18, 18, 18)}


def _compose_thumbnail(bg: Path, out: Path, ticker: str, headline: str,
                       accent=(217, 178, 90), kicker: str = None,
                       ribbon: str = None, elements: list = None) -> bool:
    """Cover-crop the AI background to 1080x1920, dark-scrim the top & bottom
    for legibility, then stamp a big ticker (top) and the tease headline
    (bottom) in the channel font with a heavy stroke. An optional `kicker`
    (e.g. "COMING TOMORROW") prints as a smaller accent line above the ticker —
    the teaser-thumbnail variant posted a day before the video drops. An
    optional `ribbon` (the SHOW mark, e.g. "HOW THEY BUILT IT" — channel.yaml
    thumbnail.ribbon) draws as a gold letterspaced banner across the very top,
    pushing the ticker/kicker down beneath it."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    img = Image.open(str(bg)).convert("RGB")
    scale = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
    ox, oy = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((ox, oy, ox + W, oy + H)).convert("RGBA")
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for y in range(H):
        top = max(0.0, 1 - y / (H * 0.34))          # dark over the top third
        bot = max(0.0, (y - H * 0.52) / (H * 0.48))  # dark over the bottom ~half
        sd.line([(0, y), (W, y)], fill=(6, 10, 8, int(205 * max(top, bot))))
    img = Image.alpha_composite(img, scrim)
    draw = ImageDraw.Draw(img)

    def block(text, size, fill, top_y=None, bottom_y=None):
        text = _upper_display(text)
        f = _display_font(text, size)
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=f) <= W * 0.9 or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        asc, desc = f.getmetrics()
        lh = asc + desc + 12
        y = top_y if top_y is not None else bottom_y - lh * len(lines)
        for ln in lines:
            x = (W - draw.textlength(ln, font=f)) / 2
            draw.text((x, y), ln, font=f, fill=fill + (255,),
                      stroke_width=max(7, size // 12), stroke_fill=(0, 0, 0, 255))
            y += lh

    if elements:
        # USER-DESIGNED cover (the browser cover editor): each element is a
        # centred text block at its own vertical position — replaces the
        # default ticker/headline layout entirely. `pos` is a 0-1 vertical
        # fraction (or top|middle|bottom); `size` a named tier or px; `color`
        # a named ink, 'accent', or #rrggbb.
        for el in elements:
            txt = str(el.get("text") or "").strip()
            if not txt:
                continue
            sz = el.get("size", "l")
            try:
                size = _EL_SIZES.get(str(sz).lower()) or max(24, int(sz))
            except (TypeError, ValueError):
                size = _EL_SIZES["l"]
            c = str(el.get("color") or "ivory").lower()
            if c == "accent":
                fill = accent
            elif c.startswith("#") and len(c) == 7:
                try:
                    fill = tuple(int(c[k:k + 2], 16) for k in (1, 3, 5))
                except ValueError:
                    fill = _EL_COLORS["ivory"]
            else:
                fill = _EL_COLORS.get(c, _EL_COLORS["ivory"])
            pos = el.get("pos", 0.5)
            if isinstance(pos, str):
                pos = {"top": 0.06, "middle": 0.42,
                       "bottom": 0.82}.get(pos.lower(), 0.42)
            try:
                pos = max(0.0, min(float(pos), 0.95))
            except (TypeError, ValueError):
                pos = 0.42
            block(txt, size, fill, top_y=int(H * pos))
        if kicker:                     # the teaser variant keeps its kicker
            block(kicker, 78, accent, top_y=int(H * 0.012))
        img.convert("RGB").save(str(out), quality=92)
        return True

    top0 = int(H * 0.045)
    if ribbon:
        # the show mark: a gold banner with dark letterspaced caps, top-centre
        disp = "   ".join(" ".join(w) for w in ribbon.upper().split())
        f = _display_font(disp, 38)
        tw = draw.textlength(disp, font=f)
        asc, desc = f.getmetrics()
        pad, bh = 40, asc + desc + 30
        bw = min(tw + pad * 2, W * 0.94)
        x0, y0 = (W - bw) / 2, int(H * 0.028)
        draw.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=14,
                               fill=accent + (255,),
                               outline=(15, 18, 34, 255), width=3)
        draw.text(((W - tw) / 2, y0 + (bh - asc - desc) / 2), disp, font=f,
                  fill=(15, 18, 34, 255))
        top0 = y0 + bh + 22
    if kicker:
        block(kicker, 78, accent, top_y=top0)
        if ticker:
            block(ticker, 150, (243, 234, 211), top_y=top0 + int(H * 0.055))
    elif ticker:
        block(ticker, 150, accent if not ribbon else (243, 234, 211),
              top_y=top0 + (int(H * 0.01) if not ribbon else 0))
    if headline:
        block(headline, 124, (243, 234, 211), bottom_y=int(H * 0.94))
    img.convert("RGB").save(str(out), quality=92)
    return True


def _thumb_headline_safe(headline: str, d: dict, ch) -> str:
    """Code-enforced: the thumbnail headline TEASES, it never states the
    verdict. A question (contains '?') is always a tease and passes. A
    statement that asserts the decision (the positive-verdict word, a negative
    marker, a pass/fail word, or a ✓/✗-style glyph) is rejected and rebuilt as
    the canonical question. Mirrors the script's facts/verdict lock for the
    cover."""
    h = (headline or "").strip()
    low = h.lower()
    pos = (ch.positive_verdict or "").lower()
    glyphs = ("✓", "✔", "✗", "✘", "☑", "✅", "❌", "🟢", "🔴", "👍", "👎")
    spoils = any(g in h for g in glyphs)
    if not spoils and "?" not in h:        # a statement — does it assert it?
        words = ch.negative_verdict_markers + (
            "passes", "passed", "approved", "cleared", "rejected", "failed")
        if any(w in low for w in words):
            spoils = True
        elif pos and re.search(r"\b" + re.escape(pos) + r"\b", low):
            spoils = True
    if not spoils:
        return h
    tk = d.get("ticker") or ""
    subject = tk or d.get("title") or d.get("name") or d.get("topic") or ""
    safe = (f"{subject}: {ch.positive_verdict}?" if pos and subject
            else f"{subject}?" if subject else "?")
    print(f"  thumbnail: headline withheld the verdict — using a tease "
          f"(\"{safe}\")", file=sys.stderr)
    return safe


def thumb_bg_prompt(folder: Path, d: dict, tpl: dict) -> str:
    """The exact prompt the thumbnail BACKGROUND generates from — extracted so
    the browser UI can show (and the concept edit can steer) what produced the
    image. Pure read; step_thumbnail uses it verbatim."""
    ch = config.channel()
    direction = _direction(folder)
    thumb = direction.get("thumbnail") or {}
    style = direction.get("style") or tpl.get("style", "")
    title = d.get("title") or d.get("name") or d.get("topic", "")
    concept = (thumb.get("concept")
               or " ".join(x for x in (direction.get("visual_concept"),
                                       direction.get("anchor_motif")) if x)
               or f"a dramatic cinematic hero shot representing {title}")
    verdict_guard = ("Do NOT reveal the verdict or answer: no green/red "
                     "check marks, ticks, crosses, stamps, seals, traffic "
                     "lights, thumbs up/down or any pass/fail symbol — the "
                     "cover poses the question, it must not show the answer.")
    # the SHOW's locked cover FORMAT (channel.yaml `thumbnail:` — shows
    # override it): the format structures the frame, the director's concept
    # supplies this episode's content for it. No block → the classic
    # single-hero cover, unchanged.
    tcfg = ch.cfg.get("thumbnail") or {}
    fmt = (tcfg.get("format") or "").strip()
    if fmt:
        return (f"YouTube Shorts thumbnail cover art, vertical 9:16. "
                f"{fmt} THIS EPISODE's content for that design: {concept}. "
                f"{(tcfg.get('palette') or '').strip()} "
                f"Absolutely NO text, letters, numbers, words or captions "
                f"anywhere in the image — a real logo on signage inside "
                f"the scene is the ONE exception. "
                f"{verdict_guard}")
    return (f"YouTube Shorts thumbnail background, vertical 9:16. "
            f"{concept}. "
            f"{style} A single bold hero subject, dramatic high-contrast "
            f"lighting, punchy saturated colour, depth and atmosphere; "
            f"keep the TOP and BOTTOM thirds clean and uncluttered for "
            f"large text. Absolutely NO text, letters, numbers, words, "
            f"logos or captions anywhere in the image. {verdict_guard}")


def step_thumbnail(folder: Path, d: dict, tpl: dict):
    """Two bespoke vertical covers from ONE AI background (the director's
    thumbnail concept, NO baked text): `thumbnail.png` (the video cover — big
    ticker + tease headline, or the user's custom text `elements` from the
    browser cover editor) and `teaser.png` (a "COMING TOMORROW" variant of
    the same shot, to post a day before the drop). Both tease the question,
    never spoil the verdict. ~2 credits (the bg generates once); non-fatal."""
    out = folder / "thumbnail.png"
    teaser = folder / "teaser.png"
    if out.exists() and teaser.exists():
        print("  thumbnail.png + teaser.png (cached)")
        return out
    ch = config.channel()
    direction = _direction(folder)
    thumb = direction.get("thumbnail") or {}
    title = d.get("title") or d.get("name") or d.get("topic", "")
    bg = folder / "thumb_bg.png"
    tcfg = ch.cfg.get("thumbnail") or {}
    fmt = (tcfg.get("format") or "").strip()
    args = ["--aspect_ratio", "9:16"]
    prompt = thumb_bg_prompt(folder, d, tpl)
    if not generate(tpl["image_model"], prompt, bg, (".png", ".jpg", ".webp"),
                    args, "thumbnail bg",
                    alt_prompts=[f"{prompt} {SAFE_TAIL}"]):
        print("  ! thumbnail background failed (non-fatal)", file=sys.stderr)
        return None
    try:
        script = json.loads((folder / "script.json").read_text())
        seg0 = (script.get("segments") or [{}])[0]
        ovs0 = _seg_overlays(seg0)
        hook_overlay = ovs0[0]["text"] if ovs0 else ""
    except (OSError, ValueError):
        hook_overlay = ""
    ticker = d.get("ticker") or ""
    ticker_disp = ticker or ""
    if fmt and not ticker_disp:
        # the format's giant name line: the episode title
        ticker_disp = str(d.get("title") or "").split("—")[0].split("(")[0].strip()
    headline = thumb.get("text") or hook_overlay or title
    # code-enforced: the cover teases, it never states the verdict
    headline = _thumb_headline_safe(headline, d, ch)
    ribbon = (tcfg.get("ribbon") or "").strip() or None
    # `elements` (browser cover editor) — the user's own text layout replaces
    # the default ticker/headline composition entirely
    elements = thumb.get("elements") or None
    if not out.exists():
        if not _compose_thumbnail(bg, out, ticker_disp, headline,
                                  ribbon=ribbon, elements=elements):
            import shutil as _sh
            _sh.copy(str(bg), str(out))   # no Pillow → ship the raw background
            print("  ! thumbnail needs Pillow for text — shipped the raw bg",
                  file=sys.stderr)
        elif elements:
            print(f"  thumbnail.png -> {folder.name}/  "
                  f"({len(elements)} custom text element(s))")
        else:
            print(f"  thumbnail.png -> {folder.name}/  (headline: \"{headline}\")")
    # the teaser: the SAME background, a "COMING TOMORROW" kicker, post a day
    # before the drop. Teases the question; the headline is already verdict-safe.
    if not teaser.exists():
        kicker = tpl.get("teaser_kicker", "COMING TOMORROW")
        if _compose_thumbnail(bg, teaser, ticker_disp, headline, kicker=kicker,
                              ribbon=ribbon, elements=elements):
            print(f"  teaser.png -> {folder.name}/  (kicker: \"{kicker}\")")
        else:
            print("  ! teaser needs Pillow for text — skipped", file=sys.stderr)
    return out


def _paste_watermark_still(img, watermark, cw, ch):
    """Paste a (png, pos, opacity) watermark lockup onto a PIL carousel still."""
    from PIL import Image
    wpng, wpos, wop = watermark
    if not Path(wpng).exists():
        return
    wm = Image.open(str(wpng)).convert("RGBA")
    if wop < 1:
        wm.putalpha(wm.split()[3].point(lambda v: int(v * wop)))
    p = (wpos or "top-right").lower()
    m = 32
    wx = cw - wm.width - m if "right" in p else m
    wy = ch - wm.height - m if "bottom" in p else m
    img.alpha_composite(wm, (max(0, wx), max(0, wy)))


def step_seo(topic: str, folder: Path):
    if (folder / "seo.md").exists():
        print("  seo.md (cached)")
        return
    import seo
    seo.run(topic)


def reset_hook(folder: Path):
    """Drop every artifact derived from the opening segment so a rerun
    regenerates it (cheap — everything else stays cached). audio.json is kept:
    step_voice rebuilds it, reusing cached segments' word timings."""
    # a multi-shot opener also has key0_<k>.png / clip0_<k>.mp4 sub-shots
    extra = [p.name for p in config.art_glob(folder, "key0_*.png")] + \
            [p.name for p in config.art_glob(folder, "clip0_*.mp4")]
    done = archive(folder, "seg0.mp3", "key0.png", "clip0.mp4",
                   "text0.png", "short.mp4", "virality.md", *extra)
    for name in done:
        print(f"  reset {name} (kept in .versions/)")


def _upload_seo_text(folder: Path, topic: str) -> str:
    """A paste-ready seo.txt for the upload bundle (title / description+hashtags /
    tags / music credit), built from seo.json."""
    sp = folder / "seo.json"
    title, desc, tags, hashtags = "", "", [], []
    if sp.exists():
        try:
            s = json.loads(sp.read_text())
            title = s.get("title") or ""
            desc = s.get("description") or ""
            tags = s.get("tags") or []
            hashtags = s.get("hashtags") or []
        except ValueError:
            pass
    title = title or topic
    body = (desc + "\n\n" + " ".join(hashtags)).strip() if hashtags else desc
    lines = ["TITLE", title, "", "DESCRIPTION", body, "",
             "TAGS", ", ".join(tags), ""]
    mc = folder / "music.txt"
    if mc.exists():
        lines += ["MUSIC CREDIT (add to the description if the track needs it)",
                  mc.read_text().strip(), ""]
    return "\n".join(lines)


def export_upload(folder: Path, tpl: dict) -> Path:
    """Gather an UPLOAD-READY bundle into <run>/upload/ — everything needed to
    post, in one folder: BOTH video cuts (`<TOPIC>_music.mp4` with the
    background track + a clean `<TOPIC>_nomusic.mp4` for platform audio), the
    `thumbnail.png`, and a paste-ready `seo.txt`. Idempotent (rebuilt each call);
    cheap (copies + one watermark pass). A music cut is produced only when a
    track was actually mixed (`music.txt` + the no-music master both present)."""
    import shutil
    folder = Path(folder)
    short = config.art(folder, "short.mp4")           # the final cut (root)
    if not short.exists():
        print("  ! no short.mp4 yet — assemble first", file=sys.stderr)
        return folder / "upload"
    upd = folder / "upload"
    upd.mkdir(exist_ok=True)
    topic = folder.name.split("_", 1)[-1] or folder.name
    nomaster = config.art(folder, "nomusic.mp4")      # build/nomusic.mp4 (pre-wm)
    has_music = (folder / "music.txt").exists() and nomaster.exists()
    made = []
    # clean no-music cut (always). With music mixed, the no-music master is
    # pre-watermark — copy it and stamp the watermark so BOTH cuts carry the
    # brand; with no music, short.mp4 IS the clean cut (already watermarked).
    nm = upd / f"{topic}_nomusic.mp4"
    shutil.copy(str(nomaster if has_music else short), str(nm))
    if has_music:
        _apply_watermark(nm, folder, tpl)
    made.append(nm.name)
    # with-music cut (only when a track was mixed)
    if has_music:
        wm = upd / f"{topic}_music.mp4"
        shutil.copy(str(short), str(wm))
        made.append(wm.name)
    else:
        print(f"  (no music track mixed — only the clean cut; add a track to "
              f"channels/{config.channel().name}/assets/music/ + set "
              "overrides.music for a music version)")
    thumb = folder / "thumbnail.png"
    if thumb.exists():
        shutil.copy(str(thumb), str(upd / "thumbnail.png"))
        made.append("thumbnail.png")
    (upd / "seo.txt").write_text(_upload_seo_text(folder, topic))
    made.append("seo.txt")
    print(f"  ✓ upload bundle → {folder.name}/upload/  ({', '.join(made)})")
    return upd


def _img_arg_path(img_args: list):
    """The single `--image`/`--start-image` path in an args list, or None."""
    for i, a in enumerate(img_args):
        if a in ("--image", "--start-image") and i + 1 < len(img_args):
            return img_args[i + 1]
    return None


def export_manual_manifest(folder: Path, tpl: dict) -> Path:
    """Write <run>/manual/prompts.md — every keyframe + clip PROMPT and its
    reference DEPENDENCY, in generation order, for hand-generating on the
    Higgsfield WEBSITE (an unlimited account) instead of spending CLI credits.

    Each item lists the exact prompt, the model, its ONE reference image (a cast
    avatar / style ref is copied into manual/refs/ for easy upload; a reference
    that's an EARLIER keyframe in this run is named so you generate it first),
    and the '→ save as' path to drop the finished file. The pipeline then picks
    them up by presence — finish with `kalinga.py run assemble <TOPIC>`.

    Needs the voiced run (audio.json) so clip durations are exact."""
    import shutil
    folder = Path(folder)
    script = json.loads((folder / "script.json").read_text())
    facts = json.loads((folder / "facts.json").read_text())
    ap = folder / "audio.json"
    if not ap.exists():
        raise StepFailed("manual export needs the voice stage first "
                         "(no audio.json) — run `kalinga.py run voice <TOPIC>`")
    audio = json.loads(ap.read_text())
    shots = shot_list(script, audio, folder)
    mdir = folder / "manual"
    refs = mdir / "refs"
    mdir.mkdir(exist_ok=True)
    refs.mkdir(exist_ok=True)
    topic = folder.name.split("_", 1)[-1] or folder.name

    def _ref_note(path_str):
        """A human line for a reference dependency + stage any static file."""
        if not path_str:
            return "none (this shot sets the look — no reference)"
        p = Path(path_str)
        try:
            inside = folder.resolve() in p.resolve().parents
        except OSError:
            inside = False
        if inside and p.name.startswith("key"):
            return (f"{p.name}  ← an EARLIER keyframe in this run; generate that "
                    "one FIRST, then upload your saved keyframes/" + p.name +
                    " as the reference image")
        # a static asset (cast avatar / style ref): copy it in for easy upload
        if p.exists():
            dst = refs / p.name
            try:
                if not dst.exists():
                    shutil.copy(str(p), str(dst))
            except OSError:
                pass
            return f"manual/refs/{p.name}  (uploaded as the reference image)"
        return f"{path_str}  (reference image)"

    L = []
    L.append(f"# Manual generation — {topic}\n")
    L.append("Generate each item below ON THE WEBSITE, IN ORDER (later keyframes "
             "reference earlier ones). Download each result and SAVE it to the "
             "exact '→ save as' path. The pipeline picks them up automatically; "
             "when every file is in place, run:\n")
    L.append(f"    python3 kalinga.py run assemble {topic}\n")
    L.append(f"Image model: `{tpl.get('image_model', 'nano_banana_2')}` · "
             f"aspect 9:16   |   Video model: `{tpl.get('video_model', 'seedance_2_0')}` · aspect 9:16\n")
    L.append("Reference images live in `manual/refs/`. Shots not listed under "
             "CLIPS are free Ken Burns stills — no video needed.\n")

    L.append("\n" + "─" * 56 + "\n## KEYFRAMES (images)\n")
    for shot in shots:
        i = shot["index"]
        full, img_args, _ = _keyframe_assembled(shot, facts, folder, tpl)
        ref = _img_arg_path(img_args)
        L.append(f"\n### key{i} — {shot['label']}")
        L.append(f"reference image: {_ref_note(ref)}")
        L.append(f"→ save as: keyframes/key{i}.png")
        L.append("prompt:")
        L.append(full)

    clip_shots = [sh for sh in shots
                  if sh["seg"].get("animate", True) or clip_voice_on(sh["seg"])]
    L.append("\n" + "─" * 56 + "\n## CLIPS (videos)\n")
    if not clip_shots:
        L.append("(none — every shot is a still/Ken Burns; no video generation "
                 "needed)")
    for shot in clip_shots:
        i = shot["index"]
        L.append(f"\n### clip{i} — {shot['label']}")
        startflag = ("start image" if _clip_keyframe_as_start(tpl)
                     else "reference image")
        L.append(f"{startflag}: keyframes/key{i}.png  (generate that keyframe first)")
        if clip_voice_on(shot["seg"]):
            L.append(f"audio reference: audio/seg{i}.mp3  (lip-sync driver — "
                     "upload it as the voice/audio reference)")
        L.append(f"duration: {clip_duration(shot, tpl)}s")
        L.append(f"→ save as: clips/clip{i}.mp4")
        L.append("prompt:")
        L.append(clip_prompt(shot, tpl))

    out = mdir / "prompts.md"
    out.write_text("\n".join(L) + "\n")
    print(f"  ✓ manual manifest → {folder.name}/manual/prompts.md  "
          f"({len(shots)} keyframes, {len(clip_shots)} clips)")
    print(f"    generate them on the website, drop files into {folder.name}/"
          f"keyframes|clips/, then: kalinga.py run assemble {topic}")
    return out


def main(topic: str) -> Path:
    print(f"=== {topic} → short.mp4 (Higgsfield) ===")
    ensure_cli()
    folder = config.topic_dir(topic)
    usage.bind(folder)
    tpl = templates.load_pinned(folder)
    print(f"  channel: {config.channel().name} · template: {tpl['name']}")
    d = step_research(topic, folder)
    script = step_script(d, folder)
    audio = step_voice(script, folder, tpl)
    shots = shot_list(script, audio, folder)
    step_keyframes(shots, d, folder, tpl)
    step_clips(shots, folder, tpl)
    final = step_assemble(shots, audio, folder, tpl)
    step_seo(topic, folder)
    step_thumbnail(folder, d, tpl)
    step_score(final, folder)
    export_upload(folder, tpl)            # upload-ready bundle (both cuts + thumb + seo)
    print(f"\nDone: {final}\nUpload bundle: {folder / 'upload'}/  "
          f"(music + no-music cuts, thumbnail, seo.txt)"
          f"\nVirality: {folder / 'virality.md'}")
    return final


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
    except StepFailed as e:
        print(f"\n  ✗ {e}", file=sys.stderr)
        sys.exit(1)
