"""
interactive.py — `kalinga.py make`: the interactive production session.

A UI layer over kalinga's existing stage functions. Every decision persists
as a regular pipeline artifact (script.json, template.json, overrides.json),
so [A]uto-from-here hands the rest to kalinga.run_to_done() — literally
ship's resume path, one code path, no drift — and a quit session resumes
with `kalinga.py make TOPIC`.

Uniform grammar at every checkpoint:
    [a]ccept  [r]etry  [e]dit/feedback  …stage keys…  [A]uto  [q]uit  [?]help
[a][r][e][A][q][?] are reserved globally and never reused per stage.

Design doc: docs/interactive-make.md.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import config
import daily
import kalinga
import make_video
import templates
import validate


# ---------- color (config.tint no-ops when piped / NO_COLOR) ----------
def _b(s):                   # bold — the thing to look at
    return config.tint(s, "1")


def _d(s):                   # dim — secondary detail, hints, costs
    return config.tint(s, "2")


def _cy(s):                  # cyan — structure: stage names, menu keys
    return config.tint(s, "1", "36")


def _g(s):                   # green — good / done / pass
    return config.tint(s, "1", "32")


def _y(s):                   # yellow — warnings, money
    return config.tint(s, "33")


def _r(s):                   # red — failures
    return config.tint(s, "1", "31")


_KEY_RE = re.compile(r"\[(Enter|[A-Za-z?#])\]")


def _menu(text: str) -> str:
    """Highlight the [k]ey tokens of a menu line."""
    return _KEY_RE.sub(lambda m: _cy(f"[{m.group(1)}]"), text)

CREDITS_PER_KEYFRAME = config.CREDITS_PER_KEYFRAME   # one home: config.py
CREDITS_PER_CLIP_SEC = config.CREDITS_PER_CLIP_SEC
STALE_HOURS = 24                # confirm before resuming an older run

ASSEMBLY = ("short.mp4", "nomusic.mp4", "subs.ass", "music.txt", "text*.png",
            "ai_review.json")

STAGE_ORDER = ["research", "concept", "script", "direction",
               "voice", "keyframes", "clips", "music", "assemble", "thumbnail",
               "seo", "gates", "wrap"]


class Quit(Exception):
    """User quit — artifacts kept, session resumable."""


class Auto(Exception):
    """User pressed [A] — hand the rest to kalinga.run_to_done()."""


class Session:
    def __init__(self, topic, folder, tpl, in_queue):
        self.topic = topic
        self.folder = folder
        self.tpl = tpl
        self.in_queue = in_queue
        self.chat = None                       # script co-writing llm.Chat
        self.dchat = None                      # visual-design llm.Chat
        self.result = None                     # last validate.run() output
        self.credits_start = None
        self.iterations = {}                   # stage -> retry/edit count
        self.learned = []                      # every critique this session

    def bump(self, stage):
        self.iterations[stage] = self.iterations.get(stage, 0) + 1


# ---------- input ----------
_warned_tty = False


def ask(prompt: str, choices: dict, globals_on: bool = True,
        free: str = None) -> str:
    """Validated menu reader. choices: token -> help line; the token '#'
    accepts any number (returned as the digit string). [?] prints help,
    invalid input reprints, EOFError quits. With globals_on, [A] raises
    Auto and [q] raises Quit so stage code never handles them.

    free: when set (a hint like "feedback for the writer"), anything typed
    that isn't a menu key is treated as that — collected multi-line (empty
    line sends) and returned verbatim. Menu keys are single chars, so a
    multi-char return IS the freeform text. Enter alone = [a]ccept when
    accept is on the menu."""
    global _warned_tty
    if not sys.stdin.isatty() and not _warned_tty:
        print("  ! stdin is not a TTY — reading choices from piped input")
        _warned_tty = True
    suffix = "  [A]uto  [q]uit  [?]" if globals_on else "  [?]"
    if free:
        suffix = "  — or just type ({})".format(free) + suffix
    line = "\n" + _menu(prompt.rstrip()) + _menu(suffix) + " "
    while True:
        try:
            raw = input(line).strip()
        except EOFError:
            raise Quit()
        if raw == "" and "a" in choices:
            return "a"
        if globals_on and raw == "A":
            raise Auto()
        if globals_on and raw == "q":
            raise Quit()
        if raw == "?":
            for k, desc in choices.items():
                print("      " + _cy(f"[{'number' if k == '#' else k}]")
                      + f" {desc}")
            if free:
                print(_d(f"      …or type anything else — it's taken as "
                         f"{free} (multi-line; empty line sends)"))
            if "a" in choices:
                print("      " + _cy("[Enter]") + " = accept")
            if globals_on:
                print("      " + _cy("[A]") + " auto mode — finish "
                      "everything headless\n      " + _cy("[q]")
                      + " quit — artifacts kept, session resumable")
            continue
        if raw in choices:
            return raw
        if "#" in choices and raw.isdigit():
            return raw
        if free and len(raw) > 1:
            return _compose(raw)
        keys = ", ".join(("number" if k == "#" else k) for k in choices)
        print(f"    invalid — expected one of: {keys}, ?"
              + (", A, q" if globals_on else ""))


def _compose(first: str = "") -> str:
    """Multi-line input: keep typing (or paste a block); empty line sends."""
    lines = [first] if first else []
    if sys.stdin.isatty():
        print("    … multi-line — empty line sends")
    while True:
        try:
            ln = input()
        except EOFError:
            break
        if not ln.strip():
            break
        lines.append(ln)
    return "\n".join(lines).strip()


def ask_text(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_long(prompt: str) -> str:
    """One-line or pasted multi-line free text; empty line sends."""
    first = ask_text(prompt)
    if not first:
        return ""
    return _compose(first)


# ---------- macOS helpers ----------
def _open(*paths):
    # stdin=DEVNULL: external viewers must never swallow the session's stdin
    # (the documented gotcha — otherwise the next prompt hangs)
    if shutil.which("open"):
        subprocess.run(["open"] + [str(p) for p in paths],
                       stdin=subprocess.DEVNULL)
    else:
        print("  view: " + " ".join(str(p) for p in paths))


def _true_audio_ext(path: Path) -> str:
    """The container the bytes actually are. Higgsfield TTS downloads can
    be WAV data named segN.mp3 — afplay trusts the extension and dies with
    AudioFileOpen 'dta?', so playback must sniff the magic bytes."""
    try:
        head = path.open("rb").read(12)
    except OSError:
        return path.suffix
    if head[:4] == b"RIFF":
        return ".wav"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3",
                                          b"\xff\xf2"):
        return ".mp3"
    return path.suffix


def _play(path):
    path = Path(path)
    if not shutil.which("afplay"):
        _open(path)
        return
    print("  " + _d(f"▶ {path.name}"))
    target = path
    ext = _true_audio_ext(path)
    if ext != path.suffix:      # extension lies — alias it for afplay
        target = Path(tempfile.gettempdir()) / f"kalinga-{path.stem}{ext}"
        target.unlink(missing_ok=True)
        target.symlink_to(path.resolve())
    if subprocess.run(["afplay", str(target)],
                      stdin=subprocess.DEVNULL).returncode != 0:
        print(_y(f"  ! afplay couldn't read {path.name} — opening instead"))
        _open(path)


_music_proc = None


def _preview_music(track: Path):
    """Play a music track in the BACKGROUND so the menu stays responsive —
    unlike _play (which blocks). A new preview replaces the previous one."""
    global _music_proc
    _stop_music()
    if not shutil.which("afplay"):
        print(_y("    (no afplay on this OS — open the file to listen)"))
        return
    _music_proc = subprocess.Popen(["afplay", str(track)],
                                   stdin=subprocess.DEVNULL)
    print("    " + _d(f"♪ {track.name} — playing in the background; "
                      "pick a number, [p]N another, or [k]eep"))


def _stop_music():
    global _music_proc
    if _music_proc and _music_proc.poll() is None:
        _music_proc.terminate()
    _music_proc = None


def _notify(text: str):
    if shutil.which("osascript"):
        subprocess.run(["osascript", "-e",
                        f'display notification "{text}" with title "kalinga"'],
                       capture_output=True, stdin=subprocess.DEVNULL)


def _editor(path: Path) -> bool:
    """$EDITOR on a JSON artifact; loops until it parses (or reverts).
    Returns True when the file changed and parses."""
    if not sys.stdin.isatty():
        print(f"  ! $EDITOR needs a terminal — edit {path} yourself and "
              f"rerun")
        return False
    ed = os.environ.get("EDITOR", "vi")
    backup = path.read_text()
    while True:
        subprocess.call([ed, str(path)])
        try:
            json.loads(path.read_text())
            return path.read_text() != backup
        except ValueError as e:
            print(f"  ! {path.name} no longer parses: {e}")
            try:
                c = ask(f"  [e]dit {path.name} again / [u]ndo my edits:",
                        {"e": "reopen the editor", "u": "revert the file"},
                        globals_on=False)
            except Quit:
                path.write_text(backup)
                raise
            if c == "u":
                path.write_text(backup)
                return False


# ---------- artifacts ----------
def _read_json(folder: Path, name: str):
    p = folder / name
    return json.loads(p.read_text()) if p.exists() else None


def _write_json(folder: Path, name: str, obj):
    (folder / name).write_text(json.dumps(obj, indent=2))


def _pin_template(folder: Path, **kv) -> dict:
    """Persist a choice (voice, tts_engine, …) into the pinned
    template.json so headless resume honors it (templates.load_pinned)."""
    tpl = json.loads((folder / "template.json").read_text())
    tpl.update(kv)
    _write_json(folder, "template.json", tpl)
    return tpl


# run-level pins the creator set THIS session — carried over when the whole
# template (the visual WORLD/models) is switched, so a swap doesn't lose them
_SESSION_PINS = ("voice", "tts_engine", "audio_speed", "target_words",
                 "duration_range", "score_hook_first", "music",
                 "clip_fill", "caption_style")


def _switch_template(folder: Path, name: str, regen_visuals: bool) -> dict:
    """Swap the WHOLE template (visual world, models, motion) mid-run and re-pin
    template.json. The new template's look/models apply; the creator's run-level
    pins (voice/engine/speed/length/…) carry over (`_SESSION_PINS`). When
    regen_visuals, the frames made in the OLD look are archived (never deleted)
    so keyframes/clips/thumbnail/cut regenerate in the new template; otherwise
    the existing art is kept (the look may be mixed until you regen). Returns the
    new template dict."""
    old = {}
    p = folder / "template.json"
    if p.exists():
        old = json.loads(p.read_text())
    new = templates.load(name)
    new["name"] = name
    for k in _SESSION_PINS:                       # keep this session's choices
        if k in old:
            new[k] = old[k]
    if regen_visuals:
        # the look lives in the DIRECTOR's work, not just the template: the
        # keyframe/thumbnail prompts are the per-shot `visual` briefs + the
        # `style` token, BOTH authored under the old template's world. So drop the
        # `directed` flag (and the stale style/thumbnail) → the director RE-RUNS
        # under the new template, rewriting the visuals + style in the new world;
        # then the old-look frames are archived to regenerate. The spoken script
        # stays (the facts/verdict lock holds); a concept, if set, still steers
        # it. Caller re-runs the director (a_template_switch as a job).
        sp = folder / "script.json"
        if sp.exists():
            try:
                sc = json.loads(sp.read_text())
                sc.pop("directed", None)
                d = sc.get("direction") or {}
                d.pop("style", None)
                d.pop("thumbnail", None)
                sc["direction"] = d
                _write_json(folder, "script.json", sc)
            except (OSError, ValueError):
                pass
        _unlink(folder, ["key*.png", "clip*.mp4", "chart*.png",
                         "thumb_bg.png", "thumbnail.png"]
                + list(ASSEMBLY) + ["virality.md", "report.md", "critique.md"])
    _write_json(folder, "template.json", new)
    return new


def _choose_template(default: str) -> str:
    """At the start of a NEW run, when the channel defines MORE THAN ONE
    template (visual world), ASK which one to use — default = the channel's
    `default_template`. A single-template channel or a non-TTY run skips the
    prompt and takes the default silently (headless stays unattended)."""
    names = templates.available()
    if len(names) <= 1 or not sys.stdin.isatty():
        return default or (names[0] if names else "")
    # default first, then the rest
    ordered = ([default] if default in names else []) + [
        n for n in names if n != default]
    print(_cy("\n  This channel has several templates (visual worlds):"))
    for idx, n in enumerate(ordered, 1):
        try:
            world = (templates.load(n).get("world") or "").replace("\n", " ")
        except Exception:
            world = ""
        tag = _g(" · default") if n == default else ""
        snippet = ("  " + _d("— " + world[:64].strip())) if world else ""
        print(f"    {_cy(f'[{idx}]')} {n}{tag}{snippet}")
    choices = {"#": "pick a template by number",
               "a": f"use the default ({default or ordered[0]})"}
    try:
        sel = ask("  which template for this video?", choices, globals_on=False)
    except Quit:
        return default or ordered[0]
    if sel != "a":
        i = int(sel) - 1
        if 0 <= i < len(ordered):
            print(_g(f"  → {ordered[i]}"))
            return ordered[i]
    return default or ordered[0]


def _unlink(folder: Path, patterns) -> None:
    """Supersede artifacts WITHOUT deleting: each matched file is archived to
    .versions/ (indexed in versions.json, restorable) and its canonical name
    freed so the stage regenerates a fresh one. Nothing is ever lost."""
    names = sorted({p.name for g in patterns for p in config.art_glob(folder, g)
                    if p.is_file()})
    gone = make_video.archive(folder, *names)
    if gone:
        print(f"  archived (kept in .versions/): {', '.join(gone)}")


def _invalidate(folder: Path, text_idx=None, voice=False, visual_idx=None,
                motion_idx=None, keyframe_idx=None, music=False,
                section_idx=None):
    """THE invalidation matrix — what is superseded when a decision changes.
    Superseded artifacts are ARCHIVED to .versions/ (never deleted; indexed in
    versions.json and restorable via make_video.restore) and their canonical
    names freed so the stage regenerates fresh ones:

      text_idx=N     segN.mp3, assembly set, virality.md
      section_idx=N  segN.mp3, textN.png, assembly set — used when the script
                     of that segment changed (only that section)

    A SCRIPT REWRITE KEEPS THE PAID IMAGES (owner call 2026-07-02): keyN.png /
    clipN.mp4 are NOT archived on a text change — instead the UIs show a ⭐
    on any shot whose CURRENT prompt no longer matches the one it was
    generated from (make_video.keyframe_stale/clip_stale vs genmeta.json),
    and the creator regenerates only the shots that actually need it. A pure
    wording rewrite rarely changes a keyframe prompt (visual/overlay-driven),
    so most images never star; a clip_voice clip stars on a line change (the
    spoken words are in its prompt). The clip's duration drift vs the new
    voice is absorbed at assembly (trim / clip_fill).

    audio.json is NOT archived on a per-segment invalidation: it is the
    derived manifest step_voice rewrites on every run, and it carries every
    OTHER beat's per-unit cache keys (section `say`s, word timings, dialogue
    line files) — killing it made ONE changed segment re-voice EVERY
    sectioned beat and drop the unchanged beats' karaoke timings. The
    changed segment still regenerates because its audio FILES are archived
    (presence-cache miss) and its manifest text no longer matches the script
    (step_voice's text-drift gate). voice=True (a global engine/voice
    change) still archives it.
      voice=True     seg*.mp3, audio.json, assembly set, virality.md
      visual_idx=N   keyN.png, clipN.mp4 (start image), assembly, virality
      keyframe_idx=N keyN.png, clipN.mp4, assembly set, virality.md
      motion_idx=N   clipN.mp4, assembly set, virality.md
      music=True     assembly set only (re-assemble is free/local)
      always         report.md; critique.md whenever the video died

    assembly set = short.mp4, nomusic.mp4, subs.ass, music.txt."""
    # the *_*.png / *_*.mp4 globs also catch a multi-shot beat's sub-shots
    # (key<N>_<k>.png, clip<N>_<k>.mp4) so a changed segment regenerates all of
    # its stitched shots, not just sub-shot 0.
    doomed, video_dies, scores_die = [], False, False
    if text_idx is not None:
        # seg<i>_s*.mp3 = a SECTION-NATIVE beat's per-section voiceover (the norm
        # now) — without it a text edit leaves the old section audio cached and
        # the change never reaches the video. audio.json survives (see above);
        # images survive too (⭐ staleness instead — see above).
        doomed += [f"seg{text_idx}.mp3", f"seg{text_idx}_s*.mp3"]
        video_dies = scores_die = True
    if section_idx is not None:
        doomed += [f"seg{section_idx}.mp3", f"seg{section_idx}_s*.mp3",
                   f"text{section_idx}.png"]
        video_dies = scores_die = True
    if voice:
        doomed += ["seg*.mp3", "audio.json"]
        video_dies = scores_die = True
    if visual_idx is not None:
        doomed += [f"key{visual_idx}.png", f"key{visual_idx}_*.png",
                   f"clip{visual_idx}.mp4", f"clip{visual_idx}_*.mp4"]
        video_dies = scores_die = True
    if keyframe_idx is not None:
        doomed += [f"key{keyframe_idx}.png", f"key{keyframe_idx}_*.png",
                   f"clip{keyframe_idx}.mp4", f"clip{keyframe_idx}_*.mp4"]
        video_dies = scores_die = True
    if motion_idx is not None:
        doomed += [f"clip{motion_idx}.mp4", f"clip{motion_idx}_*.mp4"]
        video_dies = scores_die = True
    if music:
        video_dies = True
    if video_dies:
        doomed += list(ASSEMBLY) + ["critique.md"]
    if scores_die:
        doomed += ["virality.md"]
    doomed += ["report.md"]
    _unlink(folder, doomed)


def _n_segments(folder: Path):
    script = _read_json(folder, "script.json")
    return len(script["segments"]) if script else None


def _count(folder: Path, pattern: str) -> int:
    return len(config.art_glob(folder, pattern))


def _done_map(folder: Path) -> dict:
    """stage name -> done? keyframes/clips need one artifact PER segment
    (per-index caching means partial sets exist); wrap is never 'done'."""
    n = _n_segments(folder)
    sess = _read_json(folder, "session.json") or {}
    segs = (_read_json(folder, "script.json") or {}).get("segments") or []
    tpl = (templates.load_pinned(folder)
           if (folder / "template.json").exists() else {})
    # the EXACT keyframe/clip files this script's shot plan should produce — a
    # segment split into several distinct shots (multi-shot) yields key<i>_<k>/
    # clip<i>_<k> too, and only the shots that animate (gated by the per-segment
    # cap) get a clip. Using the shared plan keeps caching honest for sub-shots.
    exp_keys = [config.art(folder, nm)
                for nm in make_video.expected_keyframe_names(segs)]
    exp_clips = [config.art(folder, nm)
                 for nm in make_video.expected_clip_names(segs, tpl)]
    keys_done = bool(segs) and n is not None and all(p.exists() for p in exp_keys)
    clips_done = ((folder / "short.mp4").exists()
                  or (bool(segs) and all(p.exists() for p in exp_clips)))
    return {
        "research": (folder / "facts.json").exists(),
        # done once the creator has visited it (accepted/skipped → concept.json
        # written) or the script already exists (too late to matter)
        "concept": (folder / "concept.json").exists()
                   or (folder / "script.json").exists(),
        "script": (folder / "script.json").exists(),
        # directed once the shot plan exists (or we're already past it)
        "direction": bool((_read_json(folder, "script.json") or {})
                          .get("directed"))
                     or (folder / "audio.json").exists(),
        "voice": (folder / "audio.json").exists() and n is not None
                 and _count(folder, "seg*.mp3") >= n,
        "keyframes": keys_done,
        "clips": clips_done,
        "music": (folder / "short.mp4").exists(),
        "assemble": (folder / "short.mp4").exists(),
        "thumbnail": (folder / "thumbnail.png").exists(),
        "seo": (folder / "seo.md").exists(),
        "gates": (folder / "virality.md").exists(),
        "wrap": False,
    }


def first_incomplete(folder: Path) -> int:
    done = _done_map(folder)
    for i, name in enumerate(STAGE_ORDER):
        if not done[name]:
            return i
    return len(STAGE_ORDER) - 1


def _spend_line(s: Session):
    now = kalinga.hf_credits()
    if s.credits_start is not None and now is not None:
        print("  " + _y(f"credits: {now} left")
              + _d(f" · session spent {s.credits_start - now}"
                   f" · template budget {s.tpl['budget_credits']}"))


def _stage_header(s: Session, idx: int):
    name = STAGE_ORDER[idx]
    done = "".join(_g("●") if i < idx else
                   _cy("●") if i == idx else _d("·")
                   for i in range(len(STAGE_ORDER)))
    print(f"\n\n  {done}  " + _cy(name.upper())
          + _d(f"  ({idx + 1}/{len(STAGE_ORDER)})"))
    print("  " + _d("─" * 56))
    _spend_line(s)


# ---------- stages ----------
def _show_facts(folder: Path):
    ch = config.channel()
    d = _read_json(folder, "facts.json")
    print()
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v)[:90] + "…"
        if k == "verdict":
            v = _g(v) if v == ch.positive_verdict else _r(v)
        print("  " + _d(f"{k:>14}:") + f" {v}")


def st_research(s: Session):
    kalinga.run_research(s.topic)
    while True:
        _show_facts(s.folder)
        c = ask("  [a]ccept facts / [r]e-run research / [e]dit in $EDITOR:",
                {"a": "facts are right — continue",
                 "r": "delete facts.json and research again",
                 "e": "open facts.json in your editor"})
        if c == "a":
            return
        s.bump("research")
        if c == "r":
            _unlink(s.folder, ["facts.json"])
            kalinga.run_research(s.topic)
        elif c == "e":
            _editor(s.folder / "facts.json")


# words spoken per second of FINAL video (after the template speed-up) — the
# 300-500 words ≈ 115-190s relation in the docs (≈2.6 wps)
_WPS = 2.6


def _parse_length(a: str):
    """Parse a length spec → (target_words tuple, duration_range tuple|None).
    Accepts words ("200", "180-240") or time ("45s", "1m", "1m30s", "90s").
    Time is converted to words at ~2.6 wps and also yields a matching QC
    duration_range so the tech gate doesn't fail a deliberately short/long cut.
    Returns (None, None) if it can't parse."""
    import re
    a = (a or "").strip().lower().replace(" ", "")
    if not a:
        return None, None
    # time forms: 1m30s / 1m / 45s / 90s
    tm = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s?)?", a)
    if ("m" in a or "s" in a) and tm and (tm.group(1) or tm.group(2)):
        total = int(tm.group(1) or 0) * 60 + int(tm.group(2) or 0)
        if total <= 0:
            return None, None
        mid = round(total * _WPS)
        tw = (max(round(mid * 0.85), 20), round(mid * 1.15))
        dr = (max(int(total * 0.6), 8), max(int(total * 1.6), total + 15))
        return tw, dr
    # word forms: 180-240 or 200
    if "-" in a:
        lo, _, hi = a.partition("-")
        if lo.isdigit() and hi.isdigit() and int(lo) < int(hi):
            return (int(lo), int(hi)), None
        return None, None
    if a.isdigit():
        n = int(a)
        if n < 10:
            return None, None
        return (round(n * 0.9), round(n * 1.1)), None
    return None, None


def _save_concept(s: Session, text: str) -> None:
    """Persist the creator's concept; if a script already exists (jumped back),
    rewrite it + everything downstream so the new concept actually takes."""
    text = (text or "").strip()
    had = _read_json(s.folder, "concept.json") or {}
    _write_json(s.folder, "concept.json", {"concept": text, "set": True})
    daily.set_concept(s.topic, text)         # track it in the queue CSV (NOT
    # learnings — a per-video concept is not a durable learning)
    if (had.get("concept") or "") == text:
        return
    if (s.folder / "script.json").exists():
        # the concept drives the script + the whole look — regenerate from the
        # script down (this discards generated keyframes/clips for this run)
        if ask("  a script already exists — rewrite it (and regenerate the "
               "visuals) for the new concept? [y]es / [n]o keep current:",
               {"y": "rewrite the script + downstream for the new concept",
                "n": "keep the existing script (concept saved for next time)"},
               globals_on=False) == "y":
            _unlink(s.folder, ["script.json", "audio.json", "seg*.mp3",
                               "key*.png", "clip*.mp4", "chart*.png",
                               "text*.png", "seo.json", "seo.md", "virality.md",
                               "thumbnail.png", "thumb_bg.png", "ai_review.json",
                               "report.md", "critique.md"] + list(ASSEMBLY))
            print(_y("  script + visuals cleared — they'll rebuild with the "
                     "concept"))


def _refine_concept_loop(s: Session, cur: str) -> bool:
    """Co-write the concept WITH the AI: the creator gives rough primers/notes,
    the AI writes a refined concept; the creator accepts, gives MORE notes to
    iterate (built on the last draft), edits by hand, or backs out. Returns True
    when a concept was saved (caller returns), False to go back to the menu."""
    import textwrap
    import creative
    print(_d("  give the AI your rough idea — a setting, a gag, a motif, even "
             "half a sentence. It'll write a polished, producible concept; then "
             "you can keep refining with more notes."))
    primers = ask_long("  your notes / rough idea (empty line sends, blank "
                       "cancels): ")
    if not primers.strip():
        return False
    draft = ""
    while True:
        print(_d("  the AI is refining your idea…"))
        out = creative.refine_concept(s.topic, primers, prior=draft)
        if not out:
            print(_y("  (no LLM backend, or nothing to work with — paste the "
                     "concept yourself with [e])"))
            return False
        draft = out
        print(_g("  refined concept:"))
        for ln in textwrap.wrap(draft, 72):
            print("    " + ln)
        cc = ask("  [a]ccept / [m]ore notes (iterate) / [e]dit by hand / "
                 "[c]ancel:",
                 {"a": "use this concept",
                  "m": "give more notes — the AI revises THIS draft",
                  "e": "tweak the text yourself",
                  "c": "back to the concept menu"},
                 globals_on=False)
        if cc == "a":
            s.bump("concept")
            _save_concept(s, draft)
            print(_g("  concept saved — the script & director build on it"))
            return True
        if cc == "m":
            more = ask_long("  what to change / add (empty line sends): ")
            if more.strip():
                primers = more            # revise THIS draft (prior=draft)
            continue
        if cc == "e":
            edited = ask_long("  edit the concept (empty line sends): ")
            if edited.strip():
                s.bump("concept")
                _save_concept(s, edited)
                print(_g("  concept saved"))
                return True
            continue
        return False                      # [c]ancel


def st_concept(s: Session):
    """The creator's CONCEPT — the hook idea + theme/motif this video runs on.
    It feeds the script writer (the opening beat) and the director (the bespoke
    world + anchor motif). Optional: skip for the default treatment."""
    import textwrap
    import creative
    cur = creative.user_concept(s.folder)
    rdir = s.folder / "concept"
    rdir.mkdir(exist_ok=True)                # so refs have a place to be dropped
    print(_cy("\n  concept")
          + _d(" — your hook idea + recurring theme/motif for this video"))
    print(_d("  Drives the OPENING beat and the whole look: the writer builds "
             "the hook around it, the director builds the world + motif."))
    print(_d("  e.g. “A late-night workshop skit — someone bursts in demanding "
             "the answer mid-broadcast; the workbench is the recurring "
             "motif.”  ·  [g] = let the AI suggest one."))
    done = daily.concepts(exclude=s.topic)
    if done:
        print(_d(f"  already used ({len(done)}): "
                 + "; ".join(f"{t} — {c[:40]}" for t, c in done[-4:])))
    r_imgs, r_vids = creative._reference_files(s.folder)
    if r_imgs or r_vids:
        rec = creative.extract_reference_brief(s.folder)        # cached
        rb = (rec or {}).get("brief") or {}
        if rb.get("summary"):
            print(_cy("  references: ") + _d(f"{len(r_imgs)} image(s) → ")
                  + rb["summary"][:78])
        else:
            print(_d(f"  {len(r_imgs)} reference image(s) in concept/ "
                     "— press [i] to analyse"))
    else:
        print(_d("  tip: drop reference images into the concept/ folder + press "
                 "[i] — the AI extracts a LOOK that steers the concept + the "
                 "video director"))
    if cur:
        print(_g("  current concept:"))
        for ln in textwrap.wrap(cur, 72):
            print("    " + ln)
    while True:
        lo, hi = s.tpl.get("target_words", [300, 500])
        c = ask("  [a]ccept / [e]dit (paste your own) / [r]efine with AI "
                "(give notes → AI writes it) / [g] AI-suggest a fresh one / "
                "[l] copy a TikTok/IG video link / [w] set length / "
                "[i] use dropped references / [s]kip:",
                {"a": ("keep this concept" if cur else "skip — no concept"),
                 "e": "write / paste the concept yourself (multi-line)",
                 "r": "give the AI your rough idea/primers and it writes a "
                      "refined version — iterate until it's right",
                 "g": "let the AI suggest a concept from scratch (reads what's "
                      "been done, won't repeat it)",
                 "l": "paste a TikTok/Instagram video LINK to copy its visual "
                      "concept (you say WHAT to pull) → steers the directors",
                 "w": f"set the video LENGTH for this one (now {lo}-{hi} words "
                      f"≈ {int(lo / _WPS)}-{int(hi / _WPS)}s) — words or time",
                 "i": "analyse reference images in concept/ → a look that steers "
                      "the concept + directors",
                 "s": "no concept — default hook & theme"},
                free="type your concept directly (hook idea + motif)")
        if c == "a":
            return
        if c == "w":
            lo, hi = s.tpl.get("target_words", [300, 500])
            print(_d(f"  current: {lo}-{hi} words ≈ {int(lo / _WPS)}-"
                     f"{int(hi / _WPS)}s of final video"))
            a = ask_text("  length for THIS video — words (e.g. 200 or 180-240) "
                         "or time (e.g. 45s, 1m30s). Enter = keep default: ")
            if not a.strip():
                print(_d("  keeping the default length"))
                continue
            tw, dr = _parse_length(a)
            if not tw:
                print(_y("  couldn't read that — try '200', '180-240', '45s' or "
                         "'1m30s'"))
                continue
            kv = {"target_words": list(tw)}
            if dr:
                kv["duration_range"] = list(dr)
            s.tpl = _pin_template(s.folder, **kv)
            msg = (f"  length pinned: {tw[0]}-{tw[1]} words "
                   f"≈ {int(tw[0] / _WPS)}-{int(tw[1] / _WPS)}s")
            if dr:
                msg += f"  (QC gate {dr[0]}-{dr[1]}s)"
            print(_g(msg))
            if (s.folder / "script.json").exists():
                print(_d("  → rewrite the script ([r]edo script) for the new "
                         "length to take effect"))
            continue
        if c == "l":
            url = ask_text("  paste the TikTok/Instagram video URL "
                           "(Enter to cancel): ").strip()
            if not url:
                continue
            iv = ask_text("  how often to sample a frame? seconds between frames "
                          "(Enter = 10; lower = more frames = finer read of the "
                          "editing): ").strip()
            interval = None
            if iv:
                try:
                    interval = max(1.0, float(iv))
                except ValueError:
                    print(_y("  not a number — using 10s"))
            focus = ask_long("  what do you want to PULL from it? (e.g. “the "
                             "fast jump-cut editing + bold center captions”, or "
                             "“the moody neon color grade”) — Enter = the overall "
                             "look:\n")
            print(_d("  pulling frames + caption…"))
            rec = creative.reference_from_url(s.folder, url, focus=focus,
                                              interval=interval)
            rb = (rec or {}).get("brief") or {}
            if not rb:
                print(_y("  couldn't pull anything (no LLM backend, or the link "
                         "is private/unreachable). Try yt-dlp, or screenshot the "
                         "video into concept/ + press [i]."))
                continue
            print(_g(f"  reference brief from the link:"))
            if rb.get("summary"):
                for ln in textwrap.wrap(rb["summary"], 72):
                    print("    " + ln)
            for k in ("palette", "mood", "composition", "motifs", "recreate"):
                v = rb.get(k)
                if not v:
                    continue
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                print(_d(f"    {k}: {v[:76]}"))
            print(_d("  → now steers [g]/[r] + the directors. Use [g] or [r] to "
                     "build a concept rooted in this reference."))
            continue
        if c == "i":
            imgs, vids = creative._reference_files(s.folder)
            if not imgs and not vids:
                print(_y(f"  drop reference images into {rdir}/ first, "
                         "then press [i]"))
                continue
            print(_d("  reading the references…"))
            rec = creative.extract_reference_brief(s.folder, regen=True)
            rb = (rec or {}).get("brief") or {}
            if not rb:
                print(_y("  no brief (no LLM backend?)"))
                continue
            print(_g(f"  reference brief ({len(rec.get('sources') or [])} image(s)):"))
            if rb.get("summary"):
                for ln in textwrap.wrap(rb["summary"], 72):
                    print("    " + ln)
            for k in ("palette", "mood", "composition", "motifs", "recreate"):
                v = rb.get(k)
                if not v:
                    continue
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                print(_d(f"    {k}: {v[:76]}"))
            print(_d("  → now steers [g]/[r] + the directors. Use [g] or [r] to "
                     "build a concept rooted in this look."))
            continue
        if c == "s":
            _save_concept(s, "")
            print(_d("  no concept — the writer/director choose freely"))
            return
        if c == "r":
            if _refine_concept_loop(s, cur):
                return
            cur = creative.user_concept(s.folder)   # may have been saved
            continue
        if c == "g":
            print(_d("  the AI is dreaming up a fresh concept (not repeating "
                     "past videos)…"))
            sug = creative.suggest_concept(s.topic)
            if not sug:
                print(_y("  (no LLM backend — write your own below)"))
                continue
            print(_g("  suggested:"))
            for ln in textwrap.wrap(sug, 72):
                print("    " + ln)
            cc = ask("  [a]ccept / [e]dit it / [g] another / [c]ancel:",
                     {"a": "use this concept", "e": "tweak it first",
                      "g": "suggest a different one", "c": "back"},
                     globals_on=False)
            if cc == "a":
                s.bump("concept")
                _save_concept(s, sug)
                cur = sug
                print(_g("  concept saved — the script & director build on it"))
                return
            if cc == "e":
                edited = ask_long("  edit the concept (empty line sends): ")
                if edited.strip():
                    s.bump("concept")
                    _save_concept(s, edited)
                    cur = edited.strip()
                    print(_g("  concept saved"))
                    return
            continue        # [g] another / [c]ancel → back to the menu
        # [e] or typed prose IS the concept
        text = c if len(c) > 1 else ask_long(
            "  your concept — the hook idea + the theme/motif to carry through "
            "(empty line sends, '-' clears): ")
        if text.strip() == "-":
            _save_concept(s, "")
            print(_d("  concept cleared"))
            return
        if text.strip():
            s.bump("concept")
            _save_concept(s, text)
            cur = text.strip()
            print(_g("  concept saved — the script & director will build on it"))


def _show_script(folder: Path):
    script = _read_json(folder, "script.json")
    kalinga._print_script(script)
    j = script.get("judge", {})
    if j.get("pass"):
        sc = j.get("score")
        print(f"  judge: ✓ good enough"
              + (f" ({sc}/10)" if sc is not None else "")
              + (f" — {j['reason']}" if j.get("reason") else ""))
    else:
        if j.get("score") is not None:
            print(f"  judge: {j['score']}/10")
        if j.get("reason"):
            print(f"  judge: {j['reason']}")
        if j.get("improve"):
            print(f"  judge's hint: {j['improve']}")
    return script


def _judge_feedback(script: dict):
    """The script judge's OWN critique, framed as revision notes — so the writer
    can act on the bar it'll be re-scored against. None when the script already
    PASSED (good enough — nothing to nag) or the judge had no actionable hint."""
    j = script.get("judge", {}) or {}
    if j.get("pass"):
        return None                       # good enough — no revision to apply
    hint, reason = j.get("improve"), j.get("reason")
    if not (hint or reason):
        return None
    score = j.get("score")
    parts = [f"The script judge scored this {score}/10." if score is not None
             else "The script judge flagged a weakness."]
    if reason:
        parts.append(f"Its critique: {reason}")
    if hint:
        parts.append(f"Specifically improve: {hint}")
    parts.append("Rewrite to address this — keep everything else, same labels, "
                 "same order, facts unchanged.")
    return " ".join(parts)


def st_script(s: Session):
    import creative
    script0 = _read_json(s.folder, "script.json") or {}
    hist0 = len(script0.get("draft_history", []))
    kalinga.write_script(s.topic)
    while True:
        script = _show_script(s.folder)
        judge_fb = _judge_feedback(script)
        prior = make_video.list_versions(s.folder).get("script.json", [])
        opts = {"a": "lock this script in",
                "r": "discard and regenerate from scratch (best-of-N)",
                "e": "compose revision notes — the writer remembers every "
                     "round",
                "o": "hand-edit script.json (re-judged afterwards)"}
        menu = ("  [a]ccept script / [r]e-roll fresh / [e] feedback / "
                "[o]pen in $EDITOR")
        if judge_fb:
            opts["j"] = ("revise using the hook judge's OWN critique "
                         "(auto-applies its hint, then re-scores)")
            menu = ("  [a]ccept / [j] apply the judge's hint / [r]e-roll / "
                    "[e] feedback / [o]pen in $EDITOR")
        if prior:
            opts["b"] = (f"roll back to the previous script "
                         f"({len(prior)} kept in .versions/)")
            menu += " / [b]ack to previous"
        c = ask(menu + ":", opts, free="revision notes")
        if c == "a":
            break
        if c == "b" and prior:
            print(_cy("  previous script drafts:"))
            for i, v in enumerate(prior, 1):
                try:
                    old = json.loads((s.folder / v["file"]).read_text())
                    hk = old["segments"][0]["text"][:54]
                    sj = old.get("judge", {}).get("score")
                except Exception:
                    hk, sj = "?", None
                print(f"   {i}. {_d(v['ts'])}"
                      + (_d(f"  hook {sj}/10") if sj is not None else "")
                      + f"  “{hk}…”")
            pick = ask_text(f"  restore which? [1-{len(prior)}, "
                            f"Enter = most recent ({len(prior)})]: ")
            idx = ((int(pick) if pick.isdigit()
                    and 1 <= int(pick) <= len(prior) else len(prior)) - 1)
            s.bump("script")
            make_video.restore(s.folder, prior[idx]["file"])
            s.chat = None
            print(_g("  restored that draft (the current one is kept in "
                     ".versions/ too)"))
            continue
        if c == "j" and judge_fb:
            s.bump("script")
            if s.chat is None:
                s.chat = creative.make_chat(s.topic)
            print(_d("  revising on the judge's own critique…"))
            try:
                creative.revise_script(s.topic, judge_fb, s.chat)
            except (RuntimeError, ValueError) as e:
                print(f"  ! revision failed: {e}")
            continue
        if c == "e" or len(c) > 1:
            fb = c if len(c) > 1 else ask_long("  revision notes: ")
            if not fb:
                continue
            s.bump("script")
            if s.chat is None:
                s.chat = creative.make_chat(s.topic)
            try:
                creative.revise_script(s.topic, fb, s.chat)
            except (RuntimeError, ValueError) as e:
                print(f"  ! revision failed: {e}")
            continue
        s.bump("script")
        if c == "r":
            _unlink(s.folder, ["script.json", "run_state.json"])
            s.chat = None
            kalinga.write_script(s.topic)
        elif c == "o":
            before = (s.folder / "script.json").read_text()
            if _editor(s.folder / "script.json"):
                # archive the pre-edit draft (keyed as script.json so it shows
                # up in [b]ack), then re-apply the edit as the new canonical
                after = (s.folder / "script.json").read_text()
                (s.folder / "script.json").write_text(before)
                make_video.archive(s.folder, "script.json")
                (s.folder / "script.json").write_text(after)
                s.chat = None      # the chat's view of the draft is stale
                script = _read_json(s.folder, "script.json")
                d = _read_json(s.folder, "facts.json")
                try:
                    problems = creative._validate(script["segments"],
                                                  s.tpl, config.channel())
                    for p in problems:
                        print(f"  ! {p}")
                except ValueError as e:
                    print(f"  ! {e} — fix the labels before continuing")
                    continue
                script["judge"] = creative.judge_script(
                    script["segments"], d, prev=script.get("judge"),
                    min_score=s.tpl["script_min_score"])
                hist = script.setdefault("draft_history", [])
                hist.append({"feedback": "manual edit via $EDITOR",
                             "judge": None, "segments": None})
                del hist[:-10]
                script["mode"] = "llm-revised"
                _write_json(s.folder, "script.json", script)

    # script accepted — your critiques feed the channel's learnings so
    # every FUTURE script pre-applies them (creative.learnings_tail)
    script = _read_json(s.folder, "script.json") or {}
    notes = [h["feedback"] for h in script.get("draft_history", [])[hist0:]
             if h.get("feedback") and "manual edit" not in h["feedback"]]
    for n in notes:
        _remember(s, "script critique", n)
    if notes:
        print(f"  {len(notes)} critique(s) → learnings.md — future scripts "
              f"read these before writing")

    # already produced this video? Redo ONLY the sections whose text changed —
    # their voice, keyframe and clip — and leave every untouched section
    # cached. (audio.json carries the text that was last voiced.)
    audio = _read_json(s.folder, "audio.json")
    if audio:
        script = _read_json(s.folder, "script.json")
        if len(script["segments"]) != len(audio["segments"]):
            print("  segment count changed — regenerating the voiceover")
            _invalidate(s.folder, voice=True)
        else:
            changed = [i for i, (seg, am) in
                       enumerate(zip(script["segments"], audio["segments"]))
                       if seg["text"] != am["text"]]
            if changed:
                labels = [script["segments"][i]["label"] for i in changed]
                print(f"  changed: {', '.join(labels)} — redoing only "
                      f"those sections (voice + keyframe + clip)")
                import creative
                resplit = False
                for i in changed:
                    seg = script["segments"][i]
                    # a section-native beat re-derives each section's `say`
                    # from the new text so each section voices the right line
                    # (covers the [o] $EDITOR path; revise_script already
                    # re-splits its own rounds)
                    if make_video._section_specs(seg):
                        creative._sectionize_beats([seg])
                        resplit = True
                    _invalidate(s.folder, section_idx=i)
                if resplit:
                    _write_json(s.folder, "script.json", script)
            else:
                print("  no spoken-text changes — keeping all cached media")


def _set_tweak(script, folder, idx, field, value):
    """Persist a per-segment prompt tweak (visual_extra/motion_extra).
    REPLACES any previous tweak — these never accumulate; '-' clears it."""
    seg = script["segments"][idx]
    if value.strip() == "-":
        seg.pop(field, None)
        print(_d("  tweak cleared"))
    else:
        seg[field] = value
    _write_json(folder, "script.json", script)


def _remember(s: Session, tag: str, text: str):
    """Collect one critique on the session and PERSIST it to session.json (so it
    survives a quit/resume) — but do NOT dump the raw text into learnings.md.
    The raw edit is not a learning; at WRAP these are DISTILLED into durable
    channel learnings (extract_session_learnings) and the transferable craft into
    the GLOBAL learnings (globalize_critiques). Keeping learnings.md clean —
    only distilled, generalizable rules ever land there."""
    text = " ".join((text or "").split())
    if not text:
        return
    s.learned.append({"tag": tag, "text": text[:400]})
    # quit-surviving scratch (the wrap distiller reads it back on resume too)
    sess = _read_json(s.folder, "session.json") or {}
    sess["critiques"] = s.learned
    _write_json(s.folder, "session.json", sess)


def _invalidate_visuals(folder: Path):
    """A new visual design supersedes every rendered frame — drop keyframes,
    clips, charts and the cut so they regenerate against the new direction."""
    _unlink(folder, ["key*.png", "clip*.png", "chart*.png"]
            + list(ASSEMBLY) + ["virality.md", "report.md", "critique.md"])


def _show_direction(folder: Path):
    import textwrap
    script = _read_json(folder, "script.json") or {}
    d = script.get("direction") or {}

    def block(label, text, color=_d):
        if not text:
            return
        wrapped = textwrap.wrap(text, width=70)
        print("  " + _cy(f"{label:>8} ") + color(wrapped[0]))
        for ln in wrapped[1:]:
            print("           " + color(ln))

    if d.get("visual_concept"):
        block("concept", d["visual_concept"], _b)
    block("motif", d.get("anchor_motif"))
    block("teases", d.get("reveal_arc"))
    j = d.get("judge") or {}
    if j.get("overall"):
        ov = j["overall"]
        col = _g if ov >= 7 else _y if ov >= 5 else _r
        sc = j.get("scores", {})
        print("  " + _cy("   judge ") + col(f"{ov}/10")
              + _d("  " + " ".join(f"{k} {v}" for k, v in sc.items())))
        if j.get("weakest"):
            print("           " + _d(f"weakest: {j['weakest']} — "
                                     + (j.get("reason") or "")))
        jsegs = j.get("segments") or []
        if jsegs:
            print("           " + _d(f"{len(jsegs)} beat(s) to fix section by "
                                     "section; the rest are kept as-is"))
        elif j.get("improve"):
            for ln in textwrap.wrap("fix: " + j["improve"], width=70):
                print("           " + _d(ln))
    jnotes = {n.get("label"): n.get("improve", "")
              for n in ((j.get("segments") if j else None) or [])}
    segs = script.get("segments", [])
    for seg in segs:
        kind = (_y("▶ animated clip") if seg.get("animate", True)
                else _g("▮ Ken Burns still"))
        note = jnotes.get(seg["label"])
        flag = (_y("  ⚖ fix") if note else (_g("  ⚖ good") if j and j.get("overall")
                                            else ""))
        print("\n  " + _cy(seg["label"]) + "  " + kind + flag)
        if seg.get("overlay"):
            print("    " + _b(f"on screen: [{seg['overlay']}]"))
        for ln in textwrap.wrap(seg.get("visual") or "", width=72):
            print("    " + ln)
        for tag, val in (("move", seg.get("motion")),
                         ("hides", seg.get("tease"))):
            if val:
                wrapped = textwrap.wrap(val, width=66)
                print("    " + _d(f"{tag}:  {wrapped[0]}"))
                for ln in wrapped[1:]:
                    print("    " + _d(f"       {ln}"))
        ri = seg.get("ref_idx")
        if ri:
            names = ", ".join(segs[k]["label"] for k in ri if k < len(segs))
            print("    " + _d(f"links: continues the look of {names}"))
        if note:
            for ln in textwrap.wrap("judge fix: " + note, width=66):
                print("    " + _y(ln))


def st_direction(s: Session):
    """The director co-design stage: a bespoke per-company visual world,
    staged teases, per-shot framing/motion and real EDGAR data charts —
    iterated with conversation memory before any keyframe credits. The
    director may sharpen the script too, but the facts/verdict lock keeps
    numbers and the decision frozen (enforced in creative._lock_text)."""
    import creative
    import llm
    if not llm.available():
        print(_y("  no LLM backend — skipping direction; template visuals "
                 "are used"))
        return
    script = _read_json(s.folder, "script.json") or {}
    if not script.get("directed"):
        print(_d("  director: designing the visual world, reveal arc, shots "
                 "and data charts…"))
        try:
            creative.direct_script(s.topic)
        except (RuntimeError, ValueError) as e:
            print(_y(f"  ! direction failed ({e}) — template visuals used"))
            return
    else:
        # a run directed before the judge existed has none; one whose judge the
        # model returned without an improve note has no usable fix — ensure both
        # (computes only when absent, else cheaply backfills improve) so the
        # scores + [j] fix always show on older runs.
        try:
            creative.ensure_direction_judge(s.topic)
        except Exception as e:                       # noqa: BLE001
            print(_y(f"  ! director judge skipped: {e}"))
    while True:
        _show_direction(s.folder)
        judge = ((_read_json(s.folder, "script.json") or {})
                 .get("direction") or {}).get("judge") or {}
        seg_notes = judge.get("segments") or []
        has_fix = bool(judge.get("improve") or seg_notes)
        choices = {"a": "lock this visual design in",
                   "e": "give notes — the director remembers each round "
                        "(facts/verdict stay locked)",
                   "f": "fill ONLY missing pieces (e.g. a new field) — leaves "
                        "everything you've set untouched",
                   "r": "throw it out and design again from scratch"}
        if has_fix:
            choices["j"] = (f"apply the judge's fixes"
                            + (f" ({len(seg_notes)} beat(s), section by section)"
                               if seg_notes else
                               f" (weakest: {judge.get('weakest') or '?'})"))
        c = ask("  [a]ccept design / [e] revise it"
                + ("  / [j] apply judge's fixes" if has_fix else "")
                + " / [f]ill gaps only / [r]e-plan fresh:",
                choices, free="visual-design notes")
        if c == "a":
            return
        s.bump("direction")
        if c == "j" and has_fix:
            try:
                creative.revise_direction_from_judge(s.topic)
                print(_g(f"  applied the judge's fixes"
                         + (f" to {len(seg_notes)} beat(s)" if seg_notes else "")))
            except (RuntimeError, ValueError) as e:
                print(_y(f"  ! revision failed: {e}"))
                continue
            _remember(s, "visual direction", judge["improve"])
            _invalidate_visuals(s.folder)
            continue
        if c == "f":
            try:
                creative.direct_script(s.topic, fill=True)
                print(_d("  filled in only the missing fields"))
            except (RuntimeError, ValueError) as e:
                print(_y(f"  ! fill failed: {e}"))
            continue
        if c == "e" or len(c) > 1:
            fb = c if len(c) > 1 else ask_long("  visual-design notes: ")
            if not fb:
                continue
            if s.dchat is None:
                s.dchat = creative.make_direction_chat(s.topic)
            try:
                creative.revise_direction(s.topic, fb, s.dchat)
            except (RuntimeError, ValueError) as e:
                print(_y(f"  ! revision failed: {e}"))
                continue
            _remember(s, "visual direction", fb)
            _invalidate_visuals(s.folder)
        elif c == "r":
            sc = _read_json(s.folder, "script.json")
            sc["directed"] = None
            _write_json(s.folder, "script.json", sc)
            s.dchat = None
            creative.direct_script(s.topic)
            _invalidate_visuals(s.folder)


def _voice_settings(tpl: dict):
    engine = os.environ.get("TTS_ENGINE", tpl.get("tts_engine", "higgsfield"))
    voice = os.environ.get("HF_VOICE", tpl.get("voice", "Mark (en)"))
    return engine, voice


def _warn_env_pins():
    for var in ("TTS_ENGINE", "HF_VOICE"):
        if os.environ.get(var):
            print(f"  ! {var}={os.environ[var]} is exported — the env var "
                  f"BEATS the pinned template; unset it for the pin to win")


def _pick_voice_i(engine: str, gender: str, current=None, name: str = ""):
    """In-session voice picker: a numbered, gender-sorted catalog with [s]N
    sampling (hear it first), 'auto', typed names (validated), and [b]ack.
    Returns the chosen voice string, or None to cancel."""
    import cast_setup
    catalog = (cast_setup.EDGE_CATALOG if engine == "edge"
               else cast_setup.INWORLD_CATALOG)
    g = gender if gender in ("male", "female") else None
    ordered = ([v for v in catalog if g and v[1] == g]
               + [v for v in catalog if not g or v[1] != g])
    names = [v[0] for v in ordered]
    if current and current not in names and str(current).lower() != "auto":
        ordered = [(current, gender or "", "current")] + ordered
        names = [current] + names
    print(_cy(f"\n  {engine} voices") + _d(f"  (current: {current or '—'})"))
    for i, (v, gd, note) in enumerate(ordered):
        tail = f" — {note}" if note else ""
        gl = gd[0].upper() if gd else "?"
        print(f"   {_cy(str(i + 1))} {v}  ({gl}){tail}"
              + (_g("  ← current") if v == current else ""))
    cost = ("(free)" if make_video.tts_engine(engine).free
            else "(~1 credit each)")
    print(_d(f"   [s]N sample {cost}  ·  'auto' auto-assign  ·  "
             "type a name  ·  [b]ack"))
    while True:
        pick = ask_text("  number / [s]N / name / 'auto' / [b]ack: ").strip()
        if not pick or pick.lower() == "b":
            return None
        if pick.lower() == "auto":
            return "auto"
        if pick[:1].lower() == "s" and pick[1:].strip().isdigit():
            j = int(pick[1:].strip())
            if 1 <= j <= len(names):
                cast_setup._sample_voice(engine, names[j - 1], name)
            else:
                print(_y("   no such number"))
            continue
        if pick.isdigit():
            if 1 <= int(pick) <= len(names):
                return names[int(pick) - 1]
            print(_y("   no such number"))
            continue
        if cast_setup.voice_ok(engine, pick):
            return pick
        print(_y(f"   '{pick}' isn't a valid {engine} voice — pick a number, "
                 "type a full voice name, 'auto', or [b]ack"))


def _st_cast(s: Session) -> bool:
    """View / reassign the script's character voices. Returns True if any voice
    changed (the caller re-synthesizes)."""
    script = _read_json(s.folder, "script.json")
    cast = script.get("cast") or {}
    if not cast:
        print(_d("  single narrator — no cast. The writer can introduce "
                 "characters; or add a `cast` map in script.json."))
        return False
    engine, vdef = _voice_settings(s.tpl)
    changed = False
    while True:
        _resolve, assigned = make_video._voice_resolver(cast, engine, vdef)
        print(_cy("\n  cast / voices") + _d(f"  (engine: {engine})"))
        names = list(cast.keys())
        for i, n in enumerate(names):
            desc = (cast[n] or {}).get("desc") or ""
            print(f"   {_cy(str(i))} {n:12s} {_y(assigned.get(n, ''))}  "
                  + _d(desc))
        c = ask("  number to set a voice / [b]ack:",
                {"#": "set that character's voice", "b": "back"},
                globals_on=False)
        if c == "b":
            return changed
        if not c.isdigit() or not (0 <= int(c) < len(names)):
            print(_y("  no such character"))
            continue
        n = names[int(c)]
        gender = (config.channel().cast_member(n) or {}).get("gender", "")
        v = _pick_voice_i(engine, gender, current=(cast.get(n) or {}).get("voice"),
                          name=n)
        if not v:
            continue
        cast.setdefault(n, {})["voice"] = v
        script["cast"] = cast
        _write_json(s.folder, "script.json", script)
        _invalidate(s.folder, voice=True)     # re-synthesize all audio
        changed = True
        print(_g(f"  {n} → {v} (audio will regenerate)"))


def _choose_cast_voices(s: Session) -> bool:
    """When a script has MULTIPLE characters, ask the creator to pick a voice
    for each (instead of silently auto-assigning). Runs once — only while some
    cast voice is still 'auto'/unset; after picking, every character has a
    concrete voice pinned in script.json so it never re-prompts. Returns True if
    anything was set (the caller re-synthesizes)."""
    import voiceover
    script = _read_json(s.folder, "script.json")
    cast = script.get("cast") or {}
    if len(cast) < 2:
        return False
    needs = [n for n, info in cast.items()
             if not (info or {}).get("voice")
             or str((info or {}).get("voice")).lower() == "auto"]
    if not needs:
        return False
    engine, vdef = _voice_settings(s.tpl)
    _resolve, suggested = make_video._voice_resolver(cast, engine, vdef)
    base = make_video.tts_engine(engine).default_voice(vdef)
    print(_cy(f"\n  this script has {len(cast)} characters — pick a voice for "
              f"each") + _d(f"  (engine: {engine}"
                           + ("; edge has many natural voices)" if engine == "edge"
                              else "; Inworld names)")))
    changed = False
    for name in cast:
        info = cast[name] = (cast[name] or {})
        if name not in needs:
            print(_d(f"   {name}: {info['voice']} (already set)"))
            continue
        desc = (info.get("desc") or "").strip()
        sug = suggested.get(name, base)
        gender = (config.channel().cast_member(name) or {}).get("gender", "")
        print("\n  " + _cy(name) + (_d(f"  ({desc})") if desc else ""))
        chosen = _pick_voice_i(engine, gender, current=sug, name=name) or sug
        info["voice"] = chosen
        changed = True
        print(_g(f"  {name} → {chosen}"))
    if changed:
        script["cast"] = cast
        _write_json(s.folder, "script.json", script)
        _invalidate(s.folder, voice=True)     # synthesize with the chosen voices
    return changed


def _st_dialogue(s: Session) -> bool:
    """Edit a multi-speaker segment's per-line timing (when each line STARTS,
    in seconds, or 'auto' for back-to-back) and per-line EXPRESSION (how it's
    said). A timing-only change recomposes segN.mp3 for FREE (no TTS); an
    expression change re-voices the affected lines. The keyframe is untouched;
    the clip refits at assembly."""
    script = _read_json(s.folder, "script.json")
    audio = _read_json(s.folder, "audio.json") or {}
    amap = {m["index"]: m for m in audio.get("segments", [])}
    opts = [(i, seg) for i, seg in enumerate(script["segments"])
            if len([l for l in (seg.get("lines") or [])
                    if (l.get("text") or "").strip()]) > 1]
    if not opts:
        print("    no multi-speaker segments to time (single-voice script)")
        return False
    print("  dialogue segments:")
    for i, seg in opts:
        nl = len([l for l in seg["lines"] if (l.get("text") or "").strip()])
        print(f"    seg{i} [{seg['label']}] — {nl} lines")
    n = ask_text("  segment # to edit: ")
    if not n.isdigit() or not any(i == int(n) for i, _ in opts):
        return False
    i = int(n)
    seg = script["segments"][i]
    lines = [l for l in seg["lines"] if (l.get("text") or "").strip()]
    ameta = {k: ml for k, ml in enumerate(amap.get(i, {}).get("lines", []))}
    changed = expr_changed = False
    while True:
        print(f"\n  seg{i} [{seg['label']}] — lines (start/dur from last render):")
        for k, ln in enumerate(lines):
            ml = ameta.get(k, {})
            pin = (f" start={ln['start']}s" if ln.get("start") is not None
                   else (f" gap={ln['gap']}s" if ln.get("gap") else ""))
            print(f"    [{k}] {ln.get('speaker', '?'):6} "
                  f"@{ml.get('start', '?')}s ({ml.get('duration', '?')}s)"
                  f"{pin}  expr={ln.get('expression') or '—'}  "
                  f"“{ln['text'][:46]}”")
        c = ask_text("  line # to edit (Enter = apply & exit): ")
        if not c:
            break
        if not c.isdigit() or int(c) >= len(lines):
            continue
        k = int(c)
        ln = lines[k]
        val = ask_text(f"  line {k} START seconds — number, 'auto' for "
                       "back-to-back, blank=keep: ").strip()
        if val.lower() == "auto":
            if ln.pop("start", None) is not None or ln.pop("gap", None):
                changed = True
        elif val:
            try:
                ln["start"] = round(float(val), 3)
                ln.pop("gap", None)
                changed = True
            except ValueError:
                print("    not a number")
        ex = ask_text(f"  line {k} EXPRESSION (e.g. excited/deadpan/concerned; "
                      f"'-'=none, blank=keep) [{ln.get('expression') or ''}]: "
                      ).strip()
        if ex == "-":
            if ln.pop("expression", None):
                expr_changed = True
        elif ex:
            ln["expression"] = ex
            expr_changed = True
    if not (changed or expr_changed):
        return False
    _write_json(s.folder, "script.json", script)
    victims = ([f"seg{i}.mp3"] + list(ASSEMBLY)
               + ["virality.md", "report.md", "critique.md"])
    if expr_changed:                       # audio differs → re-voice the lines
        victims += [f"seg{i}_l{k}.mp3" for k in range(len(lines))]
    _unlink(s.folder, victims)             # keep audio.json + line clips → free
    s.bump("voice")                        # recompose if only timing changed
    kalinga.run_voice(s.topic)
    _play(config.art(s.folder, f"seg{i}.mp3"))
    return True


def _first_seg_audio(folder: Path, i: int):
    """The playable audio for segment i — seg<i>.mp3, else its first section
    seg<i>_s0.mp3 (a section-native beat has no whole-segment file)."""
    for name in (f"seg{i}.mp3", f"seg{i}_s0.mp3"):
        p = config.art(folder, name)
        if p.exists():
            return p
    return config.art(folder, f"seg{i}.mp3")


def _record_one(s: Session, unit: dict) -> bool:
    """Record (or re-record) ONE voice unit — mic capture with press-Enter to
    stop, replay, then keep/retake; falls back to pasting a file path when the
    mic can't be opened. Returns True if a take was applied."""
    import make_video
    import recording
    i, sec = unit["index"], unit["section"]
    lbl = (f"seg{i}.s{sec}" if sec is not None else f"seg{i}")
    print(_b(f"\n  ● {lbl} [{unit['label']}]"))
    print(f"    say: {unit['say'][:140]}")
    tmp = config.art(s.folder, (f"seg{i}_s{sec}.rec.wav" if sec is not None
                                else f"seg{i}.rec.wav"))
    while True:
        proc = recording.mic_record(tmp) if recording.ffmpeg_ok() else None
        if proc is not None:
            ask_text("    🔴 recording — press Enter to STOP ")
            recording.stop(proc)
            if not tmp.exists() or make_video.ffprobe_duration(tmp) < 0.2:
                print("    ! nothing captured (mic permission?) — try a file")
                proc = None
        if proc is None:                          # mic unavailable → file path
            path = ask_text("    paste a path to an audio file (or blank to "
                            "cancel): ").strip().strip("'\"")
            if not path:
                return False
            src = Path(path).expanduser()
            if not src.exists():
                print("    ! no file there")
                continue
            tmp = src
        else:
            print(f"    captured {make_video.ffprobe_duration(tmp):.1f}s")
            _play(tmp)
        c = ask("    [k]eep this take / [r]etake / [c]ancel:",
                {"k": "use this recording", "r": "record again",
                 "c": "keep the AI voice"})
        if c == "r":
            continue
        if c == "c":
            return False
        res = make_video.apply_recording(s.folder, i, sec, tmp)
        if not res.get("ok"):
            print(f"    ! {res.get('error', 'failed')}")
            return False
        cap = ("karaoke (whisper-aligned)" if res.get("aligned")
               else "section text held (install whisper for word-sync)")
        print(_g(f"    ✓ recorded {res['duration']:.1f}s — captions: {cap}"))
        return True


def _st_record(s: Session) -> bool:
    """Record your OWN narration, unit by unit (each section of a section-native
    beat, else each segment). Returns True if any take was applied."""
    import make_video
    import recording
    script = _read_json(s.folder, "script.json")
    units = make_video.recordable_units(script)
    if not units:
        print("  nothing to record (native-audio / dialogue beats only)")
        return False
    audio = _read_json(s.folder, "audio.json") or {}
    amap = {a["index"]: a for a in audio.get("segments", [])}

    def _is_rec(u):
        e = amap.get(u["index"], {})
        if u["section"] is None:
            return bool(e.get("recorded"))
        return any(sx.get("k") == u["section"] and sx.get("recorded")
                   for sx in (e.get("sections") or []))

    devs = recording.list_devices()
    if devs:
        print(f"  mic: {devs[0][1]} (live capture)")
    else:
        print("  no mic detected — you can paste an audio file path per unit")
    try:
        import align
        print("  captions: " + ("whisper installed — recorded takes get "
              "word-synced karaoke" if align.whisper_available()
              else "no whisper — recorded takes show the section text held "
              "(pip install faster-whisper for karaoke)"))
    except Exception:                             # noqa: BLE001
        pass
    changed = False
    while True:
        print("\n  record which? (recorded ✓ shown)")
        for n, u in enumerate(units):
            tag = (f"seg{u['index']}.s{u['section']}" if u["section"] is not None
                   else f"seg{u['index']}")
            mark = _g(" ✓") if _is_rec(u) else "  "
            print(f"   [{n}]{mark} {tag} [{u['label']}] — {u['say'][:54]}")
        c = ask("  unit number to record / [x] clear one / [a] done:",
                {"#": "record/re-record that unit",
                 "x": "drop a recorded take (revert to AI voice)",
                 "a": "done recording"})
        if c == "a":
            return changed
        if c == "x":
            n = ask_text("  unit # to clear: ")
            if n.isdigit() and int(n) < len(units):
                u = units[int(n)]
                make_video.clear_recording(s.folder, u["index"], u["section"])
                print("    reverted to AI voice — re-runs at next voice gen")
                changed = True
            continue
        if c.isdigit() and int(c) < len(units):
            if _record_one(s, units[int(c)]):
                changed = True


def st_voice(s: Session):
    engine, voice = _voice_settings(s.tpl)
    _choose_cast_voices(s)
    audio = _read_json(s.folder, "audio.json")
    if audio and audio.get("engine") != engine:
        print(f"  ! cached voiceover used engine '{audio.get('engine')}', "
              f"current setting is '{engine}'")
        c = ask("  [k]eep the cached audio / [r]egenerate it all:",
                {"k": "keep what's there", "r": "regenerate with the "
                 "current engine"})
        if c == "r":
            _invalidate(s.folder, voice=True)
    kalinga.run_voice(s.topic)
    audio = _read_json(s.folder, "audio.json")
    print(f"  engine: {engine} | voice: {voice}"
          + (" | karaoke captions: yes" if make_video.tts_engine(engine).timings
             else " | karaoke captions: no (edge-tts adds them, free)"))
    for m in audio["segments"]:
        rec = " ⦿rec" if m.get("recorded") else ""
        print(f"    seg{m['index']} [{m['label']}]{rec} {m['duration']:.1f}s — "
              f"{m['text'][:60]}")
        for sx in (m.get("sections") or []):
            srec = _g(" ⦿rec") if sx.get("recorded") else ""
            print(f"        ·s{sx['k']}{srec} {sx.get('duration', 0):.1f}s — "
                  f"{(sx.get('say') or '')[:48]}")
    _play(_first_seg_audio(s.folder, 0))
    while True:
        engine, voice = _voice_settings(s.tpl)
        c = ask("  [a]ccept / [R]ecord your own / [p]lay a segment / re-read "
                "segment number / [v]oice / [g] engine / [c]ast / [d]ialogue / "
                "[t]ext edit:",
                {"a": "voiceover is good — continue",
                 "R": "record your OWN narration, section by section",
                 "p": "play one segment again",
                 "#": "re-read that segment "
                      + ("(free)" if make_video.tts_engine(engine).free
                         else "(one paid TTS call)"),
                 "v": "pick a different voice (pinned to this run)",
                 "g": "switch TTS engine (cycles the available ones, "
                      "pinned to this run)",
                 "c": "view/reassign character voices (multi-cast scripts)",
                 "d": "set per-line start + expression in a dialogue segment",
                 "t": "rewrite one segment's spoken text"})
        if c == "a":
            return
        if c == "R":
            if _st_record(s):
                kalinga.run_voice(s.topic)
                audio = _read_json(s.folder, "audio.json")
                _play(_first_seg_audio(s.folder, 0))
            continue
        if c == "c":
            if _st_cast(s):
                kalinga.run_voice(s.topic)
                _play(_first_seg_audio(s.folder, 0))
            continue
        if c == "d":
            _st_dialogue(s)
            continue
        if c == "p":
            n = ask_text("  segment # [0]: ") or "0"
            if n.isdigit():
                _play(_first_seg_audio(s.folder, int(n)))
            continue
        s.bump("voice")
        if c.isdigit():
            i = int(c)
            f = _first_seg_audio(s.folder, i)
            if not f.exists():
                print("    no such segment")
                continue
            # a section-native beat re-reads ALL its section files
            adir = config.art(s.folder, f"seg{i}_s0.mp3").parent
            secs = sorted(p.name for p in adir.glob(f"seg{i}_s*.mp3"))
            victims = (secs or [f"seg{i}.mp3"])
            _unlink(s.folder, victims + list(ASSEMBLY)
                    + ["virality.md", "report.md", "critique.md"])
            kalinga.run_voice(s.topic)
            _play(_first_seg_audio(s.folder, i))
        elif c == "v":
            print(f"  current voice: {voice} (Inworld names, e.g. "
                  f"'Mark (en)', 'Ashley (en)')")
            name = ask_text("  voice name: ")
            if not name:
                continue
            s.tpl = _pin_template(s.folder, voice=name)
            _warn_env_pins()
            _invalidate(s.folder, voice=True)
            engine, voice = _voice_settings(s.tpl)
            kalinga.run_voice(s.topic)
            _play(_first_seg_audio(s.folder, 0))
        elif c == "g":
            new = make_video.next_engine(engine)
            print(f"  switching to {new} — {make_video.ENGINES[new].label}")
            s.tpl = _pin_template(s.folder, tts_engine=new)
            _warn_env_pins()
            _invalidate(s.folder, voice=True)
            engine, voice = _voice_settings(s.tpl)
            kalinga.run_voice(s.topic)
            _play(_first_seg_audio(s.folder, 0))
        elif c == "t":
            n = ask_text("  segment # to rewrite: ")
            script = _read_json(s.folder, "script.json")
            if not n.isdigit() or int(n) >= len(script["segments"]):
                print("    no such segment")
                continue
            i = int(n)
            print(f"  current: {script['segments'][i]['text']}")
            new = ask_text("  new text: ")
            if not new:
                continue
            script["segments"][i]["text"] = new
            # a section-native beat re-derives each section's `say` from the new
            # text (visuals kept where they line up) so audio/captions stay synced
            if make_video._section_specs(script["segments"][i]):
                import creative
                creative._sectionize_beats([script["segments"][i]])
            _write_json(s.folder, "script.json", script)
            s.chat = None
            _invalidate(s.folder, text_idx=i)
            kalinga.run_voice(s.topic)
            _play(_first_seg_audio(s.folder, i))


def _shots(s: Session):
    script = _read_json(s.folder, "script.json")
    audio = _read_json(s.folder, "audio.json")
    return make_video.shot_list(script, audio, s.folder), script


def _ensure_directed(s: Session):
    """Safety net: if a run jumped straight to keyframes/clips and skipped
    the direction stage, run the director ($0) so the bespoke visuals/charts
    exist. No-op once directed; never blocks the stage."""
    import llm
    script = _read_json(s.folder, "script.json")
    if not llm.available() or (script or {}).get("directed"):
        return
    print(_d("  director: designing visuals + data charts (skipped the "
             "direction stage)…"))
    try:
        import creative
        creative.direct_script(s.topic)
        _show_direction(s.folder)
    except Exception as e:
        print(_y(f"  ! director pass skipped: {e}"))


def _hook_first(s: Session):
    """Hook-first virality: build + score ONLY the opener (its voice + key0 +
    clip0/ken-burns) and offer a hook rewrite if it's weak — BEFORE the rest of
    the keyframes are generated, so a weak hook is caught before the full
    spend. Costs the opener's key/clip + one virality read per check."""
    print(_cy("\n  hook-first: scoring the opener before the rest"))
    while True:
        make_video.score_hook(s.topic)
        hv = validate.virality_gate(s.folder, s.tpl)
        scores = hv.get("scores") or {}
        print("  hook virality: "
              + (_g(hv["status"]) if hv["status"] != "weak"
                 else _y(hv["status"])) + _d(f"   {scores}"))
        if hv["status"] != "weak":
            return
        c = ask("  weak hook — [h] rewrite the hook + re-score (cheap) / "
                "[a]ccept and generate the rest anyway:",
                {"h": "rewrite seg0, reset its shots, re-score",
                 "a": "keep this hook and continue"}, globals_on=False)
        if c != "h":
            return
        import creative
        creative.rewrite_hook(s.topic,
                              kalinga.hook_feedback({"virality": hv}, s.folder))
        make_video.reset_hook(s.folder)
        kalinga.direct(s.topic)
        s.bump("keyframes")


def _gen_keyframe(s: Session, idx: int, facts: dict) -> str:
    """Show the EXACT image prompt for this shot, let the creator edit/refine
    it, and generate ONLY on confirm — a keyframe is NEVER generated without
    showing the prompt and asking first. Returns 'done' (a file landed) or
    'skip'."""
    import textwrap
    import llm
    while True:
        shot = _shots(s)[0][idx]
        full = make_video.keyframe_prompt(shot, facts, s.folder, s.tpl)
        ov = bool((shot["seg"].get("image_prompt") or "").strip())
        print("\n  " + _cy(f"key{idx} [{shot['label']}] image prompt")
              + _d("  (sent to the model" + (", EDITED" if ov else "") + "):"))
        for ln in textwrap.wrap(full, 74):
            print("    " + _d(ln))
        choices = {"g": "generate this keyframe as shown"}
        if llm.available():
            choices["r"] = "refine the prompt with AI from your notes (straight in)"
        choices["p"] = "paste/replace or revert the FULL prompt"
        choices["s"] = "leave it ungenerated for now"
        cc = ask(f"  key{idx}: [g]enerate (~{CREDITS_PER_KEYFRAME} cr) / "
                 + ("[r] refine with AI / " if llm.available() else "")
                 + "[p] edit prompt / [s]kip for now:",
                 choices, globals_on=False)
        if cc == "r":
            _ai_refine_prompt(s, idx, "image")
            continue                      # re-show with the refined prompt
        if cc == "p":
            _edit_full_prompt(s, idx, "image")
            continue                      # re-show with the edited prompt
        if cc == "s":
            return "skip"
        print(_d(f"    key{idx} [{shot['label']}]…"))
        if make_video.step_keyframe(shot, facts, s.folder, s.tpl):
            return "done"
        c = ask(f"  keyframe {idx} failed — [r]etry / [s]kip for now:",
                {"r": "try the generation again",
                 "s": "leave it ungenerated for now"}, globals_on=False)
        s.bump("keyframes")
        if c != "r":
            return "skip"


def st_keyframes(s: Session):
    facts = _read_json(s.folder, "facts.json")
    _ensure_directed(s)
    s.tpl = templates.load_pinned(s.folder)
    hf_on = bool(s.tpl.get("score_hook_first"))
    tog = ask("  hook-first virality is " + ("ON" if hf_on else "off")
              + " — [a] keep / [h] toggle (score the opener before the rest):",
              {"a": "keep this setting",
               "h": "toggle scoring the opener's virality first"},
              globals_on=False)
    if tog == "h":
        hf_on = not hf_on
        s.tpl = _pin_template(s.folder, score_hook_first=hf_on)
        print(_g("  hook-first ON") if hf_on else _d("  hook-first off"))
    if hf_on:
        _hook_first(s)
    shots, script = _shots(s)
    n_sub = len(_subshot_shots(s))
    stitched = sum(1 for sh in shots
                   if len(make_video.shot_plan(sh["seg"], sh["index"])) > 1)
    print(f"  {n_sub} keyframes ≈ "
          + _y(f"{n_sub * CREDITS_PER_KEYFRAME} credits")
          + _d(" when none are cached")
          + (_d(f"  ({stitched} multi-shot beat(s) stitched from several shots)")
             if stitched else ""))
    # First instance: if NOT ONE keyframe exists yet, generate the whole set up
    # front (in index order so continuity refs resolve), THEN review one by one.
    if _count(s.folder, "key*.png") == 0 and len(shots) > 1:
        rv = ask(f"  no keyframes yet ({len(shots)}) — [g] generate them all "
                 "now / [p] preview & confirm each prompt first:",
                 {"g": "generate the whole set as-is (no per-shot confirm)",
                  "p": "see/edit each image prompt and confirm before it spends"},
                 globals_on=False)
        if rv == "p":
            print(_cy(f"\n  {len(shots)} keyframes — prompt-by-prompt:"))
            for j in range(len(shots)):
                _gen_keyframe(s, j, facts)
        else:
            print(_cy(f"\n  generating {len(shots)} keyframes…"))
            for j in range(len(shots)):
                sh = _shots(s)[0][j]
                print(_d(f"    key{j} [{sh['label']}]…"))
                while not make_video.step_keyframe(sh, facts, s.folder, s.tpl):
                    c = ask(f"  keyframe {j} failed — [r]etry / [s]kip for now:",
                            {"r": "try the generation again",
                             "s": "leave it ungenerated; you can make it below"},
                            globals_on=False)
                    s.bump("keyframes")
                    if c != "r":
                        break
        print(_g(f"  ✓ {_count(s.folder, 'key*.png')} keyframes generated")
              + _d(" — now review them one by one"))
    i = 0
    while i < len(shots):
        shots, script = _shots(s)
        shot = shots[i]
        was_cached = shot["key"].exists()
        if not was_cached:
            # never auto-generate: show the prompt + confirm (or edit) first
            if _gen_keyframe(s, i, facts) == "skip" or not shot["key"].exists():
                i += 1
                continue
            shots, script = _shots(s)
            shot = shots[i]
        print(f"  key{i} [{shot['label']}]" + (" (cached)" if was_cached else ""))
        if was_cached and make_video.keyframe_stale(shot, facts, s.folder,
                                                    s.tpl):
            print(_y("  ⭐ the prompt that generated this image has changed "
                     "since it was made — [r]egen to match the current plan"))
        _open(shot["key"])
        import llm
        choices = {"a": "keep this keyframe",
                   "r": "delete + regenerate with the same prompt",
                   "e": "add extra prompt text (persisted), then regenerate"}
        if llm.available():
            choices["f"] = "refine the prompt with AI from your notes, regenerate"
        choices["p"] = "see/replace the EXACT image prompt (verbatim), regenerate"
        choices["o"] = "open every keyframe generated so far in Preview"
        c = ask(f"  key{i}: [a]ccept / [r]egen (~{CREDITS_PER_KEYFRAME} cr) "
                f"/ [e] tweak / "
                + ("[f] refine with AI / " if llm.available() else "")
                + "[p] edit FULL prompt / [o]pen all so far:",
                choices,
                free="extra visual prompt text — regenerates this keyframe")
        if c == "a":
            i += 1
        elif c == "o":
            _open(*config.art_glob(s.folder, "key*.png"))
        elif c == "f":
            if _ai_refine_prompt(s, i, "image"):
                s.bump("keyframes")
                _invalidate(s.folder, visual_idx=i)
            shots, script = _shots(s)
        elif c == "p":
            if _edit_full_prompt(s, i, "image"):
                s.bump("keyframes")
                _invalidate(s.folder, visual_idx=i)
            shots, script = _shots(s)
        elif c == "r":
            s.bump("keyframes")
            if i == 0 and _count(s.folder, "key*.png") > 1:
                print(_d("  note: later keyframes were built to match the old "
                         "key0 — regen them too if you want the world to stay "
                         "consistent"))
            _invalidate(s.folder, keyframe_idx=i)
        else:   # [e] or typed prose
            cur = script["segments"][i].get("visual_extra")
            if cur:
                print(_d(f"  current tweak (will be REPLACED, not added to): "
                         f"{cur}"))
            extra = c if len(c) > 1 else ask_long(
                "  extra prompt text (replaces the above; '-' clears it): ")
            if not extra:
                continue
            s.bump("keyframes")
            _set_tweak(script, s.folder, i, "visual_extra", extra)
            if extra.strip() != "-":
                _remember(s, "keyframe", extra)
            _invalidate(s.folder, visual_idx=i)
    _ensure_subshot_keyframes(s, facts)


def _subshot_shots(s: Session) -> list:
    """Every sub-shot, segment-level shots expanded — a MULTI-SHOT beat
    (director `shots`) yields several; a normal beat yields one (§3)."""
    return [sub for shot in _shots(s)[0]
            for sub in make_video._subshots(shot, s.folder)]


def _ensure_subshot_keyframes(s: Session, facts: dict) -> None:
    """Generate any MISSING stitched sub-shot keyframes (k≥1). The per-segment
    review above curates sub-shot 0 (the canonical key<i>.png); the extra shots
    of a stitched beat are generated here so the stage completes and assembly
    can stitch them."""
    pending = [sub for sub in _subshot_shots(s)
               if sub.get("sub", 0) >= 1 and not sub["key"].exists()]
    if not pending:
        return
    print(_cy(f"\n  {len(pending)} stitched sub-shot keyframe(s) to generate "
              "(multi-shot beats):"))
    for sub in pending:
        tag = f"{sub['index']}.{sub['sub']}"
        print(_d(f"    key{tag} [{sub['label']}]…"))
        while not make_video.step_keyframe(sub, facts, s.folder, s.tpl):
            c = ask(f"  keyframe {tag} failed — [r]etry / [s]kip for now:",
                    {"r": "try again", "s": "leave it for now"},
                    globals_on=False)
            s.bump("keyframes")
            if c != "r":
                break


def _ensure_subshot_clips(s: Session) -> None:
    """Generate any MISSING stitched sub-shot clips (k≥1) the plan wants animated
    — honouring the per-segment animated-clip cap (the surplus stay free Ken
    Burns stills). Sub-shot 0 is handled by the per-segment clips review."""
    shots = _shots(s)[0]
    for shot in shots:
        plan = make_video.shot_plan(shot["seg"], shot["index"])
        if len(plan) < 2:
            continue
        subs = make_video._subshots(shot, s.folder)
        wants = make_video._plan_clip_wanted(plan, s.tpl)
        for sub, want in zip(subs, wants):
            if sub.get("sub", 0) < 1 or not want or sub["clip"].exists():
                continue
            tag = f"{sub['index']}.{sub['sub']}"
            print(_d(f"    clip{tag} [{sub['label']}] (stitched shot)…"))
            if not make_video.step_clip(sub, s.folder, s.tpl):
                print(_y(f"  clip{tag} failed — Ken Burns fallback at assembly"))


def _animate(seg) -> bool:
    return seg.get("animate", True)     # default animate when undirected


def _ensure_clip_motion(s: Session, idx: int) -> bool:
    """Before generating a clip for a shot the director planned as a STILL,
    make sure it has a DYNAMIC `clip_motion` brief (real action) instead of the
    gentle Ken Burns push — so the clip actually moves, not just zooms.
    Backfills via the director (free) when missing. Returns True if it ran."""
    import creative
    script = _read_json(s.folder, "script.json")
    seg = script["segments"][idx]
    if (seg.get("clip_motion") or "").strip():
        return False
    print(_d("  writing a dynamic clip brief (so it moves, not just zooms)…"))
    try:
        creative.direct_script(s.topic, fill=True)   # backfills clip_motion
        return True
    except Exception as e:                            # noqa: BLE001
        print(_y(f"  ! director fill failed ({str(e)[:80]}) — using the "
                 "keyframe push"))
        return False


def _toggle_clip_voice(s: Session, idx: int, on: bool) -> None:
    """Flip a segment's `clip_voice` — whether the GENERATED clip delivers the
    spoken line on-camera (lip-synced to the voiceover, which is passed to the
    model as a reference) instead of a separate voiceover. The clip must be
    (re)generated with/without the `--audio` reference, and the audio SOURCE
    for assembly changes, so the clip + cut die and audio.json is refreshed
    (free — the seg.mp3 is cached, just re-marked)."""
    script = _read_json(s.folder, "script.json")
    seg = script["segments"][idx]
    if on:
        seg["clip_voice"] = True
    else:
        seg.pop("clip_voice", None)
    _write_json(s.folder, "script.json", script)
    _unlink(s.folder, [f"clip{idx}.mp4"] + list(ASSEMBLY)
            + ["virality.md", "report.md", "critique.md"])
    kalinga.run_voice(s.topic)            # refresh audio.json's clip_voice flag


def _clip_mode(seg: dict, tpl: dict) -> str:
    """The seedance model tier this clip uses: per-segment `clip_mode`
    (fast | std) over the template's video_params default."""
    m = str(seg.get("clip_mode")
            or (tpl.get("video_params") or {}).get("mode", "fast")).lower()
    return m if m in ("fast", "std") else "fast"


def _toggle_clip_mode(s: Session, idx: int) -> str:
    """Flip this clip's seedance tier (fast <-> std) and persist it. If a clip
    already exists it's invalidated (regenerate at the new quality)."""
    script = _read_json(s.folder, "script.json")
    seg = script["segments"][idx]
    nxt = "std" if _clip_mode(seg, s.tpl) == "fast" else "fast"
    seg["clip_mode"] = nxt
    _write_json(s.folder, "script.json", script)
    if config.art(s.folder, f"clip{idx}.mp4").exists():
        _invalidate(s.folder, motion_idx=idx)
    return nxt


def _current_prompt(s: Session, idx: int, kind: str):
    """The EXACT prompt text that will be sent for this shot's image/video
    generation — the override if one is set, else the auto-assembled one.
    Returns (shot, prompt_text)."""
    shots, _ = _shots(s)
    shot = shots[idx]
    if kind == "image":
        facts = _read_json(s.folder, "facts.json")
        return shot, make_video.keyframe_prompt(shot, facts, s.folder, s.tpl)
    return shot, make_video.clip_prompt(shot, s.tpl)


def _ai_refine_prompt(s: Session, idx: int, kind: str) -> bool:
    """Refine this shot's IMAGE/VIDEO prompt WITH AI — straight to 'what should
    change', iterate on the refined draft, save it VERBATIM as the override. The
    direct entry point the keyframe/clip menus call so refine-with-AI is ONE key
    away (no submenu). kind in {'image','video'}. Returns True if it changed."""
    import textwrap
    import creative
    import llm
    if not llm.available():
        print(_y("  no LLM backend — paste an edit with [p] instead"))
        return False
    field = "image_prompt" if kind == "image" else "clip_prompt_text"
    _, base = _current_prompt(s, idx, kind)
    changed = False
    while True:
        notes = ask_long("  AI refine — what should change about the prompt "
                         "(empty = cancel): ")
        if not notes.strip():
            return changed
        script = _read_json(s.folder, "script.json")
        seg = script["segments"][idx]
        ctx = (seg.get("visual") or "") if kind == "image" else ""
        print(_d("  refining with AI…"))
        refined = creative.refine_prompt(kind, base, notes, context=ctx)
        if not refined:
            print(_y("  refine failed — prompt unchanged"))
            return changed
        print(_cy("\n  refined prompt:"))
        for ln in textwrap.wrap(refined, 74):
            print("    " + ln)
        keep = ask(_d("  use this refined prompt:"),
                   {"a": "accept it",
                    "m": "refine further with more notes",
                    "d": "discard, keep the old prompt"})
        if keep == "m":
            base = refined                    # iterate ON the refined draft
            continue
        if keep == "a":
            seg[field] = refined.strip()
            _write_json(s.folder, "script.json", script)
            _remember(s, "keyframe" if kind == "image" else "clip",
                      "AI-refined prompt — notes: " + notes.strip())
            print(_g("  refined prompt saved — sent verbatim next generation"))
            changed = True
        return changed


def _edit_full_prompt(s: Session, idx: int, kind: str) -> bool:
    """Show the EXACT prompt that will be sent for this shot's IMAGE or VIDEO
    generation and let the creator: [e] paste a full replacement, [r] refine it
    WITH AI from notes, [-] revert to the auto-assembled prompt, or keep it. The
    result is stored VERBATIM as `image_prompt` / `clip_prompt_text`.
    kind in {'image','video'}. Returns True if the stored override changed."""
    import textwrap
    import llm
    field = "image_prompt" if kind == "image" else "clip_prompt_text"
    changed = False
    while True:
        script = _read_json(s.folder, "script.json")
        seg = script["segments"][idx]
        shot, cur = _current_prompt(s, idx, kind)
        overridden = bool((seg.get(field) or "").strip())
        print(_cy(f"\n  {kind} prompt for {seg.get('label')}")
              + _d("  (sent verbatim to the model"
                   + ("; currently EDITED" if overridden else "; auto-assembled")
                   + "):"))
        for ln in textwrap.wrap(cur, 74):
            print("    " + _d(ln))
        choices = {"e": "paste a full replacement prompt"}
        if llm.available():
            choices["r"] = "refine with AI from your notes (keeps this as base)"
        if overridden:
            choices["-"] = "revert to the auto-assembled prompt"
        choices["a"] = "keep this prompt as-is"
        pick = ask(_d("  edit this prompt:"), choices)
        if pick == "a":
            return changed
        if pick == "-":
            if seg.pop(field, None) is not None:
                _write_json(s.folder, "script.json", script)
                print(_d("  reverted to the auto-assembled prompt"))
                changed = True
            continue
        if pick == "e":
            new = ask_long("  paste the FULL replacement prompt "
                           "(empty = cancel): ")
            if not new.strip():
                continue
            seg[field] = new.strip()
            _write_json(s.folder, "script.json", script)
            _remember(s, "keyframe" if kind == "image" else "clip",
                      "full prompt override: " + new.strip())
            print(_g("  prompt override saved — sent verbatim next generation"))
            changed = True
            continue
        if pick == "r":
            if _ai_refine_prompt(s, idx, kind):
                changed = True
            continue


def st_clips(s: Session):
    _ensure_directed(s)
    shots, script = _shots(s)
    subs = _subshot_shots(s)               # multi-shot beats expanded (§3)
    exp_clips = set(make_video.expected_clip_names(
        [sh["seg"] for sh in shots], s.tpl))
    anim = [sub for sub in subs if sub["clip"].name in exp_clips]
    kb = len(subs) - len(anim)
    total = sum(make_video.clip_duration(sub, s.tpl) for sub in anim)
    stitched = len(subs) - len(shots)
    print(f"  director's plan: " + _y(f"{len(anim)} animated clips")
          + _d(f" ({total}s ≈ {total * CREDITS_PER_CLIP_SEC:.0f} cr) + ")
          + _g(f"{kb} free Ken Burns") + _d(" stills")
          + (_d(f"  (+{stitched} stitched sub-shot(s) across multi-shot beats)")
             if stitched else ""))
    i = 0
    while i < len(shots):
        shot = shots[i]
        dur = make_video.clip_duration(shot, s.tpl)
        if shot["clip"].exists():
            md = _clip_mode(shot["seg"], s.tpl)
            if make_video.clip_stale(shot, s.folder, s.tpl):
                print(_y(f"  ⭐ clip{i}: the prompt that generated it has "
                         "changed since — [r]egen to match the current plan"))
            c = ask(f"  clip{i} [{shot['label']}] (cached, seedance {md}): "
                    f"[a]ccept / [v]iew / [r]egen "
                    f"(~{dur * CREDITS_PER_CLIP_SEC:.0f} cr) / [e] note / "
                    "[p] edit prompt / [m] model:",
                    {"a": "keep it", "v": "watch it",
                     "r": "delete + regenerate",
                     "e": "add a note (action, lighting, movement, camera — "
                          "anything), then regenerate",
                     "p": "see/replace the EXACT video prompt (verbatim)",
                     "m": f"switch to seedance {'fast' if md=='std' else 'std'} "
                          "+ regenerate"},
                    free="note for this clip — action, lighting, movement, "
                         "camera, anything")
            if c == "a":
                i += 1
            elif c == "v":
                _open(shot["clip"])
            elif c == "p":
                if _edit_full_prompt(s, i, "video"):
                    s.bump("clips")
                    _invalidate(s.folder, motion_idx=i)
                shots, script = _shots(s)
            elif c == "m":
                nm = _toggle_clip_mode(s, i)
                print(_g(f"  model → seedance {nm}") + _d(" — regenerating"))
                shots, script = _shots(s)
            else:   # [r], [e], or typed prose
                if c != "r":
                    cur = script["segments"][i].get("motion_extra")
                    if cur:
                        print(_d(f"  current note (REPLACED, not added to): "
                                 f"{cur}"))
                    extra = c if len(c) > 1 else ask_long(
                        "  note for this clip — action, lighting, movement, "
                        "camera ('-' clears it): ")
                    if not extra:
                        continue
                    _set_tweak(script, s.folder, i, "motion_extra", extra)
                    if extra.strip() != "-":
                        _remember(s, "clip", extra)
                s.bump("clips")
                _invalidate(s.folder, motion_idx=i)
                # regenerate NOW and re-show — don't fall through to the
                # generate/skip prompt (which would advance on a still shot)
                print(_d(f"  regenerating clip{i}…"))
                if make_video.step_clip(shot, s.folder, s.tpl):
                    _open(shot["clip"])
                else:
                    print(_y("  ! regeneration failed (often moderation) — "
                             "retry, tweak the note, or [a]ccept ken-burns"))
            continue
        if not _animate(shot["seg"]):
            c = ask(f"  clip{i} [{shot['label']}] — director: "
                    + _g("Ken Burns still (free)")
                    + ".  [a]ccept the Ken Burns / [g]enerate a clip anyway "
                    f"(~{dur * CREDITS_PER_CLIP_SEC:.0f} cr):",
                    {"a": "keep the free Ken Burns and continue (recommended)",
                     "g": "spend credits to animate this shot after all — "
                          "with a DYNAMIC brief, not a zoom"})
            if c == "a":
                i += 1
                continue
            # [g]: animate this still after all — make sure there's a DYNAMIC
            # clip brief (not the gentle keyframe push) before we spend
            if _ensure_clip_motion(s, i):
                shots, script = _shots(s)
                shot = shots[i]
        # animated shots AND forced ken-burns shots both land here: video
        # generation is billed per attempt — show the DYNAMIC brief and let the
        # creator enrich it BEFORE spending, to nail it in one run
        import textwrap
        # the EXACT prompt that will be sent to the video model (single source
        # of truth — make_video.clip_prompt), so you see and can edit what's
        # actually generated before spending
        full = make_video.clip_prompt(shot, s.tpl)
        overridden = bool((shot["seg"].get("clip_prompt_text") or "").strip())
        mode = _clip_mode(shot["seg"], s.tpl)
        print("\n  " + _cy("video prompt") + _d(" (sent to the model"
              + (", EDITED verbatim" if overridden else "") + "):"))
        for ln in textwrap.wrap(full, width=72):
            print("    " + _d(ln))
        print("  model: " + _y(f"seedance {mode}")
              + _d("  (fast = cheap/quick · std = full quality)"))
        # clip_voice: for a segment a CHARACTER speaks, let the CLIP deliver the
        # line on-camera (lip-synced to the voiceover, passed as a voice
        # reference) instead of a separate voiceover track
        speaks = make_video._seg_speaks(shot["seg"])
        cv_on = bool(shot["seg"].get("clip_voice"))
        if speaks:
            print("  " + (_g("speech: the CLIP speaks the line (lip-synced to "
                             "the voice)") if cv_on else
                          _d("speech: separate voiceover over the clip")))
        keys = {"g": "looks complete — spend the credits and animate",
                "e": "add a note to the brief (action, lighting, subject, "
                     "camera), then regenerate the brief",
                "p": "edit the FULL prompt directly (sent verbatim)",
                "m": f"switch model to seedance {'fast' if mode=='std' else 'std'}",
                "s": "no clip — assemble zoom-pans the keyframe instead"}
        if speaks:
            keys["c"] = ("turn OFF clip speech (use a separate voiceover)"
                         if cv_on else
                         "let the CLIP speak this line on-camera, lip-synced "
                         "to the voice (the voice is passed to the model)")
        c = ask(f"  clip{i} [{shot['label']}] — {dur}s ≈ "
                + _y(f"{dur * CREDITS_PER_CLIP_SEC:.0f} cr") +
                ":  [g]enerate / [e] note / [p] edit prompt / [m] model "
                + _d(f"({mode})") + " / [s]kip"
                + (" / [c] " + ("voiceover" if cv_on else "speak-in-clip")
                   if speaks else "") + ":",
                keys,
                free="extra detail for the brief — the more specific, the "
                     "better the first take")
        if c == "p":
            _edit_full_prompt(s, i, "video")
            shots, script = _shots(s)
            shot = shots[i]
            continue          # re-show the (possibly edited) prompt
        if c == "m":
            nm = _toggle_clip_mode(s, i)
            print(_g(f"  model → seedance {nm}"))
            shots, script = _shots(s)
            shot = shots[i]
            continue
        if c == "c" and speaks:
            _toggle_clip_voice(s, i, not cv_on)
            if not cv_on and not config.art(s.folder, f"seg{i}.mp3").exists():
                print(_y("  ! no voiceover for this segment yet — run the voice "
                         "stage first so it can drive the clip"))
            shots, script = _shots(s)
            shot = shots[i]
            continue          # re-show the brief with the new speech setting
        if c == "s":
            i += 1
            continue
        if c == "e" or len(c) > 1:
            cur = shot["seg"].get("motion_extra")
            if cur:
                print(_d(f"  current note (REPLACED): {cur}"))
            extra = c if len(c) > 1 else ask_long(
                "  extra detail (action, lighting, subject, camera; "
                "'-' clears): ")
            if extra:
                _set_tweak(script, s.folder, i, "motion_extra", extra)
                if extra.strip() != "-":
                    _remember(s, "clip", extra)
            continue          # re-show the enriched brief before spending
        if make_video.step_clip(shot, s.folder, s.tpl):
            _open(shot["clip"])
            # loop back to the cached-clip menu for accept/regen/tweak
        else:
            c = ask(f"  clip{i} failed (often a moderation false-positive): "
                    f"[r]etry / [e] add a note + retry / [k] accept "
                    f"ken-burns fallback:",
                    {"r": "try again as-is",
                     "e": "add a note (action, lighting, movement…), retry",
                     "k": "continue without a clip (free zoompan)"})
            s.bump("clips")
            if c == "e":
                extra = ask_text("  note for this clip (action, lighting, "
                                 "movement, camera): ")
                if extra:
                    _set_tweak(script, s.folder, i, "motion_extra", extra)
            elif c == "k":
                i += 1
    _ensure_subshot_clips(s)


def _generate_music(s: Session) -> bool:
    """Generate a from-scratch royalty-free track with ElevenLabs from an
    LLM-written (editable) brief, PREVIEW it, regenerate/edit until happy, and on
    accept pin it as the run's music (music_source=elevenlabs, cached as
    <run>/music.mp3) with an option to save it to assets/music for reuse. Returns
    True if a track was accepted. No-op (False) without ELEVENLABS_API_KEY."""
    import elevenlabs
    ch = config.channel()
    if not elevenlabs.available():
        print(_y("  ElevenLabs music needs ELEVENLABS_API_KEY (not set) — export "
                 "it and retry, or pick a local track."))
        return False
    audio = _read_json(s.folder, "audio.json") or {}
    total = float(audio.get("total") or 0) or 90.0
    try:
        import creative
        brief = creative.music_brief(s.topic)
    except Exception as e:                       # noqa: BLE001
        print(_y(f"  brief generation skipped ({e}); type your own"))
        brief = ""
    cached = s.folder / "music.mp3"
    while True:
        print(_cy("\n  music brief") + _d("  (sent to ElevenLabs Music):"))
        for ln in __import__("textwrap").wrap(brief or "(empty — edit it)", 74):
            print("    " + _d(ln))
        c = ask("  [g]enerate a track with this brief / [e]dit the brief / "
                "[c]ancel:",
                {"g": "generate the track now (~10-20s)",
                 "e": "rewrite the brief", "c": "cancel — no change"},
                globals_on=False)
        if c == "c":
            return False
        if c == "e":
            new = ask_long("  new brief (one line, or paste multi-line; "
                           "empty line sends):\n")
            if new:
                brief = new
            continue
        print(_d("  generating…"))
        if not elevenlabs.music(brief or "subtle modern cinematic underscore, "
                                "soft pads, gentle low pulse, no vocals, sits "
                                "under narration", cached, total,
                                instrumental=True):
            print(_y("  generation failed — try a different brief"))
            continue
        _preview_music(cached)
        d = ask("  [a]ccept & use it / [r]egenerate (new take) / [e]dit brief / "
                "[c]ancel:",
                {"a": "use this track", "r": "another take, same brief",
                 "e": "change the brief & regenerate", "c": "discard it"},
                globals_on=False)
        _stop_music()
        if d == "a":
            ov = _read_json(ch.overrides.parent, ch.overrides.name) or {}
            ov["music_source"] = "elevenlabs"
            ov["music_brief"] = brief
            ov["music"] = "none"          # elevenlabs source ignores `music`
            ch.overrides.write_text(json.dumps(ov, indent=2))
            if ask("  also SAVE this track to assets/music for reuse on other "
                   "videos? [y]/[n]:",
                   {"y": "save a copy", "n": "this run only"},
                   globals_on=False) == "y":
                ch.music_dir.mkdir(parents=True, exist_ok=True)
                dst = ch.music_dir / f"elevenlabs_{s.topic}.mp3"
                shutil.copy(str(cached), str(dst))
                print(_g(f"  saved {dst.name}"))
            if (s.folder / "short.mp4").exists():
                _invalidate(s.folder, music=True)
            print(_g("  music: generated ElevenLabs track (cached as music.mp3)"))
            return True
        if d == "c":
            cached.unlink(missing_ok=True)
            return False
        if d == "e":
            new = ask_long("  new brief:\n")
            if new:
                brief = new
        # r / e → loop and regenerate


def st_music(s: Session):
    ch = config.channel()
    tracks = (sorted(ch.music_dir.glob("*.mp3"))
              if ch.music_dir.exists() else [])
    ov = _read_json(ch.overrides.parent, ch.overrides.name) or {}
    current = ov.get("music") or "none"
    cur_start = int(ov.get("music_start_seg", 0) or 0)
    assembled = (s.folder / "short.mp4").exists()
    # music OFF = rely on the platform's own audio (no baked track)
    if not s.tpl.get("music", True):
        c = ask("  music is OFF — relying on platform audio. "
                "[k]eep off / [o] turn music on:",
                {"k": "keep music off (use the platform's music/audio)",
                 "o": "turn background music back on"}, globals_on=False)
        if c != "o":
            return
        s.tpl = _pin_template(s.folder, music=True)
        print(_g("  music on"))
    if not tracks:
        print(f"  no tracks in channels/{ch.name}/assets/music/ — "
              f"[g]enerate one with ElevenLabs / [s] skip music (platform "
              f"audio) / [k]eep:")
        c = ask("  choose:", {"g": "generate a royalty-free track with ElevenLabs",
                              "s": "pin music OFF (rely on platform audio)",
                              "k": "leave as-is"}, globals_on=False)
        if c == "g":
            _generate_music(s)
        elif c == "s":
            s.tpl = _pin_template(s.folder, music=False)
            print(_g("  music pinned OFF — platform audio"))
        return
    print(f"  current choice: {current}"
          + (_d(f"  (from segment {cur_start})") if current != "none"
             and cur_start else "")
          + (_d("  (already assembled — a change re-assembles the cut)")
             if assembled else ""))
    for i, t in enumerate(tracks, 1):
        print(f"  {i}. {t.name}" + (_g("  ← current") if t.name == current
                                    else ""))
    print(_d("   [p]N preview a track (plays in the background)"))
    try:
        while True:
            c = ask("  track number / [p]N preview / [g]enerate (ElevenLabs) / "
                    "[n]one / [s]kip (platform audio) / [k]eep current:",
                    {"#": "use that track (7% volume, faded)",
                     "p": "preview track N in the background",
                     "g": "generate a NEW royalty-free track with ElevenLabs",
                     "n": "no background music this run",
                     "s": "pin music OFF — rely on the platform's own audio",
                     "k": "keep the current choice"})
            if c == "k":
                return
            if c == "g":
                _stop_music()
                if _generate_music(s):
                    return
                continue
            if c == "s":
                _stop_music()
                s.tpl = _pin_template(s.folder, music=False)
                print(_g("  music pinned OFF — relying on platform audio"))
                return
            if c[:1].lower() == "p" and c[1:].strip().isdigit():
                j = int(c[1:].strip())
                if 1 <= j <= len(tracks):
                    _preview_music(tracks[j - 1])
                else:
                    print("    no such track")
                continue
            if c == "n":
                newmusic = "none"
                break
            if c.isdigit() and 1 <= int(c) <= len(tracks):
                newmusic = tracks[int(c) - 1].name
                break
            print("    no such track")
    finally:
        _stop_music()
    # where does the music come in? (fades in at the chosen segment's start)
    new_start = cur_start
    if newmusic != "none":
        segs = (_read_json(s.folder, "script.json") or {}).get("segments", [])
        if segs:
            print(_cy("\n  start the music from which segment?")
                  + _d("  (it fades in there — 0 = under the whole video)"))
            for i, seg in enumerate(segs):
                print(f"   {i}. {seg.get('label', '')}"
                      + (_g("  ← current") if i == cur_start else ""))
            a = ask_text(f"  segment number [Enter keeps {cur_start}]: ").strip()
            if a.isdigit() and 0 <= int(a) < len(segs):
                new_start = int(a)
    ov["music"] = newmusic
    ov["music_start_seg"] = new_start
    ch.overrides.write_text(json.dumps(ov, indent=2))
    print(f"  music: {newmusic}"
          + (f", from segment {new_start}" if newmusic != "none" and new_start
             else ""))
    if (newmusic != current or new_start != cur_start) and assembled:
        _invalidate(s.folder, music=True)     # re-assemble with the new track
        print(_d("  the cut will re-assemble with the new music next stage"))


def _ai_available(s: Session) -> bool:
    """Whether an AI review can run (enabled + a creative backend present)."""
    import llm
    return bool(int(s.tpl.get("ai_review_iters", 3)) and llm.available())


def _ai_review(s: Session, confirm=None):
    """Run the post-assembly AI QC loop and return its result so the caller can
    present the verdict + the remaining notes. Keeps every iteration.

    confirm: a `(iter_n, review) -> bool` callback the loop asks BEFORE each
    repair re-roll (so the AI never regenerates/re-renders without the creator
    agreeing). None = auto-apply (used by the headless/auto path)."""
    import llm
    iters = int(s.tpl.get("ai_review_iters", 3))
    if not iters or not llm.available():
        return None
    import evaluator
    print(_cy("\n  AI review — looking at the assembled video "
              "(a few model calls)…"))
    try:
        return evaluator.review_and_repair(s.topic, max_iters=iters,
                                           confirm=confirm)
    except Exception as e:
        print(_y(f"  ! AI review skipped: {e}"))
        return None


def _review_only(n: int, review: dict) -> bool:
    """A confirm that always declines the auto re-roll — the AI reviews and
    reports, but never regenerates on its own. Used by the browser UI (which
    can't show a blocking terminal prompt and offers a manual apply button)."""
    return False


def _ai_repair_prompt(n: int, review: dict) -> bool:
    """Terminal confirm for an AI repair re-roll: show what the AI wants to
    change (and its credit cost) and ask before regenerating + re-rendering."""
    print()
    print("  " + _y(f"AI review #{n}: ") + (review.get("summary") or ""))
    sc = review.get("scores") or {}
    if sc:
        print("  " + _d("  ·  ".join(f"{k} {v}/10" for k, v in sc.items())))
    fixes = review.get("fixes") or []
    for fx in fixes:
        print(_d(f"    - {fx.get('segment')}: {fx.get('problem')}"))
    # the auto-loop only ever regenerates ken-burns keyframes / reassembles
    kf = sum(1 for fx in fixes
             if (fx.get("action") or "").lower() == "keyframe")
    cost = (f" (~{kf * 2} credits to regen {kf} keyframe"
            f"{'s' if kf != 1 else ''})" if kf else " (free reassemble)")
    c = ask("  apply the AI's fixes and re-render this cut?" + _y(cost),
            {"y": "yes — let the AI regenerate + re-render",
             "n": "no — keep this cut as-is (you can apply notes yourself)"},
            globals_on=False)
    return c == "y"


def _apply_ai_notes(s: Session, notes: list, extra: str = "") -> bool:
    """Act on the AI's notes, routing each to the RIGHT artifact (so a chart or
    caption note never re-rolls the keyframe):
    - chart   → move / change-kind / remove the data-chart overlay (free).
    - overlay → re-render the on-screen text overlay (free).
    - keyframe (or any note the creator attaches a comment to) → regenerate the
      base keyframe (and the clip, for animated shots) with the AI instruction
      merged with the creator's `extra`. User-initiated, so clip regens (which
      cost credits) are allowed here.
    The caller reassembles afterwards."""
    ch = config.channel()
    script = _read_json(s.folder, "script.json")
    audio = _read_json(s.folder, "audio.json")
    facts = _read_json(s.folder, "facts.json")
    # label→index over the SCRIPT's real segments (writer's order; an optional
    # segment may be absent), not the channel's static label list
    labels = [seg.get("label") for seg in script["segments"]]
    shots = make_video.shot_list(script, audio, s.folder)
    extra = (extra or "").strip()
    did = False
    for fx in notes:
        act = (fx.get("action") or "").lower()
        lbl = fx.get("segment")
        if lbl not in labels:
            continue
        i = labels.index(lbl)
        seg = script["segments"][i]
        if act == "overlay":
            make_video.archive(s.folder, f"text{i}.png")
            print(_d(f"  re-rendering the on-screen text on {lbl}"))
            did = True
            continue
        # keyframe note (or any note when the creator attached a comment) →
        # regenerate the base image
        if act != "keyframe" and not extra:
            continue
        instr = " ".join(p for p in ((fx.get("instruction") or "").strip(),
                                     extra) if p)
        if instr:
            seg["visual_extra"] = instr
            _write_json(s.folder, "script.json", script)
        make_video.archive(s.folder, f"key{i}.png",
                           f"text{i}.png", f"clip{i}.mp4")
        print(_d(f"  regenerating keyframe {i} [{lbl}]"
                 + (" per the AI note + your comments…" if extra
                    else " per the AI note…")))
        make_video.step_keyframe(shots[i], facts, s.folder, s.tpl)
        if seg.get("animate", True):
            print(_d(f"  regenerating clip {i} (costs credits)…"))
            make_video.step_clip(shots[i], s.folder, s.tpl)
        did = True
    if not did:
        print(_d("  the AI only suggested a reassemble — rebuilding"))
    return did


def _reveal_label(seg: dict, tpl: dict, i: int) -> str:
    ov = (seg.get("overlay") or "").strip()
    if not ov:
        return _d("text: —")
    frac = seg.get("overlay_reveal_at")
    if frac is None:
        default = 0.0 if i == 0 else float(tpl.get("overlay_reveal_at", 0.6))
        return _d(f"text@ {int(default * 100)}% (default)")
    return _d(f"text@ {int(float(frac) * 100)}%")


_CLIP_FILLS = ("slow", "loop", "boomerang", "freeze")


def _clip_fill(seg: dict, tpl: dict) -> str:
    f = str(seg.get("clip_fill") or tpl.get("clip_fill", "slow")).lower()
    if f in ("pingpong", "ping-pong", "loop-reverse", "bounce"):
        f = "boomerang"
    elif f in ("playstop", "play-stop", "play_stop", "hold", "once", "stop"):
        f = "freeze"
    return f if f in _CLIP_FILLS else "slow"


def _fill_label(seg: dict, folder: Path, i: int, tpl: dict) -> str:
    """Clip-fill mode for an animated segment that has a generated clip."""
    if not config.art(folder, f"clip{i}.mp4").exists():
        return _d("clip: —")
    f = _clip_fill(seg, tpl)
    return (_d("clip: slow") if f == "slow" else _y(f"clip: {f}"))


def _pct_label(v) -> str:
    """Display a stored overlay start/end (fraction 0-1, or seconds >1, or
    None) for the editor."""
    if v is None:
        return "default"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "default"
    return f"{int(round(v * 100))}%" if v <= 1.0 else f"{v:.1f}s"


def _parse_pct(raw: str):
    """Parse a typed start/end: blank -> None (use default/hold); 0-100 -> a
    fraction of the segment. Returns (ok, value)."""
    raw = (raw or "").strip().rstrip("%")
    if raw == "":
        return True, None
    try:
        return True, round(max(0.0, min(float(raw), 100.0)) / 100.0, 3)
    except ValueError:
        return False, None


def _overlay_prompt(existing: dict):
    """Prompt for one overlay's text / position / start% / end%. Returns the
    spec dict, or None if cancelled (empty text)."""
    cur = existing or {}
    txt = ask_text(f"  text{(' [' + cur['text'] + ']') if cur.get('text') else ''}: ")
    txt = (txt or cur.get("text", "")).strip()
    if not txt:
        print(_y("  empty text — cancelled"))
        return None
    pos = ask_text("  position — top/middle/bottom [+ left/center/right] "
                   f"[{cur.get('pos') or 'top-center'}]: ").strip()
    ok, st = _parse_pct(ask_text(
        f"  start at % of segment (blank = default) "
        f"[{_pct_label(cur.get('start'))}]: "))
    ok2, en = _parse_pct(ask_text(
        f"  end at % of segment (blank = hold to end) "
        f"[{_pct_label(cur.get('end'))}]: "))
    o = {"text": txt}
    if pos:
        o["pos"] = pos
    elif cur.get("pos"):
        o["pos"] = cur["pos"]
    if st is not None:
        o["start"] = st
    elif ok and cur.get("start") is not None and ask_text(
            "  keep existing start? [y]/n: ").lower() not in ("n", "no"):
        o["start"] = cur["start"]
    if en is not None:
        o["end"] = en
    elif ok2 and cur.get("end") is not None and ask_text(
            "  keep existing end? [y]/n: ").lower() not in ("n", "no"):
        o["end"] = cur["end"]
    return o


def _save_overlays(s: Session, script: dict, i: int, ovs: list):
    """Write a segment's overlay list back; keep the legacy single `overlay`
    in sync with the first one (thumbnail/hook read it)."""
    seg = script["segments"][i]
    if ovs:
        seg["overlays"] = ovs
        seg["overlay"] = ovs[0]["text"]
    else:
        seg.pop("overlays", None)
        seg.pop("overlay", None)
    _write_json(s.folder, "script.json", script)


def _edit_overlays(s: Session, i: int) -> bool:
    """Add / edit / remove a segment's on-screen overlays — each with its own
    text, position and time window (start%→end%). Returns True if changed."""
    changed = False
    while True:
        script = _read_json(s.folder, "script.json")
        seg = script["segments"][i]
        lbl = seg.get("label")
        ovs = make_video._seg_overlays(seg)
        print(_cy(f"\n  {lbl} — on-screen overlays"))
        if not ovs:
            print(_d("   (none yet)"))
        for k, o in enumerate(ovs):
            win = (f"{_pct_label(o.get('start'))}→"
                   f"{'end' if o.get('end') is None else _pct_label(o.get('end'))}")
            print(f"   {_cy(str(k))} \"{o.get('text', '')}\"  "
                  + _d(f"[{o.get('pos') or 'top-center'}]  {win}"))
        c = ask("  [a]dd overlay / number to edit / [b]ack:",
                {"a": "add a new overlay", "#": "edit/remove that overlay",
                 "b": "back"}, globals_on=False)
        if c == "b":
            return changed
        if c == "a":
            o = _overlay_prompt({})
            if o:
                _save_overlays(s, script, i, [dict(x) for x in ovs] + [o])
                changed = True
            continue
        if not c.isdigit() or not (0 <= int(c) < len(ovs)):
            print(_y("  no such overlay"))
            continue
        k = int(c)
        act = ask(f"  overlay {k}: [e]dit / [x] remove / [b]ack:",
                  {"e": "edit it", "x": "remove it", "b": "back"},
                  globals_on=False)
        if act == "x":
            new = [dict(x) for j, x in enumerate(ovs) if j != k]
            _save_overlays(s, script, i, new)
            print(_y(f"  removed overlay {k}"))
            changed = True
        elif act == "e":
            o = _overlay_prompt(ovs[k])
            if o:
                new = [dict(x) for x in ovs]
                new[k] = o
                _save_overlays(s, script, i, new)
                changed = True


def _edit_segment_overlay(s: Session, i: int, text_global: bool) -> bool:
    script = _read_json(s.folder, "script.json")
    seg = script["segments"][i]
    lbl = seg.get("label")
    opts = {}
    has_clip = config.art(s.folder, f"clip{i}.mp4").exists()
    cur_fill = _clip_fill(seg, s.tpl)
    nxt_fill = _CLIP_FILLS[(_CLIP_FILLS.index(cur_fill) + 1) % len(_CLIP_FILLS)]
    opts["o"] = "edit on-screen overlays (add/remove, position, start→end)"
    if make_video._seg_overlays(seg):
        opts["t"] = "set the DEFAULT text reveal time (overlays w/o their own)"
    if has_clip:
        opts["l"] = (f"clip fill {cur_fill} → {nxt_fill}  (slow=slow-to-fit, "
                     "loop=native repeat, boomerang=forward+reverse, "
                     "freeze=play once then a slow ken-burns drift on the last "
                     "frame)")
        native_on = bool(seg.get("native_audio"))
        full_on = bool(seg.get("full_clip"))
        mix_on = bool(seg.get("mix_clip_audio"))
        opts["n"] = ("use the TTS voiceover again" if native_on
                     else "use the CLIP'S OWN audio (skip TTS for this segment)")
        opts["f"] = ("trim to the voiceover again" if full_on
                     else "let the WHOLE clip play out (don't trim to the VO)")
        opts["x"] = ("stop mixing the clip's audio" if mix_on
                     else "MIX the clip's own audio (sfx/ambience) UNDER the "
                     "voiceover — segment runs the longer of clip/VO")
    opts["b"] = "back"
    pick = ask(f"  {lbl} — "
               + "[o] overlays / "
               + ("[t] default timing / " if ("t" in opts) else "")
               + (f"[l] clip fill ({cur_fill}) / " if has_clip else "")
               + (f"[n] native audio ({'on' if seg.get('native_audio') else 'off'}) / "
                  if has_clip else "")
               + (f"[f] full clip ({'on' if seg.get('full_clip') else 'off'}) / "
                  if has_clip else "")
               + (f"[x] mix clip sfx ({'on' if seg.get('mix_clip_audio') else 'off'}) / "
                  if has_clip else "")
               + "[b]ack:", opts, globals_on=False)
    if pick == "b":
        return False
    changed = False
    if pick == "o":
        return _edit_overlays(s, i)
    if pick == "n" and has_clip:
        new = not seg.get("native_audio")
        if new:
            seg["native_audio"] = True
            seg.pop("full_clip", None)        # native already plays the clip out
        else:
            seg.pop("native_audio", None)
        _write_json(s.folder, "script.json", script)
        # the audio SOURCE changed — drop this seg's TTS + the cut, refresh voice
        _unlink(s.folder, [f"seg{i}.mp3"] + list(ASSEMBLY)
                + ["virality.md", "report.md", "critique.md"])
        kalinga.run_voice(s.topic)            # rebuild audio.json for the flag
        print((_g("  native clip audio ON for ") + lbl
               + _d("  (the clip's own sound; no TTS, full clip plays)"))
              if new else _y("  back to the TTS voiceover for " + lbl))
        return True
    if pick == "f" and has_clip:
        new = not seg.get("full_clip")
        if new:
            seg["full_clip"] = True
        else:
            seg.pop("full_clip", None)
        _write_json(s.folder, "script.json", script)
        print((_g("  full clip plays out for ") + lbl
               + _d("  (length = the whole clip, VO underneath)"))
              if new else _y("  trimmed to the voiceover for " + lbl))
        return True
    if pick == "x" and has_clip:
        new = not seg.get("mix_clip_audio")
        if new:
            seg["mix_clip_audio"] = True
            seg.pop("native_audio", None)     # mixing needs the TTS VO on top
        else:
            seg.pop("mix_clip_audio", None)
        _write_json(s.folder, "script.json", script)
        # we now need the TTS VO present (native may have skipped it) + a
        # re-assemble; run_voice regenerates it when the audio source changed
        _unlink(s.folder, list(ASSEMBLY)
                + ["virality.md", "report.md", "critique.md"])
        kalinga.run_voice(s.topic)
        print((_g("  clip sfx MIXED under the voiceover for ") + lbl
               + _d("  (ducked; segment runs the longer of clip/VO, video "
                    "loops/boomerangs to fill)"))
              if new else _y("  clip sfx mix OFF for " + lbl))
        return True
    if pick == "l" and has_clip:
        seg["clip_fill"] = nxt_fill
        _write_json(s.folder, "script.json", script)
        print(_y(f"  clip fill for {lbl}: {nxt_fill}"))
        return True
    if pick == "t":
        if not text_global:
            print(_y("  note: text reveals are globally OFF (the overlay is "
                     "baked into the keyframe) — this timing applies once you "
                     "turn reveals on with [o]"))
        raw = ask_text("  reveal at what % of the segment? "
                       "(0 = immediately, e.g. 60; Enter = default): ")
        if raw == "":
            if seg.pop("overlay_reveal_at", None) is not None:
                changed = True
            print(_d("  reset to the default timing"))
        else:
            try:
                pct = max(0.0, min(float(raw), 100.0))
                seg["overlay_reveal_at"] = round(pct / 100.0, 3)
                changed = True
                print(_g(f"  {lbl} text reveals at {int(pct)}%"))
            except ValueError:
                print(_y("  not a number — unchanged"))
    if changed:
        _write_json(s.folder, "script.json", script)
    return changed


def _st_segment_overlays(s: Session) -> bool:
    """Per-segment overlay control: set when each segment's on-screen text
    reveal starts and tune its clip fill / audio. The caller reassembles.
    Returns True if anything was changed (so the caller can skip a no-op
    reassemble on a pure browse)."""
    text_global = bool(s.tpl.get("reveal_overlay_text", True)
                       and s.tpl.get("assemble_overlays", True))
    any_changed = False
    while True:
        script = _read_json(s.folder, "script.json")
        segs = script["segments"]
        print(_cy("\n  per-segment overlays")
              + _d(f"  (text reveals {'ON' if text_global else 'OFF'})"))
        for i, seg in enumerate(segs):
            print(f"   {_cy(str(i))} {(seg.get('label') or ''):<9} "
                  + _reveal_label(seg, s.tpl, i)
                  + "   " + _fill_label(seg, s.folder, i, s.tpl))
        c = ask("  pick a segment number to edit / [b]ack:",
                {"#": "edit that segment", "b": "back to assemble"},
                globals_on=False)
        if c == "b":
            return any_changed
        i = int(c)
        if not (0 <= i < len(segs)):
            print(_y("  no such segment"))
            continue
        if _edit_segment_overlay(s, i, text_global):
            any_changed = True


def st_assemble(s: Session):
    while True:
        kalinga.run_assemble(s.topic)
        _open(s.folder / "short.mp4")
        # the AI review runs ONLY when asked ([v]); show a prior result if it's
        # still valid (any re-assemble invalidates ai_review.json)
        review = _read_json(s.folder, "ai_review.json")
        ov = s.tpl.get("assemble_overlays", True)
        cap = s.tpl.get("assemble_captions", True)
        sp = s.tpl.get("audio_speed", 1.0)
        dur = make_video.ffprobe_duration(s.folder / "short.mp4")
        print("  " + _y(f"{dur:.0f}s")
              + _d(f" · subtitles {'ON' if cap else 'OFF'}"
                   f" · overlays {'ON' if ov else 'OFF (plain cut)'}"
                   f" · voice {sp}x"))
        notes = (review or {}).get("fixes") or []
        if review:
            if review.get("passed"):
                print("  " + _g(f"✓ AI review passed (iteration "
                                f"{review['iters']})"))
            else:
                print("  " + _y(f"⚠ AI review after {review['iters']} tries: ")
                      + (review.get("summary") or ""))
            sc = review.get("scores") or {}
            if sc:
                print("  " + _d("  ·  ".join(f"{k} {v}/10"
                                             for k, v in sc.items())))
            for fx in notes:
                print(_d(f"    - {fx.get('segment')}: {fx.get('problem')}"))
            vers = sorted(s.folder.glob("short_v*.mp4"))
            if len(vers) > 1:
                print(_d("  versions kept: " + ", ".join(v.name
                                                         for v in vers)))
        ai_on = _ai_available(s)
        opts = {"a": "the video is right — continue"}
        if ai_on:
            opts["v"] = ("run the AI review on this cut now (asks before any "
                         "re-roll; never changes anything on its own)")
        if review:
            opts["k"] = ("agree with the AI's review — save it as a learned "
                         "standard (future reviews/scripts apply it)")
        if notes:
            opts["i"] = ("regenerate the flagged shots using the AI's notes "
                         "(you can add your own comments)")
        opts.update({"g": "per-segment overlays: set each text reveal's start "
                          "time, tune each clip's fill/audio",
                     "t": "toggle SUBTITLES (burned-in captions) on/off",
                     "o": "toggle the on-screen text overlays on/off",
                     "s": "set voiceover speed (1 / 1.25 / 1.5 / 2x)",
                     "m": "change the background music",
                     "r": "drop the cut and assemble again"})
        c = ask("  [a]ccept the cut"
                + ("  / [v] AI review" if ai_on else "")
                + ("  / [k] reinforce AI review" if review else "")
                + ("  / [i] regen flagged shots (AI notes)" if notes else "")
                + " / [g] per-segment overlays"
                + " / [t] subtitles " + ("off" if cap else "on")
                + " / [o] overlays " + ("off" if ov else "on")
                + " / [s]peed / [m]usic / [r]e-assemble:", opts)
        if c == "a":
            return
        if c == "v" and ai_on:
            # explicit, on-demand review (and gated repair); loop back to show it
            _ai_review(s, confirm=_ai_repair_prompt)
            continue
        if c == "k" and review:
            # reinforcement: the creator agrees with the AI's critique, so
            # bank it as a standard the critics/writers will hold to
            note = review.get("summary", "")
            for fx in notes:
                note += f"; {fx.get('segment')}: {fx.get('problem')}"
            _remember(s, "review standard (creator-endorsed)", note)
            print(_g("  reinforced — future reviews will hold this standard"))
            continue
        s.bump("assemble")
        if c == "i" and notes:
            extra = ask_long("  add your own comments to the regen "
                             "(Enter to use the AI's notes only): ")
            _apply_ai_notes(s, notes, extra)
            if extra:
                _remember(s, "review standard (creator-endorsed)", extra)
        elif c == "g":
            if not _st_segment_overlays(s):
                continue           # nothing changed — skip the no-op reassemble
        elif c == "t":
            s.tpl = _pin_template(s.folder, assemble_captions=not cap)
        elif c == "o":
            s.tpl = _pin_template(s.folder, assemble_overlays=not ov)
        elif c == "s":
            pick = ask("  voice speed: [1] 1x / [2] 1.25x / [3] 1.5x / "
                       "[4] 2x:",
                       {"1": "normal", "2": "1.25x", "3": "1.5x",
                        "4": "2x (punchy; shortens the video)"},
                       globals_on=False)
            s.tpl = _pin_template(s.folder, audio_speed={
                "1": 1.0, "2": 1.25, "3": 1.5, "4": 2.0}.get(pick, 1.0))
        elif c == "m":
            _invalidate(s.folder, music=True)
            st_music(s)
        # any change re-conforms the cut and re-runs the review
        _invalidate(s.folder, music=True)


def _set_thumb(folder: Path, text=None, concept=None, elements=None):
    """Persist a thumbnail edit into script.json's direction.thumbnail.
    `elements` is the browser cover editor's custom text layout ([] clears it —
    back to the default ticker/headline composition)."""
    sc = _read_json(folder, "script.json") or {}
    t = sc.setdefault("direction", {}).setdefault("thumbnail", {})
    if text is not None:
        t["text"] = text
    if concept is not None:
        t["concept"] = concept
    if elements is not None:
        t["elements"] = elements
    _write_json(folder, "script.json", sc)


def st_thumbnail(s: Session):
    """The cover image: a bespoke AI background + a tease headline. Editing the
    headline recomposes for FREE (reuses the background); changing the concept
    or regenerating re-renders the background (~2 cr)."""
    _ensure_directed(s)
    facts = _read_json(s.folder, "facts.json")
    out = s.folder / "thumbnail.png"
    if not out.exists():
        print(_d(f"  generating the cover thumbnail "
                 f"(~{CREDITS_PER_KEYFRAME} cr)…"))
        make_video.step_thumbnail(s.folder, facts, s.tpl)
    while True:
        if out.exists():
            _open(out)
        thumb = ((_read_json(s.folder, "script.json") or {})
                 .get("direction") or {}).get("thumbnail") or {}
        print("  headline: " + _b(thumb.get("text") or "(hook overlay)"))
        if thumb.get("concept"):
            print("  " + _d("concept: " + thumb["concept"][:88]))
        c = ask(f"  [a]ccept / [e] edit headline (free) / [v] edit concept + "
                f"regen (~{CREDITS_PER_KEYFRAME} cr) / [r]egen / [o]pen:",
                {"a": "keep this thumbnail",
                 "e": "change just the headline — recomposes on the same "
                      "background, no credits",
                 "v": "change the background concept, then regenerate",
                 "r": "regenerate the background + recompose",
                 "o": "open the thumbnail"})
        if c == "a":
            return
        s.bump("thumbnail")
        if c == "o":
            continue
        if c == "e":
            new = ask_text("  new headline (2-4 words, teases the question): ")
            if not new:
                continue
            _set_thumb(s.folder, text=new)
            out.unlink(missing_ok=True)         # keep thumb_bg → free recompose
            make_video.step_thumbnail(s.folder, facts, s.tpl)
            _remember(s, "thumbnail", new)
        elif c == "v":
            new = ask_long("  new background concept: ")
            if new:
                _set_thumb(s.folder, concept=new)
                _remember(s, "thumbnail", new)
            _unlink(s.folder, ["thumb_bg.png", "thumbnail.png"])
            make_video.step_thumbnail(s.folder, facts, s.tpl)
        elif c == "r":
            _unlink(s.folder, ["thumb_bg.png", "thumbnail.png"])
            make_video.step_thumbnail(s.folder, facts, s.tpl)


def _show_seo(s: Session):
    meta = _read_json(s.folder, "seo.json")
    print(f"\n  title ({len(meta['title'])} chars): {meta['title']}")
    print("  description:")
    for line in meta["description"].splitlines():
        print(f"    {line}")
    print(f"  hashtags: {' '.join(meta['hashtags'])}")
    print(f"  tags: {', '.join(meta['tags'])}")
    issues = validate.seo_lint(s.folder, s.topic)
    print("  lint: " + ("; ".join(issues) if issues else "clean ✓"))


def st_seo(s: Session):
    import seo
    make_video.step_seo(s.topic, s.folder)
    while True:
        _show_seo(s)
        c = ask("  [a]ccept metadata / [r]egenerate / [e] feedback / "
                "[o]pen seo.json in $EDITOR:",
                {"a": "metadata is good — continue",
                 "r": "regenerate from scratch",
                 "e": "compose notes — regenerate applying them",
                 "o": "hand-edit (seo.md re-synced afterwards)"},
                free="notes for the SEO writer")
        if c == "a":
            return
        s.bump("seo")
        if c == "e" or len(c) > 1:
            fb = c if len(c) > 1 else ask_long("  notes for the SEO writer: ")
            if fb:
                seo.run(s.topic, fb)
                _remember(s, "seo", fb)
        elif c == "r":
            _unlink(s.folder, ["seo.json", "seo.md"])
            seo.run(s.topic)
        elif c == "o":
            if _editor(s.folder / "seo.json"):
                seo._write_md(s.folder, _read_json(s.folder, "seo.json"))


def st_gates(s: Session):
    import llm
    if not (s.folder / "virality.md").exists():
        try:
            kalinga.run_score(s.topic)
        except make_video.StepFailed as e:
            print(f"  ! scoring skipped: {e}")
    s.result = validate.run(s.topic)
    r = s.result
    print("  tech QC:  " + (_y("; ".join(r["qc"])) if r["qc"]
                            else _g("clean ✓")))
    vir = r["virality"]
    v_col = {"weak": _r, "ok": _g, "strong": _g}.get(vir["status"], _y)
    print(f"  virality: {v_col(vir['status'])} {vir['scores'] or ''} "
          + _d(f"— {vir.get('why', '')}"))
    print("  SEO lint: " + (_y("; ".join(r["seo"])) if r["seo"]
                            else _g("clean ✓")))

    state_f = s.folder / "run_state.json"
    state = (_read_json(s.folder, "run_state.json")
             or {"hook_retries": 0})
    if (vir["status"] == "weak"
            and state["hook_retries"] < s.tpl["max_hook_retries"]
            and llm.available()):
        c = ask(f"  weak hook — [h] rewrite the hook + regenerate seg0 "
                f"(~15–25 cr) / [a]ccept as-is:",
                {"h": "LLM rewrites segment 0; only seg0/key0/clip0/"
                      "assemble/score redo",
                 "a": "ship it with the weak score"})
        if c == "h":
            import creative
            s.bump("gates")
            creative.rewrite_hook(s.topic,
                                  kalinga.hook_feedback(s.result, s.folder))
            make_video.reset_hook(s.folder)
            state["hook_retries"] += 1
            state_f.write_text(json.dumps(state))
            return "voice"           # jump the stage loop back
        # [a] accepted as-is — fall through to the decision point below

    # The video is done and through the gates. Decision point: wrap up (then
    # the upload step) or quit now ([q] / [A] handled globally).
    ok = not r["qc"] and vir["status"] != "weak" and not r["seo"]
    ask("  " + (_g("✓ gates passed") if ok else _y("gates done"))
        + " — [a] wrap up & finalize:",
        {"a": "finalize (wrap): critique, report, learnings, upload bundle"})
    return None                      # next (and last) stage is wrap


def st_wrap(s: Session):
    # don't finalize on entry — wrap is irreversible-ish (marks the queue done,
    # distills learnings into learnings.md, clears session.json), so ASK first.
    c = ask("\n  " + _cy("wrap up this video?")
            + _d("  finalizes it: AI critique → report.md, mark the queue done, "
                 "distill this session's learnings, build the upload bundle, "
                 "clear the session.")
            + "\n  [a] wrap up now / [s] not yet (leave it, stay resumable):",
            {"a": "finalize now (critique, report, queue, learnings, bundle)",
             "s": "don't wrap yet — nothing finalized; rerun resumes here"})
    if c != "a":
        print(_d("  ok — not wrapping. nothing finalized; "
                 "rerun `make` to come back here."))
        raise Quit()
    print("\n  " + _cy("─── wrapping up ───")
          + _d("  finalizing this video — no more generation"))
    print(_d("  → critique: AI looks at the final cut → critique.md"))
    try:
        kalinga.run_critique(s.topic)
    except Exception as e:
        print(f"  ! critique skipped: {e}", file=sys.stderr)
    if s.result is None:
        s.result = validate.run(s.topic)

    sess = _read_json(s.folder, "session.json") or {}
    credits_now = kalinga.hf_credits()
    start = sess.get("credits_start", s.credits_start)
    spent = (start - credits_now
             if start is not None and credits_now is not None else None)
    script = _read_json(s.folder, "script.json") or {}
    state = _read_json(s.folder, "run_state.json") or {"hook_retries": 0}
    meta = {"template": s.tpl["name"],
            "script_mode": script.get("mode"),
            "script_judge": script.get("judge", {}).get("score", "?"),
            "hook_retries": state["hook_retries"],
            "credits": (f"{spent} spent, {credits_now} left"
                        if spent is not None else None)}
    print(_d("  → report: writing the audit trail → report.md"))
    validate.write_report(s.topic, s.result, meta)
    if s.in_queue:
        print(_d(f"  → queue: marking {s.topic} done"))
        daily.mark(s.topic, "done")

    mins = None
    if sess.get("started"):
        mins = int((datetime.now()
                    - datetime.fromisoformat(sess["started"])).seconds / 60)
    print("\n  " + _cy("─── session summary ───"))
    if mins is not None:
        print(f"  duration: {mins} min")
    if spent is not None:
        print(f"  credits:  {spent} spent ({credits_now} left)")
    if s.iterations:
        print("  retries:  " + ", ".join(f"{k} ×{v}" for k, v
                                         in s.iterations.items()))
    import usage
    for ln in usage.summary_lines(credits_now=credits_now):
        print("  " + ln)
    ov = _read_json(config.channel().overrides.parent,
                    config.channel().overrides.name) or {}
    # gather the upload-ready bundle — both cuts + thumbnail + seo.txt, one folder
    upd = None
    if (s.folder / "short.mp4").exists():
        upd = make_video.export_upload(s.folder, s.tpl)
    print("\n  " + _cy("─── upload checklist ───") + "\n"
          + (f"  ⬆ bundle:  {upd}/  "
             + _d("(music + no-music cuts, thumbnail, seo.txt)") + "\n"
             if upd else "")
          + f"  video:    {s.folder / 'short.mp4'}\n"
          f"  metadata: {s.folder / 'seo.md'}"
          + (f"\n  thumb:    {s.folder / 'thumbnail.png'}"
             if (s.folder / "thumbnail.png").exists() else "")
          + (f"\n  music:    credit note in seo.txt (music.txt)"
             if (s.folder / "music.txt").exists() else "")
          + (f"\n  post at:  {ov['posting_time']}"
             if ov.get("posting_time") else "")
          + f"\n  report:   {s.folder / 'report.md'}")
    print(_d("  (everything to upload is in the bundle folder above)"))

    # ── learnings: review EVERYTHING that changed this session and extract
    #    durable lessons from the pattern of edits ──
    print("\n  " + _cy("─── what changed this session ───"))
    changes = _session_changes(s)
    if changes:
        for ln in changes:
            print("  " + ln)
    else:
        print(_d("  (accepted as-is — no edits to learn from)"))

    import creative
    sess = _read_json(s.folder, "session.json") or {}
    since = sess.get("started", "")
    versioned = _versioned_changes(s.folder, since)
    script_changes = _script_changes_plain(s.folder, since)
    # capture any closing note FIRST so it's part of the distillation input
    note = ask_text("  anything ELSE you learned? (Enter to skip): ")
    if note:
        _remember(s, "session", note)

    # only extract when THIS session actually changed something — a no-op rerun
    # of wrap (artifacts already there) must not re-distill the same lessons.
    # The version trail + the real script diff count too, so a resumed session
    # (whose in-memory counters reset) still learns from what it changed.
    # extract_session_learnings DISTILLS the raw edits + the creator's critiques
    # into durable channel learnings — the raw text never lands in learnings.md.
    if s.iterations or s.learned or versioned or script_changes:
        print(_d("  → distilling learnings from this session's edits + "
                 "critiques…"))
        try:
            extracted = creative.extract_session_learnings(
                s.topic, iterations=s.iterations, learned=s.learned,
                versioned=versioned, script_changes=script_changes)
        except Exception as e:                 # noqa: BLE001
            extracted = ""
            print(_y(f"  ! learning extraction skipped: {e}"))
        if extracted:
            print(_g("  ↳ learnings added to "
                     f"channels/{config.channel().name}/learnings.md:"))
            for ln in extracted.splitlines():
                if ln.strip():
                    print(_d("    " + ln.strip()))

    # also distill the TRANSFERABLE craft into the GLOBAL learnings
    if s.learned:
        print(_d(f"  → distilling transferable craft from {len(s.learned)} "
                 "critique(s) to global…"))
        try:
            if creative.globalize_critiques(s.learned, s.topic):
                print(_g("  ↳ transferable craft distilled into "
                         "channels/learnings.md (global)"))
        except Exception as e:
            print(_y(f"  ! global distill skipped: {e}"))
    print(_d("  → cleanup: removing session.json (report.md keeps the audit)"))
    (s.folder / "session.json").unlink(missing_ok=True)
    print(_g("\n  ✓ wrapped") + _d(" — next: upload the bundle to your platform"))


# canonical artifact name -> the stage it belongs to (for the version trail)
_ART_KIND = [
    (r"seg\d+(_l\d+)?\.mp3$", "voice"),
    (r"key\d+\.png$", "keyframe"),
    (r"clip\d+\.mp4$", "clip"),
    (r"text\d+(_\d+)?\.png$", "overlay"),
    (r"(short|nomusic|merged)\.mp4$", "assembly"),
    (r"(subs\.ass|music\.txt)$", "assembly"),
    (r"script\.json$", "script"),
    (r"facts\.json$", "research"),
    (r"seo\.(json|md)$", "seo"),
    (r"thumb.*\.png$", "thumbnail"),
    (r"virality\.md$", "score"),
]
# the order stages run, so the trail reads top-to-bottom like the pipeline
_KIND_ORDER = ["research", "script", "voice", "keyframe", "clip",
               "overlay", "assembly", "thumbnail", "seo", "score", "other"]


def _artifact_kind(name: str) -> str:
    for pat, kind in _ART_KIND:
        if re.match(pat, name):
            return kind
    return "other"


def _versioned_changes(folder: Path, since: str) -> dict:
    """{canonical name -> times superseded} for every artifact ARCHIVED to
    .versions/ since `since` (the session-start ISO ts). This is the ground
    truth of WHAT actually changed this session — and unlike the in-memory
    edit counters it SURVIVES quit/resume (the version log + session start are
    both on disk). Empty when nothing was superseded (a clean accept)."""
    hits = {}
    for name, versions in make_video.list_versions(folder).items():
        n = sum(1 for v in versions
                if not since or (v.get("ts") or "") >= since)
        if n:
            hits[name] = n
    return hits


def _seg_text(seg: dict) -> str:
    """The spoken words of a script segment — the joined dialogue `lines`, else
    plain `text`. The actual content we diff to see what the SCRIPT changed."""
    if seg.get("lines"):
        return " ".join((l.get("text") or "").strip() for l in seg["lines"]
                        if (l.get("text") or "").strip()).strip()
    return (seg.get("text") or "").strip()


def _read_versioned(folder: Path, rel: str):
    """Read an archived artifact (a versions.json `file` like
    '.versions/script.1.json') from the run folder. None on any error."""
    try:
        return json.loads((Path(folder) / rel).read_text())
    except (OSError, ValueError):
        return None


def _script_change_items(folder: Path, since: str) -> list:
    """Structured diff of what ACTUALLY changed in the script this session — the
    script as it was at session start (the EARLIEST script.json archived this
    session, i.e. the pre-edit state) vs the current one. Reads the real spoken
    content, NOT the critique/feedback notes. Returns tuples:
      ('added', label, text) ('removed', label, text)
      ('rewrite', label, old, new) ('reorder', [labels])
    [] when the script wasn't touched this session."""
    vlog = make_video.list_versions(folder).get("script.json", [])
    sess = [v for v in vlog if not since or (v.get("ts") or "") >= since]
    if not sess:
        return []
    old = _read_versioned(folder, sess[0]["file"])   # oldest-this-session = start
    cur = _read_json(folder, "script.json")
    if not old or not cur:
        return []
    old_by = {sg.get("label"): sg for sg in (old.get("segments") or [])}
    cur_by = {sg.get("label"): sg for sg in (cur.get("segments") or [])}
    old_lbls = [sg.get("label") for sg in (old.get("segments") or [])]
    cur_lbls = [sg.get("label") for sg in (cur.get("segments") or [])]
    items = []
    for lbl in cur_lbls:
        if lbl not in old_by:
            items.append(("added", lbl, _seg_text(cur_by[lbl])))
    for lbl in old_lbls:
        if lbl not in cur_by:
            items.append(("removed", lbl, _seg_text(old_by[lbl])))
    for lbl in cur_lbls:
        if lbl in old_by:
            o, n = _seg_text(old_by[lbl]), _seg_text(cur_by[lbl])
            if o != n:
                items.append(("rewrite", lbl, o, n))
    if old_lbls != cur_lbls and sorted(old_lbls) == sorted(cur_lbls):
        items.append(("reorder", cur_lbls))
    return items


def _script_diff(folder: Path, since: str) -> list:
    """The colored display lines for the script diff (from _script_change_items)."""
    out = []
    for it in _script_change_items(folder, since):
        if it[0] == "added":
            out.append(_g("  + added ") + str(it[1])
                       + _d(": “" + it[2][:72] + "”"))
        elif it[0] == "removed":
            out.append(_r("  − removed ") + str(it[1])
                       + _d(": “" + it[2][:72] + "”"))
        elif it[0] == "rewrite":
            out.append(_y("  ~ " + str(it[1]) + " rewritten"))
            out.append(_d("      was: ") + it[2][:88])
            out.append(_d("      now: ") + it[3][:88])
        elif it[0] == "reorder":
            out.append(_cy("  ↻ reordered: ")
                       + _d(" → ".join(map(str, it[1]))))
    return out


def _script_changes_plain(folder: Path, since: str) -> str:
    """A plain-text (uncolored) summary of the script diff for the learning
    extractor — so learnings are grounded in what actually changed in the
    words, not the critique notes. '' when the script wasn't touched."""
    lines = []
    for it in _script_change_items(folder, since):
        if it[0] == "added":
            lines.append(f"- ADDED {it[1]}: {it[2]}")
        elif it[0] == "removed":
            lines.append(f"- REMOVED {it[1]}: {it[2]}")
        elif it[0] == "rewrite":
            lines.append(f"- REWROTE {it[1]}:\n    was: {it[2]}\n    now: {it[3]}")
        elif it[0] == "reorder":
            lines.append("- REORDERED: " + " -> ".join(map(str, it[1])))
    return "\n".join(lines)


def _session_changes(s: Session) -> list:
    """Human-readable lines of WHAT changed this session. Two concrete,
    on-disk sources (both survive quit/resume): the VERSION TRAIL — every
    artifact the session superseded, grouped by stage — and the SCRIPT DIFF —
    what actually changed in the spoken words (segments added/removed/rewritten/
    reordered), read from the archived script, not the critique notes."""
    out = []
    sess = _read_json(s.folder, "session.json") or {}
    since = sess.get("started", "")
    hits = _versioned_changes(s.folder, since)
    if hits:
        by_kind = {}
        for name in sorted(hits, key=lambda nm: (_human_idx(nm), nm)):
            by_kind.setdefault(_artifact_kind(name), []).append(
                f"{name}×{hits[name]}" if hits[name] > 1 else name)
        for kind in _KIND_ORDER:
            if kind in by_kind:
                out.append(_cy(f"  {kind}: ") + _d(", ".join(by_kind[kind])))
    elif s.iterations:
        # nothing versioned but edits happened (rare) — fall back to counters
        out.append(_cy("  edits per stage: ")
                   + ", ".join(f"{k}×{v}" for k, v in s.iterations.items()))
    diff = _script_diff(s.folder, since)
    if diff:
        out.append(_cy("  script changes:"))
        out += diff
    return out


def _human_idx(name: str) -> int:
    """The numeric index in an indexed artifact name (seg3.mp3 -> 3) for a
    natural sort; -1 for unindexed names."""
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else -1


def st_segment(s: Session, idx: int):
    """Focused re-edit of ONE segment — change only its pieces (text, overlay,
    voice, keyframe, clip) with ONLY that segment's files invalidated;
    everything else stays cached. Re-assembles on [a]ccept. The single-segment
    counterpart to the full stage flow (entered via `--segment N` or `s<N>` in
    the resume menu)."""
    _ensure_directed(s)
    facts = _read_json(s.folder, "facts.json")
    while True:
        script = _read_json(s.folder, "script.json")
        if idx >= len(script["segments"]):
            print(_r(f"  no segment {idx} (script has "
                     f"{len(script['segments'])})"))
            return
        seg = script["segments"][idx]
        label = seg.get("label", f"seg{idx}")
        key, clip = (config.art(s.folder, f"key{idx}.png"),
                     config.art(s.folder, f"clip{idx}.mp4"))
        mp3 = config.art(s.folder, f"seg{idx}.mp3")
        animate = seg.get("animate", True)
        print("\n  " + _cy(f"SEGMENT {idx} · {label}") + _d("  (re-edit)"))
        print("  " + _d("─" * 46))
        for ln in __import__("textwrap").wrap("text:    "
                                              + (seg.get("text") or ""), 72):
            print("  " + ln)
        if seg.get("overlay"):
            print("  overlay: " + _b(f"[{seg['overlay']}]"))
        if seg.get("visual"):
            print("  " + _d("visual:  " + seg["visual"][:90]))
        print("  files:   "
              + (_g("voice✓ ") if mp3.exists() else _d("voice· "))
              + (_g("key✓ ") if key.exists() else _d("key· "))
              + (_g("clip✓") if clip.exists()
                 else _g("ken-burns") if not animate else _d("clip·")))
        c = ask("  [t]ext / over[l]ay / [v]oice re-read / [k]eyframe / "
                "[c]lip / [o]pen / [a]ccept (re-assemble) / [q]uit:",
                {"t": "rewrite the spoken text (re-voices this segment)",
                 "l": "edit the on-screen overlay text",
                 "v": "re-read the audio — same text, fresh TTS",
                 "k": "regenerate the keyframe (optional prompt tweak)",
                 "c": "regenerate/animate the clip (optional motion tweak)",
                 "o": "open this segment's keyframe + clip",
                 "a": "re-assemble with the changes and finish",
                 "q": "quit — changes kept, nothing re-assembled"},
                globals_on=False)
        if c == "a":
            kalinga.run_assemble(s.topic)
            _open(s.folder / "short.mp4")
            print(_g(f"  re-assembled with segment {idx} [{label}] changes"))
            return
        if c == "q":
            raise Quit()
        s.bump("segment")
        if c == "t":
            print("  current: " + (seg.get("text") or ""))
            new = ask_long("  new spoken text: ")
            if new:
                seg["text"] = new
                _write_json(s.folder, "script.json", script)
                s.chat = None
                _invalidate(s.folder, text_idx=idx)
                kalinga.run_voice(s.topic)
                _play(mp3)
        elif c == "l":
            print("  current overlay: " + (seg.get("overlay") or "(none)"))
            new = ask_text("  new overlay (≤5 words, '-' clears): ")
            if new:
                seg["overlay"] = "" if new.strip() == "-" else new
                _write_json(s.folder, "script.json", script)
                # overlay re-renders as text<N>.png at assembly
                _unlink(s.folder, [f"text{idx}.png"] + list(ASSEMBLY)
                        + ["virality.md"])
        elif c == "v":
            _unlink(s.folder, [f"seg{idx}.mp3"] + list(ASSEMBLY)
                    + ["virality.md", "report.md", "critique.md"])
            kalinga.run_voice(s.topic)
            _play(mp3)
        elif c == "k":
            extra = ask_long("  keyframe prompt tweak (Enter = same, "
                             "'-' clears the saved tweak): ")
            if extra:
                _set_tweak(script, s.folder, idx, "visual_extra", extra)
                if extra.strip() != "-":
                    _remember(s, "keyframe", extra)
            _invalidate(s.folder, visual_idx=idx)
            shots, _ = _shots(s)
            if make_video.step_keyframe(shots[idx], facts, s.folder, s.tpl):
                _open(key)
        elif c == "c":
            if not animate:
                print(_d("  director marked this a free Ken Burns still — a "
                         "clip will animate it (costs credits)"))
            extra = ask_long("  motion note (Enter = same, '-' clears): ")
            if extra:
                _set_tweak(script, s.folder, idx, "motion_extra", extra)
                if extra.strip() != "-":
                    _remember(s, "clip", extra)
            _invalidate(s.folder, motion_idx=idx)
            shots, _ = _shots(s)
            if make_video.step_clip(shots[idx], s.folder, s.tpl):
                _open(clip)
            else:
                print(_y("  clip failed (often moderation) — assembly will "
                         "Ken Burns the keyframe instead"))
        elif c == "o":
            targets = [p for p in (key, clip) if p.exists()]
            if targets:
                _open(*targets)
            else:
                print(_d("  nothing rendered for this segment yet"))


STAGE_FN = {"research": st_research,
            "concept": st_concept,
            "script": st_script, "direction": st_direction, "voice": st_voice,
            "keyframes": st_keyframes, "clips": st_clips,
            "music": st_music, "assemble": st_assemble,
            "thumbnail": st_thumbnail, "seo": st_seo,
            "gates": st_gates, "wrap": st_wrap}


# ---------- startup / main ----------
def _resolve_topic(arg_topic):
    ch = config.channel()
    topic = (arg_topic or "").strip()
    if not topic:
        row = daily.next_pending(daily.load_queue())
        if not row:
            print(f"queue empty — add topics to channels/{ch.name}/"
                  f"queue.csv or pass one")
            return None, False
        topic = row[0].strip()
    topic = ch.normalize_topic(topic)
    in_queue = any(ch.normalize_topic(r[0]) == topic
                   for r in daily.load_queue() if r)
    return topic, in_queue


def _set_aside(pre: Path):
    """Rename a run folder out of topic_dir's *_TOPIC glob."""
    aside = pre.with_name(pre.name + ".old")
    n = 1
    while aside.exists():
        n += 1
        aside = pre.with_name(f"{pre.name}.old{n}")
    pre.rename(aside)
    print(f"  moved aside → {aside.name}")


def _resume_menu(topic: str):
    """An existing run never resumes silently: show the numbered stage list
    up front — typing a number (or name) jumps straight to that stage to
    redo it; `s<N>` re-edits JUST segment N (the focused single-segment loop);
    Enter continues; [n]ew sets the folder aside; [q]uit.
    Returns a stage index to start at, ("seg", N) to re-edit one segment, or
    None for the default (first incomplete). EOF/scripted input continues."""
    pre = config.topic_dir(topic, create=False)
    if not pre.exists() or not any(pre.iterdir()):
        return None
    done = _done_map(pre)
    nxt = STAGE_ORDER[first_incomplete(pre)]
    age_h = (time.time() - pre.stat().st_mtime) / 3600
    print("\n  found " + _b(pre.name)
          + (_y(f" — last touched {age_h / 24:.1f} days ago")
             if age_h > STALE_HOURS else ""))
    print()
    for i, name in enumerate(STAGE_ORDER, 1):
        mark = _g("✓") if done[name] else _d("·")
        label = _cy(name) if name == nxt else name
        print(f"   {_d(f'{i:2d}.')} {mark} {label}"
              + (_d("  ← next") if name == nxt else ""))
    segs = (_read_json(pre, "script.json") or {}).get("segments") or []
    if segs:
        print("\n  " + _d("re-edit one segment — ")
              + " ".join(_d(f"s{i}·") + sg.get("label", "?")
                         for i, sg in enumerate(segs)))
    while True:
        try:
            raw = input("\n  " + _menu(
                f"[Enter] continue at '{nxt}'  /  stage number or name  /  "
                + ("s<N> = re-edit segment N  /  " if segs else "")
                + "[n]ew run  /  [q]uit: ")).strip().lower()
        except EOFError:
            return None
        if raw in ("", "y", "yes", "c"):
            return None
        if raw == "q":
            raise Quit()
        if raw in ("n", "no"):
            _set_aside(pre)
            return None
        m = re.fullmatch(r"s\s*(\d+)", raw) or re.fullmatch(r"seg\s*(\d+)", raw)
        if m and segs:
            n = int(m.group(1))
            if 0 <= n < len(segs):
                return ("seg", n)
            print(_d(f"    no segment {n} (0–{len(segs) - 1})"))
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(STAGE_ORDER):
            return int(raw) - 1
        if raw in STAGE_ORDER:
            return STAGE_ORDER.index(raw)
        print(_d("    Enter, n, q — a stage number/name, or s<N> for a "
                 "segment"))


def main(args) -> int:
    ch = config.channel()
    topic, in_queue = _resolve_topic(args.topic)
    if not topic:
        return 1
    print(f"=== kalinga make — channel={ch.name}, topic={topic} ===")
    try:
        make_video.ensure_cli()
    except make_video.StepFailed as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    import llm
    if not llm.available():
        print("! no LLM backend — script co-writing and SEO "
              "fall back to dry templates", file=sys.stderr)

    jump = None
    try:
        if (getattr(args, "at", None) is None
                and getattr(args, "segment", None) is None):
            jump = _resume_menu(topic)
    except Quit:
        print("  nothing changed")
        return 0
    folder = config.topic_dir(topic)
    if not (folder / "template.json").exists():
        chosen = args.template or _choose_template(ch.default_template)
        templates.resolve(chosen, folder)
    elif args.template:
        print(f"  pinned template wins on resume — `redo` stages or start "
              f"fresh to change it")
    tpl = templates.load_pinned(folder)

    s = Session(topic, folder, tpl, in_queue)
    credits = kalinga.hf_credits()
    sess = _read_json(folder, "session.json")
    if sess is None:
        sess = {"started": datetime.now().isoformat(timespec="seconds"),
                "credits_start": credits}
        _write_json(folder, "session.json", sess)
    s.credits_start = sess.get("credits_start", credits)
    s.learned = sess.get("critiques", [])          # restore critiques on resume
    import usage
    usage.bind(folder, credits_start=s.credits_start)
    if credits is not None:
        n = _n_segments(folder) or len(ch.segments)
        est = n * CREDITS_PER_KEYFRAME + n * 6 * CREDITS_PER_CLIP_SEC
        print(f"  credits: {credits} | template budget "
              f"{tpl['budget_credits']} | first-pass estimate ≈ {est:.0f}")
        if credits < tpl["budget_credits"]:
            print(f"  ! balance is below the template budget — free stages "
                  f"still work; generation may run dry")

    # stale-voice guard: script and audio must agree on segment count
    script, audio = (_read_json(folder, "script.json"),
                     _read_json(folder, "audio.json"))
    if script and audio and (len(script["segments"])
                             != len(audio["segments"])):
        print(f"  ! segment count mismatch (script "
              f"{len(script['segments'])} / audio {len(audio['segments'])})"
              f" — regenerating the voiceover")
        _invalidate(folder, voice=True)

    # focused single-segment re-edit (--segment N, or s<N> in the resume menu):
    # change only that segment's pieces, everything else stays cached
    seg_edit = getattr(args, "segment", None)
    if isinstance(jump, tuple) and jump and jump[0] == "seg":
        seg_edit, jump = jump[1], None
    if seg_edit is not None:
        if not (folder / "script.json").exists():
            print(_r("  no script yet — run the normal flow first, then "
                     "re-edit a segment"))
            return 1
        print(f"  re-editing segment {seg_edit} only — every other segment "
              f"stays cached")
        try:
            st_segment(s, seg_edit)
        except Quit:
            print(f"\n  changes kept — `python3 kalinga.py make {topic}` "
                  f"resumes.\n  artifacts: {folder}")
        except make_video.StepFailed as e:
            print(f"✗ {e}\n  (artifacts cached — rerun to resume)",
                  file=sys.stderr)
            return 1
        return 0
    if getattr(args, "at", None):
        jump = STAGE_ORDER.index(args.at)
    if jump is not None:
        i = jump
        print(f"  jumping to stage '{STAGE_ORDER[i]}' — cached artifacts "
              f"stay; anything you change invalidates downstream "
              f"automatically")
    else:
        i = first_incomplete(folder)
    try:
        while i < len(STAGE_ORDER):
            _stage_header(s, i)
            jump = STAGE_FN[STAGE_ORDER[i]](s)
            if jump:
                i = STAGE_ORDER.index(jump)
            else:
                i += 1
    except Quit:
        nxt = STAGE_ORDER[min(first_incomplete(folder),
                              len(STAGE_ORDER) - 1)]
        print(f"\n  session saved — `python3 kalinga.py make {topic}` "
              f"resumes at '{nxt}'.\n  artifacts: {folder}")
        return 0
    except Auto:
        print(f"\n  ── auto mode ── finishing headless via ship's path "
              f"(~10–30 min); output → {folder / 'short.mp4'}")
        rc = kalinga.run_to_done(topic, folder, s.tpl,
                                 in_queue=in_queue,
                                 credits_before=s.credits_start)
        _notify("short.mp4 ready" if rc == 0 else "run failed — rerun to "
                "resume")
        (folder / "session.json").unlink(missing_ok=True)
        return rc
    except make_video.StepFailed as e:
        print(f"✗ {e}\n  (artifacts are cached — rerun the same command to "
              f"resume)", file=sys.stderr)
        return 1

    vir = (s.result or {}).get("virality", {})
    ok = (s.result or {}).get("ok")
    print(f"\n{_g('✓ READY') if ok else _y('⚠ REVIEW report.md first')} — "
          f"{folder / 'short.mp4'}")
    _notify("session complete — short.mp4 ready")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="interactive")
    ap.add_argument("topic", nargs="?", default=None)
    ap.add_argument("--template", default=None)
    ap.add_argument("--at", default=None, choices=STAGE_ORDER)
    ap.add_argument("--segment", type=int, default=None,
                    help="re-edit ONLY this segment (index), leaving the rest "
                         "cached")
    ap.add_argument("--channel", default=None)
    a = ap.parse_args()
    config.set_channel(a.channel)
    sys.exit(main(a))
