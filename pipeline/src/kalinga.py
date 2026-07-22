"""
kalinga.py — ONE CLICK: topic in, validated viral Short out. Channel-aware,
stage-addressable, resumable.

    python3 kalinga.py ship the-first-marathon        # this topic
    python3 kalinga.py ship                           # next from the channel queue
    python3 kalinga.py ship TOPIC --template inked    # different look
    python3 kalinga.py ship TOPIC --review            # pause before credits are spent
    python3 kalinga.py --channel history-shorts ship  # explicit channel
    python3 kalinga.py run script TOPIC               # one stage (cached-aware)
    python3 kalinga.py redo keyframes TOPIC           # clear stage + downstream, rerun
    python3 kalinga.py redo hook TOPIC                # surgical opening-segment reset
    python3 kalinga.py show [TOPIC]                   # artifact status table
    python3 kalinga.py status | templates | channels
    python3 kalinga.py new-channel cooking            # scaffold a new channel

What `ship` does, resumably (rerun after any failure — artifacts are cached):
  research       channel adapter (stock screen / manual / llm)   facts.json
  script         LLM viral script, hook-judge gated              script.json
  produce        TTS + keyframes + clips + assembly + SEO
                 + virality score (make_video.py)                short.mp4
  validate       tech QC / virality / SEO gates; ONE automatic
                 hook rewrite + regeneration if the hook is weak
  wrap           critique.md, report.md, queue.csv marked

The channel (channels/<name>/) owns premise, persona, segment structure,
research adapter, templates, queue and state; pick it with --channel,
KALINGA_CHANNEL, or let the only channel win.

Daily cron (Mac must be awake):
  0 7 * * * cd /Users/desault/BillionDollarIdeas/Kalinga/pipeline/src && /usr/bin/env python3 kalinga.py ship >> ../daily.log 2>&1
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

import config
import daily
import make_video
import templates
import validate


# ---------- helpers ----------
def hf_credits():
    """Best-effort credit balance from the CLI; None if unavailable."""
    try:
        blob = json.dumps(make_video.hf(["account", "status"], timeout=60))
        m = re.search(r'"[^"]*(?:credit|balance)[^"]*"\s*:\s*"?([\d.]+)',
                      blob, re.IGNORECASE)
        return int(float(m.group(1))) if m else None
    except Exception:
        return None


def write_script(topic: str) -> dict:
    """LLM script unless one already exists (resume / written in-session)."""
    folder = config.topic_dir(topic)
    p = folder / "script.json"
    if p.exists():
        s = json.loads(p.read_text())
        print(f"  script.json exists ({s.get('mode')}) — keeping it")
        return s
    import creative
    try:
        return creative.write_script(topic)
    except Exception as e:
        print(f"  ! LLM script failed ({e})\n"
              f"  ! falling back to the dry template — the video will be "
              f"functional, not viral", file=sys.stderr)
        d = json.loads((folder / "facts.json").read_text())
        return make_video.step_script(d, folder)


def hook_feedback(result: dict, folder: Path) -> str:
    """Assemble what we tell the rewriter about why the hook is weak."""
    vir = result["virality"]
    parts = [vir["why"], f"scores: {json.dumps(vir['scores'])}"]
    script = json.loads((folder / "script.json").read_text())
    if script.get("judge", {}).get("improve"):
        parts.append(f"script judge noted: {script['judge']['improve']}")
    return " | ".join(parts)


def _confirm(question: str) -> bool:
    try:
        return input(f"\n  {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _print_script(script: dict):
    """The FULL script — every spoken word, wrapped, with per-segment and
    total word counts so nothing reads as truncated."""
    import textwrap
    T = config.tint

    def words(t):
        return len([w for w in t.split() if any(c.isalnum() for c in w)])

    def seg_text(s):
        t = (s.get("text") or "").strip()
        if not t and s.get("lines"):
            t = " ".join((l.get("text") or "").strip() for l in s["lines"])
        return t

    j = script.get("judge", {})
    score = j.get("score", "?")
    s_col = ("32" if isinstance(score, int) and score >= 7
             else "33" if isinstance(score, int) and score >= 5 else "31")
    total = sum(words(seg_text(s)) for s in script["segments"])
    print("\n  " + T("─── script ───", "1", "36")
          + "  script judge " + T(f"{score}/10", "1", s_col)
          + (T(" ✓", "1", "32") if j.get("pass") else "")
          + T(f"  ·  {total} words ≈ {total / 2.6:.0f}s spoken", "2"))
    setup = (script.get("setup") or "").strip()
    if setup:
        print("  " + T("setup:", "1", "36") + " " + T(setup, "2"))
    cast = script.get("cast") or {}
    if cast:
        print("  " + T("cast:", "1", "36"))
        for name, info in cast.items():
            info = info or {}
            v = info.get("voice") or "auto"
            desc = (info.get("desc") or "").strip()
            print("    " + T(name, "1") + T(f"  [{v}]", "2")
                  + (T(f" — {desc}", "2") if desc else ""))
    for s in script["segments"]:
        n = words(seg_text(s))
        lines = s.get("lines")
        spk = s.get("speaker")
        hdr = "\n  " + T(f"{s['label']}", "1", "36") + T(f"  ({n}w)", "2")
        if spk and not lines:
            hdr += T(f"  · {spk}", "2")
        print(hdr)
        if lines:
            for ln in lines:
                who = ln.get("speaker") or "?"
                wrapped = textwrap.wrap((ln.get("text") or "").strip(), width=66)
                print("    " + T(f"{who}: ", "1") + (wrapped[0] if wrapped else ""))
                for cont in wrapped[1:]:
                    print("      " + cont)
        else:
            for ln in textwrap.wrap(seg_text(s), width=72):
                print(f"    {ln}")
            # how this beat will BREAK into voiced/captioned sections (the
            # same creative.section_sizes the voicing uses; editable in the
            # browser script stage)
            try:
                import creative
                sizes = creative.section_sizes(s)
                if len(sizes) > 1:
                    sents = creative._sentences(seg_text(s))
                    wc, k = [], 0
                    for z in sizes:
                        wc.append(f"{words(' '.join(sents[k:k + z]))}w")
                        k += z
                    print("    " + T(f"sections: {len(sizes)} "
                                     f"({' ∥ '.join(wc)})"
                                     + ("  ✎custom" if s.get("section_breaks")
                                        else ""), "2"))
            except Exception:                    # noqa: BLE001 — cosmetic only
                pass
        if s.get("overlay"):
            print("    " + T(f"on screen: [{s['overlay']}]", "2"))
    print("\n  " + T("──────────────", "1", "36"))


# ---------- stage registry ----------
def _load_ctx(topic: str):
    """(folder, tpl, facts, script, audio) — pieces may be None if their
    stage hasn't run yet."""
    folder = config.topic_dir(topic)
    tpl = templates.load_pinned(folder)

    def _read(name):
        p = folder / name
        return json.loads(p.read_text()) if p.exists() else None

    return folder, tpl, _read("facts.json"), _read("script.json"), \
        _read("audio.json")


def _need(thing, what, stage):
    if thing is None:
        raise make_video.StepFailed(
            f"{what} missing — run `kalinga.py run {stage} <topic>` first")
    return thing


def run_research(topic):
    folder = config.topic_dir(topic)
    make_video.step_research(topic, folder)


def run_script(topic):
    run_research(topic)
    write_script(topic)


def run_voice(topic):
    folder, tpl, facts, script, _ = _load_ctx(topic)
    make_video.step_voice(_need(script, "script.json", "script"), folder, tpl)


def direct(topic):
    """Director pass ($0, cached via script.json's `directed` flag): plans
    shot variety into the segments' visual_extra/motion_extra. Never blocks
    a run — the template motion map is the fallback."""
    try:
        import creative
        creative.direct_script(topic)
    except Exception as e:
        print(f"  ! director pass skipped: {e}", file=sys.stderr)


def run_keyframes(topic):
    folder, tpl, facts, script, audio = _load_ctx(topic)
    _need(script, "script.json", "script")
    direct(topic)
    script = json.loads((folder / "script.json").read_text())
    shots = make_video.shot_list(script,
                                 _need(audio, "audio.json", "voice"), folder)
    make_video.step_keyframes(shots, _need(facts, "facts.json", "research"),
                              folder, tpl)


def run_clips(topic):
    folder, tpl, facts, script, audio = _load_ctx(topic)
    shots = make_video.shot_list(_need(script, "script.json", "script"),
                                 _need(audio, "audio.json", "voice"), folder)
    make_video.step_clips(shots, folder, tpl)


def run_assemble(topic):
    folder, tpl, facts, script, audio = _load_ctx(topic)
    shots = make_video.shot_list(_need(script, "script.json", "script"),
                                 _need(audio, "audio.json", "voice"), folder)
    make_video.step_assemble(shots, audio, folder, tpl)


def run_seo(topic):
    make_video.step_seo(topic, config.topic_dir(topic))


def run_thumbnail(topic):
    folder = config.topic_dir(topic)
    facts = make_video.step_research(topic, folder)
    make_video.step_thumbnail(folder, facts, templates.load_pinned(folder))


def run_score(topic):
    folder = config.topic_dir(topic)
    final = folder / "short.mp4"
    if not final.exists():
        raise make_video.StepFailed(
            "short.mp4 missing — run `kalinga.py run assemble <topic>` first")
    make_video.step_score(final, folder)


def run_validate(topic):
    r = validate.run(topic)
    print(json.dumps(r, indent=2))


def run_critique(topic):
    folder = config.topic_dir(topic)
    if (folder / "critique.md").exists():
        print("  critique.md (cached) — `redo critique` to regenerate")
        return
    import evaluator
    evaluator.critique(topic)


# name -> (artifact globs, runner). Order matters: redo cascades downstream.
STAGES = [
    ("research", ["facts.json"], run_research),
    ("script", ["script.json", "run_state.json"], run_script),
    ("voice", ["audio.json", "seg*.mp3"], run_voice),
    ("keyframes", ["key*.png"], run_keyframes),
    ("clips", ["clip*.mp4"], run_clips),
    ("assemble", ["short.mp4", "subs.ass", "music.txt", "nomusic.mp4",
                  "chart*.png", "text*.png"], run_assemble),
    ("seo", ["seo.json", "seo.md"], run_seo),
    ("thumbnail", ["thumbnail.png", "thumb_bg.png"], run_thumbnail),
    ("score", ["virality.md"], run_score),
    ("validate", [], run_validate),
    ("critique", ["critique.md"], run_critique),
]
STAGE_NAMES = [s[0] for s in STAGES]


def _topic_or_latest(arg_topic):
    """An explicit topic, or the topic of the newest output folder — the
    thing you're iterating on."""
    if arg_topic:
        return arg_topic
    folders = sorted(config.channel().output.glob("*_*"),
                     key=lambda p: p.stat().st_mtime)
    if not folders:
        print("no runs yet in this channel — pass a topic", file=sys.stderr)
        return None
    return folders[-1].name.split("_", 1)[1]


def cmd_run(args) -> int:
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    runner = dict((n, fn) for n, _, fn in STAGES)[args.stage]
    if args.stage in ("voice", "keyframes", "clips", "score"):
        make_video.ensure_cli()
    try:
        runner(topic)
    except make_video.StepFailed as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


def cmd_redo(args) -> int:
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    folder = config.topic_dir(topic, create=False)
    if not folder.exists():
        print(f"no run folder for {topic}", file=sys.stderr)
        return 1
    if args.stage == "hook":
        make_video.reset_hook(folder)
        print("  hook artifacts cleared — rerun `ship` (or `run voice` → "
              "`run keyframes` → …) to regenerate")
        return 0
    idx = STAGE_NAMES.index(args.stage)
    doomed = [g for _, globs, _ in STAGES[idx:] for g in globs]
    doomed += ["report.md"]
    if idx <= STAGE_NAMES.index("assemble"):
        doomed += ["critique.md"]
    # archive (never delete): superseded artifacts move to .versions/, indexed
    # in versions.json and restorable; the canonical name is freed to regen
    names = sorted({p.name for g in doomed for p in config.art_glob(folder, g)
                    if p.is_file()})
    cleared = make_video.archive(folder, *names)
    print(f"  archived {args.stage}→end (kept in .versions/): "
          f"{', '.join(cleared) or 'nothing to clear'}")
    return cmd_run(args)


def cmd_show(args) -> int:
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    folder = config.topic_dir(topic, create=False)
    print(f"{config.channel().name} · {folder.name} "
          f"({'exists' if folder.exists() else 'no folder yet'})")
    if not folder.exists():
        return 1
    for name, globs, _ in STAGES:
        if not globs:
            continue
        found = [p.name for g in globs for p in config.art_glob(folder, g)]
        mark = "✓" if found else "—"
        print(f"  {mark} {name:10s} {', '.join(found) if found else ''}")
    script = folder / "script.json"
    if script.exists():
        s = json.loads(script.read_text())
        print(f"  · script: {s.get('mode')}, hook judge "
              f"{s.get('judge', {}).get('score', '?')}/10")
    scores = validate.parse_virality(folder)
    if scores:
        print(f"  · virality: "
              + ", ".join(f"{k} {v}" for k, v in scores.items()
                          if k != "report"))
    if (folder / "report.md").exists():
        print(f"  · report: {folder / 'report.md'}")
    return 0


# ---------- commands ----------
def produce(topic: str, review: bool) -> bool:
    """The paid half. Plain mode: make_video.main. Review mode: same steps,
    but pause after the keyframes (cheap) before the clips (expensive).
    Returns False when the user stops at a checkpoint."""
    if not review:
        make_video.main(topic)
        return True
    folder = config.topic_dir(topic)
    tpl = templates.load_pinned(folder)
    d = make_video.step_research(topic, folder)
    script = make_video.step_script(d, folder)
    audio = make_video.step_voice(script, folder, tpl)
    shots = make_video.shot_list(script, audio, folder)
    make_video.step_keyframes(shots, d, folder, tpl)
    if not _confirm(f"keyframes ready — review {folder}/key*.png. "
                    "Animate clips + assemble (the expensive part)?"):
        print("  stopped after keyframes — artifacts kept; rerun to continue")
        return False
    make_video.step_clips(shots, folder, tpl)
    final = make_video.step_assemble(shots, audio, folder, tpl)
    make_video.step_seo(topic, folder)
    make_video.step_thumbnail(folder, d, tpl)
    make_video.step_score(final, folder)
    return True


def run_to_done(topic: str, folder: Path, tpl: dict, *, review: bool = False,
                in_queue: bool = False, credits_before=None) -> int:
    """Ship's tail: research → script → produce → gates (one hook retry) →
    critique → report → queue mark. Presence-cached at every step, so it
    resumes wherever the artifacts stop — both `ship` and the interactive
    session's auto-handoff end here (one code path)."""
    import usage
    usage.bind(folder, credits_start=credits_before)
    try:
        print("[3] research")
        make_video.step_research(topic, folder)
        print("[4] script")
        script = write_script(topic)
        if review:
            _print_script(script)
            if not _confirm("proceed to paid generation (TTS + visuals)? "
                            "(edit script.json first if you want)"):
                print("  stopped after script — rerun the same command to "
                      "continue")
                return 0
            script = json.loads((folder / "script.json").read_text())
        direct(topic)
        state_f = folder / "run_state.json"
        state = (json.loads(state_f.read_text())
                 if state_f.exists() else {"hook_retries": 0})
        import llm as _llm
        if tpl.get("score_hook_first"):
            # build + score ONLY the opener and run the hook-retry loop BEFORE
            # the rest of the (expensive) keyframes/clips are generated
            print("[4b] hook-first virality (opener only, before the rest)")
            import creative
            while True:
                make_video.score_hook(topic)
                hv = validate.virality_gate(folder, tpl)
                print(f"  hook virality: {hv['status']} {hv['scores'] or ''}")
                if (hv["status"] != "weak"
                        or state["hook_retries"] >= tpl["max_hook_retries"]
                        or not _llm.available()):
                    break
                print(f"  weak hook ({hv['why']}) — rewriting before spending "
                      f"on the rest")
                creative.rewrite_hook(topic, hook_feedback({"virality": hv},
                                                           folder))
                make_video.reset_hook(folder)
                direct(topic)
                state["hook_retries"] += 1
                state_f.write_text(json.dumps(state))
        print("[5] produce")
        if not produce(topic, review):
            return 0

        if tpl.get("ai_review_iters", 3):
            print("[5b] AI review")
            try:
                import evaluator
                evaluator.review_and_repair(
                    topic, max_iters=int(tpl.get("ai_review_iters", 3)))
            except Exception as e:
                print(f"  ! AI review skipped: {e}", file=sys.stderr)

        print("[6] validate")
        result = validate.run(topic)
        state_f = folder / "run_state.json"
        state = (json.loads(state_f.read_text())
                 if state_f.exists() else {"hook_retries": 0})
        import llm as _llm
        while (result["virality"]["status"] == "weak"
               and state["hook_retries"] < tpl["max_hook_retries"]
               and _llm.available()):
            print(f"  weak hook ({result['virality']['why']}) — "
                  f"one rewrite + regeneration")
            import creative
            creative.rewrite_hook(topic, hook_feedback(result, folder))
            make_video.reset_hook(folder)
            direct(topic)        # the new seg0 needs its shot plan back
            state["hook_retries"] += 1
            state_f.write_text(json.dumps(state))
            make_video.main(topic)
            result = validate.run(topic)
    except make_video.StepFailed as e:
        print(f"✗ {e}\n  (artifacts are cached — rerun the same command to "
              f"resume)", file=sys.stderr)
        if in_queue:
            daily.mark(topic, "failed")
        return 1

    print("[7] wrap")
    try:
        import evaluator
        if not (folder / "critique.md").exists():
            evaluator.critique(topic)
    except Exception as e:
        print(f"  ! critique skipped: {e}", file=sys.stderr)

    # promote the AI critique's transferable craft to the GLOBAL learnings,
    # mirroring the interactive `wrap` stage (globalize_critiques). One $0 LLM
    # call, guarded so a missing backend / prior promotion is a silent no-op.
    cq = folder / "critique.md"
    if cq.exists() and not (folder / ".globalized").exists():
        try:
            import creative
            import llm
            if llm.available():
                text = cq.read_text().strip()
                if text and creative.globalize_critiques(
                        [{"tag": "ai-critique", "text": text}], topic):
                    print("  ↑ promoted transferable craft to global learnings")
                (folder / ".globalized").write_text("")   # once per run
        except Exception as e:
            print(f"  ! global distill skipped: {e}", file=sys.stderr)

    credits_after = hf_credits()
    spent = (credits_before - credits_after
             if credits_before is not None and credits_after is not None
             else None)
    meta = {"template": tpl["name"],
            "script_mode": script.get("mode"),
            "script_judge": script.get("judge", {}).get("score", "?"),
            "hook_retries": state["hook_retries"],
            "credits": f"{spent} spent, {credits_after} left"
                       if spent is not None else None}
    validate.write_report(topic, result, meta)
    if in_queue:
        daily.mark(topic, "done")

    vir = result["virality"]
    print(f"\n{'✓ READY' if result['ok'] else '⚠ REVIEW report.md first'}"
          f" — {folder / 'short.mp4'}\n"
          f"  virality: {vir['status']} {vir['scores'] or ''}\n"
          f"  metadata: {folder / 'seo.md'}\n"
          f"  report:   {folder / 'report.md'}")
    for ln in usage.summary_lines(credits_now=credits_after):
        print(f"  {ln}")
    return 0


def ship(args) -> int:
    ch = config.channel()
    review = args.review and sys.stdin.isatty()
    if args.review and not review:
        print("! --review needs a terminal (stdin is not a TTY) — "
              "running unattended", file=sys.stderr)
    print(f"=== kalinga ship — channel={ch.name}, "
          f"template={args.template or ch.default_template} ===")
    try:
        make_video.ensure_cli()
    except make_video.StepFailed as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    import llm
    if llm.available():
        print(f"  creative LLM: {llm.describe()}")
    else:
        print("! no LLM backend — script/SEO/critique fall back to dry "
              "templates. Install the claude CLI (curl -fsSL "
              "https://claude.ai/install.sh | bash — uses your Claude "
              "subscription) or export ANTHROPIC_API_KEY.", file=sys.stderr)

    topic = (args.topic or "").strip()
    if not topic:
        row = daily.next_pending(daily.load_queue())
        if not row:
            print(f"queue empty — add topics to channels/{ch.name}/queue.csv "
                  "or pass one")
            return 1
        topic = row[0].strip()
        # a queue row may be tagged with its SHOW (col 5, written when the
        # topic was planned under one) — run this video in that format
        if len(row) >= 5 and row[4].strip() and not ch.show:
            try:
                ch.set_show(row[4].strip())
                print(f"  show: {ch.show} (from the queue row)")
            except ValueError as e:
                print(f"  ! {e} — using the channel base format",
                      file=sys.stderr)
    topic = ch.normalize_topic(topic)
    in_queue = any(ch.normalize_topic(r[0]) == topic
                   for r in daily.load_queue() if r)
    print(f"[2] topic: {topic}")

    folder = config.topic_dir(topic)
    if not (folder / "template.json").exists():
        templates.resolve(args.template or ch.default_template, folder)
    tpl = templates.load_pinned(folder)
    credits_before = hf_credits()
    if credits_before is not None:
        print(f"  higgsfield credits: {credits_before}")
        if credits_before < tpl["budget_credits"]:
            print(f"✗ balance {credits_before} below template budget "
                  f"{tpl['budget_credits']} — top up first", file=sys.stderr)
            return 2

    return run_to_done(topic, folder, tpl, review=review,
                       in_queue=in_queue, credits_before=credits_before)


def cmd_make(args) -> int:
    # lazy import — kalinga is fully loaded by dispatch time
    if getattr(args, "ui", False):
        import webui
        return webui.main(args)
    import interactive
    return interactive.main(args)


def cmd_condense_learnings(args) -> int:
    """Condense the learnings file(s) — dedupe + merge repeated guidance into a
    tight durable set so the most useful lessons stay in the tail the writers
    read. The learnings only ever grow; this keeps them dense. Originals are
    backed up (never deleted). `--dry-run` previews without writing."""
    import creative
    ch = config.channel()
    scope = getattr(args, "scope", "channel")
    targets = []
    if scope in ("channel", "both"):
        targets.append(("channel", ch.learnings))
    if scope in ("global", "both"):
        targets.append(("global", config.GLOBAL_LEARNINGS))
    rc = 1
    for name, path in targets:
        print(f"[{name}] {path}")
        res = creative.condense_learnings(path, dry_run=args.dry_run)
        if not res.get("ok"):
            print(f"  skipped — {res.get('reason')}")
            continue
        rc = 0
        print(f"  {res['before']} → {res['after']} chars "
              f"(−{res['removed_pct']}%)")
        if args.dry_run:
            print("  --- preview (NOT written) ---")
            for ln in res["text"].splitlines():
                print("  | " + ln)
        else:
            print(f"  ✓ condensed; backup: {res['backup'].name}")
    if not targets:
        print("nothing to condense", file=sys.stderr)
    return rc


def cmd_ideas(args) -> int:
    """AI TOPIC IDEATION for the channel/show — fresh queue candidates that
    avoid everything already queued/produced; with a seed, REFINES the
    creator's rough idea into concrete options. Writes channels/<name>/
    ideas.json (the browser Home renders it as pickable chips) + prints."""
    import creative
    from datetime import datetime
    ch = config.channel()
    seed = " ".join(args.seed or []).strip()
    ideas = creative.suggest_topics(n=args.n, seed=seed)
    if not ideas:
        print("no ideas — needs the LLM backend (claude CLI or "
              "ANTHROPIC_API_KEY)")
        return 1
    (ch.dir / "ideas.json").write_text(json.dumps(
        {"show": ch.show or "", "seed": seed,
         "when": datetime.now().isoformat(timespec="seconds"),
         "ideas": ideas}, indent=2))
    print(f"{len(ideas)} idea(s)"
          + (f" for show '{ch.show}'" if ch.show else "")
          + (f", refined from: “{seed}”" if seed else "") + "\n")
    for i, it in enumerate(ideas, 1):
        print(f"  {i}. {it['topic']}")
        if it.get("why"):
            print(f"     {it['why']}")
    print("\nqueue one:  kalinga.py queue-topic <TOPIC>  (or ➕ in the UI)")
    return 0


def cmd_queue_topic(args) -> int:
    """Append one topic to the queue, tagged with the active show."""
    import daily
    ch = config.channel()
    if daily.queue_topic(args.topic, ch.show or ""):
        print(f"queued {ch.normalize_topic(args.topic)}"
              + (f"  [{ch.show}]" if ch.show else ""))
    else:
        print(f"{args.topic} is already queued")
    return 0


def cmd_cast(args) -> int:
    """Interactive setup of the channel's persistent cast (name, personality,
    voice, avatar)."""
    import cast_setup
    return cast_setup.main(args)


def cmd_brand(args) -> int:
    """Design + render the channel BRAND KIT (logo mark, icon, lockup,
    watermark, banner, background) into channels/<name>/brand/."""
    import brand
    return brand.main(args)


def cmd_usage(args) -> int:
    """FYI: LLM tokens (per model) + Higgsfield generations/credits for a run,
    or the cross-project GLOBAL ledger with --global."""
    import usage
    if getattr(args, "global_ledger", False):
        print("\n".join(usage.ledger_lines(since=getattr(args, "since", None))))
        return 0
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    folder = config.topic_dir(topic, create=False)
    usage.bind(folder)
    lines = usage.summary_lines(credits_now=hf_credits())
    print(f"{config.channel().name} · {folder.name} usage:")
    print("\n".join("  " + ln for ln in lines) if lines
          else "  (nothing tracked yet)")
    return 0


def cmd_bundle(args) -> int:
    """Build the UPLOAD-READY bundle for a finished run — <run>/upload/ with both
    video cuts (with-music + a clean no-music cut), the thumbnail, and a
    paste-ready seo.txt. Everything needed to upload, in one folder."""
    import make_video
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    folder = config.topic_dir(topic, create=False)
    if not (folder / "short.mp4").exists():
        print(f"  ! {folder.name} has no short.mp4 yet — produce it first")
        return 1
    make_video.export_upload(folder, templates.load_pinned(folder))
    return 0


def cmd_manual(args) -> int:
    """Prep a run (research → script → direction → voice, all $0/free) and EXPORT
    a manual-generation manifest: <run>/manual/prompts.md lists every keyframe +
    clip prompt and its reference dependency, in generation order, for hand-
    generating on the Higgsfield WEBSITE (unlimited account) instead of spending
    CLI credits. Generate them there, drop the files into the run's keyframes/ &
    clips/ folders, then `kalinga.py run assemble <TOPIC>`."""
    import make_video
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    # ensure the cheap/free stages so prompts + clip durations exist
    run_research(topic)
    write_script(topic)
    direct(topic)
    run_voice(topic)
    folder = config.topic_dir(topic, create=False)
    make_video.export_manual_manifest(folder, templates.load_pinned(folder))
    return 0


def cmd_voices(args) -> int:
    """List the ElevenLabs voices on the account (needs ELEVENLABS_API_KEY) —
    id · name · accent · gender · age — optionally filtered by a search term, so
    you can pick the exact voice_id/name for a cast member's `voice.elevenlabs`."""
    import elevenlabs
    if not elevenlabs.available():
        print("  ! no ELEVENLABS_API_KEY set — export it, then rerun "
              "(e.g.  ! export ELEVENLABS_API_KEY=...)")
        return 1
    voices = elevenlabs.list_voices(refresh=True)
    if not voices:
        print("  ! couldn't read the voice library (key invalid or no access?)")
        return 1
    q = (getattr(args, "search", None) or "").lower().strip()
    shown = 0
    for v in voices:
        hay = f"{v['name']} {v['accent']} {v['gender']} {v['age']} {v['labels']}".lower()
        if q and q not in hay:
            continue
        meta = " · ".join(x for x in (v["accent"], v["gender"], v["age"]) if x)
        print(f"  {v['id']}  {v['name']:18} {meta}")
        shown += 1
    print(f"\n  {shown} voice(s)" + (f" matching {q!r}" if q else "")
          + ".  Set a cast member's voice.elevenlabs to an id, a name, or a "
          "descriptor like \"australian female\".")
    return 0


def cmd_ui(args) -> int:
    """Open the browser control center (the HOME launcher). Channel is optional
    — best-effort pre-select (a sole channel / --channel / KALINGA_CHANNEL), and
    you pick it in the browser otherwise."""
    import types
    import webui
    pre = getattr(args, "channel", None) or os.environ.get("KALINGA_CHANNEL")
    if not pre and len(config.available()) == 1:
        pre = config.available()[0]
    if pre:
        try:
            config.set_channel(pre)
        except (RuntimeError, FileNotFoundError):
            pre = None
    webui.HOME["channel"] = pre
    return webui.main(types.SimpleNamespace(topic=None, template=None,
                                            at=None, channel=pre))


def cmd_refs(args) -> int:
    """Analyse the reference IMAGES dropped in <run>/concept/ → a style brief
    (concept_refs.json) that steers the CONCEPT and the video DIRECTOR
    (recreate the look, never copy it). Creates concept/ if missing so
    you know where to drop files. Videos are noted but not yet analysed."""
    import creative
    topic = _topic_or_latest(args.topic)
    if not topic:
        return 1
    topic = config.channel().normalize_topic(topic)
    folder = config.topic_dir(topic)
    rdir = folder / "concept"
    rdir.mkdir(exist_ok=True)
    # a TikTok/IG video LINK to copy the visual concept from — you say WHAT to
    # pull (--focus); pulls frames + caption into concept/ then extracts the brief
    url = getattr(args, "url", None)
    if url:
        rec = creative.reference_from_url(
            folder, url, focus=getattr(args, "focus", "") or "",
            interval=getattr(args, "interval", None))
        b = rec.get("brief") or {}
        if not b:
            print("  ! couldn't pull anything (install yt-dlp for full frames, "
                  "or the link is private/unreachable, or no LLM backend)")
            return 1
        print(f"\n  reference brief from {url}:")
        if b.get("summary"):
            print("  " + b["summary"])
        for k in ("palette", "lighting", "composition", "mood", "motifs",
                  "recreate"):
            v = b.get(k)
            if v:
                print(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
        return 0
    imgs, vids = creative._reference_files(folder)
    if not imgs and not vids:
        print(f"  drop reference images into {rdir}/ then rerun "
              f"`kalinga.py refs {topic}`")
        return 0
    rec = creative.extract_reference_brief(folder,
                                           regen=getattr(args, "regen", False))
    b = rec.get("brief") or {}
    if not b:
        print("  ! no brief produced (need images + an LLM backend)")
        return 1
    print(f"\n  reference brief ({len(rec.get('sources') or [])} image(s)):")
    if b.get("summary"):
        print("  " + b["summary"])
    for k in ("palette", "lighting", "composition", "mood", "motifs",
              "typography", "recreate"):
        v = b.get(k)
        if not v:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        print(f"    {k}: {v}")
    print("\n  → now steers the concept + the video director "
          "(concept_refs.json written).")
    return 0


def _env_checks() -> bool:
    """Channel-independent environment checks — ffmpeg, the generation CLI, the
    creative LLM, and the python deps. Prints ✓/✗ (+ the fix) for each and
    returns True when everything the pipeline needs is present."""
    import shutil
    ok = True

    def check(label, good, fix=""):
        nonlocal ok
        print(f"  {'✓' if good else '✗'} {label}"
              + (f" — {fix}" if not good and fix else ""))
        ok = ok and good

    check("ffmpeg", bool(shutil.which("ffmpeg")), "brew install ffmpeg")
    check("higgsfield CLI", bool(shutil.which("higgsfield")),
          "curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/"
          "main/install.sh | sh")
    if shutil.which("higgsfield"):
        authed = make_video.sh(
            ["higgsfield", "account", "status"]).returncode == 0
        check("higgsfield auth", authed, "higgsfield auth login")
        if authed:
            credits = hf_credits()
            if credits is not None:
                print(f"    credits: {credits}")
    import llm
    check("creative LLM", llm.available(),
          "install the claude CLI: curl -fsSL https://claude.ai/install.sh "
          "| bash (uses your Claude subscription) — or export "
          "ANTHROPIC_API_KEY")
    if llm.available():
        print(f"    backend: {llm.describe()}"
              + ("  (KALINGA_MODEL/KALINGA_BACKEND to change)"))
    for mod in ("PIL", "yaml", "edge_tts"):
        try:
            __import__(mod)
            check(f"python {mod}", True)
        except ImportError:
            check(f"python {mod}", False,
                  "pip3 install -r requirements.txt")
    return ok


def status(args) -> int:
    ch = config.channel()
    print(f"kalinga environment (channel: {ch.name} — {ch.title}):")
    ok = _env_checks()
    print(f"  · research adapter: {ch.research}")
    print(f"  · templates: {', '.join(templates.available()) or 'none'}")
    row = daily.next_pending(daily.load_queue()) \
        if ch.queue.exists() else None
    print(f"  · next in queue: {row[0] if row else '(empty)'}")
    others = [c for c in config.available() if c != ch.name]
    if others:
        print(f"  · other channels: {', '.join(others)}")
    return 0 if ok else 1


def cmd_init(args) -> int:
    """Guided first-run setup: check the environment, then walk through logging
    in, picking a channel, and producing the first video. Works before any
    channel is chosen, so it never needs --channel."""
    print("\n  Kalinga — let's get you making videos.\n")
    print("  [1/3] Checking your environment:")
    ok = _env_checks()
    print("\n  [2/3] The two logins (no API keys needed):")
    print("    • claude CLI     — the creative model, on your Claude "
          "subscription ($0)")
    print("    • higgsfield CLI — image/video/voice generation "
          "(`higgsfield auth login`)")
    if not ok:
        print("    ↳ install/authenticate anything marked ✗ above, then rerun "
              "`kalinga.py init`.")

    print("\n  [3/3] Pick a channel and make a video:")
    names = config.available()
    if not names:
        print("    • No channels yet — scaffold one:")
        print("        python3 kalinga.py new-channel my-channel")
        print("      then edit channels/my-channel/channel.yaml (see the "
              "commented daily-science reference).")
    else:
        multi = len(names) > 1
        pick = names[0]
        flag = f" --channel {pick}" if multi else ""
        print(f"    • Channels ({len(names)}): {', '.join(names)}")
        if multi:
            print("      (more than one — add --channel <name> or set "
                  "KALINGA_CHANNEL; with a single channel it's optional)")
        print("    • Interactive, stage by stage:")
        print(f"        python3 kalinga.py{flag} make \"your topic here\"")
        print("    • Or headless, straight through:")
        print(f"        python3 kalinga.py{flag} ship \"your topic here\"")
        print("    • New channel of your own:")
        print("        python3 kalinga.py new-channel my-channel")

    print("\n  Docs: README.md (setup) · docs/EXTENDING.md (add a channel / "
          "research adapter / look) · ARCHITECTURE.md (how it fits together)\n")
    return 0 if ok else 1


def list_templates(args) -> int:
    for name in templates.available():
        tpl = templates.load(name)
        print(f"  {name:12s} {tpl.get('description', '')}")
    return 0


def list_channels(args) -> int:
    names = config.available()
    if not names:
        print("no channels yet — scaffold one: python3 kalinga.py "
              "new-channel <name>")
        return 1
    default = os.environ.get("KALINGA_CHANNEL") or (
        names[0] if len(names) == 1 else None)
    for n in names:
        ch = config.Channel(n)
        star = " *" if n == default else ""
        print(f"  {n:16s}{star} {ch.title} — research: {ch.research}, "
              f"{len(ch.labels) if ch.cfg.get('segments') else 0} segments")
    return 0


CHANNEL_SCAFFOLD = """\
# Channel definition for "{name}". Fill in every <...> before shipping.
# See channels/daily-science/channel.yaml for a complete, working reference.

title: {name}
premise: >-
  <one paragraph: what this channel does, one video at a time — used in
  the optimizer/evaluator/SEO prompts>

persona: >-
  <the narrator: who they sound like, what they never do>

topic_noun: topic          # what one video covers (stock/app/book/place/…)
research: llm              # llm (Claude researches the topic) | manual (drop
                           # facts.json/facts.md into the topic folder)
default_template: default
# positive_verdict: <X>    # if videos end in a verdict, facts.verdict == X
                           # turns `ink: verdict` overlays green (else red)

# posting:                 # the channel's posting cadence (kalinga.py
#   time: "09:00"          # calendar + `publish --at next`); a learned
#   days: daily            # best time (analyze) overrides it. days: daily
                           # or [mon, wed, fri]. Default: daily 09:00.

segments:
  - label: HOOK
    max_words: 12
    guidance: >-
      <pattern interrupt: the most famous detail or a startling fact,
      framed as a question the viewer needs answered>
  - label: SETUP
    guidance: <the context the viewer needs, in one breath>
  - label: MEAT
    guidance: <the core insight/payoff of this video — the reason to watch>
  - label: TWIST
    guidance: <the thing nobody expects; re-grab attention>
  - label: CTA
    max_words: 8
    guidance: <loop back to the hook so a rewatch feels natural>

voice_rules:
  - <channel-specific writing rule>
  - <another>

visual_rules: >-
  <fixed visual beats the DIRECTOR must honor, e.g. what the HOOK shot must
  star or how the VERDICT must look>

seo:
  title_must_include: ["{{topic}}"]
  description_must_include: []
  base_hashtags: []
  base_tags: []
  prompt_rules: |-
    - <channel-specific title/description/tag rules>

optimizer_dimensions: [hook style, CTA phrasing, posting time, tags/SEO,
                       pacing/segment length, overlay text style,
                       content depth, title style]
"""

TEMPLATE_SCAFFOLD = """\
# Visual template "default" — the look and rhythm. `world` and `style` are
# required; everything else falls back to DEFAULTS in src/templates.py.
# See channels/daily-science/templates/brightlab.yaml for a full reference.

description: <one line>

world: >-
  <the visual world every shot lives in — referenced in the script prompt>

style: >-
  <the rendering style appended to every keyframe prompt; keep the bottom
  third dark and empty for captions, vertical 9:16>

motion:
  HOOK: <camera move for the opening shot>
  CTA: <camera move that mirrors the opening, inviting a loop>
"""


def new_channel(args) -> int:
    name = args.name.strip().replace(" ", "-").lower()
    d = config.CHANNELS_DIR / name
    if (d / "channel.yaml").exists():
        print(f"channels/{name}/ already exists", file=sys.stderr)
        return 1
    (d / "templates").mkdir(parents=True, exist_ok=True)
    (d / "assets" / "music").mkdir(parents=True, exist_ok=True)
    (d / "output").mkdir(parents=True, exist_ok=True)
    if not (d / "queue.csv").exists():
        (d / "queue.csv").write_text("")
    (d / "channel.yaml").write_text(CHANNEL_SCAFFOLD.format(name=name))
    (d / "templates" / "default.yaml").write_text(TEMPLATE_SCAFFOLD)
    print(f"scaffolded channels/{name}/ — next:\n"
          f"  1. edit channels/{name}/channel.yaml (premise, persona, "
          f"segments, seo)\n"
          f"  2. edit channels/{name}/templates/default.yaml (world, style, "
          f"motion)\n"
          f"  3. add topics to channels/{name}/queue.csv\n"
          f"  4. python3 kalinga.py --channel {name} ship --review")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kalinga",
        description="one-click multi-channel Shorts factory")
    ap.add_argument("--channel", default=None,
                    help="channel folder name (default: KALINGA_CHANNEL "
                         "env, or the only channel)")
    ap.add_argument("--show", default=None,
                    help="the channel SHOW (format) to run under — a named "
                         "override set from channel.yaml `shows:` (default: "
                         "KALINGA_SHOW env, else the channel base; a resumed "
                         "run reactivates its pinned show automatically)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ship", help="produce one validated Short")
    s.add_argument("topic", nargs="?", default=None,
                   help="topic (default: next pending in the "
                        "channel's queue.csv)")
    s.add_argument("--template", default=None,
                   help="visual template (default: the channel's "
                        "default_template)")
    s.add_argument("--review", action="store_true",
                   help="pause for approval after the script (free) and "
                        "after the keyframes (cheap), before the "
                        "expensive clips")
    s.set_defaults(fn=ship)

    mk = sub.add_parser("make",
                        help="interactive stage-by-stage production session")
    mk.add_argument("topic", nargs="?", default=None,
                    help="topic (default: next pending in the "
                         "channel's queue.csv)")
    mk.add_argument("--template", default=None,
                    help="visual template (ignored when the run already "
                         "pinned one)")
    mk.add_argument("--at", default=None,
                    choices=["research", "concept", "script", "direction",
                             "voice", "keyframes", "clips", "music",
                             "assemble", "seo", "gates", "wrap"],
                    help="jump straight to a stage (e.g. revise the script "
                         "of a finished video — changed pieces regenerate, "
                         "the rest stays cached)")
    mk.add_argument("--ui", action="store_true",
                    help="drive the same session in a local browser UI "
                         "instead of the terminal")
    mk.add_argument("--segment", type=int, default=None,
                    help="re-edit ONLY this segment index (text/voice/keyframe/"
                         "clip/overlay) — every other segment stays cached")
    mk.set_defaults(fn=cmd_make)

    cl = sub.add_parser("condense-learnings",
                        help="dedupe + tighten the learnings file(s) so the "
                             "best lessons stay in the read window")
    cl.add_argument("--scope", choices=["channel", "global", "both"],
                    default="channel",
                    help="which learnings to condense (default: channel)")
    cl.add_argument("--dry-run", action="store_true",
                    help="preview the condensed result without writing")
    cl.set_defaults(fn=cmd_condense_learnings)

    idp = sub.add_parser("ideas",
                         help="AI topic ideas for the channel/--show; pass a "
                              "rough idea to REFINE it into concrete options")
    idp.add_argument("seed", nargs="*",
                     help="rough idea to refine (optional — omit for fresh "
                          "ideation)")
    idp.add_argument("--n", type=int, default=8,
                     help="how many ideas (default 8)")
    idp.set_defaults(fn=cmd_ideas)

    qt = sub.add_parser("queue-topic",
                        help="append one topic to the channel's queue")
    qt.add_argument("topic")
    qt.set_defaults(fn=cmd_queue_topic)

    cs = sub.add_parser("cast",
                        help="set up the channel's recurring cast "
                             "(name, personality, voice, avatar)")
    cs.set_defaults(fn=cmd_cast)

    br = sub.add_parser("brand",
                        help="design + render the channel brand kit (logo, "
                             "icon, watermark, banner, background)")
    br.add_argument("--auto", action="store_true",
                    help="render headless (no confirm)")
    br.add_argument("--regen", action="store_true",
                    help="re-design the spec + regenerate assets")
    br.add_argument("--only", default=None,
                    help="comma list: mark,icon,logo,watermark,banner,background")
    br.set_defaults(fn=cmd_brand)

    us = sub.add_parser("usage",
                        help="FYI: LLM tokens + Higgsfield generations/credits "
                             "for a run (or --global for the cross-project ledger)")
    us.add_argument("topic", nargs="?", default=None)
    us.add_argument("--global", dest="global_ledger", action="store_true",
                    help="show the cross-project append-only spend ledger")
    us.add_argument("--since", default=None,
                    help="with --global: only events at/after this ISO date "
                         "(e.g. 2026-06-17)")
    us.set_defaults(fn=cmd_usage)

    ui = sub.add_parser("ui",
                        help="open the browser CONTROL CENTER — pick channel → "
                             "run folder → the video workflow or a tool")
    ui.set_defaults(fn=cmd_ui)

    bd = sub.add_parser("bundle",
                        help="gather an upload-ready folder for a finished run "
                             "(both video cuts + thumbnail + seo.txt)")
    bd.add_argument("topic", nargs="?", default=None)
    bd.set_defaults(fn=cmd_bundle)

    mn = sub.add_parser("manual",
                        help="export every keyframe/clip prompt + dependency "
                             "(<run>/manual/prompts.md) to hand-generate on the "
                             "website, then drop files back + run assemble")
    mn.add_argument("topic", nargs="?", default=None)
    mn.set_defaults(fn=cmd_manual)

    vc = sub.add_parser("voices",
                        help="list the ElevenLabs voices on the account "
                             "(needs ELEVENLABS_API_KEY) to pick a cast voice")
    vc.add_argument("search", nargs="?", default=None,
                    help="filter by a term (e.g. australian, female, a name)")
    vc.set_defaults(fn=cmd_voices)

    rf = sub.add_parser("refs",
                        help="analyse reference images dropped in <run>/concept/ "
                             "(or a --url video link) → a style brief that steers "
                             "the concept + directors")
    rf.add_argument("topic", nargs="?", default=None)
    rf.add_argument("--regen", action="store_true",
                    help="re-analyse even if a brief already exists")
    rf.add_argument("--url", default=None,
                    help="a TikTok/Instagram video link to copy the visual "
                         "concept from (pulls frames + caption into concept/)")
    rf.add_argument("--focus", default=None,
                    help="what to PULL from the link (e.g. 'the jump-cut editing "
                         "+ bold captions'); default = the overall look")
    rf.add_argument("--interval", type=float, default=None,
                    help="seconds between sampled frames from --url (default 10; "
                         "lower = more frames = finer read of the editing)")
    rf.set_defaults(fn=cmd_refs)

    r = sub.add_parser("run", help="run one stage (cached steps skipped)")
    r.add_argument("stage", choices=STAGE_NAMES)
    r.add_argument("topic", nargs="?", default=None,
                   help="topic (default: the newest run folder)")
    r.set_defaults(fn=cmd_run)

    rd = sub.add_parser("redo",
                        help="clear a stage + everything downstream, rerun it")
    rd.add_argument("stage", choices=STAGE_NAMES + ["hook"])
    rd.add_argument("topic", nargs="?", default=None)
    rd.set_defaults(fn=cmd_redo)

    sh_ = sub.add_parser("show", help="artifact status for a topic")
    sh_.add_argument("topic", nargs="?", default=None)
    sh_.set_defaults(fn=cmd_show)

    sub.add_parser("init", help="guided first-run setup (env check + next steps)"
                   ).set_defaults(fn=cmd_init)
    sub.add_parser("status", help="check environment").set_defaults(fn=status)
    sub.add_parser("templates", help="list the channel's templates"
                   ).set_defaults(fn=list_templates)
    sub.add_parser("channels", help="list channels").set_defaults(
        fn=list_channels)
    nc = sub.add_parser("new-channel", help="scaffold a new channel folder")
    nc.add_argument("name")
    nc.set_defaults(fn=new_channel)

    args = ap.parse_args(argv)
    if args.cmd not in ("init", "channels", "new-channel", "ui"):
        try:
            config.set_channel(args.channel)
            if getattr(args, "show", None):
                config.channel().set_show(args.show)
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
