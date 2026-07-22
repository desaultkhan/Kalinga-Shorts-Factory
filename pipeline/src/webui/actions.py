"""
webui.actions — the non-interactive core of each interactive.st_* stage.

One `a_<stage>_<action>` function per button; ACTIONS maps (stage, action) → the
handler, and act() dispatches. Each handler either does instant work and returns
("ok", result) — the server then re-reads build_state() — or backgrounds a job
and returns ("job", id). Adding a stage action = add an a_* here + one ACTIONS
entry (+ its snapshot in state.py).
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import config
import creative
import daily
import interactive as iv
import kalinga
import make_video
import templates
import validate

from . import context as ctx
from .context import _job
from .state import _asm_mark, _asm_clear, _save_concept_web


def _advance():
    if ctx.APP.stage < len(iv.STAGE_ORDER) - 1:
        ctx.APP.stage += 1


def _goto(stage):
    if stage in iv.STAGE_ORDER:
        ctx.APP.stage = iv.STAGE_ORDER.index(stage)


def act(stage: str, action: str, p: dict):
    """Dispatch one action. Returns ("job", id) for backgrounded work,
    ("ok", None) for instant work. Raises on bad input."""
    s = ctx.APP.s
    handler = ACTIONS.get((stage, action))
    if not handler:
        raise ValueError(f"unknown action {stage}/{action}")
    return handler(s, p)


# ---- research ---------------------------------------------------------
def a_research_run(s, p):
    return _job("research", lambda: kalinga.run_research(s.topic))

def a_research_rerun(s, p):
    def fn():
        iv._unlink(s.folder, ["facts.json"])
        kalinga.run_research(s.topic)
    return _job("re-running research", fn)

def a_research_save(s, p):
    json.loads(p["json"])                       # validate
    (s.folder / "facts.json").write_text(p["json"])
    return ("ok", None)

def a_research_accept(s, p):
    _advance()
    return ("ok", None)


# ---- script -----------------------------------------------------------
def a_script_gen(s, p):
    return _job("writing the script", lambda: kalinga.write_script(s.topic))

def a_script_reroll(s, p):
    def fn():
        iv._unlink(s.folder, ["script.json", "run_state.json"])
        s.chat = None
        kalinga.write_script(s.topic)
    return _job("re-rolling the script", fn)

def a_script_feedback(s, p):
    fb = (p.get("text") or "").strip()
    if not fb:
        raise ValueError("empty feedback")
    def fn():
        import creative
        if s.chat is None:
            s.chat = creative.make_chat(s.topic)
        creative.revise_script(s.topic, fb, s.chat)
        iv._remember(s, "script critique", fb)
    return _job("revising the script", fn)

def a_script_judge(s, p):
    """Revise using the script judge's OWN critique — its hint is the bar the
    next draft is scored against, so this auto-applies it. (Hidden once the
    script passes: _judge_feedback returns None when the judge says good enough.)"""
    fb = iv._judge_feedback(iv._read_json(s.folder, "script.json") or {})
    if not fb:
        raise ValueError("the judge has no actionable hint")
    def fn():
        import creative
        if s.chat is None:
            s.chat = creative.make_chat(s.topic)
        creative.revise_script(s.topic, fb, s.chat)   # not a creator critique
    return _job("revising on the judge's critique", fn)

def a_script_save(s, p):
    import creative
    segs = json.loads(p["json"])
    sc = iv._read_json(s.folder, "script.json")
    sc["segments"] = segs if isinstance(segs, list) else segs["segments"]
    problems = creative._validate(sc["segments"], s.tpl, config.channel())
    facts = iv._read_json(s.folder, "facts.json")
    sc["judge"] = creative.judge_script(sc["segments"], facts,
                                        prev=sc.get("judge"),
                                        min_score=s.tpl["script_min_score"])
    sc["mode"] = "llm-revised"
    make_video.archive(s.folder, "script.json")   # keep the pre-edit draft
    iv._write_json(s.folder, "script.json", sc)
    s.chat = None
    return ("ok", {"problems": problems})


def a_script_sections(s, p):
    """Creator-set SECTION grouping for one beat — `sizes` = sentences per
    section (contiguous, must sum to the beat's sentence count; [] = back to
    auto). Stored as segment.section_breaks (creative.section_sizes honours
    it everywhere). If the beat is already sectionized, its per-shot `say`s
    re-derive NOW and the beat's audio/overlay are archived so the regrouped
    sections re-voice; pre-direction, the breaks simply steer the coming
    sectionize."""
    import creative
    i = int(p.get("index", -1))
    sizes = [int(x) for x in (p.get("sizes") or [])]
    sc = iv._read_json(s.folder, "script.json")
    if not sc or i < 0 or i >= len(sc.get("segments", [])):
        raise ValueError("no such segment")
    seg = sc["segments"][i]
    n_sents = len(creative._sentences(seg.get("text", "")))
    if sizes and (any(z < 1 for z in sizes) or sum(sizes) != n_sents):
        raise ValueError(f"section sizes must be ≥1 and sum to the beat's "
                         f"{n_sents} sentence(s)")
    if sizes:
        seg["section_breaks"] = sizes
    else:
        seg.pop("section_breaks", None)
    resynth = creative._has_section_say(seg) or bool(seg.get("shots"))
    if resynth:
        creative._sectionize_beats([seg])
    iv._write_json(s.folder, "script.json", sc)
    if resynth:
        iv._invalidate(s.folder, section_idx=i)
    return ("ok", {"sizes": creative.section_sizes(seg)})

def a_script_accept(s, p):
    # redo only the sections whose spoken text changed (mirror st_script tail)
    audio = iv._read_json(s.folder, "audio.json")
    if audio:
        sc = iv._read_json(s.folder, "script.json")
        if len(sc["segments"]) != len(audio["segments"]):
            iv._invalidate(s.folder, voice=True)
        else:
            import creative
            changed = False
            for i, (seg, am) in enumerate(zip(sc["segments"],
                                              audio["segments"])):
                if seg["text"] != am["text"]:
                    # re-split the beat's sections from the NEW text so each
                    # section voices the right line, then drop the stale audio
                    if make_video._section_specs(seg):
                        creative._sectionize_beats([seg])
                        changed = True
                    iv._invalidate(s.folder, section_idx=i)
            if changed:
                iv._write_json(s.folder, "script.json", sc)
    _advance()
    return ("ok", None)


# ---- direction --------------------------------------------------------
def a_dir_gen(s, p):
    return _job("designing the visuals",
                lambda: __import__("creative").direct_script(s.topic))

def a_dir_revise(s, p):
    fb = (p.get("text") or "").strip()
    if not fb:
        raise ValueError("empty notes")
    def fn():
        import creative
        if s.dchat is None:
            s.dchat = creative.make_direction_chat(s.topic)
        creative.revise_direction(s.topic, fb, s.dchat)
        iv._remember(s, "visual direction", fb)
        iv._invalidate_visuals(s.folder)
    return _job("revising the direction", fn)

def a_dir_judge(s, p):
    """Compute the director judge for an already-directed run that has none
    (directed before the judge existed) — $0, advisory."""
    return _job("judging the direction",
                lambda: __import__("creative").ensure_direction_judge(
                    s.topic, regen=True))

def a_dir_judgefix(s, p):
    """Apply the AI judge's PER-SECTION critique — the per-beat director agent
    redesigns each flagged beat with its own fix (falls back to the whole-plan
    note when the judge left only an overall one)."""
    import creative
    j = creative.ensure_direction_judge(s.topic, compute=False) or {}
    n = len(j.get("segments") or [])
    if not (n or (j.get("improve") or "").strip()):
        raise ValueError("no judge fix to apply")
    def fn():
        import creative
        creative.revise_direction_from_judge(s.topic)
        iv._remember(s, "visual direction",
                     f"applied the director judge's per-section notes ({n} beat(s))")
        iv._invalidate_visuals(s.folder)
    return _job(f"applying the judge's fixes to {n} beat(s)" if n
                else "applying the judge's fix", fn)

def a_dir_fill(s, p):
    return _job("filling gaps",
                lambda: __import__("creative").direct_script(s.topic,
                                                             fill=True))

def a_dir_replan(s, p):
    def fn():
        import creative
        sc = iv._read_json(s.folder, "script.json")
        sc["directed"] = None
        iv._write_json(s.folder, "script.json", sc)
        s.dchat = None
        creative.direct_script(s.topic)
        iv._invalidate_visuals(s.folder)
    return _job("re-planning the visuals", fn)

def a_dir_accept(s, p):
    _advance()
    return ("ok", None)


# ---- voice ------------------------------------------------------------
def a_voice_gen(s, p):
    return _job("generating the voiceover",
                lambda: kalinga.run_voice(s.topic))

def a_voice_reread(s, p):
    """Re-generate the AI voiceover for a whole segment, or — when `section` is
    given — just ONE section of a section-native beat (each section is its own
    seg<i>_s<k>.mp3, so a single line can be re-read without touching the rest)."""
    i = int(p["index"])
    sec = p.get("section")
    sec = int(sec) if sec is not None and str(sec) != "" else None
    def fn():
        if sec is not None:
            targets = [f"seg{i}_s{sec}.mp3"]
        else:
            adir = config.art(s.folder, f"seg{i}_s0.mp3").parent
            secs = sorted(x.name for x in adir.glob(f"seg{i}_s*.mp3"))
            targets = secs or [f"seg{i}.mp3"]
        # keep audio.json: step_voice busts its cache on the missing section file
        # and reuses every OTHER section's cached take + word timings, so only the
        # archived unit is re-synthesized.
        iv._unlink(s.folder, targets + list(iv.ASSEMBLY)
                   + ["virality.md", "report.md", "critique.md"])
        kalinga.run_voice(s.topic)
    return _job(f"re-reading segment {i}"
                + (f".s{sec}" if sec is not None else ""), fn)

def a_voice_unrecord(s, p):
    """Drop a recorded take and revert that unit to the AI voice."""
    i = int(p["index"])
    sec = p.get("section")
    sec = int(sec) if sec is not None and str(sec) != "" else None
    def fn():
        make_video.clear_recording(s.folder, i, sec)
        kalinga.run_voice(s.topic)
    return _job(f"reverting seg{i}"
                + (f".s{sec}" if sec is not None else "") + " to AI voice", fn)

def a_voice_setvoice(s, p):
    name = (p.get("text") or "").strip()
    if not name:
        raise ValueError("empty voice name")
    def fn():
        s.tpl = iv._pin_template(s.folder, voice=name)
        iv._invalidate(s.folder, voice=True)
        kalinga.run_voice(s.topic)
    return _job(f"switching voice → {name}", fn)

def a_voice_engine(s, p):
    engine, _ = iv._voice_settings(s.tpl)
    want = (p.get("engine") or "").strip()   # dropdown pick; blank = cycle
    if want:
        if want not in make_video.ENGINES:
            raise ValueError(f"unknown engine {want}")
        ok, why = make_video.ENGINES[want].available()
        if not ok:
            raise ValueError(f"{want} unavailable — {why}")
        if want == engine:
            return ("ok", None)
        new = want
    else:
        new = make_video.next_engine(engine)   # cycles every AVAILABLE engine
    def fn():
        s.tpl = iv._pin_template(s.folder, tts_engine=new)
        iv._invalidate(s.folder, voice=True)
        kalinga.run_voice(s.topic)
    return _job(f"switching engine → {new}", fn)


def a_voice_sample(s, p):
    """Audition a voice WITHOUT touching the run: synthesize one cached line
    in the current engine and hand back its URL — the panel plays it inline.
    edge = free; paid engines cost one tiny TTS call."""
    import cast_setup
    engine, cur = iv._voice_settings(s.tpl)
    voice = (p.get("text") or "").strip() or cur
    if engine in ("edge", "higgsfield") \
            and not cast_setup.voice_ok(engine, voice):
        raise ValueError(f"'{voice}' doesn't look like a {engine} voice")
    out, line = cast_setup.sample_file(engine, voice)
    return ("ok", {"sampleUrl": "/sample/" + out.name, "line": line})

def a_voice_text(s, p):
    i = int(p["index"])
    new = (p.get("text") or "").strip()
    if not new:
        raise ValueError("empty text")
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        sc["segments"][i]["text"] = new
        # a section-native beat re-derives each section's `say` from the new text
        if make_video._section_specs(sc["segments"][i]):
            import creative
            creative._sectionize_beats([sc["segments"][i]])
        iv._write_json(s.folder, "script.json", sc)
        s.chat = None
        iv._invalidate(s.folder, text_idx=i)
        kalinga.run_voice(s.topic)
    return _job(f"rewriting segment {i}", fn)

def a_voice_dialogue(s, p):
    """Edit ONE dialogue line's START timing (a number of seconds, or 'auto'
    for back-to-back) and/or EXPRESSION (how it's said). A timing-only change
    recomposes segN.mp3 for FREE; an expression change re-voices the affected
    line. Mirrors the interactive `_st_dialogue` stage."""
    i = int(p["index"])
    k = int(p["line"])
    start = p.get("start")            # "" keep · "auto" back-to-back · number
    expr = p.get("expression")        # None keep · "-" none · text set
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        seg = sc["segments"][i]
        spoken = [l for l in (seg.get("lines") or [])
                  if (l.get("text") or "").strip()]
        if k >= len(spoken):
            raise ValueError(f"segment {i} has no line {k}")
        ln = spoken[k]
        changed = expr_changed = False
        if start is not None:
            sv = str(start).strip()
            if sv.lower() == "auto":
                if ln.pop("start", None) is not None or ln.pop("gap", None):
                    changed = True
            elif sv:
                try:
                    ln["start"] = round(float(sv), 3)
                    ln.pop("gap", None)
                    changed = True
                except ValueError:
                    raise ValueError(f"start '{sv}' is not a number or 'auto'")
        if expr is not None:
            ev = str(expr).strip()
            if ev == "-":
                if ln.pop("expression", None):
                    expr_changed = True
            elif ev:
                ln["expression"] = ev
                expr_changed = True
        if not (changed or expr_changed):
            return
        iv._write_json(s.folder, "script.json", sc)
        victims = ([f"seg{i}.mp3"] + list(iv.ASSEMBLY)
                   + ["virality.md", "report.md", "critique.md"])
        if expr_changed:              # audio differs → re-voice this line
            victims += [f"seg{i}_l{k}.mp3"]
        iv._unlink(s.folder, victims)  # keep audio.json + line clips → free retime
        s.bump("voice")
        kalinga.run_voice(s.topic)
    return _job(f"retiming seg{i} line {k}", fn)

def a_voice_accept(s, p):
    _advance()
    return ("ok", None)


# ---- keyframes --------------------------------------------------------
def _step_key(s, i):
    iv._ensure_directed(s)
    shots, _ = iv._shots(s)
    facts = iv._read_json(s.folder, "facts.json")
    make_video.step_keyframe(shots[i], facts, s.folder, s.tpl)

def a_key_gen(s, p):
    """Generate every missing keyframe in order (refs need earlier ones),
    including the sub-shots of multi-shot beats (shot 0 = the canonical
    key<i>.png, k≥1 = key<i>_<k>.png)."""
    def fn():
        iv._ensure_directed(s)
        shots, _ = iv._shots(s)
        facts = iv._read_json(s.folder, "facts.json")
        for sh in shots:
            for sub in make_video._subshots(sh, s.folder):
                if not sub["key"].exists():
                    make_video.step_keyframe(sub, facts, s.folder, s.tpl)
    return _job("generating keyframes", fn)

def a_key_one(s, p):
    i = int(p["index"])
    return _job(f"generating keyframe {i}", lambda: _step_key(s, i))

def a_key_regen(s, p):
    i = int(p["index"])
    def fn():
        iv._invalidate(s.folder, keyframe_idx=i)
        _step_key(s, i)
    return _job(f"regenerating keyframe {i}", fn)

def a_key_tweak(s, p):
    i = int(p["index"])
    extra = (p.get("text") or "").strip()
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        iv._set_tweak(sc, s.folder, i, "visual_extra", extra)
        if extra and extra != "-":
            iv._remember(s, "keyframe", extra)
        iv._invalidate(s.folder, visual_idx=i)
        _step_key(s, i)
    return _job(f"regenerating keyframe {i} with your note", fn)

def _subshot(s, i, k):
    """The expanded sub-shot dict for segment i, sub-index k (k=0 is the
    canonical shot). None if not found."""
    sc = iv._read_json(s.folder, "script.json") or {}
    cast = sc.get("cast") or {}
    sg = sc["segments"][i]
    shot = {"index": i, "label": sg.get("label"), "seg": sg, "cast": cast,
            "need": 0.0, "key": config.art(s.folder, f"key{i}.png"),
            "clip": config.art(s.folder, f"clip{i}.mp4")}
    for sub in make_video._subshots(shot, s.folder):
        if sub.get("sub", 0) == k:
            return sub
    return None

def a_key_subgen(s, p):
    """Generate or regenerate ONE sub-shot keyframe of a multi-shot beat."""
    i, k = int(p["index"]), int(p["section"])
    regen = bool(p.get("regen"))
    def fn():
        sub = _subshot(s, i, k)
        if sub is None:
            raise ValueError("no such sub-shot")
        if regen:
            make_video.archive(s.folder, sub["key"].name)
            iv._unlink(s.folder, list(iv.ASSEMBLY))
        facts = iv._read_json(s.folder, "facts.json")
        make_video.step_keyframe(sub, facts, s.folder, s.tpl)
    return _job(f"{'regenerating' if regen else 'generating'} keyframe {i}.{k}", fn)

def a_key_subtweak(s, p):
    """Append a creator note (visual_extra) to ONE sub-shot, then regenerate it."""
    i, k = int(p["index"]), int(p["section"])
    extra = (p.get("text") or "").strip()
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        shots = sc["segments"][i].get("shots") or []
        if k < len(shots):
            if extra and extra != "-":
                shots[k]["visual_extra"] = extra
                iv._remember(s, "keyframe", extra)
            else:
                shots[k].pop("visual_extra", None)
            iv._write_json(s.folder, "script.json", sc)
        sub = _subshot(s, i, k)
        make_video.archive(s.folder, sub["key"].name)
        iv._unlink(s.folder, list(iv.ASSEMBLY))
        facts = iv._read_json(s.folder, "facts.json")
        make_video.step_keyframe(sub, facts, s.folder, s.tpl)
    return _job(f"regenerating keyframe {i}.{k} with your note", fn)

def a_key_prompt(s, p):
    """Set (or clear with '-') the VERBATIM image-prompt override for a shot or a
    sub-shot — 'copy the prompt and use my own'. Stored as `image_prompt`, sent
    exactly as written next generation."""
    i = int(p["index"])
    k = p.get("section")
    k = int(k) if k is not None and str(k) != "" else None
    text = (p.get("text") or "").strip()
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        target = (sc["segments"][i] if k is None
                  else (sc["segments"][i].get("shots") or [])[k])
        if text and text != "-":
            target["image_prompt"] = text
            iv._remember(s, "keyframe", "custom prompt override")
        else:
            target.pop("image_prompt", None)
        iv._write_json(s.folder, "script.json", sc)
        # regenerate the affected keyframe with the new prompt
        if k is None:
            iv._invalidate(s.folder, keyframe_idx=i)
            _step_key(s, i)
        else:
            sub = _subshot(s, i, k)
            make_video.archive(s.folder, sub["key"].name)
            iv._unlink(s.folder, list(iv.ASSEMBLY))
            facts = iv._read_json(s.folder, "facts.json")
            make_video.step_keyframe(sub, facts, s.folder, s.tpl)
    return _job(f"applying your prompt to keyframe {i}"
                + (f".{k}" if k is not None else ""), fn)

def a_key_accept(s, p):
    _advance()
    return ("ok", None)


# ---- clips ------------------------------------------------------------
def _step_clip(s, i):
    iv._ensure_directed(s)
    shots, _ = iv._shots(s)
    return make_video.step_clip(shots[i], s.folder, s.tpl)

def _clip_sub(s, i, k):
    """The expanded sub-shot dict for segment i, sub-index k (k=0 is the
    canonical shot) — for generating ONE clip of a multi-shot beat. None if not
    found."""
    iv._ensure_directed(s)
    shots, _ = iv._shots(s)
    for sub in make_video._subshots(shots[i], s.folder):
        if sub.get("sub", 0) == k:
            return sub
    return None

def _regen_subclip(s, sub):
    """Archive a sub-shot's clip + bust the cut, then regenerate just it."""
    make_video.archive(s.folder, sub["clip"].name)
    iv._unlink(s.folder, list(iv.ASSEMBLY))
    make_video.step_clip(sub, s.folder, s.tpl)

def _sec(p):
    """Optional sub-shot index from an action payload (None = the whole beat)."""
    k = p.get("section")
    return int(k) if k is not None and str(k) != "" else None

def a_clip_gen(s, p):
    """Generate ONE clip — a whole beat (section omitted) or a single sub-shot of
    a multi-shot beat (section=k). Converting a STILL to a clip first backfills a
    DYNAMIC motion brief (so it actually moves, not just zooms — sub-shots inherit
    the segment brief when they lack their own), mirroring the terminal flow."""
    i, k = int(p["index"]), _sec(p)
    if k is None:
        def fn():
            iv._ensure_clip_motion(s, i)        # still → dynamic brief
            _step_clip(s, i)
        return _job(f"generating clip {i} (costs credits)", fn)
    def fn():
        iv._ensure_clip_motion(s, i)
        sub = _clip_sub(s, i, k)
        if sub is None:
            raise ValueError("no such sub-shot")
        make_video.step_clip(sub, s.folder, s.tpl)
    return _job(f"generating clip {i}.{k} (costs credits)", fn)

def a_clip_brief(s, p):
    """Write a DYNAMIC motion brief for a still shot — backfill `clip_motion` via
    the director ($0) WITHOUT generating, so the new video prompt can be reviewed
    / edited before spending. No-op if a dynamic brief already exists."""
    i = int(p["index"])
    def fn():
        if not iv._ensure_clip_motion(s, i):
            print("  a dynamic clip brief already exists for this beat")
    return _job(f"writing a dynamic motion brief for shot {i}", fn)

def a_clip_regen(s, p):
    i, k = int(p["index"]), _sec(p)
    if k is None:
        def fn():
            iv._invalidate(s.folder, motion_idx=i)
            _step_clip(s, i)
        return _job(f"regenerating clip {i}", fn)
    def fn():
        sub = _clip_sub(s, i, k)
        if sub is None:
            raise ValueError("no such sub-shot")
        _regen_subclip(s, sub)
    return _job(f"regenerating clip {i}.{k}", fn)

def a_clip_tweak(s, p):
    """Append a creator motion note to a beat or sub-shot, then (optionally)
    regenerate just that clip."""
    i, k = int(p["index"]), _sec(p)
    extra = (p.get("text") or "").strip()
    regen = bool(p.get("regen"))
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        if k is None:
            iv._set_tweak(sc, s.folder, i, "motion_extra", extra)
        else:
            shots = sc["segments"][i].get("shots") or []
            if k < len(shots):
                if extra and extra != "-":
                    shots[k]["motion_extra"] = extra
                else:
                    shots[k].pop("motion_extra", None)
                iv._write_json(s.folder, "script.json", sc)
        if extra and extra != "-":
            iv._remember(s, "clip", extra)
        if regen:
            if k is None:
                iv._invalidate(s.folder, motion_idx=i)
                _step_clip(s, i)
            else:
                _regen_subclip(s, _clip_sub(s, i, k))
    return _job(f"updating clip {i}" + (f".{k}" if k is not None else "")
                + " brief", fn)

def a_clip_prompt(s, p):
    """Set (or clear with '-') the VERBATIM clip-prompt override (`clip_prompt_text`)
    for a beat or sub-shot — 'copy the prompt and use my own' — then regenerate
    that clip with it."""
    i, k = int(p["index"]), _sec(p)
    text = (p.get("text") or "").strip()
    def fn():
        sc = iv._read_json(s.folder, "script.json")
        target = (sc["segments"][i] if k is None
                  else (sc["segments"][i].get("shots") or [])[k])
        if text and text != "-":
            target["clip_prompt_text"] = text
            iv._remember(s, "clip", "custom clip prompt override")
        else:
            target.pop("clip_prompt_text", None)
        iv._write_json(s.folder, "script.json", sc)
        if k is None:
            iv._invalidate(s.folder, motion_idx=i)
            _step_clip(s, i)
        else:
            _regen_subclip(s, _clip_sub(s, i, k))
    return _job(f"applying your prompt to clip {i}"
                + (f".{k}" if k is not None else ""), fn)

def a_clip_still(s, p):
    """Toggle 'use a still instead of a generated video' for a beat/sub-shot
    (`use_still`). Instant + non-destructive: it flips the flag in script.json so
    assembly renders a free Ken Burns frame and IGNORES any clip on disk (kept,
    restorable). Toggling back to video re-uses the existing clip (or generate
    one). Takes effect on the next assemble."""
    i, k = int(p["index"]), _sec(p)
    sc = iv._read_json(s.folder, "script.json")
    target = (sc["segments"][i] if k is None
              else (sc["segments"][i].get("shots") or [])[k])
    now = not target.get("use_still")
    if now:
        target["use_still"] = True
    else:
        target.pop("use_still", None)
    iv._write_json(s.folder, "script.json", sc)
    return ("ok", None)

def a_clip_genall(s, p):
    """Generate every planned-but-missing clip (the director's animated shots,
    sub-shots included; the per-segment cap + caching are honoured by
    step_clips). Bulk credit spend — a deliberate button."""
    def fn():
        iv._ensure_directed(s)
        shots, _ = iv._shots(s)
        make_video.step_clips(shots, s.folder, s.tpl)
    return _job("generating clips", fn)

def a_clip_accept(s, p):
    _advance()
    return ("ok", None)


# ---- music ------------------------------------------------------------
def a_music_set(s, p):
    ch = config.channel()
    ov = iv._read_json(ch.overrides.parent, ch.overrides.name) or {}
    ov["music"] = p.get("track") or "none"
    ch.overrides.write_text(json.dumps(ov, indent=2))
    if (s.folder / "short.mp4").exists():
        iv._invalidate(s.folder, music=True)
    return ("ok", None)

def a_music_accept(s, p):
    _advance()
    return ("ok", None)


# ---- assemble ---------------------------------------------------------
def a_asm_build(s, p):
    # APPLY the staged changes — this is the ONE re-assemble, fired by the
    # creator after they've finished editing. step_assemble presence-caches
    # (early-returns if short.mp4 exists), so drop the cut + build artifacts
    # FIRST. The AI review runs on demand, never automatically. Clears the
    # pending batch on success.
    def fn():
        # always run voice: it's near-instant when cached, re-synthesizes a beat
        # whose source changed (staged native/mix), AND backfills karaoke word
        # timings onto any wordless beat (so captions don't switch style mid-cut)
        kalinga.run_voice(s.topic)
        iv._unlink(s.folder, list(iv.ASSEMBLY)
                   + ["virality.md", "report.md", "critique.md"])
        kalinga.run_assemble(s.topic)
        _asm_clear(s)
    return _job("applying staged changes — voice check → re-assemble", fn)

def a_asm_review(s, p):
    return _job("AI review", lambda: iv._ai_review(s, confirm=iv._review_only))

def a_asm_overlays(s, p):
    ov = s.tpl.get("assemble_overlays", True)
    s.tpl = iv._pin_template(s.folder, assemble_overlays=not ov)
    return _asm_mark(s, f"overlays {'OFF' if ov else 'ON'}")

def a_asm_captions(s, p):
    on = s.tpl.get("assemble_captions", True)
    s.tpl = iv._pin_template(s.folder, assemble_captions=not on)
    return _asm_mark(s, f"subtitles {'OFF' if on else 'ON'}")

def a_asm_seg(s, p):
    """Per-segment assemble edit (the browser `[g]` editor). field ∈
    reveal | fill | native | full | mix — mutates script.json INSTANTLY and
    STAGES the change; the actual re-assemble waits for the creator to apply
    (a_asm_build). native/mix flag a re-voice for the apply (the audio source
    changed) and drop this seg's TTS now so it's re-synthesized then."""
    i = int(p["index"])
    field = (p.get("field") or "").strip()
    val = p.get("value")
    sc = iv._read_json(s.folder, "script.json")
    seg = sc["segments"][i]
    lbl = seg.get("label", f"seg{i}")
    revoice, drop_seg = False, False
    if field == "reveal":
        raw = ("" if val is None else str(val)).strip()
        if raw == "" or raw == "-":
            seg.pop("overlay_reveal_at", None)
        else:
            pct = max(0.0, min(float(raw), 100.0))
            seg["overlay_reveal_at"] = round(pct / 100.0, 3)
    elif field == "fill":
        cur = iv._clip_fill(seg, s.tpl)
        nxt = iv._CLIP_FILLS[(iv._CLIP_FILLS.index(cur) + 1)
                             % len(iv._CLIP_FILLS)]
        seg["clip_fill"] = nxt
    elif field == "native":
        if seg.get("native_audio"):
            seg.pop("native_audio", None)
        else:
            seg["native_audio"] = True
            seg.pop("full_clip", None)
        revoice, drop_seg = True, True
    elif field == "full":
        if seg.get("full_clip"):
            seg.pop("full_clip", None)
        else:
            seg["full_clip"] = True
    elif field == "mix":
        if seg.get("mix_clip_audio"):
            seg.pop("mix_clip_audio", None)
        else:
            seg["mix_clip_audio"] = True
            seg.pop("native_audio", None)         # mixing needs the TTS VO
        revoice = True
    else:
        raise ValueError(f"unknown field {field!r}")
    iv._write_json(s.folder, "script.json", sc)
    if drop_seg:
        iv._unlink(s.folder, [f"seg{i}.mp3"])     # force re-synth at apply
    return _asm_mark(s, f"{lbl}: {field}", revoice=revoice)

def _ov_parse_pct(raw):
    """Editor percent (0-100, blank/None) -> a fraction 0-1, or None for the
    default/hold. Mirrors interactive._parse_pct."""
    raw = ("" if raw is None else str(raw)).strip().rstrip("%")
    if raw == "":
        return None
    try:
        return round(max(0.0, min(float(raw), 100.0)) / 100.0, 3)
    except ValueError:
        return None


def a_asm_overlay(s, p):
    """Add / edit / remove one of a segment's on-screen text overlays — the
    browser equivalent of the terminal `[o]` overlay editor (text, position,
    start%→end% window). op ∈ add | edit | remove. Mutates script.json INSTANTLY
    and STAGES the change; the apply (a_asm_build) drops the stale text<N>.png so
    each overlay re-renders with its new text on the single re-assemble."""
    i = int(p["index"])
    op = (p.get("op") or "").strip()
    k = int(p["k"]) if str(p.get("k", "")).strip() != "" else -1
    sc = iv._read_json(s.folder, "script.json")
    segs = sc.get("segments") or []
    if not (0 <= i < len(segs)):
        raise ValueError(f"no such segment {i}")
    ovs = [dict(x) for x in make_video._seg_overlays(segs[i])]
    if op == "remove":
        if not (0 <= k < len(ovs)):
            raise ValueError(f"no such overlay {k}")
        ovs.pop(k)
    elif op in ("add", "edit"):
        txt = (p.get("text") or "").strip()
        if not txt:
            raise ValueError("overlay text is required")
        o = {"text": txt}
        pos = (p.get("pos") or "").strip()
        if pos:
            o["pos"] = pos
        st = _ov_parse_pct(p.get("start"))
        if st is not None:
            o["start"] = st
        en = _ov_parse_pct(p.get("end"))
        if en is not None:
            o["end"] = en
        if op == "edit":
            if not (0 <= k < len(ovs)):
                raise ValueError(f"no such overlay {k}")
            ovs[k] = o
        else:
            ovs.append(o)
    else:
        raise ValueError(f"unknown overlay op {op!r}")
    iv._save_overlays(s, sc, i, ovs)              # writes script.json + legacy sync
    lbl = segs[i].get("label", f"seg{i}")
    return _asm_mark(s, f"{lbl}: overlay {op}")


def a_asm_ov_bulk(s, p):
    """SAVE ONCE (owner call 2026-07-04: saving each overlay row was
    irritating): the browser assemble editor keeps every overlay text /
    position / timing edit and each beat's reveal LOCAL, then posts the whole
    editor here as ONE action. Each entry REPLACES its segment's overlay list
    wholesale (adds + edits + removes together) and sets/clears its reveal;
    the batch stages a single change. Segments that match what's already on
    disk are skipped, so an untouched beat never stages noise."""
    sc = iv._read_json(s.folder, "script.json")
    segs = sc.get("segments") or []
    changed = []
    for e in (p.get("segs") or []):
        try:
            i = int(e.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(segs)):
            continue
        seg = segs[i]
        ovs = []
        for row in (e.get("overlays") or []):
            txt = (row.get("text") or "").strip()
            if not txt:
                continue
            o = {"text": txt}
            pos = (row.get("pos") or "").strip()
            if pos:
                o["pos"] = pos
            st = _ov_parse_pct(row.get("start"))
            if st is not None:
                o["start"] = st
            en = _ov_parse_pct(row.get("end"))
            if en is not None:
                o["end"] = en
            ovs.append(o)
        before_ovs = [dict(x) for x in make_video._seg_overlays(seg)]
        before_rev = seg.get("overlay_reveal_at")
        raw = ("" if e.get("reveal") is None else str(e["reveal"])).strip()
        if raw in ("", "-"):
            seg.pop("overlay_reveal_at", None)
        else:
            try:
                pct = max(0.0, min(float(raw.rstrip("%")), 100.0))
                seg["overlay_reveal_at"] = round(pct / 100.0, 3)
            except ValueError:
                pass
        if ovs != before_ovs:
            iv._save_overlays(s, sc, i, ovs)      # script.json + legacy sync
            changed.append(seg.get("label", f"seg{i}"))
        elif seg.get("overlay_reveal_at") != before_rev:
            iv._write_json(s.folder, "script.json", sc)
            changed.append(seg.get("label", f"seg{i}"))
    if not changed:
        return ("ok", {"msg": "nothing changed"})
    names = ", ".join(changed[:4]) + ("…" if len(changed) > 4 else "")
    return _asm_mark(s, f"text/timing on {len(changed)} beat(s): {names}")


def a_asm_speed(s, p):
    speed = float(p["speed"])
    s.tpl = iv._pin_template(s.folder, audio_speed=speed)
    return _asm_mark(s, f"voice {speed}x")

def a_asm_apply_notes(s, p):
    extra = (p.get("text") or "").strip()
    def fn():
        review = iv._read_json(s.folder, "ai_review.json") or {}
        iv._apply_ai_notes(s, review.get("fixes") or [], extra)
        if extra:
            iv._remember(s, "review standard (creator-endorsed)", extra)
        iv._invalidate(s.folder, music=True)
        kalinga.run_assemble(s.topic)
        # no auto re-review — run the AI review on demand to re-check
    return _job("regenerating flagged shots + re-assembling", fn)

def a_asm_reinforce(s, p):
    review = iv._read_json(s.folder, "ai_review.json") or {}
    note = review.get("summary", "")
    for fx in review.get("fixes") or []:
        note += f"; {fx.get('segment')}: {fx.get('problem')}"
    iv._remember(s, "review standard (creator-endorsed)", note)
    return ("ok", None)

def a_asm_accept(s, p):
    _advance()
    return ("ok", None)


# ---- thumbnail --------------------------------------------------------
def a_thumb_gen(s, p):
    facts = iv._read_json(s.folder, "facts.json")
    return _job("generating the thumbnail",
                lambda: make_video.step_thumbnail(s.folder, facts, s.tpl))

def a_thumb_regen(s, p):
    def fn():
        make_video.archive(s.folder, "thumb_bg.png", "thumbnail.png")
        make_video.step_thumbnail(s.folder, iv._read_json(s.folder,
                                  "facts.json"), s.tpl)
    return _job("regenerating the thumbnail", fn)

def a_thumb_headline(s, p):
    text = (p.get("text") or "").strip()
    if not text:
        raise ValueError("empty headline")
    def fn():
        iv._set_thumb(s.folder, text=text)
        make_video.archive(s.folder, "thumbnail.png")   # keep bg → free recompose
        make_video.step_thumbnail(s.folder, iv._read_json(s.folder,
                                  "facts.json"), s.tpl)
        iv._remember(s, "thumbnail", text)
    return _job("recomposing the thumbnail headline", fn)

def a_thumb_concept(s, p):
    concept = (p.get("text") or "").strip()
    if not concept:
        raise ValueError("empty concept")
    def fn():
        iv._set_thumb(s.folder, concept=concept)
        make_video.archive(s.folder, "thumb_bg.png", "thumbnail.png")
        make_video.step_thumbnail(s.folder, iv._read_json(s.folder,
                                  "facts.json"), s.tpl)
        iv._remember(s, "thumbnail", concept)
    return _job("regenerating the thumbnail background", fn)

def a_thumb_elements(s, p):
    """The cover EDITOR: a validated list of custom text elements
    [{text, pos 0-1, size xl|l|m|s, color}] recomposed onto the SAME
    background for free. An empty list clears the custom layout (back to the
    default ticker/headline composition)."""
    els = p.get("elements")
    if not isinstance(els, list):
        raise ValueError("elements must be a list")
    clean = []
    for el in els:
        if not isinstance(el, dict):
            continue
        t = str(el.get("text") or "").strip()
        if not t:
            continue
        e = {"text": t}
        try:
            e["pos"] = round(max(0.0, min(float(el.get("pos", 0.5)), 0.95)), 3)
        except (TypeError, ValueError):
            e["pos"] = 0.5
        sz = str(el.get("size") or "l").lower()
        e["size"] = sz if sz in make_video._EL_SIZES else "l"
        e["color"] = str(el.get("color") or "ivory").lower()
        clean.append(e)

    def fn():
        iv._set_thumb(s.folder, elements=clean)
        # bg kept → the recompose is free (no generation)
        make_video.archive(s.folder, "thumbnail.png", "teaser.png")
        make_video.step_thumbnail(s.folder, iv._read_json(s.folder,
                                  "facts.json"), s.tpl)
    return _job("recomposing the cover text", fn)


def a_thumb_accept(s, p):
    _advance()
    return ("ok", None)


# ---- seo --------------------------------------------------------------
def a_seo_gen(s, p):
    return _job("writing SEO metadata",
                lambda: make_video.step_seo(s.topic, s.folder))

def a_seo_regen(s, p):
    def fn():
        import seo
        iv._unlink(s.folder, ["seo.json", "seo.md"])
        seo.run(s.topic)
    return _job("regenerating SEO", fn)

def a_seo_feedback(s, p):
    fb = (p.get("text") or "").strip()
    if not fb:
        raise ValueError("empty feedback")
    def fn():
        import seo
        seo.run(s.topic, fb)
        iv._remember(s, "seo", fb)
    return _job("applying SEO notes", fn)

def a_seo_save(s, p):
    import seo
    json.loads(p["json"])
    (s.folder / "seo.json").write_text(p["json"])
    seo._write_md(s.folder, iv._read_json(s.folder, "seo.json"))
    return ("ok", None)

def a_seo_accept(s, p):
    _advance()
    return ("ok", None)


# ---- gates ------------------------------------------------------------
def a_gates_run(s, p):
    def fn():
        if not (s.folder / "virality.md").exists():
            try:
                kalinga.run_score(s.topic)
            except make_video.StepFailed as e:
                print(f"  ! scoring skipped: {e}")
        s.result = validate.run(s.topic)
    return _job("scoring + running gates", fn)

def a_gates_rehook(s, p):
    def fn():
        import creative
        if s.result is None:
            s.result = validate.run(s.topic)
        creative.rewrite_hook(s.topic,
                              kalinga.hook_feedback(s.result, s.folder))
        make_video.reset_hook(s.folder)
        state = iv._read_json(s.folder, "run_state.json") or {"hook_retries": 0}
        state["hook_retries"] = state.get("hook_retries", 0) + 1
        (s.folder / "run_state.json").write_text(json.dumps(state))
    out = _job("rewriting the hook → re-do seg0", fn)
    _goto("voice")
    return out

def a_gates_accept(s, p):
    _advance()
    return ("ok", None)


# ---- wrap -------------------------------------------------------------
def a_wrap_finalize(s, p):
    def fn():
        try:
            kalinga.run_critique(s.topic)
        except Exception as e:
            print(f"  ! critique skipped: {e}")
        if s.result is None:
            s.result = validate.run(s.topic)
        sess = iv._read_json(s.folder, "session.json") or {}
        now = kalinga.hf_credits()
        start = sess.get("credits_start", s.credits_start)
        spent = (start - now if start is not None and now is not None
                 else None)
        sc = iv._read_json(s.folder, "script.json") or {}
        state = iv._read_json(s.folder, "run_state.json") or {"hook_retries": 0}
        meta = {"template": s.tpl["name"],
                "script_mode": sc.get("mode"),
                "script_judge": sc.get("judge", {}).get("score", "?"),
                "hook_retries": state["hook_retries"],
                "credits": (f"{spent} spent, {now} left"
                            if spent is not None else None)}
        validate.write_report(s.topic, s.result, meta)
        if s.in_queue:
            daily.mark(s.topic, "done")
    return _job("writing the report", fn)

def a_wrap_learn(s, p):
    note = (p.get("text") or "").strip()
    if note:
        iv._remember(s, "session", note)
    def fn():
        import creative
        # channel tier: distil THIS session's edits + critiques into the
        # channel's learnings.md — only when something actually changed (same
        # guard as the terminal wrap, so the browser path is identical).
        sess = iv._read_json(s.folder, "session.json") or {}
        since = sess.get("started", "")
        versioned = iv._versioned_changes(s.folder, since)
        script_changes = iv._script_changes_plain(s.folder, since)
        if s.iterations or s.learned or versioned or script_changes:
            try:
                creative.extract_session_learnings(
                    s.topic, iterations=s.iterations, learned=s.learned,
                    versioned=versioned, script_changes=script_changes)
            except Exception as e:
                print(f"  ! learning extraction skipped: {e}")
        # global tier: promote the transferable craft to channels/learnings.md
        if s.learned:
            try:
                creative.globalize_critiques(s.learned, s.topic)
            except Exception as e:
                print(f"  ! global distill skipped: {e}")
        (s.folder / "session.json").unlink(missing_ok=True)
    return _job("saving learnings", fn)


# ---- concept ----------------------------------------------------------
def a_concept_accept(s, p):
    _advance()
    return ("ok", None)

def a_concept_skip(s, p):
    _save_concept_web(s, "")
    _advance()
    return ("ok", None)

def a_concept_set(s, p):
    """Save a creator-written/edited concept. `rewrite` (when a script exists)
    clears the script + downstream so the new concept actually takes."""
    text = (p.get("text") or "").strip()
    _save_concept_web(s, text, rewrite=bool(p.get("rewrite")))
    ctx.APP.concept_suggestion = None
    _advance()
    return ("ok", None)

def a_concept_suggest(s, p):
    def fn():
        import creative
        ctx.APP.concept_suggestion = creative.suggest_concept(s.topic) or ""
        print("  suggestion:\n  " + (ctx.APP.concept_suggestion or "(no LLM backend)"))
    return _job("AI is drafting a concept", fn)

def a_concept_refine(s, p):
    primers = (p.get("text") or "").strip()
    if not primers:
        raise ValueError("give the AI a rough idea / notes to refine")
    prior = getattr(ctx.APP, "concept_suggestion", None) or ""
    def fn():
        import creative
        ctx.APP.concept_suggestion = creative.refine_concept(
            s.topic, primers, prior=prior) or ""
        print("  refined:\n  " + (ctx.APP.concept_suggestion or "(no LLM backend)"))
    return _job("AI is refining the concept", fn)

def a_concept_length(s, p):
    tw, dr = iv._parse_length((p.get("text") or "").strip())
    if not tw:
        raise ValueError("couldn't read that — try '200', '180-240', '45s', "
                         "'1m30s'")
    kv = {"target_words": list(tw)}
    if dr:
        kv["duration_range"] = list(dr)
    s.tpl = iv._pin_template(s.folder, **kv)
    return ("ok", {"targetWords": list(tw)})

def a_concept_link(s, p):
    url = (p.get("url") or p.get("text") or "").strip()
    if not url:
        raise ValueError("paste a TikTok/Instagram video URL")
    focus = (p.get("focus") or "").strip()
    interval = p.get("interval")
    try:
        interval = float(interval) if interval else None
    except (TypeError, ValueError):
        interval = None
    def fn():
        import creative
        rec = creative.reference_from_url(s.folder, url, focus=focus,
                                          interval=interval)
        rb = (rec or {}).get("brief") or {}
        print("  reference: " + (rb.get("summary") or
              "(nothing pulled — private/unreachable link or no LLM backend)"))
    return _job("pulling the reference video", fn)


# ---- versions ---------------------------------------------------------
def a_restore(s, p):
    """Make an archived version active again (nothing is lost — the current
    copy is parked in .versions/ first). Restoring a component archives the
    assembled cut so the next assemble rebuilds with it."""
    name = make_video.restore(s.folder, p["file"])
    if name not in ("short.mp4", "nomusic.mp4", "seo.json", "seo.md",
                    "facts.json", "script.json"):
        make_video.archive(s.folder, "short.mp4", "nomusic.mp4")
    return ("ok", {"restored": name})


# ---- global -----------------------------------------------------------
def a_auto(s, p):
    def fn():
        kalinga.run_to_done(s.topic, s.folder, s.tpl,
                            in_queue=s.in_queue,
                            credits_before=s.credits_start)
        (s.folder / "session.json").unlink(missing_ok=True)
    return _job("auto mode — finishing every remaining stage headless", fn)


def a_template_switch(s, p):
    """Swap the WHOLE template (visual world + models) mid-run. Keeps this
    session's run pins. p['regen'] = adopt the new look: re-runs the director so
    the prompts/visuals re-style to the new template, and archives the old-look
    frames to regenerate. Without regen it only swaps models/mechanics."""
    name = (p.get("name") or "").strip()
    if not name or name == s.tpl.get("name"):
        return ("ok", None)
    if name not in templates.available():
        raise ValueError(f"unknown template {name!r}")
    regen = bool(p.get("regen"))
    s.tpl = iv._switch_template(s.folder, name, regen)
    if regen:
        def fn():
            import creative
            creative.direct_script(s.topic)      # re-derive the look (new world)
        return _job(f"switching to {name} — re-directing in the new look", fn)
    return ("ok", None)


ACTIONS = {
    ("global", "template"): a_template_switch,
    ("research", "run"): a_research_run,
    ("research", "rerun"): a_research_rerun,
    ("research", "save"): a_research_save,
    ("research", "accept"): a_research_accept,
    ("concept", "accept"): a_concept_accept,
    ("concept", "skip"): a_concept_skip,
    ("concept", "set"): a_concept_set,
    ("concept", "suggest"): a_concept_suggest,
    ("concept", "refine"): a_concept_refine,
    ("concept", "length"): a_concept_length,
    ("concept", "link"): a_concept_link,
    ("script", "gen"): a_script_gen,
    ("script", "reroll"): a_script_reroll,
    ("script", "feedback"): a_script_feedback,
    ("script", "judge"): a_script_judge,
    ("script", "save"): a_script_save,
    ("script", "sections"): a_script_sections,
    ("script", "accept"): a_script_accept,
    ("direction", "gen"): a_dir_gen,
    ("direction", "revise"): a_dir_revise,
    ("direction", "judge"): a_dir_judge,
    ("direction", "judgefix"): a_dir_judgefix,
    ("direction", "fill"): a_dir_fill,
    ("direction", "replan"): a_dir_replan,
    ("direction", "accept"): a_dir_accept,
    ("voice", "gen"): a_voice_gen,
    ("voice", "reread"): a_voice_reread,
    ("voice", "setvoice"): a_voice_setvoice,
    ("voice", "engine"): a_voice_engine,
    ("voice", "sample"): a_voice_sample,
    ("voice", "text"): a_voice_text,
    ("voice", "unrecord"): a_voice_unrecord,
    ("voice", "dialogue"): a_voice_dialogue,
    ("voice", "accept"): a_voice_accept,
    ("keyframes", "gen"): a_key_gen,
    ("keyframes", "one"): a_key_one,
    ("keyframes", "regen"): a_key_regen,
    ("keyframes", "tweak"): a_key_tweak,
    ("keyframes", "subgen"): a_key_subgen,
    ("keyframes", "subtweak"): a_key_subtweak,
    ("keyframes", "prompt"): a_key_prompt,
    ("keyframes", "accept"): a_key_accept,
    ("clips", "gen"): a_clip_gen,
    ("clips", "still"): a_clip_still,
    ("clips", "genall"): a_clip_genall,
    ("clips", "brief"): a_clip_brief,
    ("clips", "regen"): a_clip_regen,
    ("clips", "tweak"): a_clip_tweak,
    ("clips", "prompt"): a_clip_prompt,
    ("clips", "accept"): a_clip_accept,
    ("music", "set"): a_music_set,
    ("music", "accept"): a_music_accept,
    ("assemble", "build"): a_asm_build,
    ("assemble", "review"): a_asm_review,
    ("assemble", "overlays"): a_asm_overlays,
    ("assemble", "captions"): a_asm_captions,
    ("assemble", "seg"): a_asm_seg,
    ("assemble", "overlay"): a_asm_overlay,
    ("assemble", "ov_bulk"): a_asm_ov_bulk,
    ("assemble", "speed"): a_asm_speed,
    ("assemble", "apply_notes"): a_asm_apply_notes,
    ("assemble", "reinforce"): a_asm_reinforce,
    ("assemble", "accept"): a_asm_accept,
    ("thumbnail", "gen"): a_thumb_gen,
    ("thumbnail", "regen"): a_thumb_regen,
    ("thumbnail", "headline"): a_thumb_headline,
    ("thumbnail", "concept"): a_thumb_concept,
    ("thumbnail", "elements"): a_thumb_elements,
    ("thumbnail", "accept"): a_thumb_accept,
    ("seo", "gen"): a_seo_gen,
    ("seo", "regen"): a_seo_regen,
    ("seo", "feedback"): a_seo_feedback,
    ("seo", "save"): a_seo_save,
    ("seo", "accept"): a_seo_accept,
    ("gates", "run"): a_gates_run,
    ("gates", "rehook"): a_gates_rehook,
    ("gates", "accept"): a_gates_accept,
    ("wrap", "finalize"): a_wrap_finalize,
    ("wrap", "learn"): a_wrap_learn,
    ("versions", "restore"): a_restore,
    ("global", "auto"): a_auto,
}
