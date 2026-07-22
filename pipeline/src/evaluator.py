"""
Evaluator agent — critiques the finished video BEFORE publishing.

Samples frames from the video and sends them + the script through llm.py
(default: `claude -p` / claude-opus-4-8 on the user's subscription; Anthropic
API fallback). The model:
1. Critiques hook strength, animation/visual clarity, pacing, content depth
2. Simulates the comment section: realistic comments viewers would leave
   about the animation, the content, and the hook (positive and negative)
3. Scores each axis 1-10 and flags anything worth fixing before upload

Writes critique.md into the stock folder.

Usage:
    python evaluator.py AAPL
"""
from __future__ import annotations
import prompts
import json
import subprocess
import sys
from pathlib import Path

import config
import llm

PROMPT = prompts.load("evaluator.critic")


def sample_frames(video: Path, folder: Path, n: int = 6) -> list:
    """Extract n evenly spaced frames to frame<i>.jpg in the folder."""
    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True,
        stdin=subprocess.DEVNULL).stdout)
    paths = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        out = config.art(folder, f"frame{i}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "quiet", "-ss", f"{t:.2f}",
             "-i", str(video), "-vframes", "1", "-q:v", "5", str(out)],
            capture_output=True, stdin=subprocess.DEVNULL)
        if out.exists():
            paths.append(out)
    return paths


def critique(topic: str):
    if not llm.available():
        print("  ! evaluator needs an LLM backend (claude CLI or "
              "ANTHROPIC_API_KEY) — skipped", file=sys.stderr)
        return None
    ch = config.channel()
    folder = config.topic_dir(topic)
    import templates
    tpl = templates.load_pinned(folder)
    script = json.loads((folder / "script.json").read_text())
    frames = sample_frames(folder / "short.mp4", folder)
    import creative
    lt = creative.learnings_tail(cap=1200)
    lt = (f"\nThe creator's learned standards — hold the video to THESE and "
          f"call out any violation:\n{lt}\n" if lt else "")
    text = llm.ask(
        PROMPT.format(channel=ch.title, premise=ch.premise, learnings=lt,
                      composition=json.dumps(
                          _composition(script["segments"], tpl), indent=1),
                      script=json.dumps(script["segments"], indent=1)),
        max_tokens=2200, image_paths=frames)
    (folder / "critique.md").write_text(text)
    for f in frames:
        try:
            f.unlink()
        except OSError:
            pass
    print(f"  critique ({llm.describe()}) -> {folder.name}/critique.md")
    return text


REVIEW_PROMPT = prompts.load("evaluator.review")


def _json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError(f"no JSON object in review: {text[:200]}")
    return json.loads(text[a:b + 1])


def _composition(segs: list, tpl: dict) -> list:
    """The per-segment LAYER breakdown the critic needs to attribute a problem
    to the right artifact (base image vs on-screen text), instead of blaming
    the keyframe for an overlay issue."""
    reveal = tpl.get("reveal_overlay_text", True)
    out = []
    for s in segs:
        anim = s.get("animate", True)
        ov = (s.get("overlay") or "").strip()
        subshots = s.get("shots") if isinstance(s.get("shots"), list) else None
        if subshots and len(subshots) >= 2:
            # a MULTI-SHOT beat: several distinct shots are stitched under one
            # voiceover, and the review samples the beat's MIDPOINT — so the
            # frame you see is ONE of these shots, not the segment's first
            # visual. A keyframe fix regenerates ALL sub-shots of the beat
            # (only when the beat is a still beat — an animated beat's clips
            # aren't auto-re-rolled).
            fixable = ("keyframe-fixable — regenerates the whole beat" if not anim
                       else "has animated clip(s) — base not auto-re-rolled")
            base = (f"MULTI-SHOT beat: {len(subshots)} distinct shots stitched; "
                    f"the sampled frame is the beat's midpoint (a LATER shot). "
                    f"{fixable}")
            depicts = " || ".join((sp.get("visual") or "")[:90]
                                  for sp in subshots) or "(default world)"
        else:
            base = ("animated clip (cannot be re-rolled)" if anim
                    else "still image — Ken Burns (keyframe-fixable)")
            depicts = (s.get("visual") or "")[:160] or "(default world)"
        out.append({
            "label": s.get("label"),
            "base_image": base,
            "image_depicts": depicts,
            "on_screen_text": ov or "(none)",
            "text_layer": ("separate timed overlay — NOT part of the image"
                           if (reveal and ov)
                           else "baked into the image" if ov else "(none)"),
        })
    return out


def _segment_frames(topic: str, tpl: dict) -> list:
    """One frame per segment, sampled at the segment's midpoint of the
    ASSEMBLED video (so overlays/ken-burns framing are included)."""
    import make_video
    folder = config.topic_dir(topic)
    audio = json.loads((folder / "audio.json").read_text())
    speed = float(tpl.get("audio_speed", 1.0))
    final = folder / "short.mp4"
    frames, t = [], 0.0
    for seg in audio["segments"]:
        need = (make_video._voice_end(config.art(folder, f"seg{seg['index']}.mp3"))
                / max(speed, 0.1))
        out = config.art(folder, f"rev{seg['index']}.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-ss",
                        f"{t + need / 2:.2f}", "-i", str(final), "-vframes",
                        "1", "-q:v", "4", str(out)], capture_output=True,
                       stdin=subprocess.DEVNULL)
        if out.exists():
            frames.append((seg["label"], out))
        t += need
    return frames


def _review(topic: str, tpl: dict) -> dict:
    ch = config.channel()
    folder = config.topic_dir(topic)
    segs = json.loads((folder / "script.json").read_text())["segments"]
    kb = [s["label"] for s in segs if not s.get("animate", True)]
    import creative
    lt = creative.learnings_tail(cap=1200)
    lt = (f"\nThe creator's learned standards — hold the video to THESE "
          f"(flag anything that violates them):\n{lt}\n" if lt else "")
    frames = _segment_frames(topic, tpl)
    raw = llm.ask(REVIEW_PROMPT.format(
        channel=ch.title, premise=ch.premise,
        kenburns=", ".join(kb) or "none", learnings=lt,
        composition=json.dumps(_composition(segs, tpl), indent=1),
        script=json.dumps([{k: s.get(k) for k in ("label", "text", "overlay")}
                           for s in segs], indent=1)),
        max_tokens=1800, image_paths=[p for _, p in frames])
    for _, p in frames:
        p.unlink(missing_ok=True)
    data = _json(raw)
    fixes = data.get("fixes", []) or []
    scores = data.get("scores", {}) or {}
    score_line = ("  ·  ".join(f"{k} {v}/10" for k, v in scores.items())
                  if scores else "")
    md = ("# AI video review\n\n**Verdict:** "
          + ("PASS" if data.get("pass") else "changes needed") + "\n\n"
          + (score_line + "\n\n" if score_line else "")
          + data.get("summary", "") + "\n\n"
          + "".join(f"- **{f.get('segment')}** ({f.get('action')}): "
                    f"{f.get('problem')} — {f.get('instruction', '')}\n"
                    for f in fixes))
    return {"pass": bool(data.get("pass")), "summary": data.get("summary", ""),
            "scores": scores, "fixes": fixes, "md": md}


def _seg_overlays_for_edit(seg: dict) -> list:
    """The segment's overlays as an editable list of dicts — normalizing the
    legacy single `overlay` string into one entry so a repair can set scrim /
    pos / color on it. Mirrors make_video._seg_overlays' normalization."""
    raw = seg.get("overlays")
    out = []
    if isinstance(raw, list) and raw:
        for o in raw:
            if isinstance(o, str) and o.strip():
                out.append({"text": o.strip()})
            elif isinstance(o, dict) and (o.get("text") or "").strip():
                out.append(dict(o))
    elif (seg.get("overlay") or "").strip():
        out.append({"text": seg["overlay"].strip()})
    return out


def _apply_fixes(topic: str, fixes: list):
    """Route each fix to the RIGHT artifact and return (changed_keyframe_indices,
    reassemble_bool):
    - keyframe  → regenerate the base still (ken-burns segments only; clips are
                  reported, not re-rolled). Returned in changed_keyframe_indices.
    - overlay   → the timed on-screen TEXT — fixed by reassembly.
    - reassemble→ general layout rebuild.
    The keyframe is never re-rolled for an overlay problem."""
    import make_video
    ch = config.channel()
    folder = config.topic_dir(topic)
    script = json.loads((folder / "script.json").read_text())
    # map fix labels to the SCRIPT's actual segments (the writer chose the order
    # and may have omitted an optional segment) — not the channel's label order
    labels = [s.get("label") for s in script["segments"]]
    changed, reassemble, dirty = [], False, False
    for fx in fixes:
        act = (fx.get("action") or "").lower()
        lbl = fx.get("segment")
        if act == "reassemble":
            reassemble = True
            continue
        if lbl not in labels:
            continue
        i = labels.index(lbl)
        seg = script["segments"][i]
        if act == "keyframe":
            if not seg.get("animate", True):
                instr = (fx.get("instruction") or "").strip()
                if instr:
                    seg["visual_extra"] = instr
                    dirty = True
                changed.append(i)
            else:
                print(f"  (skipping keyframe fix on {lbl} — it's an animated "
                      f"clip, too costly to auto-re-roll)")
        elif act == "overlay":
            # the timed on-screen TEXT. The old code just re-rendered the SAME
            # PNG with the SAME styling/position — a no-op, so a washed-out
            # headline got flagged again every pass. Now APPLY the reviewer's
            # instruction: a legibility complaint turns on a scrim band (+ forces
            # high-contrast text), and a "move it lower/upper" instruction
            # repositions the overlay. Then re-render every overlay PNG for the
            # segment so the change actually lands.
            blob = (f"{fx.get('problem','')} {fx.get('instruction','')}").lower()
            ovs = _seg_overlays_for_edit(seg)
            legible = any(w in blob for w in (
                "washed", "wash out", "illegible", "unreadable", "contrast",
                "legib", "readab", "faint", "invisible", "ghost", "barely",
                "hard to read", "low-contrast", "blown out", "blown-out"))
            newpos = ("bottom-center" if any(w in blob for w in (
                          "lower third", "lower-third", "move it lower",
                          "lower onto", "move it to the bottom",
                          "to the lower")) else
                      "top-center" if any(w in blob for w in (
                          "upper third", "top third", "move it up",
                          "move it higher")) else None)
            for ov in ovs:
                if legible:
                    ov["scrim"] = True
                    ov["color"] = "white"
                if newpos:
                    ov["pos"] = newpos
            seg["overlays"] = ovs
            seg.pop("overlay", None)            # `overlays` is now authoritative
            # archive ALL of this segment's overlay PNGs so they re-render
            make_video.archive(folder, *[p.name for p in
                                         config.art_glob(folder, f"text{i}*.png")])
            dirty = True
            reassemble = True
            print(f"  overlay on {lbl}: "
                  + ", ".join(filter(None, [
                      "scrim+high-contrast" if legible else "",
                      f"move→{newpos}" if newpos else "",
                      "re-render"])))
        elif act == "clip":
            print(f"  (clip fix on {lbl} — animated base, not auto-re-rolled)")
    if changed or dirty:
        (folder / "script.json").write_text(json.dumps(script, indent=2))
    return changed, reassemble


def review_and_repair(topic: str, max_iters: int = 3, confirm=None) -> dict | None:
    """Post-assembly AI QC loop: look at the cut, critique it, and if a
    fixable problem is found, regenerate the offending ken-burns keyframe(s)
    and/or reassemble, then re-check — up to max_iters. Every iteration's cut
    is kept as short_v<N>.mp4 (+ review_v<N>.md). Stops as soon as it passes.
    $0 to review; only keyframe regens cost (~2 cr each).

    confirm: optional callback `(iter_n, review) -> bool` invoked AFTER a
    review that found fixable problems and BEFORE the repair re-roll. Return
    False to decline the retry (keeps the current cut). None (the default,
    used by the unattended headless/auto path) auto-applies every fix. The
    interactive sessions pass a confirm so the AI never re-rolls without the
    creator's say-so."""
    if not llm.available():
        return None
    import shutil
    import templates
    import make_video
    folder = config.topic_dir(topic)
    final = folder / "short.mp4"
    if not final.exists():
        return None
    rj = folder / "ai_review.json"
    if rj.exists():
        try:
            cached = json.loads(rj.read_text())
            if cached.get("passed"):
                print("  AI review already passed (cached)")
                return cached
        except ValueError:
            pass
    tpl = templates.load_pinned(folder)
    facts = json.loads((folder / "facts.json").read_text())
    base = len(list(folder.glob("short_v*.mp4")))
    result = {"passed": False, "iters": 0}
    for n in range(1, max_iters + 1):
        rev = _review(topic, tpl)
        v = base + n
        shutil.copy(final, folder / f"short_v{v}.mp4")
        (folder / f"review_v{v}.md").write_text(rev["md"])
        result = {"passed": rev["pass"], "iters": n, "version": v,
                  "summary": rev["summary"], "scores": rev.get("scores", {}),
                  "fixes": rev["fixes"]}
        print(f"  AI review #{n} (kept short_v{v}.mp4): "
              + ("✓ PASS — " if rev["pass"] else "changes needed — ")
              + rev["summary"])
        if rev["pass"] or n == max_iters:
            if not rev["pass"]:
                print(f"  reached max {max_iters} iterations — keeping the "
                      f"best effort; compare short_v*.mp4")
            break
        if confirm is not None and not confirm(n, result):
            print("  AI repair declined — keeping this cut "
                  "(apply notes manually if you want)")
            break
        changed, reassemble = _apply_fixes(topic, rev["fixes"])
        if not changed and not reassemble:
            print("  nothing auto-fixable — presenting as-is")
            break
        audio = json.loads((folder / "audio.json").read_text())
        script = json.loads((folder / "script.json").read_text())
        shots = make_video.shot_list(script, audio, folder)
        for i in changed:
            # MULTISHOT-aware: a beat the director split has several keyframes
            # (key<i>.png + key<i>_<k>.png), and the review samples the segment
            # MIDPOINT — i.e. a LATER sub-shot, not key<i>.png. Regenerate EVERY
            # sub-shot of the segment (they share the beat's visual_extra) so the
            # flagged frame actually changes, not just sub-shot 0.
            subs = make_video._subshots(shots[i], folder)
            tag = (f"{i} [{shots[i]['label']}]"
                   + (f" — {len(subs)} sub-shots" if len(subs) > 1 else ""))
            print(f"  regenerating keyframe {tag}")
            make_video.archive(folder, *[p.name for p in
                                         config.art_glob(folder, f"text{i}*.png")])
            for sub in subs:
                make_video.archive(folder, sub["key"].name)
                make_video.step_keyframe(sub, facts, folder, tpl)
        # archive the cuts (kept in .versions/); scratch caption/audio files
        # are pure derived rebuild state — those we just remove
        make_video.archive(folder, "short.mp4", "nomusic.mp4")
        for f in ("subs.ass", "music.txt", "virality.md",
                  "audio_caps.json", "audio_scaled.json"):
            config.art(folder, f).unlink(missing_ok=True)
        make_video.step_assemble(shots, audio, folder, tpl)
    rj.write_text(json.dumps(result))
    return result


if __name__ == "__main__":
    critique(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
