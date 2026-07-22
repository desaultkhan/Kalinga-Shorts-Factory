"""
webui.state — the JSON snapshots the page renders from.

build_state() dispatches on ctx.MODE (home | video). This module holds the HOME
launcher state, every per-stage snapshot builder (_ss_*), the keyframe/clip node
builders, the deferred-assemble helpers, and the STAGE_STATE map wiring a stage
name → its snapshot function. Pure reads — it never mutates a run; the action
handlers (webui.actions) do the writing.
"""
from __future__ import annotations
import json
import re
import html
from datetime import date, datetime
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
from .context import _art, _mtime, _jobinfo


def _vers(name: str):
    """Prior versions of a canonical artifact (newest last) from the index —
    each {file, ts} so the page can preview/restore them."""
    return make_video.list_versions(ctx.APP.s.folder).get(name, [])

def build_state() -> dict:
    """The snapshot the page renders from — dispatched by ctx.MODE."""
    if ctx.MODE == "home":
        return home_state()
    return _video_state()

# ---- ctx.HOME launcher ----------------------------------------------------
def _folder_card(d: Path) -> dict:
    """A run-folder summary for the launcher: topic, date, and which
    deliverables already exist (so the home grid shows progress at a glance)."""
    name = d.name
    date, _, topic = name.partition("_")
    has = lambda n: config.art(d, n).exists() if False else (d / n).exists()
    return {
        "folder": name, "topic": topic or name, "date": date,
        "facts": (d / "facts.json").exists(),
        "brief": (d / "brief.md").exists(),
        "script": (d / "script.json").exists(),
        "video": (d / "short.mp4").exists(),
        "refs": (d / "concept").exists()
        and any((d / "concept").glob("*")),
        "mtime": _mtime(d),
    }


def _cast_home(ch) -> dict:
    """The channel's cast roster + the voice/outfit catalogs, for the home
    screen's 🎭 Cast editor. Pure read — the editor's writes go through
    session.run_tool (cast_save / cast_delete / cast_avatar / cast_refs)."""
    import cast_setup
    engine, _, _ = cast_setup._engine_and_style()
    catalog = (cast_setup.EDGE_CATALOG if engine == "edge"
               else cast_setup.INWORLD_CATALOG)
    outfits, when = cast_setup.outfit_names(), cast_setup._when_map()
    members = []
    for nm, m in (ch.cast or {}).items():
        v = m.get("voice")
        v = v.get(engine) if isinstance(v, dict) else v
        av = ch.cast_avatar(nm)
        by = {}
        for r in (m.get("refs") or []):
            if isinstance(r, dict) and r.get("outfit"):
                by[r["outfit"]] = by.get(r["outfit"], 0) + 1
        avatar = None
        if av.exists():
            try:
                rel = str(av.relative_to(ch.dir))
            except ValueError:
                rel = f"cast/{av.name}"
            avatar = {"url": "/castimg/" + rel, "mtime": _mtime(av)}
        members.append({"name": nm,
                        "personality": m.get("personality") or "",
                        "gender": m.get("gender") or "neutral",
                        "appearance": m.get("appearance") or "",
                        "voice": v or "",
                        "avatar": avatar,
                        "refs": by})
    return {"engine": engine, "members": members,
            "voices": [{"voice": vv, "gender": g, "note": n}
                       for vv, g, n in catalog],
            "genders": list(cast_setup.GENDER_VOICE),
            "outfits": [{"name": o, "when": when.get(o, ""), "per": 12}
                        for o in outfits]}


def home_state() -> dict:
    names = config.available()
    cur = ctx.HOME.get("channel") or (names[0] if len(names) == 1 else None)
    title, folders, tpls, default_tpl, queue = None, [], [], None, []
    if cur and cur in names:
        ch = config.Channel(cur)
        title = ch.title
        default_tpl = ch.default_template
        tdir = ch.templates_dir
        if tdir.exists():
            tpls = sorted(p.stem for p in tdir.glob("*.yaml"))
        out = ch.output
        if out.exists():
            dirs = [p for p in out.glob("*") if p.is_dir()]
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            folders = [_folder_card(p) for p in dirs]
        queue = _pending_queue(ch)
    shows, ideas = [], None
    if cur and cur in names:
        for nm, ov in (ch.shows or {}).items():   # the channel's FORMATS
            ov = ov or {}
            shows.append({"name": nm, "kind": ov.get("kind", "video"),
                          "premise": " ".join(
                              str(ov.get("premise", "")).split())[:110]})
        ip = ch.dir / "ideas.json"                # last AI topic ideation
        try:
            ideas = json.loads(ip.read_text()) if ip.exists() else None
        except ValueError:
            ideas = None
    cast = None
    if cur and cur in names:
        try:
            cast = _cast_home(ch)
        except Exception:                            # noqa: BLE001 — advisory
            cast = None
    draft = None                     # the AI channel-designer's pending draft
    try:
        dp = config.CHANNELS_DIR / ".channel_draft.json"
        draft = json.loads(dp.read_text()) if dp.exists() else None
    except ValueError:
        draft = None
    jobs = ctx.jobs_summary()
    running = {j["folder"] for j in jobs if j["status"] == "running"
               and j["folder"]}
    for f in folders:                 # mark cards whose job is mid-flight
        f["busy"] = f["folder"] in running
    return {
        "mode": "home",
        "channels": names,
        "channel": cur,
        "channelTitle": title,
        "folders": folders,
        "templates": tpls,            # visual worlds — picked when starting a run
        "defaultTemplate": default_tpl,
        "shows": shows,               # the channel's shows (formats) — per-run pick
        "ideas": ideas,               # last AI topic ideation (ideas.json)
        "queue": queue,               # pending topics — start a run straight from it
        "cast": cast,                 # the 🎭 Cast editor's roster + catalogs
        "channelDraft": draft,        # AI-designed channel awaiting review
        "credits": kalinga.hf_credits(),
        "job": _jobinfo(),
        "jobs": jobs,                 # every project's jobs — the in-flight board
        "setupWarning": ctx.SETUP.get("warning"),
    }


def _pending_queue(ch) -> list:
    """The channel's PENDING queue.csv rows (topic[,status,date,concept]) as
    {topic, concept} — the to-do list shown as a 'start from queue' dropdown on
    the home screen. Read straight off the file (channel-scoped, no global state);
    `done`/`failed` rows are skipped, original order kept (next-up first)."""
    import csv
    out = []
    if not ch.queue.exists():
        return out
    try:
        with ch.queue.open() as f:
            for row in csv.reader(f):
                if not row or not row[0].strip():
                    continue
                if len(row) >= 2 and row[1].strip() not in ("", "pending"):
                    continue
                out.append({"topic": row[0].strip(),
                            "concept": row[3].strip() if len(row) > 3 else "",
                            "show": row[4].strip() if len(row) > 4 else ""})
    except Exception:                            # noqa: BLE001 (queue is advisory)
        pass
    return out


def _video_state() -> dict:
    s = ctx.APP.s
    folder, ch, tpl = s.folder, config.channel(), s.tpl
    done = iv._done_map(folder)
    credits = kalinga.hf_credits()
    st = {
        "mode": "video",
        "topic": s.topic,
        "channel": ch.name,
        "channelTitle": ch.title,
        "template": tpl.get("name"),
        "templates": templates.available(),
        "credits": credits,
        "creditsStart": s.credits_start,
        "budget": tpl.get("budget_credits"),
        "stage": ctx.APP.stage,
        "stageName": iv.STAGE_ORDER[ctx.APP.stage],
        "stages": [{"name": n, "done": done[n],
                    "current": i == ctx.APP.stage}
                   for i, n in enumerate(iv.STAGE_ORDER)],
        "job": _jobinfo(),
        "iterations": s.iterations,
        "setupWarning": ctx.SETUP.get("warning"),
    }
    name = iv.STAGE_ORDER[ctx.APP.stage]
    st["data"] = STAGE_STATE.get(name, lambda: {})()
    try:
        st["board"] = _board()
    except Exception:                                # noqa: BLE001 — advisory
        st["board"] = None
    return st


def _board():
    """The video STORYBOARD — the whole project at a glance, one entry per
    beat: the spoken text, the keyframe thumb, and voice/clip/section/overlay
    status."""
    s = ctx.APP.s
    folder = s.folder
    doc = iv._read_json(folder, "script.json") or {}
    segs = doc.get("segments") or []
    if not segs:
        return None
    import creative
    out = []
    for i, sg in enumerate(segs):
        try:
            nsec = len(creative.section_sizes(sg))
        except Exception:                            # noqa: BLE001
            nsec = 1
        voiced = config.art(folder, f"seg{i}.mp3").exists() \
            or bool(config.art_glob(folder, f"seg{i}_s*.mp3"))
        try:
            n_ov = len(make_video._seg_overlays(sg))
        except Exception:                            # noqa: BLE001
            n_ov = 0
        clip = config.art(folder, f"clip{i}.mp4").exists() \
            or bool(config.art_glob(folder, f"clip{i}_*.mp4"))
        out.append({
            "i": i,
            "label": sg.get("label") or f"BEAT {i}",
            "text": (sg.get("text") or "")[:220],
            "words": len((sg.get("text") or "").split()),
            "nsec": nsec,
            "kf": _art(folder, f"key{i}.png"),
            "clip": clip,
            "still": not sg.get("animate", True) and not sg.get("clip_voice"),
            "voiced": voiced,
            "recorded": bool(sg.get("voice_recorded")),
            "speaker": sg.get("speaker") or "",
            "overlays": n_ov,
        })
    return {
        "beats": out,
        "directed": bool(doc.get("directed")),
        "cut": _art(folder, "short.mp4") is not None,
    }


# ---- per-stage state builders -----------------------------------------
def _ss_research():
    return {"facts": iv._read_json(ctx.APP.s.folder, "facts.json")}


def _ss_script():
    sc = iv._read_json(ctx.APP.s.folder, "script.json") or {}
    # the per-beat SECTION preview: sentences + the effective grouping
    # (creative.section_sizes — the same source _sectionize_beats voices
    # from), so the creator sees and edits exactly how the beat will break
    import creative
    sections = []
    for s in sc.get("segments", []):
        if s.get("lines"):                       # dialogue → its own editor
            sections.append(None)
            continue
        sections.append({"sents": creative._sentences(s.get("text", "")),
                         "sizes": creative.section_sizes(s),
                         "custom": bool(s.get("section_breaks"))})
    return {"script": sc, "judge": sc.get("judge", {}),
            "mode": sc.get("mode"),
            "drafts": len(sc.get("draft_history", [])),
            "sections": sections,
            "versions": _vers("script.json")}


def _ss_direction():
    sc = iv._read_json(ctx.APP.s.folder, "script.json") or {}
    segs = sc.get("segments", [])
    # normalize an existing judge (backfill its improve note) so the "apply the
    # judge's fix" button always shows — cheap, never triggers an LLM call
    import creative
    judge = creative.ensure_direction_judge(ctx.APP.s.topic, compute=False) \
        if sc.get("directed") else {}
    # per-section judge notes → shown on each beat's card so all can be worked at once
    notes = {n.get("label"): n for n in (judge.get("segments") or [])}
    return {
        "directed": bool(sc.get("directed")),
        "direction": sc.get("direction") or {},
        "judge": judge,
        "segments": [{
            "label": sg.get("label"), "visual": sg.get("visual"),
            "overlay": sg.get("overlay"),
            "motion": sg.get("motion"), "tease": sg.get("tease"),
            "animate": sg.get("animate", True),
            "ref_idx": sg.get("ref_idx") or [],
            "judgeNote": (notes.get(sg.get("label")) or {}).get("improve", ""),
        } for sg in segs],
    }


def _ss_voice():
    folder = ctx.APP.s.folder
    engine, voice = iv._voice_settings(ctx.APP.s.tpl)
    audio = iv._read_json(folder, "audio.json") or {}
    script = iv._read_json(folder, "script.json") or {}
    amap = {a["index"]: a for a in audio.get("segments", [])}
    # units come from SCRIPT.json so they (and their Record buttons) show even
    # BEFORE any AI voice is generated — record-first; audio.json enriches them
    segs = []
    for i, sg in enumerate(script.get("segments", [])):
        m = amap.get(i, {})
        specs = make_video._section_specs(sg)
        native = bool(sg.get("native_audio"))
        sections = []
        if specs:
            asec = {sx.get("k"): sx for sx in (m.get("sections") or [])}
            for k, sp in enumerate(specs):
                sx = asec.get(k, {})
                sections.append({"k": k, "say": (sp.get("say") or "").strip(),
                                 "duration": round(sx.get("duration", 0), 1),
                                 "recorded": bool(sx.get("recorded")),
                                 "audio": (_art(folder, sx["file"])
                                           if sx.get("file") else None)})
        # a multi-speaker beat (>1 spoken `lines`) exposes a per-line dialogue
        # editor — each line's START (or 'auto' for back-to-back) and EXPRESSION,
        # with the last render's start/duration from audio.json for reference.
        # Mirrors interactive `_st_dialogue`.
        lines = []
        spoken = [l for l in (sg.get("lines") or [])
                  if (l.get("text") or "").strip()]
        if len(spoken) > 1:
            ametaL = {k: ml for k, ml in enumerate(m.get("lines", []))}
            for k, ln in enumerate(spoken):
                ml = ametaL.get(k, {})
                lines.append({
                    "k": k, "speaker": ln.get("speaker") or "?",
                    "text": ln.get("text", ""),
                    "start": ln.get("start"), "gap": ln.get("gap"),
                    "expression": ln.get("expression") or "",
                    "renderStart": ml.get("start"),
                    "renderDur": ml.get("duration")})
        segs.append({"index": i, "label": sg.get("label"),
                     "duration": round(m.get("duration", 0), 1),
                     "text": sg.get("text", ""),
                     "recorded": bool(m.get("recorded")),
                     "native": native,
                     "sections": sections,
                     "lines": lines,
                     "audio": (None if specs or native
                               else _art(folder, f"seg{i}.mp3")),
                     "versions": _vers(f"seg{i}.mp3")})
    aligned = False
    try:
        import align
        aligned = align.whisper_available()
    except Exception:                             # noqa: BLE001
        pass
    # the pickers: every registered TTS engine (with availability), and the
    # CURRENT engine's audition-able voice catalog (cast_setup's curated lists
    # for edge/Inworld, the engine's pool otherwise)
    engines = []
    for nm, e in make_video.ENGINES.items():
        ok, why = e.available()
        engines.append({"name": nm, "label": e.label, "free": e.free,
                        "ok": ok, "why": why})
    try:
        import cast_setup
        cat = {"edge": cast_setup.EDGE_CATALOG,
               "higgsfield": cast_setup.INWORLD_CATALOG}.get(engine)
    except Exception:                             # noqa: BLE001
        cat = None
    if cat:
        voices = [{"voice": v, "gender": g, "note": n} for v, g, n in cat]
    else:
        eng = make_video.ENGINES.get(engine)
        voices = [{"voice": v, "gender": "", "note": ""}
                  for v in (eng.pool if eng else [])]
    return {"engine": engine, "voice": voice,
            "engines": engines, "voices": voices,
            "cachedEngine": audio.get("engine"), "segments": segs,
            "generated": bool(audio.get("segments")), "whisper": aligned}


def _kf_prompt(shot, facts, folder, tpl):
    """The exact keyframe prompt that WILL be sent (best-effort — never fatal in
    a state build)."""
    try:
        return make_video.keyframe_prompt(shot, facts, folder, tpl)
    except Exception:                             # noqa: BLE001
        return ""


def _kf_ref(shot, folder, tpl, labels):
    """The ONE reference IMAGE this keyframe is conditioned on (`--image`) — the
    dependency. {kind, label, art} where art is a servable thumbnail when the ref
    is a prior keyframe IN this run, else None (a cast avatar / style ref lives
    outside the run folder). None when the shot sends no reference."""
    try:
        ref_img, _ = make_video._single_keyframe_ref(shot, folder, tpl)
    except Exception:                             # noqa: BLE001
        return None
    if not ref_img or len(ref_img) < 2:
        return None
    p = Path(ref_img[1])
    name = p.name
    m = re.match(r"^key(\d+)(_\d+)?\.png$", name)
    if m and folder in p.parents:                 # a prior keyframe in this run
        j = int(m.group(1))
        lbl = labels[j] if j < len(labels) else f"seg{j}"
        return {"kind": "continuity", "label": f"continues {lbl}",
                "art": _art(folder, name)}
    low = str(p).lower()
    if "/cast/" in low or "\\cast\\" in low:
        return {"kind": "cast", "label": f"cast ref: {p.stem}", "art": None}
    return {"kind": "style", "label": "style reference", "art": None}


def _kf_node(shot, i, subk, seg, label, multi, name, facts, folder, tpl, labels):
    """One timeline node (#2) — a single renderable shot, with the ONE earlier
    shot it depends on (`dep` → another node's id) so the UI can draw the
    dependency line. `dep` is the PLANNED dependency (independent of what's on
    disk); `refArt` is the actual built-from thumbnail when it already exists."""
    ref = _kf_ref(shot, folder, tpl, labels)           # actual (existing) ref
    if (dep_name := make_video.keyframe_dep(i, subk, seg)) is None:
        kind, dlabel = ("style", "template look") if (
            not ref or ref.get("kind") == "style") else ("fresh", "fresh shot")
        if subk is None and (seg.get("ref_idx") == []):
            kind, dlabel = "fresh", "fresh — no continuity"
    elif ref and ref.get("kind") == "cast":
        kind, dlabel = "cast", ref["label"]
    else:
        m = re.match(r"^key(\d+)(?:_(\d+))?\.png$", dep_name)
        dj = int(m.group(1)) if m else i
        intra = bool(subk) and dep_name in (
            make_video.keyframe_name(i, (subk or 1) - 1),)
        jlbl = labels[dj] if dj < len(labels) else f"seg{dj}"
        if intra:
            ds = (subk or 1) - 1
            kind = "intra"
            dlabel = f"continues {label} · shot {ds}"
        else:
            kind = "cross"
            dlabel = f"matches {jlbl}"
    return {
        "id": name, "seg": i, "sub": subk, "label": label, "multi": multi,
        "main": (subk in (None, 0)),
        "shotNo": (0 if subk is None else subk),
        "img": _art(folder, name),
        "prompt": _kf_prompt(shot, facts, folder, tpl),
        "override": bool((seg.get("image_prompt") or "").strip()),
        "extra": seg.get("visual_extra", ""),
        "visual": seg.get("visual"),
        "dep": dep_name, "depKind": kind, "depLabel": dlabel,
        "refArt": (ref or {}).get("art"),
        # ⭐ the image on disk was generated from a DIFFERENT prompt than
        # today's (a rewrite kept it instead of archiving) — regen to match
        "stale": make_video.keyframe_stale(shot, facts, folder, tpl),
        "versions": _vers(name),
    }


def _ss_keyframes():
    s = ctx.APP.s
    sc = iv._read_json(s.folder, "script.json") or {}
    facts = iv._read_json(s.folder, "facts.json") or {}
    cast = sc.get("cast") or {}
    labels = [sg.get("label", f"seg{i}")
              for i, sg in enumerate(sc.get("segments", []))]
    nodes = []          # flat, render-ordered timeline of every shot (#2)
    for i, sg in enumerate(sc.get("segments", [])):
        shot = {"index": i, "label": sg.get("label"), "seg": sg, "cast": cast,
                "need": 0.0, "key": config.art(s.folder, f"key{i}.png"),
                "clip": config.art(s.folder, f"clip{i}.mp4")}
        subs = make_video._subshots(shot, s.folder)
        multi = len(subs) > 1
        for sub in subs:
            # the shot dict + its sub index (None for a single-shot segment)
            subk = sub.get("sub") if multi else None
            sshot = sub if multi else shot
            segv = sshot["seg"]
            nm = make_video.keyframe_name(i, subk if subk else 0)
            nodes.append(_kf_node(sshot, i, subk, segv, sg.get("label"),
                                  multi, nm, facts, s.folder, s.tpl, labels))
    return {"perKeyframe": iv.CREDITS_PER_KEYFRAME, "nodes": nodes}


def _clip_prompt(sub, tpl):
    """The exact clip prompt that WILL be sent (best-effort — never fatal in a
    state build)."""
    try:
        return make_video.clip_prompt(sub, tpl)
    except Exception:                             # noqa: BLE001
        return ""


def _ss_clips():
    s = ctx.APP.s
    sc = iv._read_json(s.folder, "script.json")
    audio = iv._read_json(s.folder, "audio.json")
    nodes = []          # flat, render-ordered timeline of every shot (#2 parity)
    if sc and audio:
        try:
            shots = make_video.shot_list(sc, audio, s.folder)
        except Exception:
            shots = []
        for i, sh in enumerate(shots):
            subs = make_video._subshots(sh, s.folder)
            plan = make_video.shot_plan(sh["seg"], i)
            wants = make_video._plan_clip_wanted(plan, s.tpl)
            multi = len(subs) > 1
            for sub, want in zip(subs, wants):
                subk = sub.get("sub") if multi else None
                segv = sub["seg"]
                dur = make_video.clip_duration(sub, s.tpl)
                nm = sub["clip"].name
                nodes.append({
                    "id": nm, "seg": i, "sub": subk, "label": sh["label"],
                    "multi": multi, "main": (subk in (None, 0)),
                    "shotNo": (0 if subk is None else subk),
                    "animate": bool(want),
                    # has a real DYNAMIC clip_motion (own or inherited) vs only
                    # the gentle Ken Burns push → drives the "write a dynamic
                    # brief" offer when converting a still to a moving clip
                    "dynamic": bool((segv.get("clip_motion") or "").strip()),
                    "dur": dur,
                    "cost": round(dur * iv.CREDITS_PER_CLIP_SEC),
                    "prompt": _clip_prompt(sub, s.tpl),
                    "useStill": bool(segv.get("use_still")),
                    "override": bool((segv.get("clip_prompt_text") or "").strip()),
                    "motion_extra": segv.get("motion_extra", ""),
                    "clip": _art(s.folder, nm),
                    "img": _art(s.folder, sub["key"].name),
                    "stale": make_video.clip_stale(sub, s.folder, s.tpl),
                    "versions": _vers(nm),
                })
    return {"perClipSec": iv.CREDITS_PER_CLIP_SEC, "nodes": nodes}


def _ss_music():
    ch = config.channel()
    tracks = (sorted(p.name for p in ch.music_dir.glob("*.mp3"))
              if ch.music_dir.exists() else [])
    ov = iv._read_json(ch.overrides.parent, ch.overrides.name) or {}
    return {"tracks": tracks, "current": ov.get("music") or "none"}


def _ss_assemble():
    s = ctx.APP.s
    short = _art(s.folder, "short.mp4")
    review = iv._read_json(s.folder, "ai_review.json")
    dur = (make_video.ffprobe_duration(s.folder / "short.mp4")
           if short else 0)
    vers = sorted(p.name for p in s.folder.glob("short_v*.mp4"))
    return {
        "short": short, "duration": round(dur),
        "overlays": s.tpl.get("assemble_overlays", True),
        "speed": s.tpl.get("audio_speed", 1.0),
        "review": review,
        "captions": s.tpl.get("assemble_captions", True),
        "iterations": [{"name": v, "mtime": _mtime(s.folder / v)}
                       for v in vers],
        "history": _vers("short.mp4"),
        "music": _ss_music(),
        "segments": _asm_segments(s),
        "pending": _asm_pending(s),
    }


# ---- deferred assemble: edits STAGE changes; one confirmed re-assemble applies
# them. Every assemble edit (per-segment, overlays/subtitles/speed) mutates
# script.json/template instantly but does NOT re-assemble — it records a pending
# change here, so the creator can finish editing and trigger ONE rebuild. The
# pending list (+ whether a re-voice is needed) is persisted so it survives a
# reload and the apply knows what to do.
def _asm_pending(s):
    p = iv._read_json(s.folder, "asm_pending.json") or {}
    return {"changes": p.get("changes") or [], "revoice": bool(p.get("revoice"))}


def _asm_mark(s, desc, revoice=False):
    """Record one staged assemble change (no rebuild). Returns an instant ('ok')
    action result so the UI just re-renders with the updated pending banner."""
    p = _asm_pending(s)
    p["changes"].append(desc)
    p["revoice"] = p["revoice"] or revoice
    iv._write_json(s.folder, "asm_pending.json", p)
    return ("ok", None)


def _asm_clear(s):
    iv._unlink(s.folder, ["asm_pending.json"])


def _ov_pct(v):
    """A stored overlay start/end (fraction 0-1, or seconds >1, or None) as an
    editor-friendly percent int — None stays None (the default/hold)."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(v * 100)) if v <= 1.0 else round(v, 1)


def _asm_segments(s):
    """Per-segment assemble state (the browser equivalent of the terminal `[g]`
    per-segment editor): the on-screen overlay list (each text + position +
    start→end window), text-reveal timing, and — for a segment with a generated
    clip — clip fill + the native/full/mix audio modes."""
    sc = iv._read_json(s.folder, "script.json") or {}
    out = []
    for i, seg in enumerate(sc.get("segments", [])):
        has_clip = config.art(s.folder, f"clip{i}.mp4").exists()
        rf = seg.get("overlay_reveal_at")
        ovs = make_video._seg_overlays(seg)
        out.append({
            "i": i, "label": seg.get("label", f"seg{i}"),
            "overlays": len(ovs),
            "overlayList": [{"text": o.get("text", ""),
                             "pos": o.get("pos") or "",
                             "start": _ov_pct(o.get("start")),
                             "end": _ov_pct(o.get("end"))} for o in ovs],
            "reveal": (round(float(rf) * 100) if isinstance(rf, (int, float))
                       else None),
            "hasClip": has_clip,
            "fill": iv._clip_fill(seg, s.tpl) if has_clip else None,
            "native": bool(seg.get("native_audio")),
            "full": bool(seg.get("full_clip")),
            "mix": bool(seg.get("mix_clip_audio")),
        })
    return out


def _ss_thumbnail():
    s = ctx.APP.s
    thumb = (((iv._read_json(s.folder, "script.json") or {})
              .get("direction") or {}).get("thumbnail") or {})
    try:      # the exact bg prompt — shown beside the cover editor
        prompt = make_video.thumb_bg_prompt(
            s.folder, iv._read_json(s.folder, "facts.json") or {}, s.tpl)
    except Exception:                            # noqa: BLE001 — advisory
        prompt = None
    return {"img": _art(s.folder, "thumbnail.png"),
            "teaser": _art(s.folder, "teaser.png"),
            "bg": _art(s.folder, "thumb_bg.png"),
            "text": thumb.get("text"), "concept": thumb.get("concept"),
            "elements": thumb.get("elements") or [],
            "prompt": prompt,
            "perImage": iv.CREDITS_PER_KEYFRAME,
            "versions": _vers("thumbnail.png")}


def _ss_seo():
    s = ctx.APP.s
    meta = iv._read_json(s.folder, "seo.json")
    issues = validate.seo_lint(s.folder, s.topic) if meta else []
    return {"seo": meta, "lint": issues}


def _ss_gates():
    s = ctx.APP.s
    result = s.result
    state = iv._read_json(s.folder, "run_state.json") or {"hook_retries": 0}
    return {"result": result,
            "retries": state.get("hook_retries", 0),
            "maxRetries": s.tpl.get("max_hook_retries", 1),
            "scored": (s.folder / "virality.md").exists()}


def _ss_wrap():
    s = ctx.APP.s
    return {
        "short": str(s.folder / "short.mp4"),
        "seo": str(s.folder / "seo.md"),
        "report": str(s.folder / "report.md"),
        "hasMusic": (s.folder / "music.txt").exists(),
        "learned": len(s.learned),
        "inQueue": s.in_queue,
    }


def _ss_concept():
    """The creator's CONCEPT — the hook idea + theme/motif this video runs on
    (feeds the writer's opening beat + the director's world/motif). Mirrors the
    terminal st_concept; optional (skip for the default treatment)."""
    import creative
    import daily
    s = ctx.APP.s
    (s.folder / "concept").mkdir(exist_ok=True)
    imgs, vids = creative._reference_files(s.folder)
    lo, hi = s.tpl.get("target_words", [300, 500])
    used = [{"topic": t, "concept": c} for t, c in
            daily.concepts(exclude=s.topic)[-6:]]
    return {
        "concept": creative.user_concept(s.folder),
        "used": used,
        "refImages": len(imgs), "refVideos": len(vids),
        "targetWords": [lo, hi],
        "hasScript": (s.folder / "script.json").exists(),
        "suggestion": getattr(ctx.APP, "concept_suggestion", None),
    }


def _save_concept_web(s, text: str, rewrite: bool = False) -> None:
    """Non-interactive concept save (the browser passes an explicit `rewrite`
    flag instead of the terminal y/n prompt)."""
    import daily
    text = (text or "").strip()
    had = iv._read_json(s.folder, "concept.json") or {}
    iv._write_json(s.folder, "concept.json", {"concept": text, "set": True})
    daily.set_concept(s.topic, text)
    if (had.get("concept") or "") == text:
        return
    if rewrite and (s.folder / "script.json").exists():
        iv._unlink(s.folder, ["script.json", "audio.json", "seg*.mp3",
                              "key*.png", "clip*.mp4", "text*.png",
                              "seo.json", "seo.md", "virality.md",
                              "thumbnail.png", "thumb_bg.png", "ai_review.json",
                              "report.md", "critique.md"] + list(iv.ASSEMBLY))


STAGE_STATE = {
    "research": _ss_research,
    "concept": _ss_concept,
    "script": _ss_script, "direction": _ss_direction, "voice": _ss_voice,
    "keyframes": _ss_keyframes, "clips": _ss_clips, "music": _ss_music,
    "assemble": _ss_assemble, "thumbnail": _ss_thumbnail, "seo": _ss_seo,
    "gates": _ss_gates, "wrap": _ss_wrap,
}
