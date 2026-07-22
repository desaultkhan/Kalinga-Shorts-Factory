"""
cast_setup.py — set up a channel's persistent CAST roster.

A channel can have a fixed set of recurring characters (channels/<name>/
cast.json) — each with a NAME, PERSONALITY, VOICE and an AVATAR image asset.
The script writer is then restricted to these characters, and keyframes are
conditioned on a character's avatar so the right person appears on screen.

    python3 kalinga.py [--channel X] cast

Walks you through add / edit / remove, deciding the name, personality, voice
and avatar for each character. Stores cast.json + cast/<name>.png in the
channel folder. Avatar generation uses the Higgsfield image model (~2 credits).
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import config
import make_video
import templates

# sensible per-engine defaults by gender (edge has natural voices; the Inworld
# names are the known-good ones)
GENDER_VOICE = {
    "male":    {"higgsfield": "Mark (en)",   "edge": "en-US-ChristopherNeural"},
    "female":  {"higgsfield": "Ashley (en)", "edge": "en-US-AriaNeural"},
    "neutral": {"higgsfield": "Mark (en)",   "edge": "en-US-GuyNeural"},
}

# Voice catalogs shown (numbered) by the cast wizard — (voice, gender, note).
# Not exhaustive: edge has hundreds more (any en-XX-NameNeural) and Inworld
# 117; the picker still accepts ANY typed name. Auto-assignment for extra cast
# uses the make_video.ENGINES registry's per-engine pools, kept separate.
EDGE_CATALOG = [
    ("en-US-ChristopherNeural", "male",   "US · warm"),
    ("en-US-GuyNeural",         "male",   "US · news"),
    ("en-US-EricNeural",        "male",   "US · calm"),
    ("en-US-RogerNeural",       "male",   "US · casual"),
    ("en-GB-RyanNeural",        "male",   "British"),
    ("en-AU-WilliamNeural",     "male",   "Australian"),
    ("en-IN-PrabhatNeural",     "male",   "Indian"),
    ("en-US-AriaNeural",        "female", "US · bright"),
    ("en-US-JennyNeural",       "female", "US · friendly"),
    ("en-US-MichelleNeural",    "female", "US · warm"),
    ("en-US-AnaNeural",         "female", "US · youthful"),
    ("en-GB-SoniaNeural",       "female", "British"),
    ("en-AU-NatashaNeural",     "female", "Australian"),
    ("en-IN-NeerjaNeural",      "female", "Indian"),
]
INWORLD_CATALOG = [
    ("Mark (en)",      "male",   "default"),
    ("Hades (en)",     "male",   "deep"),
    ("Craig (en)",     "male",   ""),
    ("James (en)",     "male",   ""),
    ("Oliver (en)",    "male",   ""),
    ("Simon (en)",     "male",   ""),
    ("Elliot (en)",    "male",   ""),
    ("Ethan (en)",     "male",   ""),
    ("Sebastian (en)", "male",   ""),
    ("Nate (en)",      "male",   ""),
    ("Brian (en)",     "male",   ""),
    ("Arjun (en)",     "male",   "Indian"),
    ("Ashley (en)",    "female", "default"),
    ("Olivia (en)",    "female", ""),
    ("Sarah (en)",     "female", ""),
    ("Serena (en)",    "female", ""),
    ("Jessica (en)",   "female", ""),
    ("Claire (en)",    "female", ""),
    ("Elizabeth (en)", "female", ""),
    ("Julia (en)",     "female", ""),
    ("Luna (en)",      "female", ""),
    ("Priya (en)",     "female", "Indian"),
    ("Anjali (en)",    "female", "Indian"),
]

_EDGE_VOICE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z0-9]+Neural$")
_INWORLD_VOICE_RE = re.compile(r"^[A-Za-z][\w .'-]* \([a-z]{2}\)$")


def voice_ok(engine: str, v) -> bool:
    """Does `v` look like a real voice for `engine`? Catches typos like a
    stray 'b' that would otherwise crash TTS. 'auto' is a valid sentinel."""
    v = (v or "").strip()
    if not v:
        return False
    if v.lower() == "auto":
        return True
    if engine == "edge":
        return (any(v == x[0] for x in EDGE_CATALOG)
                or bool(_EDGE_VOICE_RE.match(v)))
    return (any(v == x[0] for x in INWORLD_CATALOG)
            or bool(_INWORLD_VOICE_RE.match(v)))

# ---- character reference sheet ----------------------------------------------
# Beyond the single front portrait, a member carries a full reference LIBRARY,
# the cross product of three dimensions:
#   OUTFIT   formal (suit) · casual (t-shirt + jeans) · ethnic (traditional
#            attire — channel.yaml `cast_outfits:` overrides the wardrobe)
#   ANGLE    front · side · back
#   LIGHT    day (warm) · overcast (cool flat) · night (blue)   — FACE shots
# i.e. per outfit: 9 face (3 angles × 3 lights) + 3 full-length (3 angles) = 12,
# so the full library is 36 images. Every variant is conditioned on the primary
# avatar via --image so it stays the SAME person; the channel STYLE is dropped
# and a plain neutral backdrop forced, so the reference never confuses later
# --image conditioning. Stored under cast/<Name>/ (a folder per character), and
# every image is described in cast.json `refs` (a list of metadata dicts) AND
# embedded as PNG text — so the director LLM can pick which to include when.
REF_ANGLES = ("front", "side", "back")
REF_LIGHTS = ("day", "overcast", "night")
OUTFITS = ("formal", "casual", "ethnic")   # built-in defaults; the effective
                                           # set is outfit_names() (channel-aware)
_ANGLE_FACE = {
    "front": "head-and-shoulders portrait facing the camera directly, eye contact",
    "side":  "head-and-shoulders profile view from the side, looking off-camera",
    "back":  "head-and-shoulders view from directly behind — back of the head{} "
             "and shoulders, face not visible",
}
_ANGLE_FULL = {
    "front": "full-length head-to-toe shot, standing relaxed, facing the camera",
    "side":  "full-length head-to-toe shot, standing, full side profile",
    "back":  "full-length head-to-toe shot, standing, seen from directly behind",
}
# Dramatically distinct lighting — the model conditions on the avatar and will
# copy its lighting verbatim unless told, forcefully, to RELIGHT. Each entry is
# a strong, unambiguous description so the three reads are obviously different.
_LIGHT = {
    "day":      "warm golden sunny daylight — a bright, clearly WARM-toned "
                "directional sun, cheerful and vivid with warm golden "
                "highlights and a warm colour temperature",
    "overcast": "cold flat overcast daylight — soft, even, clearly COOL "
                "blue-grey light with no warmth at all, low contrast and "
                "desaturated, a dull grey cloudy day",
    "night":    "nighttime — a clearly COOL blue after-dark ambience with a "
                "soft warm lamp glow on the face, moody and obviously at night "
                "but the face still clearly visible (not black), deep blue "
                "shadows",
}
# Outfit clothing. `None` = formal (the avatar already wears it, no override).
# A dict is gender-keyed. The face/hair/identity is always held from the avatar.
# These are channel-NEUTRAL defaults — a channel overrides any outfit's `wear`
# and `when` (or adds/replaces outfits entirely) via channel.yaml:
#   cast_outfits:
#     ethnic: {wear: "…" | {male: …, female: …, neutral: …}, when: "…"}
_OUTFIT_WEAR = {
    "formal": None,
    "casual": "a relaxed casual outfit — a plain crew-neck t-shirt and "
              "blue denim jeans",
    "ethnic": "traditional attire appropriate to the character's cultural "
              "background, elegant and everyday-real",
}
# One-line "when to use this outfit" guidance — written into cast.json so the
# director knows which to reach for.
OUTFIT_WHEN = {
    "formal": "studio, analysis, the decisive beat — anything authoritative "
              "or to-camera",
    "casual": "relatable everyday b-roll, lifestyle, street, candid moments",
    "ethnic": "cultural, festive, family or community context",
}


def _channel_outfits():
    """(names, wear, when) with the channel.yaml `cast_outfits:` overrides
    merged over the neutral built-ins — outfit wardrobe is channel content
    (one channel may dress its cast in traditional wear; another needn't).
    Safe without a bound channel (falls back to the built-ins)."""
    wear = dict(_OUTFIT_WEAR)
    when = dict(OUTFIT_WHEN)
    try:
        cfg = config.channel().cfg.get("cast_outfits") or {}
    except Exception:                                # noqa: BLE001
        cfg = {}
    for name, spec in cfg.items():
        name = str(name).strip().lower()
        if not isinstance(spec, dict):
            continue
        if "wear" in spec:
            wear[name] = spec["wear"]
        elif name not in wear:
            continue                     # a new outfit needs a `wear`
        if spec.get("when"):
            when[name] = str(spec["when"])
        when.setdefault(name, "when the scene calls for it")
    return tuple(wear), wear, when


def outfit_names() -> tuple:
    return _channel_outfits()[0]


def _wear_map() -> dict:
    return _channel_outfits()[1]


def _when_map() -> dict:
    return _channel_outfits()[2]

# A clean character reference must be NEUTRAL — no world/set styling (that lives
# at keyframe time). A plain seamless backdrop keeps the conditioning unambiguous.
_NEUTRAL_BG = ("a plain, completely empty seamless light-grey studio backdrop — "
               "no set, no wall panels, no furniture, no scenery, nothing behind "
               "them but the flat neutral backdrop")


def _outfit_wear(outfit: str, gender: str):
    w = _wear_map().get(outfit)
    if isinstance(w, dict):
        return w.get(gender, w.get("neutral"))
    return w


def _ref_file(name: str, outfit: str, kind: str, angle: str, light: str) -> str:
    """Channel-relative path (like the avatar's), inside the per-character
    folder. Face carries the lighting in its name; full-length is day-lit."""
    stem = (f"{outfit}_{kind}_{angle}_{light}" if kind == "face"
            else f"{outfit}_{kind}_{angle}")
    return f"cast/{name}/{stem}.png"


def _ref_jobs(name: str, outfits=None):
    """Every (outfit, kind, angle, light, file) cell of the library."""
    jobs = []
    for outfit in (outfits or outfit_names()):
        for angle in REF_ANGLES:
            for light in REF_LIGHTS:
                jobs.append((outfit, "face", angle, light,
                             _ref_file(name, outfit, "face", angle, light)))
        for angle in REF_ANGLES:          # full-length: day lighting (neutral)
            jobs.append((outfit, "full", angle, "day",
                         _ref_file(name, outfit, "full", angle, "day")))
    return jobs


def _ref_prompt(name: str, member: dict, outfit: str, kind: str,
                angle: str, light: str) -> str:
    # the channel `style` is intentionally NOT used — it would bake a busy
    # background into the reference and confuse later --image conditioning.
    appearance = member.get("appearance") or ""
    gender = member.get("gender", "neutral")
    hijab = "/hijab" if gender == "female" else ""
    angle_desc = (_ANGLE_FULL if kind == "full" else _ANGLE_FACE)[angle]
    if "{}" in angle_desc:
        angle_desc = angle_desc.format(hijab)
    wear = _outfit_wear(outfit, gender)
    wear_clause = ("" if not wear else
                   f" Keep their face, hair{hijab} and build IDENTICAL to the "
                   f"reference, but dress them in {wear} instead of the suit.")
    return (f"Photorealistic studio character reference of {name}, the SAME "
            f"person as the reference image — identical face, hair{hijab} and "
            f"glasses. {appearance}.{wear_clause} {angle_desc}. Place them on "
            f"{_NEUTRAL_BG}. Dramatically RELIGHT the whole scene as {light}: "
            f"{_LIGHT[light]} — the lighting must clearly and obviously read as "
            f"{light}, do not copy the reference's lighting. Sharp focus, "
            "consistent recognizable character design, no text, letters, "
            "numbers or logos anywhere.")


def _tag_png(path, meta: dict) -> None:
    """Embed kalinga:* text metadata into a PNG (best-effort, needs Pillow)."""
    try:
        from PIL import Image, PngImagePlugin
        if path.suffix.lower() != ".png":
            return
        img = Image.open(path)
        info = PngImagePlugin.PngInfo()
        for k, v in meta.items():
            info.add_text(f"kalinga:{k}", str(v))
        img.save(path, pnginfo=info)
    except Exception:
        pass


def _sync_refs(name: str, member: dict) -> None:
    """Rebuild member['refs'] as a metadata list from whatever library images
    are actually on disk — the single source of truth the LLM reads."""
    ch = config.channel()
    refs = []
    for outfit, kind, angle, light, file in _ref_jobs(name):
        if (ch.dir / file).exists():
            refs.append({"file": file, "outfit": outfit, "kind": kind,
                         "angle": angle, "light": light,
                         "when": _when_map().get(outfit, "")})
    member["refs"] = refs


def _make_reference_set(name: str, member: dict, style: str, tpl: dict,
                        outfits=None, regen: bool = False) -> bool:
    """Generate the reference library (12 images per requested outfit: face ×9
    + full ×3), each conditioned on the primary avatar. Files land under
    cast/<Name>/; metadata is recorded in member['refs'] and embedded in each
    PNG. Skips images already on disk unless `regen`."""
    ch = config.channel()
    _migrate_flat_refs(name, member)
    avatar = ch.cast_avatar(name)
    if not avatar.exists():
        print("  ! no primary avatar yet — make it first; the reference library "
              "is seeded from it for a consistent face.", file=sys.stderr)
        return False
    img_model = tpl.get("image_model", config.DEFAULT_IMAGE_MODEL)
    seed = ["--aspect_ratio", "9:16", "--image", str(avatar)]
    jobs = _ref_jobs(name, outfits)
    pending = [j for j in jobs if regen or not (ch.dir / j[4]).exists()]
    if not pending:
        print(f"  all {len(jobs)} reference images already present — nothing to do.")
        _sync_refs(name, member)
        return True
    print(f"  generating {len(pending)} reference image(s) for {name} "
          f"(~{len(pending) * 2} credits)…")
    made = 0
    for outfit, kind, angle, light, file in pending:
        out = ch.dir / file
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            make_video.archive(out.parent, out.name)   # keep old in .versions/
        prompt = _ref_prompt(name, member, outfit, kind, angle, light)
        ok = make_video.generate(
            img_model, prompt, out, (".png", ".jpg", ".webp"), seed,
            f"ref {name} {outfit}/{kind}/{angle}/{light}",
            alt_prompts=[f"{prompt} {make_video.SAFE_TAIL}"])
        if ok:
            _tag_png(out, {"character": name, "outfit": outfit, "kind": kind,
                           "angle": angle, "light": light,
                           "when": _when_map().get(outfit, "")})
            made += 1
        else:
            print(f"    ! {outfit}/{kind}/{angle}/{light} failed", file=sys.stderr)
    _sync_refs(name, member)
    print(f"  reference library: {made}/{len(pending)} generated "
          f"({len(member['refs'])}/{len(jobs)} total on disk)")
    return made > 0


def _migrate_flat_refs(name: str, member: dict) -> bool:
    """Move an OLD flat-dict reference sheet (cast/<name>_face_front_day.png,
    refs={'face_front_day': ...}) into the new per-character folder as the
    `formal` outfit (cast/<name>/formal_face_front_day.png). Free; no regen."""
    old = member.get("refs")
    if not isinstance(old, dict):
        return False
    ch = config.channel()
    for key, rel in old.items():
        parts = key.split("_")                    # face_front_day | full_front
        kind, angle = parts[0], parts[1]
        light = parts[2] if len(parts) > 2 else "day"
        dst = ch.dir / _ref_file(name, "formal", kind, angle, light)
        src = ch.dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() and not dst.exists():
            src.rename(dst)
            _tag_png(dst, {"character": name, "outfit": "formal", "kind": kind,
                           "angle": angle, "light": light,
                           "when": _when_map().get("formal", "")})
    _sync_refs(name, member)
    print(f"  migrated {name}'s flat reference sheet → cast/{name}/ (formal)")
    return True


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def _load() -> dict:
    p = config.channel().dir / "cast.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _save(cast: dict) -> None:
    p = config.channel().dir / "cast.json"
    p.write_text(json.dumps(cast, indent=2))
    print(f"  saved {p}")


def _engine_and_style():
    """The channel's default-template TTS engine + visual style for voice menus
    and avatar generation."""
    try:
        tpl = templates.load(config.channel().default_template)
    except Exception:
        tpl = templates.DEFAULTS
    return tpl.get("tts_engine", "edge"), tpl.get("style", ""), tpl


def _pick_voice(engine: str, gender: str, current=None, name: str = "") -> dict:
    """Choose a voice for `engine`; returns an engine-keyed voice dict so it
    works whichever engine a run uses."""
    base = dict(GENDER_VOICE.get(gender, GENDER_VOICE["neutral"]))
    if isinstance(current, dict):
        base.update(current)
    catalog = EDGE_CATALOG if engine == "edge" else INWORLD_CATALOG
    suggested = base.get(engine)
    # voices matching the character's gender first, then the rest
    g = gender if gender in ("male", "female") else None
    ordered = ([v for v in catalog if g and v[1] == g]
               + [v for v in catalog if not g or v[1] != g])
    names = [v[0] for v in ordered]
    if suggested and suggested not in names:        # keep a custom/current pick
        ordered = [(suggested, gender, "current")] + ordered
        names = [suggested] + names
    print(f"  voice for the {engine} engine"
          + (f" (current: {suggested})" if suggested else "") + ":")
    for i, (v, gd, note) in enumerate(ordered):
        tail = f" — {note}" if note else ""
        print(f"    [{i + 1:2}] {v}  ({gd[0].upper()}){tail}"
              + ("  ← current" if v == suggested else ""))
    free = engine == "edge"
    print("    [s]N = sample voice N before choosing"
          + ("  (free)" if free else "  (~1 credit each, paid TTS)"))
    print("    (or type any other voice name — the lists aren't exhaustive)")
    while True:
        pick = _ask("  number to select, [s]N to sample, a name, "
                    "Enter to keep current: ")
        if not pick:
            chosen = suggested or names[0]
        elif pick[:1].lower() == "s" and pick[1:].strip().isdigit():
            j = int(pick[1:].strip())
            if 1 <= j <= len(names):
                _sample_voice(engine, names[j - 1], name)
            else:
                print("    no such number")
            continue
        elif pick.isdigit() and 1 <= int(pick) <= len(names):
            chosen = names[int(pick) - 1]
        else:
            chosen = pick
        break
    base[engine] = chosen
    return base


_SAMPLE_DIR = None


def sample_file(engine: str, voice: str, name: str = ""):
    """Synthesize (once, cached in a temp dir) a short audition line in
    `voice` → (path, line). Raises on synthesis failure. edge = free;
    Inworld = a paid TTS call (~1 credit). Used by the terminal picker
    (played via afplay) AND the browser UI (served over HTTP)."""
    import tempfile
    global _SAMPLE_DIR
    if _SAMPLE_DIR is None:
        _SAMPLE_DIR = Path(tempfile.mkdtemp(prefix="kalinga_voice_"))
    who = name or "your host"
    line = f"Hi, I'm {who} — and this is how every episode will sound."
    out = _SAMPLE_DIR / ("sample_" + "".join(
        c for c in (voice + who) if c.isalnum()) + ".mp3")
    if not out.exists():
        if engine != "edge":
            make_video.ensure_cli()
        make_video._synth_line(line, out, engine, voice)
    return out, line


def _sample_voice(engine: str, voice: str, name: str = "") -> None:
    """Synthesize a short line in `voice` and play it, so the picker can be
    auditioned."""
    import shutil
    import subprocess
    try:
        out, line = sample_file(engine, voice, name)
    except SystemExit:
        print(f"    ! could not synthesize a sample in {voice}")
        return
    except Exception as e:                           # noqa: BLE001
        print(f"    ! sample failed: {str(e)[:120]}")
        return
    print(f"    ♪ {voice}: “{line}”")
    play = out
    try:                                             # Inworld may be WAV-in-.mp3
        if out.read_bytes()[:4] == b"RIFF" and out.suffix != ".wav":
            play = out.with_suffix(".wav")
            play.write_bytes(out.read_bytes())
    except Exception:                                # noqa: BLE001
        pass
    af = shutil.which("afplay")
    if af:
        subprocess.run([af, str(play)])
    else:
        print(f"    (no afplay on this OS — open {play})")


def _make_avatar(name: str, member: dict, style: str, tpl: dict,
                 regen=None) -> bool:
    """Generate the character's reference portrait into cast/<name>.png.
    `regen`: None = ask interactively when one exists (the terminal wizard);
    True/False = non-interactive (the browser UI) — overwrite / keep."""
    ch = config.channel()
    ch.cast_dir.mkdir(parents=True, exist_ok=True)
    out = ch.cast_avatar(name)
    if out.exists():
        if regen is None:
            regen = _ask(f"  {out.name} exists — regenerate? [y/N]: "
                         ).lower() in ("y", "yes")
        if not regen:
            return True
        make_video.archive(ch.cast_dir, out.name)   # keep the old one in .versions/
    appearance = member.get("appearance") or ""
    personality = member.get("personality") or ""
    prompt = (f"Character reference portrait of {name}: {appearance}. "
              f"{personality}. {style} Head-and-shoulders portrait, facing "
              "camera, warm confident expression, clean simple studio "
              "background, consistent recognizable character design, no text, "
              "letters, numbers or logos anywhere in the image.")
    print(f"  generating avatar for {name} (~2 credits)…")
    ok = make_video.generate(
        tpl.get("image_model", config.DEFAULT_IMAGE_MODEL), prompt, out,
        (".png", ".jpg", ".webp"), ["--aspect_ratio", "9:16"],
        f"avatar {name}", alt_prompts=[f"{prompt} {make_video.SAFE_TAIL}"])
    if ok:
        member["avatar"] = f"cast/{out.name}"
        print(f"  avatar → {out}")
    else:
        print("  ! avatar generation failed (the character still works with a "
              "text appearance description)", file=sys.stderr)
    return ok


def _pick_outfits(name: str, member: dict) -> list:
    """Which outfits to (re)generate. Each outfit = 12 images ≈ 24 credits."""
    have = {r["outfit"] for r in (member.get("refs") or [])
            if isinstance(r, dict)}
    names, when = outfit_names(), _when_map()
    print("  outfits (each = 12 images ≈ 24 credits):")
    for i, o in enumerate(names):
        tag = "  ✓ done" if o in have else ""
        print(f"    [{i + 1}] {o} — {when.get(o, '')}{tag}")
    pick = _ask("  numbers (e.g. 2,3), [a]ll, Enter for any not yet done: ")
    if pick.lower() in ("a", "all"):
        return list(names)
    if not pick:
        todo = [o for o in names if o not in have]
        return todo or list(names)
    chosen = []
    for tok in pick.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(names):
            chosen.append(names[int(tok) - 1])
    return chosen or [names[0]]


def _edit(cast: dict, name: str, engine: str, style: str, tpl: dict) -> None:
    m = cast.get(name, {})
    print(f"\n  — {name} —")
    m["personality"] = _ask(
        f"  personality (Enter keeps: {m.get('personality', '') or '—'}): ",
        m.get("personality", ""))
    g = _ask(f"  voice gender male/female/neutral "
             f"(Enter keeps: {m.get('gender', 'neutral')}): ",
             m.get("gender", "neutral")).lower()
    m["gender"] = g if g in GENDER_VOICE else m.get("gender", "neutral")
    m["appearance"] = _ask(
        "  appearance for the avatar + on-screen look (Enter keeps: "
        f"{m.get('appearance', '') or '—'}): ", m.get("appearance", ""))
    m["voice"] = _pick_voice(engine, m["gender"], m.get("voice"), name)
    cast[name] = m
    if _ask("  generate / update the avatar now? [Y/n]: ").lower() not in (
            "n", "no"):
        make_video.ensure_cli()
        _make_avatar(name, m, style, tpl)
    if _ask("  build the reference library too — formal/casual/ethnic × "
            "(face ×9 + full-length ×3 each)? [y/N]: ").lower() in ("y", "yes"):
        make_video.ensure_cli()
        _make_reference_set(name, m, style, tpl, outfits=_pick_outfits(name, m))
    cast[name] = m


def main(args) -> int:
    ch = config.channel()
    engine, style, tpl = _engine_and_style()
    cast = _load()
    print(f"cast setup — {ch.name} ({ch.title})   engine: {engine}")
    while True:
        names = list(cast)
        print("\n  current cast:" if names else "\n  (no cast yet)")
        for i, n in enumerate(names):
            m = cast[n]
            v = m.get("voice")
            vshow = v.get(engine) if isinstance(v, dict) else v
            print(f"   {i}. {n}  [{vshow}]  {m.get('personality', '')[:50]}")
        c = _ask("\n  [a]dd / number to edit / [r]N reference library / "
                 "[d]N delete / [q]uit: ").strip()
        if c in ("q", ""):
            break
        if c.startswith("r") and c[1:].isdigit() and int(c[1:]) < len(names):
            n = names[int(c[1:])]
            make_video.ensure_cli()
            outfits = _pick_outfits(n, cast[n])
            regen = _ask(f"  regenerate (overwrite) {n}'s existing "
                         f"{'/'.join(outfits)} images? [y/N]: ").lower() in (
                         "y", "yes")
            _make_reference_set(n, cast[n], style, tpl, outfits=outfits,
                                regen=regen)
            _save(cast)
        elif c == "a":
            name = _ask("  name: ").strip()
            if not name:
                continue
            if name in cast and _ask(
                    f"  {name} exists — edit it? [Y/n]: ").lower() in ("n", "no"):
                continue
            _edit(cast, name, engine, style, tpl)
            _save(cast)
        elif c.startswith("d") and c[1:].isdigit() and int(c[1:]) < len(names):
            n = names[int(c[1:])]
            if _ask(f"  delete {n}? [y/N]: ").lower() in ("y", "yes"):
                cast.pop(n, None)
                _save(cast)
        elif c.isdigit() and int(c) < len(names):
            _edit(cast, names[int(c)], engine, style, tpl)
            _save(cast)
        else:
            print("  ?")
    print("done.")
    return 0


if __name__ == "__main__":
    config.set_channel(__import__("os").environ.get("KALINGA_CHANNEL"))
    sys.exit(main(None))
