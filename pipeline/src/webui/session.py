"""
webui.session — launcher bootstrap + capability tools.

open_session binds a run folder to the video workflow and switches ctx.MODE;
go_home tears it back down; _set_channel switches the active channel; run_tool
streams a channel/global capability (condense, usage, status, …) as a
background job. These are the only places (besides server.main) that REASSIGN
the ctx globals.
"""
from __future__ import annotations
from datetime import datetime

import config
import interactive as iv
import kalinga
import templates

from . import context as ctx
from .context import App, run_job
from .state import build_state, home_state


def _set_channel(name: str):
    if name and name in config.available():
        ctx.guard_channel(name)    # a foreign channel's running job owns config
        config.set_channel(name)
        ctx.HOME["channel"] = name
    return home_state()


def open_session(channel: str, topic: str, mode: str = "video",
                 template: str = None, show: str = None) -> dict:
    """Bind a run folder to the video workflow as a project Box and focus it.
    Several projects can be open at once — reopening one whose job is still
    running rejoins it AS-IS (never rebuilds the session under a live job).
    `template` (optional) is the visual world chosen on the home screen for a
    NEW run; ignored once a run has a pinned template.json (pinned wins on
    resume). `show` (optional) runs the session under one of the channel's
    SHOWS — pinned into template.json on a new run; a resumed run's pin wins."""
    ctx.guard_channel(channel)     # cross-channel parallel jobs aren't safe
    config.set_channel(channel)
    ctx.HOME["channel"] = channel
    ch = config.channel()
    if show:
        ch.set_show(show)          # ValueError on a typo — surfaced as a toast
    topic = ch.normalize_topic((topic or "").strip())
    if not topic:
        raise ValueError("a topic is required")
    folder = config.topic_dir(topic)   # creates new / reactivates pinned show
    # a project whose job is mid-flight: rejoin it untouched — rebuilding the
    # App under a live job would swap the objects its closures read
    sid = ctx.sid_for(channel, folder, "video")
    box = ctx.SESSIONS.get(sid)
    if box is not None and ctx.running_job_for(folder) is not None:
        ctx.set_focus(sid)
        return build_state()
    if not (folder / "template.json").exists():
        avail = sorted(p.stem for p in ch.templates_dir.glob("*.yaml")) \
            if ch.templates_dir.exists() else []
        chosen = template if (template and template in avail) \
            else ch.default_template
        templates.resolve(chosen, folder)
    tpl = templates.load_pinned(folder)
    s = iv.Session(topic, folder, tpl, False)
    credits = kalinga.hf_credits()
    sess = iv._read_json(folder, "session.json")
    if sess is None:
        sess = {"started": datetime.now().isoformat(timespec="seconds"),
                "credits_start": credits}
        iv._write_json(folder, "session.json", sess)
    s.credits_start = sess.get("credits_start", credits)
    ctx.bind_box("video", channel, topic, folder, app=App(s))
    return build_state()


def go_home() -> dict:
    """Back to the launcher. Project boxes and their running jobs LIVE ON —
    only the browser's focus changes."""
    ctx.set_focus(None)
    return home_state()


# ---- cast editor (the home screen's 🎭 Cast panel) --------------------------
# The browser equivalent of the `kalinga.py cast` terminal wizard: the roster
# CRUD + voice sampling are INSTANT tools (dict results), avatar / reference-
# library generation are streamed Jobs. All of it reuses cast_setup's logic —
# the same cast.json either UI edits.

def _cast_member(cast_setup, name: str):
    cast = cast_setup._load()
    m = cast.get(name)
    if m is None:
        raise ValueError(f"no cast member named {name}")
    return cast, m


def _cast_save(args: dict) -> dict:
    import cast_setup
    engine, _, _ = cast_setup._engine_and_style()
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("a character name is required")
    cast = cast_setup._load()
    m = cast.get(name, {})
    for k in ("personality", "appearance"):
        if k in args:
            m[k] = str(args.get(k) or "").strip()
    g = (args.get("gender") or m.get("gender") or "neutral").lower()
    m["gender"] = g if g in cast_setup.GENDER_VOICE else "neutral"
    v = (args.get("voice") or "").strip()
    base = dict(cast_setup.GENDER_VOICE.get(m["gender"],
                                            cast_setup.GENDER_VOICE["neutral"]))
    if isinstance(m.get("voice"), dict):
        base.update(m["voice"])
    if v:
        if not cast_setup.voice_ok(engine, v):
            raise ValueError(f"'{v}' doesn't look like a {engine} voice")
        base[engine] = v
    m["voice"] = base
    cast[name] = m
    cast_setup._save(cast)
    return {"kind": "ok", "name": name}


def _cast_delete(args: dict) -> dict:
    import cast_setup
    name = (args.get("name") or "").strip()
    cast, _ = _cast_member(cast_setup, name)
    cast.pop(name)
    cast_setup._save(cast)     # images stay on disk (same as the terminal)
    return {"kind": "ok", "deleted": name}


def _cast_sample(args: dict) -> dict:
    import cast_setup
    engine, _, _ = cast_setup._engine_and_style()
    voice = (args.get("voice") or "").strip()
    if voice.lower() == "auto" or not cast_setup.voice_ok(engine, voice):
        raise ValueError(f"'{voice}' can't be sampled — pick a concrete "
                         f"{engine} voice name")
    out, line = cast_setup.sample_file(engine, voice, args.get("name") or "")
    return {"kind": "sample", "url": "/sample/" + out.name, "line": line}


def _cast_avatar_job(name: str):
    import cast_setup
    import make_video

    def fn():
        make_video.ensure_cli()
        cast, m = _cast_member(cast_setup, name)
        _, style, tpl = cast_setup._engine_and_style()
        if not cast_setup._make_avatar(name, m, style, tpl, regen=True):
            raise RuntimeError("avatar generation failed")
        cast[name] = m
        cast_setup._save(cast)
    return run_job(f"avatar — {name}", fn)


def _cast_refs_job(name: str, outfits, regen: bool):
    import cast_setup
    import make_video

    def fn():
        make_video.ensure_cli()
        cast, m = _cast_member(cast_setup, name)
        _, style, tpl = cast_setup._engine_and_style()
        cast_setup._make_reference_set(name, m, style, tpl,
                                       outfits=outfits or None,
                                       regen=bool(regen))
        cast[name] = m
        cast_setup._save(cast)
    return run_job(f"reference library — {name}", fn)


# ---- create a channel / a template from the landing page --------------------
DRAFT = "channel_draft.json"     # the AI channel-designer's pending draft


def _draft_path():
    return config.CHANNELS_DIR / ("." + DRAFT)


def _rich_channel_yaml(name: str, args: dict) -> str:
    """A real channel.yaml from the designer/creator's sections — premise,
    persona, topic_noun, segments (label/guidance/max_words), voice_rules,
    visual_rules and starter SEO. yaml.safe_dump keeps it valid by
    construction; a header points at the commented reference."""
    import json
    import yaml
    data = {"title": (args.get("title") or "").strip() or name}
    for k in ("premise", "persona"):
        v = " ".join((args.get(k) or "").split())
        if v:
            data[k] = v
    tn = (args.get("topic_noun") or "").strip()
    data["topic_noun"] = tn or "topic"
    data["research"] = "llm"
    data["default_template"] = "default"
    segs = []
    for sg in (args.get("segments") or []):
        if not isinstance(sg, dict):
            continue
        lbl = str(sg.get("label") or "").strip().upper()
        gd = " ".join(str(sg.get("guidance") or "").split())
        if not lbl or not gd:
            continue
        one = {"label": lbl}
        try:
            one["max_words"] = int(sg["max_words"])
        except (KeyError, TypeError, ValueError):
            pass
        one["guidance"] = gd
        segs.append(one)
    if segs:
        data["segments"] = segs
    for k in ("voice_rules", "visual_rules"):
        rules = [" ".join(str(r).split())
                 for r in (args.get(k) or []) if str(r).strip()]
        if rules:
            data[k] = rules
    seo = args.get("seo") if isinstance(args.get("seo"), dict) else {}
    tags = {k: [str(t).strip() for t in (seo.get(k) or []) if str(t).strip()]
            for k in ("base_hashtags", "base_tags")}
    if any(tags.values()):
        data["seo"] = {k: v for k, v in tags.items() if v}
    return ("# Channel definition for \"%s\" — designed in the UI. Every key\n"
            "# is editable; see channels/daily-science/channel.yaml for the\n"
            "# fully commented reference (shows, verdict locks, prompt_rules…).\n"
            % name) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                     width=78, default_flow_style=False)


def _channel_new(args: dict) -> dict:
    """Scaffold channels/<name>/ (same as `kalinga.py new-channel`) and select
    it. With only a title/premise the commented scaffold is patched; with the
    designer's full sections (segments/voice_rules/…) a REAL channel.yaml is
    written instead. Consumes the pending AI draft."""
    import types
    name = (args.get("name") or "").strip().replace(" ", "-").lower()
    if not name:
        raise ValueError("a channel folder name is required")
    if name in config.available():
        raise ValueError(f"channel {name} already exists")
    if kalinga.new_channel(types.SimpleNamespace(name=name)) != 0:
        raise ValueError(f"could not scaffold {name}")
    p = config.CHANNELS_DIR / name / "channel.yaml"
    if args.get("segments") or args.get("voice_rules") \
            or args.get("visual_rules") or args.get("persona"):
        p.write_text(_rich_channel_yaml(name, args))
    else:
        txt = p.read_text()
        title = (args.get("title") or "").strip()
        premise = " ".join((args.get("premise") or "").split())
        if title:
            txt = txt.replace(f"title: {name}", f"title: {title}", 1)
        if premise:
            txt = txt.replace(
                "  <one paragraph: what this channel does, one video at a "
                "time — used in\n  the optimizer/evaluator/SEO prompts>",
                "  " + premise, 1)
        p.write_text(txt)
    _draft_path().unlink(missing_ok=True)
    _set_channel(name)
    return {"kind": "ok", "channel": name}


def _template_new(args: dict) -> dict:
    """Create channels/<name>/templates/<tpl>.yaml from the landing page —
    `world` + `style` are the two REQUIRED keys (templates.load raises without
    them); everything else falls back to DEFAULTS and is editable in the yaml."""
    import textwrap
    name = (args.get("name") or "").strip().replace(" ", "-").lower()
    if not name:
        raise ValueError("a template name is required")
    world = " ".join((args.get("world") or "").split())
    style = " ".join((args.get("style") or "").split())
    if not world or not style:
        raise ValueError("world and style are required — they define the look")
    ch = config.channel()
    ch.templates_dir.mkdir(parents=True, exist_ok=True)
    p = ch.templates_dir / f"{name}.yaml"
    if p.exists():
        raise ValueError(f"template {name} already exists")
    desc = " ".join((args.get("description") or "").split()) \
        or f"the {name} look"
    blk = lambda s: "\n".join("  " + ln for ln in textwrap.wrap(s, 74))
    p.write_text(
        f'# Visual template "{name}" — world + style are REQUIRED; every other\n'
        f"# knob (models, captions, motion, budget…) falls back to DEFAULTS in\n"
        f"# pipeline/src/templates.py. Add a `motion:` map (label → camera move)\n"
        f"# when you want per-beat camera language.\n\n"
        f"description: {desc}\n\n"
        f"world: >-\n{blk(world)}\n\n"
        f"style: >-\n{blk(style)}\n")
    return {"kind": "ok", "template": name}


def run_tool(tool: str, topic: str = None, args: dict = None) -> int:
    """Run a channel/global capability as a streamed background Job. `args`
    carries tool-specific input."""
    import types
    ns = types.SimpleNamespace
    args = args or {}
    tools = {
        "condense": ("condense-learnings",
                     lambda: kalinga.cmd_condense_learnings(
                         ns(scope="channel", dry_run=False))),
        "usage": ("usage", lambda: kalinga.cmd_usage(ns(topic=topic))),
        "show": ("show", lambda: kalinga.cmd_show(ns(topic=topic))),
        "status": ("status", lambda: kalinga.status(ns())),
        "refs": ("references",
                 lambda: kalinga.cmd_refs(ns(topic=topic, regen=False))),
        "ideas": ("suggesting topics",
                  lambda: kalinga.cmd_ideas(ns(
                      seed=[args["seed"]] if args.get("seed") else [],
                      n=int(args.get("n") or 8)))),
    }
    # cast editor: roster CRUD + voice sampling are INSTANT (dict results);
    # avatar / reference-library generation stream as Jobs
    instant = {"cast_save": _cast_save, "cast_delete": _cast_delete,
               "cast_sample": _cast_sample,
               "channel_new": _channel_new, "template_new": _template_new,
               "channel_draft_discard":
                   lambda a: (_draft_path().unlink(missing_ok=True)
                              or {"kind": "ok"})}
    if tool in instant:
        return instant[tool](args)
    if tool == "channel_design":
        seed = (args.get("seed") or "").strip()
        if not seed:
            raise ValueError("describe the channel idea first")

        def _design():
            import creative
            import json
            print(f"designing a channel from: {seed[:90]}")
            d = creative.design_channel(seed)
            if not d:
                raise RuntimeError("the designer returned nothing — is the "
                                   "claude CLI logged in? (kalinga.py status)")
            d["seed"] = seed
            _draft_path().write_text(json.dumps(d, indent=2))
            print(f"draft ready: \"{d.get('title', '?')}\" — "
                  f"{len(d.get('segments') or [])} beats; review and edit it "
                  "on the landing page, then scaffold")
        return run_job("designing the channel", _design)
    if tool == "cast_avatar":
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("a character name is required")
        return _cast_avatar_job(name)
    if tool == "cast_refs":
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("a character name is required")
        return _cast_refs_job(name, args.get("outfits") or [],
                              bool(args.get("regen")))
    # INSTANT tools return a dict (no job) — the queue append must never be
    # serialized behind the one-job-at-a-time lock
    if tool == "queue_topic":
        import daily
        t = (args.get("topic") or "").strip()
        if not t:
            raise ValueError("a topic is required")
        added = daily.queue_topic(t)
        return {"kind": "ok", "queued": added,
                "topic": config.channel().normalize_topic(t)}
    if tool not in tools:
        raise ValueError(f"unknown tool {tool}")
    label, fn = tools[tool]
    return run_job(label, fn)      # raises with the reason when it can't start
