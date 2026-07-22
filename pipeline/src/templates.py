"""
Template loader — a template owns the *look and rhythm* of a video (visual
world, motion language, models, voice, pacing, budget). The channel owns the
content (channels/<name>/channel.yaml); the daily experiment mutates ONE
variable on top via the channel's overrides.json.

Templates live in channels/<name>/templates/<t>.yaml. At the start of a run
the resolved template is pinned into the topic folder as template.json so
every later stage (and every resume) sees exactly the same settings even if
the yaml changes mid-run.

Usage:
    import templates
    tpl = templates.resolve("courtroom", folder)   # load + pin to folder
    tpl = templates.load_pinned(folder)            # later stages / resumes
"""
from __future__ import annotations
import json
from pathlib import Path

import config

# Mechanical defaults every template inherits. The visual identity (world,
# style, per-label motion, description) must come from the template yaml —
# `world` and `style` are required.
DEFAULTS = {
    # -- pacing / script gates --
    "target_words": [190, 260],         # ~60s FINAL at 1.5x playback (owner
                                        # call 2026-06-14): viewer attention is
                                        # low — keep it ~60s ±10s. ~190-260
                                        # words ≈ 75-105s raw → ~50-70s sped.
                                        # Cut filler hard; criteria live in the
                                        # IMAGE not the script, which frees words
    "hook_min_score": 7,                # LLM hook-judge gate (0-10)
    "script_min_score": 8,              # whole-script judge "good enough" bar (0-10)
    "script_attempts": 3,               # max generations to pass the judge
    # -- generation --
    "image_model": "nano_banana_2",     # CLI job-type for "Nano Banana Pro"
    "video_model": "seedance_2_0",
    # 1080p matches the 1080x1920 output canvas exactly (no upscale) and in
    # seedance fast mode costs the SAME as 720p (17.5cr/5s); only std mode pays
    # 2x for 1080p. Allowed: 480p/720p/1080p/4k.
    "video_params": {"mode": "fast", "resolution": "1080p"},
    "max_clip_seconds": 8,              # >8s trips Higgsfield moderation
    "clip_keyframe_role": "reference",  # how the keyframe guides the clip:
                                        # "reference" (--image, a related guide →
                                        # a fresh moving shot) | "start"
                                        # (--start-image, the locked opening frame)
    "keyframe_continuity": True,        # condition each keyframe on earlier
                                        # ones (--image) for a consistent
                                        # world shot-to-shot
    "motion": {},                       # per-label motion prompts; labels
                                        # without one get a slow drift
    # -- audio --
    "voice": "Mark (en)",               # Higgsfield Inworld voice (used when
                                        # tts_engine="higgsfield")
    "tts_engine": "edge",               # edge = free + per-word timings →
                                        # KARAOKE CAPTIONS (most Shorts are
                                        # watched muted); "higgsfield" = richer
                                        # Inworld voice but NO timings/captions
                                        # (needs a libass ffmpeg to burn them)
    "caption_style": "viral",           # captions.py style
    "reveal_overlay_text": True,        # overlay text fades in AFTER it's
                                        # said (a reveal) instead of being
                                        # baked into the keyframe from t=0
    "overlay_reveal_at": 0.6,           # fraction of the segment before the
                                        # answer text appears (hook = t=0)
    "assemble_overlays": True,          # master gate: bake on-screen text into
                                        # the cut; False = plain cut (preview footage)
    "watermark": True,                  # corner watermark on every video: the
                                        # brand image channels/<name>/brand/
                                        # watermark.png (scaled to watermark_logo_px
                                        # tall) ALONE; the channel name shows only
                                        # as a no-logo fallback or if watermark_text
                                        # is set. False = off
    "watermark_pos": "bottom-right",    # top-right|top-left|bottom-right|bottom-left
    "watermark_opacity": 0.5,           # 0-1 watermark alpha
    "watermark_logo_px": 150,           # brand-image height in the watermark
    # "watermark_text": <show this name beside the logo (off by default)>
    "music": True,                      # mix a background track when one is
                                        # configured (overrides.json `music`);
                                        # False = NO music, rely on platform audio
    "music_source": "local",            # where the track comes from: "local" (a
                                        # file in assets/music) or "elevenlabs"
                                        # (generate a ROYALTY-FREE track per video
                                        # via ElevenLabs Music — needs
                                        # ELEVENLABS_API_KEY). overrides.json
                                        # music_source / music_brief override
    "music_file": None,                 # local source: the default background
                                        # track (a filename in assets/music) used
                                        # on EVERY video — e.g. one saved
                                        # royalty-free track. overrides.json
                                        # `music` still wins per-channel
    "audio_speed": 1.0,                 # voiceover atempo at assembly (1.0/
                                        # 1.25/1.5/2.0) — default 1.0x (owner
                                        # call 2026-06-24): natural pacing. Raise
                                        # per-video for punchier delivery; higher
                                        # speed shortens segments so clips cover
                                        # the audio with less freeze
    "max_slowdown": 3.0,                # slow a clip up to N× to fill the VO
                                        # before freeze-holding the remainder
                                        # (raise audio_speed for long segments)
    "clip_audio_gain": 0.32,            # when a segment sets `mix_clip_audio`,
                                        # the level of the clip's OWN audio
                                        # (sfx/ambience) mixed UNDER the voiceover
                                        # (0-1; 0.32 ≈ a soft bed). Per-segment
                                        # `clip_audio_gain` overrides
    "clip_fill": "slow",                # how a clip fills a longer VO: "slow"
                                        # (setpts-slow + freeze), "loop" (repeat
                                        # at native fps — hard cut at the seam),
                                        # "boomerang" (forward+reverse loop —
                                        # native fps, seamless), or "freeze"
                                        # (play ONCE at native speed then a slow
                                        # ken-burns drift on the last frame —
                                        # "play-stop", not a dead freeze). Per-
                                        # segment `clip_fill` overrides it.
    "max_clips_per_segment": 2,         # for a MULTI-SHOT beat (director `shots`):
                                        # the max ANIMATED clips per segment — the
                                        # surplus shots become free Ken Burns
                                        # stills, bounding credit spend when a
                                        # long beat is split into several shots
    "kenburns": "ffmpeg",               # still-segment animator: "ffmpeg"
                                        # (built-in zoompan) or "remotion" (the
                                        # plugins/remotion/ animated-graphic
                                        # plugin — needs `npm install` there;
                                        # falls back to ffmpeg if unavailable).
                                        # env KALINGA_KENBURNS overrides
    "ai_review_iters": 3,               # post-assembly AI QC loop: critique +
                                        # auto-fix (reassemble / ken-burns
                                        # keyframes) up to N times (0 = off)
    # -- gates --
    "duration_range": [50, 70],         # accept window for final video (s) —
                                        # ~60s ±10s target (owner call 2026-06-14)
    "virality_min_overall": 40,         # brain_activity overall score 0-100
    "virality_min_hook": 35,            # peak-hook score 0-100
    "max_hook_retries": 1,              # regenerate the hook at most once
    "score_hook_first": False,          # hook-first: build+score ONLY the
                                        # opening segment and run the hook-retry
                                        # loop BEFORE generating the rest of the
                                        # keyframes/clips (catch a weak hook
                                        # before spending). Toggle in `make`.
    # -- brand layer (channel identity that survives ANY per-video concept) --
    # When a video has a CONCEPT, the concept owns the WORLD/setting and the
    # channel contributes ONLY this brand layer (so the theme stops fighting the
    # concept). With no concept, the director falls back to the full `world` /
    # `style` below. Channels override `brand` in their template yaml.
    "brand": {
        "palette": "a cohesive cinematic color grade in the channel's signature "
                   "colors, kept tasteful and never garish",
        "marks": "the channel's brand marks ONLY where they fit the scene "
                 "naturally; never force them into every shot",
    },
    # -- cost --
    "budget_credits": 240,
}
REQUIRED = ("world", "style")


def available() -> list[str]:
    d = config.channel().templates_dir
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load(name: str) -> dict:
    """DEFAULTS overlaid with the channel's templates/<name>.yaml."""
    tpl = json.loads(json.dumps(DEFAULTS))   # deep copy
    path = config.channel().templates_dir / f"{name}.yaml"
    if not name or not path.exists():
        raise FileNotFoundError(
            f"template '{name}' not found in {config.channel().templates_dir}"
            f" (available: {', '.join(available()) or 'none'})")
    import yaml
    overrides = yaml.safe_load(path.read_text()) or {}
    for k, v in overrides.items():
        # deep-MERGE any knob whose default is a dict (motion, video_params,
        # brand, …) so a channel overlays a sub-key without wiping the
        # rest; everything else REPLACES. Derived from the default's type, so a
        # new dict knob added to DEFAULTS merges automatically — no list to keep
        # in sync (the old hardcoded merge-keys tuple was a silent-bug footgun).
        if isinstance(tpl.get(k), dict) and isinstance(v, dict):
            tpl[k].update(v)
        else:
            tpl[k] = v
    tpl["name"] = name
    missing = [k for k in REQUIRED if not tpl.get(k)]
    if missing:
        raise ValueError(
            f"template '{name}' is missing {', '.join(missing)} — the "
            "visual identity must be defined in the template yaml")
    return tpl


def resolve(name: str, folder: Path) -> dict:
    """Load the template and pin it into the topic folder for resumability.
    The ACTIVE show (config.Channel.show) is stamped in too, so a resumed run
    reactivates its format automatically (config._activate_pinned_show)."""
    tpl = load(name)
    show = config.channel().show
    if show:
        tpl["show"] = show
    (folder / "template.json").write_text(json.dumps(tpl, indent=2))
    return tpl


def load_pinned(folder: Path) -> dict:
    """The template a run was started with; the channel's default template
    if none was pinned yet."""
    p = folder / "template.json"
    if p.exists():
        tpl = json.loads(json.dumps(DEFAULTS))
        tpl.update(json.loads(p.read_text()))
        return tpl
    return load(config.channel().default_template)
